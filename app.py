# -*- coding: utf-8 -*-
# app.py — Telegram ↔ Notion бот з кнопками
#
# Функціонал:
# - /start: головне меню з кнопками
# - ➕ Категорія: створити категорію в Catalog
# - ➕ Підкатегорія: вибрати категорію → ввести назву підкатегорії → створити
# - 📝 Нотатка: вибрати категорію/підкатегорію → ввести текст → запис у Notes
# - 🔍 Пошук: пошук у Notes (по Title та Text)
# - ℹ️ Довідка: коротка пам’ятка
# - ❌ Скасувати: вихід із діалогу
#
# Бази Notion (властивості мають бути саме з такими іменами):
# Catalog:
#   - Name (title)
#   - Type (select: Category, Subcategory)
#   - Parent (relation -> цей же Catalog)
#
# Notes:
#   - Name (title)
#   - Text (rich_text)
#   - Tags (multi_select)         [необов'язково]
#   - Category (relation->Catalog)
#   - Subcategory (relation->Catalog)
#   - Files (files)               [необов'язково]
#   - Created (date)
#   - Source (url)
#
# Змінні оточення (Railway → Variables):
#   BOT_TOKEN            — токен Telegram
#   NOTION_API_KEY       — secret_… (Internal Integration Secret)
#     (або NOTION_TOKEN  — якщо так називав, теж спрацює)
#   CATALOG_DB_ID        — ID або повний URL бази Catalog
#   NOTES_DB_ID          — ID або повний URL бази Notes
#   WEBHOOK_URL          — https://<your>.railway.app
#   WEBHOOK_SECRET       — довільний суфікс, напр. wh_XXXX (буде шлях /wh_XXXX)
#   PORT                 — 8080 (Railway підставляє сам)
#
# Стартова команда Railway:  python app.py

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import os
import re
import textwrap
from typing import Dict, List, Optional, Tuple

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    AIORateLimiter,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

# --------------------------- ЛОГИ -------------------------------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("bot")


# ---------------------- ЗМІННІ ОТОЧЕННЯ -------------------------------------
def _env(name: str, default: Optional[str] = None) -> str:
    val = os.getenv(name, default)
    if val is None:
        raise RuntimeError(f"Set {name} in environment")
    return val


BOT_TOKEN = _env("BOT_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_API_KEY") or os.getenv("NOTION_TOKEN")
if not NOTION_TOKEN:
    raise RuntimeError("Set NOTION_API_KEY (або NOTION_TOKEN) у Variables")

CATALOG_DB = _env("CATALOG_DB_ID")
NOTES_DB = _env("NOTES_DB_ID")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "wh_secret")
PORT = int(os.getenv("PORT", "8080"))

# ------------------------ НАЛАШТУВАННЯ NOTION --------------------------------
NOTION_VERSION = "2022-06-28"
N_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

NOTION_API = "https://api.notion.com/v1"


def _extract_db_id(db_or_url: str) -> str:
    """
    Дозволяє давати як чистий ID, так і повний URL бази.
    """
    db_or_url = db_or_url.strip()
    if "/" not in db_or_url and "-" in db_or_url or len(db_or_url) >= 32:
        # схоже на ID
        return db_or_url
    # URL -> дістаємо 32-символьний ID
    m = re.search(r"/([0-9a-fA-F]{32})", db_or_url.replace("-", ""))
    if m:
        return m.group(1)
    # fallback: спробуємо взяти останній сегмент
    return db_or_url.split("/")[-1].split("?")[0]


CATALOG_DB_ID = _extract_db_id(CATALOG_DB)
NOTES_DB_ID = _extract_db_id(NOTES_DB)


# --------------------------- ХЕЛПЕРИ NOTION ----------------------------------
def notion_query(db_id: str, payload: dict) -> dict:
    r = requests.post(f"{NOTION_API}/databases/{db_id}/query", headers=N_HEADERS, json=payload, timeout=30)
    if r.status_code >= 400:
        log.error("Notion query error: %s %s", r.status_code, r.text)
    r.raise_for_status()
    return r.json()


