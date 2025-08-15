import os
import re
import logging
import datetime
import requests

from typing import List, Dict, Any, Optional

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# -------------------- ЛОГИ --------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# -------------------- ENV --------------------
BOT_TOKEN       = os.environ["BOT_TOKEN"]
NOTION_TOKEN    = os.environ["NOTION_TOKEN"]
CATALOG_DB_ID   = os.environ["CATALOG_DB_ID"]   # ЛИШЕ ID, без https:// і ?v=
NOTES_DB_ID     = os.environ["NOTES_DB_ID"]     # ЛИШЕ ID
WEBHOOK_URL     = os.environ.get("WEBHOOK_URL") # https://<your>.up.railway.app
PORT            = int(os.environ.get("PORT", 8000))

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

NOTION_PAGES     = "https://api.notion.com/v1/pages"
NOTION_DB_QUERY  = "https://api.notion.com/v1/databases/{db_id}/query"

# -------------------- СТАНИ ДІАЛОГІВ --------------------
(
    MAIN,
    ADD_CAT_WAIT_NAME,
    ADD_SUB_CHOOSE_CAT,
    ADD_SUB_WAIT_NAME,
    NOTE_CHOOSE_CAT,
    NOTE_CHOOSE_SUB,
    NOTE_WAIT_TEXT,
    SEARCH_WAIT_QUERY,
) = range(8)

# -------------------- КОНСТАНТИ ТЕКСТІВ --------------------
BTN_ADD_CAT  = "➕ Категорія"
BTN_ADD_SUB  = "➕ Підкатегорія"
BTN_NEW_NOTE = "📝 Нотатка"
BTN_SEARCH   = "🔎 Пошук"
BTN_HELP     = "ℹ️ Довідка"
BTN_CANCEL   = "❌ Скасувати"
MAIN_KB = ReplyKeyboardMarkup(
    [[BTN_ADD_CAT, BTN_ADD_SUB],
     [BTN_NEW_NOTE, BTN_SEARCH],
     [BTN_HELP, BTN_CANCEL]],
    resize_keyboard=True
)

# -------------------- ДОПОМОЖНІ ФУНКЦІЇ NOTION --------------------
def notion_query(db_id: str, flt: Optional[Dict]=None, sorts: Optional[List]=None, page_size: int=100) -> Dict:
    payload: Dict[str, Any] = {"page_size": page_size}
    if flt:   payload["filter"] = flt
    if sorts: payload["sorts"]  = sorts
    r = requests.post(NOTION_DB_QUERY.format(db_id=db_id), headers=NOTION_HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def notion_create_page_in_catalog(name: str, type_val: str, parent_id: Optional[str]) -> Dict:
    props = {
        "Name": {"title":[{"text":{"content": name}}]},
        "Type": {"select":{"name": type_val}},
    }
    if parent_id:
        props["Parent"] = {"relation":[{"id": parent_id}]}
    payload = {"parent":{"database_id": CATALOG_DB_ID}, "properties": props}
    r = requests.post(NOTION_PAGES, headers=NOTION_HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def ensure_category(name: str) -> Dict:
    flt = {
        "and":[
            {"property":"Name", "title":{"equals": name}},
            {"property":"Type", "select":{"equals":"Category"}}
        ]
    }
    res = notion_query(CATALOG_DB_ID, flt, page_size=1)
    if res.get("results"):
        return res["results"][0]
    return notion_create_page_in_catalog(name, "Category", None)

def ensure_subcategory(name: str, parent_id: str) -> Dict:
    flt = {
        "and":[
            {"property":"Name", "title":{"equals": name}},
            {"property":"Type", "select":{"equals":"Subcategory"}},
            {"property":"Parent", "relation":{"contains": parent_id}}
        ]
    }
    res = notion_query(CATALOG_DB_ID, flt, page_size=1)
    if res.get("results"):
        return res["results"][0]
    return notion_create_page_in_catalog(name, "Subcategory", parent_id)

def list_categories() -> List[Dict]:
    flt = {"property":"Type","select":{"equals":"Category"}}
    res = notion_query(CATALOG_DB_ID, flt, page_size=100)
    items = []
    for r in res.get("results", []):
        nm = r["properties"]["Name"]["title"][0]["plain_text"] if r["properties"]["Name"]["title"] else "Без назви"
        items.append({"id": r["id"], "name": nm})
    # сортуємо за назвою для красивих кнопок
    return sorted(items, key=lambda x: x["name"].lower())

def list_subcategories(parent_id: str) -> List[Dict]:
    flt = {
        "and":[
            {"property":"Type","select":{"equals":"Subcategory"}},
            {"property":"Parent","relation":{"contains": parent_id}}
        ]
    }
    res = notion_query(CATALOG_DB_ID, flt, page_size=100)
    items = []
    for r in res.get("results", []):
        nm = r["properties"]["Name"]["title"][0]["plain_text"] if r["properties"]["Name"]["title"] else "Без назви"
        items.append({"id": r["id"], "name": nm})
    return sorted(items, key=lambda x: x["name"].lower())

def notion_create_note(title: str, text: str, tags: List[str], cat_id: Optional[str], sub_id: Optional[str], src_url: Optional[str]) -> Dict:
    props: Dict[str, Any] = {
        "Name": {"title":[{"text":{"content": title[:200] or "Note"}}]},
        "Text": {"rich_text":[{"text":{"content": text or ""}}]},
        "Created": {"date":{"start": datetime.datetime.now().isoformat()}},
    }
    if tags:
        props["Tags"] = {"multi_select":[{"name":t} for t in tags]}
    if cat_id:
        props["Category"] = {"relation":[{"id": cat_id}]}
    if sub_id:
        props["Subcategory"] = {"relation":[{"id": sub_id}]}
    if src_url:
        props["Source"] = {"url": src_url}

    payload = {"parent":{"database_id": NOTES_DB_ID}, "properties": props}
    r = requests.post(NOTION_PAGES, headers=NOTION_HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

# -------------------- УТИЛІТИ ДЛЯ КНОПОК --------------------
def chunk_buttons(items: List[Dict], prefix: str, per_row: int = 2) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for it in items:
        row.append(InlineKeyboardButton(it["name"], callback_data=f"{prefix}:{it['id']}"))
        if len(row) == per_row:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("🔙 Назад у меню", callback_data="back:main")])
    return InlineKeyboardMarkup(rows)

def parse_tags(text: str) -> List[str]:
    return list({m.lower() for m in re.findall(r"#([A-Za-zА-Яа-я0-9_]+)", text)})

# -------------------- ХЕНДЛЕРИ --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "Бот на Railway ✅\n"
        "Обери дію з меню нижче.",
        reply_markup=MAIN_KB
    )
    return MAIN

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_chat.send_message("Скасовано. Повертаю в головне меню.", reply_markup=MAIN_KB)
    return MAIN

