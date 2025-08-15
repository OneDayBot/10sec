import os
import re
import io
import json
import time
import logging
import datetime
import requests
from typing import List, Dict, Optional

# ---------- OpenAI (опціонально для voice) ----------
try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

# ---------- Telegram ----------
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# ============ ENV ============
BOT_TOKEN = os.environ["BOT_TOKEN"]
NOTION_TOKEN = os.environ["NOTION_API_KEY"]
CATALOG_DB = os.environ["CATALOG_DB_ID"]
NOTES_DB = os.environ["NOTES_DB_ID"]

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# ============ Notion helpers ============
NOTION_VER = "2022-06-28"
NHEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VER,
    "Content-Type": "application/json",
}

def _to_uuid(db: str) -> str:
    """
    Приймає або чистий uuid, або URL бази.
    Повертає uuid у вигляді 8-4-4-4-12.
    """
    s = re.sub(r"[^0-9a-fA-F]", "", db)
    if len(s) < 32:
        return db  # залишаємо як є, може вже uuid
    s = s[-32:]
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}".lower()

CATALOG_DB_ID = _to_uuid(CATALOG_DB)
NOTES_DB_ID   = _to_uuid(NOTES_DB)

def notion_query(database_id: str, flt: Optional[dict] = None, page_size: int = 25) -> dict:
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    payload = {"page_size": page_size}
    if flt:
        payload["filter"] = flt
    r = requests.post(url, headers=NHEADERS, data=json.dumps(payload))
    r.raise_for_status()
    return r.json()

def notion_create_page(database_id: str, properties: dict) -> dict:
    url = "https://api.notion.com/v1/pages"
    payload = {"parent": {"database_id": database_id}, "properties": properties}
    r = requests.post(url, headers=NHEADERS, data=json.dumps(payload))
    r.raise_for_status()
    return r.json()

def notion_find_catalog_by_name(name: str) -> Optional[dict]:
    """
    Шукаємо сторінку в Catalog за Title = name.
    """
    flt = {
        "property": "Name",
        "title": {"equals": name}
    }
    try:
        data = notion_query(CATALOG_DB_ID, flt, page_size=1)
        if data.get("results"):
            return data["results"][0]
    except requests.HTTPError:
        # fallback на contains (деякі робочі простори інколи мають особливості)
        flt = {"property": "Name", "title": {"contains": name}}
        data = notion_query(CATALOG_DB_ID, flt, page_size=1)
        if data.get("results"):
            return data["results"][0]
    return None

def ensure_category(name: str) -> dict:
    pg = notion_find_catalog_by_name(name)
    if pg:
        # перезаписуємо Type=Category якщо інше
        return pg

    props = {
        "Name": {"title": [{"text": {"content": name}}]},
        "Type": {"select": {"name": "Category"}},
        # Parent порожній
    }
    return notion_create_page(CATALOG_DB_ID, props)

def ensure_subcategory(cat_id: str, name: str) -> dict:
    # шукаємо підкатегорію з Parent = cat_id і Name = name
    flt = {
        "and": [
            {"property": "Type", "select": {"equals": "Subcategory"}},
            {"property": "Parent", "relation": {"contains": cat_id}},
            {"property": "Name", "title": {"equals": name}},
        ]
    }
    data = notion_query(CATALOG_DB_ID, flt, page_size=1)
    if data.get("results"):
        return data["results"][0]

    props = {
        "Name": {"title": [{"text": {"content": name}}]},
        "Type": {"select": {"name": "Subcategory"}},
        "Parent": {"relation": [{"id": cat_id}]},
    }
    return notion_create_page(CATALOG_DB_ID, props)

def list_categories() -> List[Dict]:
    flt = {"property": "Type", "select": {"equals": "Category"}}
    data = notion_query(CATALOG_DB_ID, flt, page_size=50)
    out = []
    for r in data.get("results", []):
        name = r["properties"]["Name"]["title"][0]["plain_text"] if r["properties"]["Name"]["title"] else "Без назви"
        out.append({"id": r["id"], "name": name})
    return out

def list_subcategories(cat_id: str) -> List[Dict]:
    flt = {
        "and": [
            {"property": "Type", "select": {"equals": "Subcategory"}},
            {"property": "Parent", "relation": {"contains": cat_id}},
        ]
    }
    data = notion_query(CATALOG_DB_ID, flt, page_size=50)
    out = []
    for r in data.get("results", []):
        name = r["properties"]["Name"]["title"][0]["plain_text"] if r["properties"]["Name"]["title"] else "Без назви"
        out.append({"id": r["id"], "name": name})
    return out