def notion_create_page(payload: dict) -> dict:
    r = requests.post(f"{NOTION_API}/pages", headers=N_HEADERS, json=payload, timeout=30)
    if r.status_code >= 400:
        log.error("Notion create page error: %s %s", r.status_code, r.text)
    r.raise_for_status()
    return r.json()


def notion_update_page(page_id: str, payload: dict) -> dict:
    r = requests.patch(f"{NOTION_API}/pages/{page_id}", headers=N_HEADERS, json=payload, timeout=30)
    if r.status_code >= 400:
        log.error("Notion update error: %s %s", r.status_code, r.text)
    r.raise_for_status()
    return r.json()


# -------------------------- МОДЕЛІ ДАНИХ -------------------------------------
@dataclasses.dataclass
class CatalogItem:
    id: str
    name: str
    type: str         # Category | Subcategory
    parent_id: Optional[str] = None


# ---------------------- РОБОТА З БАЗОЮ CATALOG -------------------------------
def get_categories() -> List[CatalogItem]:
    payload = {
        "filter": {
            "property": "Type",
            "select": {"equals": "Category"}
        },
        "page_size": 100
    }
    data = notion_query(CATALOG_DB_ID, payload)
    res: List[CatalogItem] = []
    for p in data.get("results", []):
        props = p["properties"]
        name = props["Name"]["title"][0]["plain_text"] if props["Name"]["title"] else "Без назви"
        typ = props["Type"]["select"]["name"] if props.get("Type", {}).get("select") else ""
        parent = None
        if props.get("Parent", {}).get("relation"):
            parent = props["Parent"]["relation"][0]["id"]
        res.append(CatalogItem(id=p["id"], name=name, type=typ, parent_id=parent))
    return sorted(res, key=lambda x: x.name.lower())


def get_subcategories(parent_id: str) -> List[CatalogItem]:
    payload = {
        "filter": {
            "and": [
                {"property": "Type", "select": {"equals": "Subcategory"}},
                {"property": "Parent", "relation": {"contains": parent_id}},
            ]
        },
        "page_size": 100
    }
    data = notion_query(CATALOG_DB_ID, payload)
    res: List[CatalogItem] = []
    for p in data.get("results", []):
        props = p["properties"]
        name = props["Name"]["title"][0]["plain_text"] if props["Name"]["title"] else "Без назви"
        typ = props["Type"]["select"]["name"] if props.get("Type", {}).get("select") else ""
        parent = None
        if props.get("Parent", {}).get("relation"):
            parent = props["Parent"]["relation"][0]["id"]
        res.append(CatalogItem(id=p["id"], name=name, type=typ, parent_id=parent))
    return sorted(res, key=lambda x: x.name.lower())


def find_catalog_by_name_exact(name: str) -> Optional[CatalogItem]:
    payload = {
        "filter": {
            "property": "Name",
            "title": {"equals": name}
        },
        "page_size": 1
    }
    data = notion_query(CATALOG_DB_ID, payload)
    if not data.get("results"):
        return None
    p = data["results"][0]
    props = p["properties"]
    typ = props["Type"]["select"]["name"] if props.get("Type", {}).get("select") else ""
    parent = None
    if props.get("Parent", {}).get("relation"):
        parent = props["Parent"]["relation"][0]["id"]
    nm = props["Name"]["title"][0]["plain_text"] if props["Name"]["title"] else "Без назви"
    return CatalogItem(id=p["id"], name=nm, type=typ, parent_id=parent)


def ensure_category(name: str) -> CatalogItem:
    ex = find_catalog_by_name_exact(name)
    if ex and ex.type == "Category":
        return ex
    payload = {
        "parent": {"database_id": CATALOG_DB_ID},
        "properties": {
            "Name": {"title": [{"type": "text", "text": {"content": name}}]},
            "Type": {"select": {"name": "Category"}},
        },
    }
    p = notion_create_page(payload)
    return CatalogItem(id=p["id"], name=name, type="Category", parent_id=None)