# ---- ДОДАТИ КАТЕГОРІЮ ----
async def add_cat_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message("Введи назву категорії:", reply_markup=MAIN_KB)
    return ADD_CAT_WAIT_NAME

async def add_cat_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Порожня назва. Введи ще раз або натисни ❌ Скасувати.")
        return ADD_CAT_WAIT_NAME
    try:
        page = ensure_category(name)
        await update.message.reply_text(f"✅ Категорія: {name}", reply_markup=MAIN_KB)
    except requests.HTTPError as e:
        await update.message.reply_text(f"Помилка Notion: {e.response.text[:200]}")
    return MAIN

# ---- ДОДАТИ ПІДКАТЕГОРІЮ ----
async def add_sub_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cats = list_categories()
    if not cats:
        await update.message.reply_text("Немає жодної категорії. Спочатку створіть категорію.", reply_markup=MAIN_KB)
        return MAIN
    kb = chunk_buttons(cats, "pick_cat")
    await update.message.reply_text("Вибери категорію:", reply_markup=kb)
    return ADD_SUB_CHOOSE_CAT

async def add_sub_pick_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "back:main":
        await q.edit_message_text("Повернув у меню."); 
        await q.message.reply_text("Меню:", reply_markup=MAIN_KB); 
        return MAIN
    _, cat_id = q.data.split(":")
    context.user_data["sub_parent_id"] = cat_id
    await q.edit_message_text("Введи назву підкатегорії:")
    return ADD_SUB_WAIT_NAME

async def add_sub_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    parent_id = context.user_data.get("sub_parent_id")
    if not parent_id:
        await update.message.reply_text("Помилка стану. Почни заново.", reply_markup=MAIN_KB)
        return MAIN
    if not name:
        await update.message.reply_text("Порожня назва. Введи ще раз.")
        return ADD_SUB_WAIT_NAME
    try:
        page = ensure_subcategory(name, parent_id)
        await update.message.reply_text(f"✅ Підкатегорія: {name}", reply_markup=MAIN_KB)
    except requests.HTTPError as e:
        await update.message.reply_text(f"Помилка Notion: {e.response.text[:200]}")
    finally:
        context.user_data.pop("sub_parent_id", None)
    return MAIN

# ---- СТВОРИТИ НОТАТКУ ----
async def new_note_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cats = list_categories()
    if not cats:
        await update.message.reply_text("Немає категорій. Спочатку створіть категорію.", reply_markup=MAIN_KB)
        return MAIN
    kb = chunk_buttons(cats, "nn_cat")
    await update.message.reply_text("Вибери категорію для нотатки:", reply_markup=kb)
    return NOTE_CHOOSE_CAT

async def new_note_pick_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "back:main":
        await q.edit_message_text("Повернув у меню.")
        await q.message.reply_text("Меню:", reply_markup=MAIN_KB)
        return MAIN
    _, cat_id = q.data.split(":")
    context.user_data["nn_cat_id"] = cat_id

    subs = list_subcategories(cat_id)
    if not subs:
        await q.edit_message_text("У цієї категорії немає підкатегорій. Спочатку створіть підкатегорію.")
        await q.message.reply_text("Меню:", reply_markup=MAIN_KB)
        return MAIN
    kb = chunk_buttons(subs, "nn_sub")
    await q.edit_message_text("Вибери підкатегорію:", reply_markup=kb)
    return NOTE_CHOOSE_SUB