def notion_create_note(
    title: str,
    text: str,
    tags: List[str],
    cat_id: str,
    sub_id: Optional[str],
    files: Optional[List[dict]],
    created: datetime.datetime,
    source_link: str,
) -> dict:
    """
    Створює сторінку в Notes з властивостями:
      - Name (title)
      - Text (rich_text)
      - Tags (multi_select)
      - Category (relation -> Catalog)
      - Subcategory (relation -> Catalog)
      - Created (date)
      - Source (url)
      - Files (files) — опційно
    """
    tags_norm = [{"name": t.lstrip("#")} for t in tags if t.startswith("#")]

    props = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Text": {"rich_text": [{"text": {"content": text}}]} if text else {"rich_text": []},
        "Tags": {"multi_select": tags_norm},
        "Category": {"relation": [{"id": cat_id}]},
        "Created": {"date": {"start": created.isoformat()}},
        "Source": {"url": source_link or None},
    }
    if sub_id:
        props["Subcategory"] = {"relation": [{"id": sub_id}]}
    if files:
        props["Files"] = {"files": files}

    return notion_create_page(NOTES_DB_ID, props)

# ========= Telegram UI =========
BTN_ADD_CAT = "Категорія"
BTN_ADD_SUB = "Підкатегорія"
BTN_NEW_NOTE = "Нотатка"
BTN_SEARCH  = "Пошук"
BTN_HELP    = "Довідка"
BTN_CANCEL  = "Скасувати"

def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_ADD_CAT), KeyboardButton(BTN_ADD_SUB)],
            [KeyboardButton(BTN_NEW_NOTE), KeyboardButton(BTN_SEARCH)],
            [KeyboardButton(BTN_HELP), KeyboardButton(BTN_CANCEL)],
        ],
        resize_keyboard=True
    )

MAIN, ADD_CAT_WAIT_NAME, ADD_SUB_CHOOSE_CAT, ADD_SUB_WAIT_NAME, \
NOTE_CHOOSE_CAT, NOTE_CHOOSE_SUB, NOTE_WAIT_TEXT, SEARCH_WAIT_QUERY = range(8)

def ikb_categories(rows) -> InlineKeyboardMarkup:
    btns = [[InlineKeyboardButton(r['name'], callback_data=f"CAT:{r['id']}")] for r in rows]
    btns.append([InlineKeyboardButton("⬅️ Назад у меню", callback_data="BACK:MAIN")])
    return InlineKeyboardMarkup(btns)

def ikb_subcategories(rows) -> InlineKeyboardMarkup:
    btns = [[InlineKeyboardButton(r['name'], callback_data=f"SUB:{r['id']}")] for r in rows]
    btns.append([InlineKeyboardButton("⬅️ Назад у меню", callback_data="BACK:MAIN")])
    return InlineKeyboardMarkup(btns)

def parse_tags(text: str) -> List[str]:
    return [w for w in text.split() if w.startswith("#")]

def src_link(message) -> str:
    # Можеш додати збереження лінку на повідомлення, якщо є deep-link. Для простоти повертаємо порожньо.
    return ""

# -------- Voice -> text (опціонально через OpenAI Whisper) --------
async def transcribe_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    if not OPENAI_AVAILABLE or not OPENAI_API_KEY:
        return None
    try:
        file = await ctx.bot.get_file(update.message.voice.file_id)
        bio = io.BytesIO()
        await file.download_to_memory(out=bio)
        bio.seek(0)

        client = OpenAI(api_key=OPENAI_API_KEY)
        # сучасна модель для транскрипції:
        # можна "gpt-4o-mini-transcribe" або "whisper-1" (якщо увімкнена)
        resp = client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=("audio.ogg", bio, "audio/ogg")
        )
        text = getattr(resp, "text", None)
        return text
    except Exception as e:
        log.exception(e)
        return None

# ---------- Handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот на Railway ✅\nОбері дію з меню нижче.", reply_markup=main_kb())
    return MAIN