def ensure_subcategory(name: str, parent: CatalogItem) -> CatalogItem:
    ex = find_catalog_by_name_exact(name)
    if ex and ex.type == "Subcategory":
        # Якщо збіг і вже зв'язаний з тим самим parent — повернемо
        if ex.parent_id == parent.id:
            return ex
    payload = {
        "parent": {"database_id": CATALOG_DB_ID},
        "properties": {
            "Name": {"title": [{"type": "text", "text": {"content": name}}]},
            "Type": {"select": {"name": "Subcategory"}},
            "Parent": {"relation": [{"id": parent.id}]},
        },
    }
    p = notion_create_page(payload)
    return CatalogItem(id=p["id"], name=name, type="Subcategory", parent_id=parent.id)


# ------------------------ РОБОТА З БАЗОЮ NOTES --------------------------------
def create_note(
    title: str,
    text: str,
    category: Optional[CatalogItem] = None,
    subcategory: Optional[CatalogItem] = None,
    tags: Optional[List[str]] = None,
    source: Optional[str] = None,
) -> str:
    title = title.strip() or "Нотатка"
    created = datetime.datetime.now().isoformat()

    props = {
        "Name": {"title": [{"type": "text", "text": {"content": title[:200]}}]},
        "Text": {"rich_text": [{"type": "text", "text": {"content": text[:1800]}}]},
        "Created": {"date": {"start": created}},
    }
    if category:
        props["Category"] = {"relation": [{"id": category.id}]}
    if subcategory:
        props["Subcategory"] = {"relation": [{"id": subcategory.id}]}
    if tags:
        props["Tags"] = {"multi_select": [{"name": t[:50]} for t in tags[:10]]}
    if source:
        props["Source"] = {"url": source}

    payload = {"parent": {"database_id": NOTES_DB_ID}, "properties": props}
    p = notion_create_page(payload)
    return p["id"]


# ------------------------- КНОПКИ ТА СТАНИ ------------------------------------
MENU, ADD_CAT_NAME, ADD_SUB_CHOOSE_CAT, ADD_SUB_NAME, NOTE_CHOOSE_CAT, NOTE_CHOOSE_SUB, NOTE_ENTER_TEXT, SEARCH_ENTER = range(
    8
)


def kb_main() -> ReplyKeyboardMarkup:
    rows = [
        ["➕ Категорія", "➕ Підкатегорія"],
        ["📝 Нотатка", "🔍 Пошук"],
        ["ℹ️ Довідка", "❌ Скасувати"],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def kb_back() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["⬅️ Назад у меню"]], resize_keyboard=True)