async def new_note_pick_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "back:main":
        await q.edit_message_text("Повернув у меню.")
        await q.message.reply_text("Меню:", reply_markup=MAIN_KB)
        return MAIN
    _, sub_id = q.data.split(":")
    context.user_data["nn_sub_id"] = sub_id
    await q.edit_message_text("Надішли текст нотатки. Хештеги додавай як #мітка.")
    return NOTE_WAIT_TEXT

async def new_note_got_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Порожній текст. Відправ ще раз.")
        return NOTE_WAIT_TEXT

    tags = parse_tags(text)
    cat_id = context.user_data.get("nn_cat_id")
    sub_id = context.user_data.get("nn_sub_id")

    # Формуємо короткий заголовок (перші слова без хештегів)
    title = re.sub(r"#\S+", "", text).strip()
    title = title.split("\n")[0][:80] or "Note"

    try:
        notion_create_note(title, text, tags, cat_id, sub_id, None)
        await update.message.reply_text("💾 Збережено в Notion.", reply_markup=MAIN_KB)
    except requests.HTTPError as e:
        await update.message.reply_text(f"Помилка Notion: {e.response.text[:300]}", reply_markup=MAIN_KB)
    finally:
        context.user_data.pop("nn_cat_id", None)
        context.user_data.pop("nn_sub_id", None)
    return MAIN

# ---- ПОШУК ----
async def search_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введи ключове слово/фразу для пошуку по нотатках:", reply_markup=MAIN_KB)
    return SEARCH_WAIT_QUERY

async def search_got_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = (update.message.text or "").strip()
    if not q:
        await update.message.reply_text("Порожній запит. Введи ще раз або натисни ❌ Скасувати.")
        return SEARCH_WAIT_QUERY
    flt = {"property":"Text","rich_text":{"contains": q}}
    try:
        res = notion_query(NOTES_DB_ID, flt, page_size=10)
        results = []
        for r in res.get("results", []):
            name_parts = r["properties"]["Name"]["title"]
            title = name_parts[0]["plain_text"] if name_parts else "(без назви)"
            results.append(f"• {title}")
        if not results:
            await update.message.reply_text("Нічого не знайдено.", reply_markup=MAIN_KB)
        else:
            await update.message.reply_text("Знайшов:\n" + "\n".join(results), reply_markup=MAIN_KB)
    except requests.HTTPError as e:
        await update.message.reply_text(f"Помилка Notion: {e.response.text[:200]}", reply_markup=MAIN_KB)
    return MAIN

# ---- HELP ----
async def help_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Що вмію:\n"
        "• ➕ Категорія — створити категорію.\n"
        "• ➕ Підкатегорія — вибрати категорію і задати підкатегорію.\n"
        "• 📝 Нотатка — вибрати категорію/підкатегорію та надіслати текст (хештеги як #мітка).\n"
        "• 🔎 Пошук — швидкий пошук по тексту нотаток.\n",
        reply_markup=MAIN_KB
    )
    return MAIN

# ---- РОЗПІЗНАВАННЯ ВИБОРУ З МЕНЮ ----
async def main_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt == BTN_ADD_CAT:
        return await add_cat_entry(update, context)
    if txt == BTN_ADD_SUB:
        return await add_sub_entry(update, context)
    if txt == BTN_NEW_NOTE:
        return await new_note_entry(update, context)
    if txt == BTN_SEARCH:
        return await search_entry(update, context)
    if txt == BTN_HELP:
        return await help_msg(update, context)
    if txt == BTN_CANCEL:
        return await cancel(update, context)
    # якщо прийшов довільний текст у головному стані — підкажемо про меню
    await update.message.reply_text("Обери дію з меню нижче.", reply_markup=MAIN_KB)
    return MAIN

# -------------------- MAIN() --------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_router)],
        states={
            MAIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_router),
            ],
            ADD_CAT_WAIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_cat_got_name),
            ],
            ADD_SUB_CHOOSE_CAT: [
                CallbackQueryHandler(add_sub_pick_cat, pattern=r"^(pick_cat|back:main):"),
            ],
            ADD_SUB_WAIT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_sub_got_name),
            ],
            NOTE_CHOOSE_CAT: [
                CallbackQueryHandler(new_note_pick_cat, pattern=r"^(nn_cat|back:main):"),
            ],
            NOTE_CHOOSE_SUB: [
                CallbackQueryHandler(new_note_pick_sub, pattern=r"^(nn_sub|back:main):"),
            ],
            NOTE_WAIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_note_got_text),
            ],
            SEARCH_WAIT_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_got_query),
            ],
        },
        fallbacks=[MessageHandler(filters.Regex(f"^{BTN_CANCEL}$"), cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)

    # --- вебхук для Railway ---
    if WEBHOOK_URL:
        path = "/tg"
        log.info("Starting webhook on %s", WEBHOOK_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=path,
            webhook_url=f"{WEBHOOK_URL}{path}",
        )
    else:
        log.info("Starting polling")
        app.run_polling()

if __name__ == "__main__":
    main()