async def main_menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == BTN_ADD_CAT:
        await update.message.reply_text("Введи назву категорії:", reply_markup=ReplyKeyboardRemove())
        return ADD_CAT_WAIT_NAME

    if text == BTN_ADD_SUB:
        try:
            cats = list_categories()
        except Exception as e:
            log.exception(e)
            await update.message.reply_text("Помилка читання категорій 😔", reply_markup=main_kb())
            return MAIN
        if not cats:
            await update.message.reply_text("Категорій поки немає. Спочатку додай категорію.", reply_markup=main_kb())
            return MAIN
        await update.message.reply_text("Вибери категорію:", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text(" ", reply_markup=ikb_categories(cats))
        return ADD_SUB_CHOOSE_CAT

    if text == BTN_NEW_NOTE:
        try:
            cats = list_categories()
        except Exception as e:
            log.exception(e)
            await update.message.reply_text("Помилка читання категорій 😔", reply_markup=main_kb())
            return MAIN
        if not cats:
            await update.message.reply_text("Категорій поки немає. Спочатку додай категорію.", reply_markup=main_kb())
            return MAIN
        await update.message.reply_text("Вибери категорію для нотатки:", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text(" ", reply_markup=ikb_categories(cats))
        return NOTE_CHOOSE_CAT

    if text == BTN_SEARCH:
        await update.message.reply_text("Введи слово або #тег для пошуку:", reply_markup=ReplyKeyboardRemove())
        return SEARCH_WAIT_QUERY

    if text == BTN_HELP:
        await update.message.reply_text(
            "Команди:\n"
            "• Категорія — створити категорію\n"
            "• Підкатегорія — створити підкатегорію\n"
            "• Нотатка — додати нотатку (текст/голос/фото)\n"
            "• Пошук — знайти в нотатках\n"
            "Підтримуються #хештеги."
        )
        return MAIN

    if text == BTN_CANCEL:
        ctx.user_data.clear()
        await update.message.reply_text("Скасовано.", reply_markup=main_kb())
        return MAIN

    # Якщо користувач одразу надіслав текст/голос/фото — вважай запитом додати нотатку (швидкий режим)
    await update.message.reply_text("Обері дію з меню нижче.", reply_markup=main_kb())
    return MAIN

# --- add category ---
async def add_cat_got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Порожня назва. Введи ще раз або натисни «Скасувати».")
        return ADD_CAT_WAIT_NAME
    try:
        page = ensure_category(name)
        title = page["properties"]["Name"]["title"][0]["plain_text"] if page["properties"]["Name"]["title"] else name
        await update.message.reply_text(f"✅ Категорію «{title}» створено.", reply_markup=main_kb())
    except Exception as e:
        log.exception(e)
        await update.message.reply_text("❌ Помилка створення категорії.", reply_markup=main_kb())
    return MAIN

# --- add subcategory (pick cat) ---
async def add_sub_pick_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "BACK:MAIN":
        await query.edit_message_text("Обері дію з меню нижче.")
        await query.message.reply_text(" ", reply_markup=main_kb())
        return MAIN

    if not data.startswith("CAT:"):
        await query.answer("Невідомий вибір", show_alert=True)
        return ADD_SUB_CHOOSE_CAT

    cat_id = data.split(":", 1)[1]
    ctx.user_data["addsub_cat"] = cat_id
    await query.edit_message_text("Введи назву підкатегорії:")
    return ADD_SUB_WAIT_NAME

async def add_sub_got_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    cat_id = ctx.user_data.get("addsub_cat")
    if not cat_id:
        await update.message.reply_text("Втрачено вибір категорії. Спробуй ще раз.", reply_markup=main_kb())
        return MAIN
    if not name:
        await update.message.reply_text("Порожня назва. Введи ще раз або «Скасувати».")
        return ADD_SUB_WAIT_NAME
    try:
        page = ensure_subcategory(cat_id, name)
        title = page["properties"]["Name"]["title"][0]["plain_text"] if page["properties"]["Name"]["title"] else name
        await update.message.reply_text(f"✅ Підкатегорію «{title}» створено.", reply_markup=main_kb())
    except Exception as e:
        log.exception(e)
        await update.message.reply_text("❌ Помилка створення підкатегорії.", reply_markup=main_kb())
    return MAIN

# --- new note (choose cat) ---
async def new_note_pick_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "BACK:MAIN":
        await query.edit_message_text("Обері дію з меню нижче.")
        await query.message.reply_text(" ", reply_markup=main_kb())
        return MAIN

    if not data.startswith("CAT:"):
        await query.answer("Невідомий вибір", show_alert=True)
        return NOTE_CHOOSE_CAT

    cat_id = data.split(":", 1)[1]
    ctx.user_data["note_cat"] = cat_id

    try:
        subs = list_subcategories(cat_id)
    except Exception as e:
        log.exception(e)
        subs = []

    if subs:
        await query.edit_message_text("Вибери підкатегорію (або повернись у меню):")
        await query.message.reply_text(" ", reply_markup=ikb_subcategories(subs))
        return NOTE_CHOOSE_SUB
    else:
        await query.edit_message_text("Надішли текст/голосове/фото для нотатки.")
        return NOTE_WAIT_TEXT

# --- new note (choose sub) ---
async def new_note_pick_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if data == "BACK:MAIN":
        await query.edit_message_text("Обері дію з меню нижче.")
        await query.message.reply_text(" ", reply_markup=main_kb())
        return MAIN

    if not data.startswith("SUB:"):
        await query.answer("Невідомий вибір", show_alert=True)
        return NOTE_CHOOSE_SUB

    sub_id = data.split(":", 1)[1]
    ctx.user_data["note_sub"] = sub_id
    await query.edit_message_text("Надішли текст/голосове/фото для нотатки.")
    return NOTE_WAIT_TEXT

# --- new note (receive text/voice/photo) ---
async def new_note_got_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cat_id = ctx.user_data.get("note_cat")
    sub_id = ctx.user_data.get("note_sub")
    if not cat_id:
        await update.message.reply_text("Втрачено вибір категорії. Спробуй знову.", reply_markup=main_kb())
        return MAIN

    text = update.message.text or update.message.caption or ""

    # Голос -> текст (опціонально)
    if update.message.voice and not text:
        t = await transcribe_voice(update, ctx)
        if t:
            text = t

    tags = parse_tags(text)
    title = (text[:50] or "Нотатка").strip()

    files = None  # можна додати завантаження фото в Notion (public url) — опустимо для стабільності
    created = datetime.datetime.now()
    link = src_link(update.message)

    try:
        notion_create_note(title, text, tags, cat_id, sub_id, files, created, link)
        await update.message.reply_text("✅ Нотатку збережено.", reply_markup=main_kb())
    except Exception as e:
        log.exception(e)
        await update.message.reply_text("❌ Помилка збереження нотатки.", reply_markup=main_kb())

    ctx.user_data.pop("note_cat", None)
    ctx.user_data.pop("note_sub", None)
    return MAIN

# --- search ---
async def search_got_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if not q:
        await update.message.reply_text("Порожній запит. Введи слово або #тег.")
        return SEARCH_WAIT_QUERY
    # TODO: додай власну реалізацію пошуку по Notes (query з фільтром title/rich_text/tags)
    await update.message.reply_text(f"Шукаю: {q} … (додай тут свій пошук)", reply_markup=main_kb())
    return MAIN

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    if update.message:
        await update.message.reply_text("Скасовано.", reply_markup=main_kb())
    return MAIN

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start),
                      MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_router)],
        states={
            MAIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_router),
            ],
            ADD_CAT_WAIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_cat_got_name),
            ],
            ADD_SUB_CHOOSE_CAT: [
                CallbackQueryHandler(add_sub_pick_cat),
            ],
            ADD_SUB_WAIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_sub_got_name),
            ],
            NOTE_CHOOSE_CAT: [
                CallbackQueryHandler(new_note_pick_cat),
            ],
            NOTE_CHOOSE_SUB: [
                CallbackQueryHandler(new_note_pick_sub),
            ],
            NOTE_WAIT_TEXT: [
                MessageHandler(filters.TEXT | filters.PHOTO | filters.VOICE, new_note_got_text),
            ],
            SEARCH_WAIT_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_got_query),
            ],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"), cancel)],
        allow_reentry=True,
        per_chat=True, per_user=True, per_message=False,
    )
    app.add_handler(conv)
    return app

def main():
    app = build_app()
    if WEBHOOK_URL:
        # дозволяє передавати https://xxx/wh_... або https://xxx/
        from urllib.parse import urlparse
        u = urlparse(WEBHOOK_URL)
        base = f"{u.scheme}://{u.netloc}"
        path = u.path if u.path and u.path != "/" else "/tg"
        full = base + path
        log.info("Starting webhook on %s", full)
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=path, webhook_url=full)
    else:
        log.info("Starting polling")
        app.run_polling()

if __name__ == "__main__":
    main()