def kb_list(items: List[str]) -> ReplyKeyboardMarkup:
    # по одному в рядок, плюс "назад"
    rows = [[s] for s in items]
    rows.append(["⬅️ Назад у меню"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# --------------------------- HANDLERS -----------------------------------------
async def cmd_start(update: Update, ctx: CallbackContext) -> int:
    await update.message.reply_text("Бот на Railway ✅\nОберіть дію з меню нижче.", reply_markup=kb_main())
    return MENU


async def menu_router(update: Update, ctx: CallbackContext) -> int:
    text = (update.message.text or "").strip()

    if text == "➕ Категорія":
        await update.message.reply_text("Введи назву категорії:", reply_markup=kb_back())
        return ADD_CAT_NAME

    if text == "➕ Підкатегорія":
        cats = get_categories()
        if not cats:
            await update.message.reply_text("Категорій поки немає. Спочатку створіть категорію.", reply_markup=kb_main())
            return MENU
        ctx.user_data["CATS"] = cats
        await update.message.reply_text(
            "Вибери категорію:", reply_markup=kb_list([c.name for c in cats])
        )
        return ADD_SUB_CHOOSE_CAT

    if text == "📝 Нотатка":
        cats = get_categories()
        if not cats:
            await update.message.reply_text("Категорій поки немає. Спочатку створіть категорію.", reply_markup=kb_main())
            return MENU
        ctx.user_data["CATS"] = cats
        await update.message.reply_text("Вибери категорію для нотатки:", reply_markup=kb_list([c.name for c in cats]))
        return NOTE_CHOOSE_CAT

    if text == "🔍 Пошук":
        await update.message.reply_text("Введіть слово/фразу для пошуку:", reply_markup=kb_back())
        return SEARCH_ENTER

    if text == "ℹ️ Довідка":
        help_text = textwrap.dedent(
            """\
            📘 Коротко:
            • «Категорія» — створює запис у Catalog з Type=Category.
            • «Підкатегорія» — спершу обери категорію, потім введи назву підкатегорії.
            • «Нотатка» — обери категорію/підкатегорію і надішли текст; буде сторінка у Notes.
            • «Пошук» — шукає нотатки у Notes (по заголовку та Text).

            У Notion інтеграція повинна мати доступ «Can read and write» до сторінок баз.
            """
        )
        await update.message.reply_text(help_text, reply_markup=kb_main())
        return MENU

    if text == "❌ Скасувати" or text == "⬅️ Назад у меню":
        await update.message.reply_text("Скасовано.", reply_markup=kb_main())
        return MENU

    # не розпізнано — просто показати меню
    await update.message.reply_text("Оберіть дію з меню нижче.", reply_markup=kb_main())
    return MENU


# ----- Додавання категорії
async def add_cat_name(update: Update, ctx: CallbackContext) -> int:
    text = (update.message.text or "").strip()
    if text == "⬅️ Назад у меню":
        await update.message.reply_text("Повернув у меню.", reply_markup=kb_main())
        return MENU

    try:
        cat = ensure_category(text)
    except Exception as e:
        log.exception("ensure_category failed")
        await update.message.reply_text(f"Помилка створення категорії: {e}", reply_markup=kb_main())
        return MENU

    await update.message.reply_text(f"✅ Категорія створена: *{cat.name}*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
    return MENU


# ----- Додавання підкатегорії
async def add_sub_choose_cat(update: Update, ctx: CallbackContext) -> int:
    txt = (update.message.text or "").strip()
    if txt == "⬅️ Назад у меню":
        await update.message.reply_text("Повернув у меню.", reply_markup=kb_main())
        return MENU

    cats: List[CatalogItem] = ctx.user_data.get("CATS", [])
    sel = next((c for c in cats if c.name == txt), None)
    if not sel:
        await update.message.reply_text("Обери категорію зі списку.", reply_markup=kb_list([c.name for c in cats]))
        return ADD_SUB_CHOOSE_CAT

    ctx.user_data["PARENT_CAT"] = sel
    await update.message.reply_text(f"Введи назву підкатегорії для «{sel.name}»:", reply_markup=kb_back())
    return ADD_SUB_NAME


async def add_sub_name(update: Update, ctx: CallbackContext) -> int:
    txt = (update.message.text or "").strip()
    if txt == "⬅️ Назад у меню":
        await update.message.reply_text("Повернув у меню.", reply_markup=kb_main())
        return MENU

    parent: CatalogItem = ctx.user_data.get("PARENT_CAT")
    if not parent:
        await update.message.reply_text("Немає вибраної категорії. Спробуй ще раз.", reply_markup=kb_main())
        return MENU

    try:
        sub = ensure_subcategory(txt, parent)
    except Exception as e:
        log.exception("ensure_subcategory failed")
        await update.message.reply_text(f"Помилка створення підкатегорії: {e}", reply_markup=kb_main())
        return MENU

    await update.message.reply_text(
        f"✅ Підкатегорія «{sub.name}» створена в «{parent.name}».", reply_markup=kb_main()
    )
    return MENU


# ----- Нотатка
async def note_choose_cat(update: Update, ctx: CallbackContext) -> int:
    txt = (update.message.text or "").strip()
    if txt == "⬅️ Назад у меню":
        await update.message.reply_text("Повернув у меню.", reply_markup=kb_main())
        return MENU

    cats: List[CatalogItem] = ctx.user_data.get("CATS", [])
    cat = next((c for c in cats if c.name == txt), None)
    if not cat:
        await update.message.reply_text("Обери категорію зі списку.", reply_markup=kb_list([c.name for c in cats]))
        return NOTE_CHOOSE_CAT

    ctx.user_data["NOTE_CAT"] = cat

    subs = get_subcategories(cat.id)
    ctx.user_data["SUBS"] = subs
    labels = ["— Без підкатегорії —"] + [s.name for s in subs]
    await update.message.reply_text("Вибери підкатегорію:", reply_markup=kb_list(labels))
    return NOTE_CHOOSE_SUB


async def note_choose_sub(update: Update, ctx: CallbackContext) -> int:
    txt = (update.message.text or "").strip()
    if txt == "⬅️ Назад у меню":
        await update.message.reply_text("Повернув у меню.", reply_markup=kb_main())
        return MENU

    sub: Optional[CatalogItem] = None
    if txt != "— Без підкатегорії —":
        subs: List[CatalogItem] = ctx.user_data.get("SUBS", [])
        sub = next((s for s in subs if s.name == txt), None)
        if not sub:
            await update.message.reply_text("Вибери підкатегорію зі списку.", reply_markup=kb_list(["— Без підкатегорії —"] + [s.name for s in subs]))
            return NOTE_CHOOSE_SUB

    ctx.user_data["NOTE_SUB"] = sub
    await update.message.reply_text("Надішли текст нотатки:", reply_markup=kb_back())
    return NOTE_ENTER_TEXT


async def note_enter_text(update: Update, ctx: CallbackContext) -> int:
    txt = (update.message.text or "").strip()
    if txt == "⬅️ Назад у меню":
        await update.message.reply_text("Повернув у меню.", reply_markup=kb_main())
        return MENU

    cat: CatalogItem = ctx.user_data.get("NOTE_CAT")
    sub: Optional[CatalogItem] = ctx.user_data.get("NOTE_SUB")

    try:
        title = txt.splitlines()[0][:60]
        note_id = create_note(title=title, text=txt, category=cat, subcategory=sub, tags=None, source=None)
    except Exception as e:
        log.exception("create_note failed")
        await update.message.reply_text(f"Помилка створення нотатки: {e}", reply_markup=kb_main())
        return MENU

    await update.message.reply_text("✅ Нотатку створено.", reply_markup=kb_main())
    return MENU


# ----- Пошук
async def search_enter(update: Update, ctx: CallbackContext) -> int:
    txt = (update.message.text or "").strip()
    if txt == "⬅️ Назад у меню":
        await update.message.reply_text("Повернув у меню.", reply_markup=kb_main())
        return MENU

    # Шукаємо по Name та Text
    payload = {
        "filter": {
            "or": [
                {"property": "Name", "title": {"contains": txt}},
                {"property": "Text", "rich_text": {"contains": txt}},
            ]
        },
        "page_size": 5,
    }
    try:
        data = notion_query(NOTES_DB_ID, payload)
    except Exception as e:
        log.exception("search failed")
        await update.message.reply_text(f"Помилка пошуку: {e}", reply_markup=kb_main())
        return MENU

    if not data.get("results"):
        await update.message.reply_text("Нічого не знайдено.", reply_markup=kb_main())
        return MENU

    msgs = []
    for p in data["results"]:
        props = p["properties"]
        title = props["Name"]["title"][0]["plain_text"] if props["Name"]["title"] else "Без назви"
        snippet = ""
        if props.get("Text", {}).get("rich_text"):
            snippet = props["Text"]["rich_text"][0]["plain_text"][:120]
        msgs.append(f"• *{title}*\n`{snippet}`")

    await update.message.reply_text("\n\n".join(msgs), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_main())
    return MENU


# ----- Скасування
async def cancel(update: Update, ctx: CallbackContext) -> int:
    await update.message.reply_text("Скасовано.", reply_markup=kb_main())
    return ConversationHandler.END


def build_application():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .rate_limiter(AIORateLimiter())
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router)],
            ADD_CAT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cat_name)],
            ADD_SUB_CHOOSE_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sub_choose_cat)],
            ADD_SUB_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_sub_name)],
            NOTE_CHOOSE_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, note_choose_cat)],
            NOTE_CHOOSE_SUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, note_choose_sub)],
            NOTE_ENTER_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, note_enter_text)],
            SEARCH_ENTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_enter)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    # Додатково на всяк випадок
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("cancel", cancel))

    return app


def main():
    app = build_application()

    if WEBHOOK_URL:
        path = f"/{WEBHOOK_SECRET}"
        log.info("Starting webhook on %s%s", WEBHOOK_URL, path)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_SECRET,
            webhook_url=f"{WEBHOOK_URL}{path}",
        )
    else:
        log.info("Starting polling (WEBHOOK_URL not set)")
        app.run_polling()


if __name__ == "__main__":
    main()
