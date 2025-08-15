import os, re, json, tempfile, datetime, logging, traceback, requests
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Update
from openai import OpenAI

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ==== ENV ====
BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
CATALOG_DB_ID = os.environ["CATALOG_DB_ID"]
NOTES_DB_ID = os.environ["NOTES_DB_ID"]
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
PORT = int(os.getenv("PORT", "8000"))

client = OpenAI(api_key=OPENAI_API_KEY)

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
NOTION_PAGES = "https://api.notion.com/v1/pages"
NOTION_DB_QUERY = "https://api.notion.com/v1/databases/{}/query"

def _slug(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())

def extract_tags(text:str) -> list[str]:
    return [m.lower() for m in re.findall(r"#(\w+)", text or "")]

def parse_cat_sub(text:str):
    cat = sub = None
    m = re.search(r"(?:cat|категорія)\s*:\s*([^\n#]+)", text, re.I)
    if m: cat = m.group(1).strip()
    m = re.search(r"(?:sub|підкатегорія)\s*:\s*([^\n#]+)", text, re.I)
    if m: sub = m.group(1).strip()
    return cat, sub

def notion_query(db_id, filter_obj, page_size=5):
    r = requests.post(NOTION_DB_QUERY.format(db_id),
                      headers=NOTION_HEADERS,
                      data=json.dumps({"filter": filter_obj, "page_size": page_size}),
                      timeout=30)
    r.raise_for_status()
    return r.json().get("results", [])

def notion_find_catalog_by_name(name: str):
    if not name:
        return None
    # шукаємо по Name exact (Notion не підтримує equals для title у деяких версіях — fallback на contains + ручна перевірка)
    flt = {"property":"Name","title":{"contains": name}}
    res = notion_query(CATALOG_DB_ID, flt, page_size=10)
    for p in res:
        try:
            t = p["properties"]["Name"]["title"][0]["plain_text"]
            if _slug(t) == _slug(name):
                return p
        except Exception:
            continue
    return None

def notion_create_catalog(name: str, type_opt: str, parent_id: str | None):
    props = {
        "Name": {"title":[{"text":{"content": name[:200]}}]},
        "Type": {"select":{"name": type_opt}},
    }
    if parent_id:
        props["Parent"] = {"relation":[{"id": parent_id}]}
    payload = {"parent":{"database_id": CATALOG_DB_ID}, "properties": props}
    r = requests.post(NOTION_PAGES, headers=NOTION_HEADERS, data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    return r.json()

def ensure_category(name:str):
    page = notion_find_catalog_by_name(name)
    if page: return page
    return notion_create_catalog(name, "Category", None)

def ensure_subcategory(sub_name:str, cat_name_or_id:str):
    parent_page = None
    if re.fullmatch(r"[0-9a-fA-F-]{36}", cat_name_or_id or ""):
        parent_id = cat_name_or_id
    else:
        parent_page = ensure_category(cat_name_or_id)
        parent_id = parent_page["id"]
    exist = notion_query(CATALOG_DB_ID, {
        "and":[
            {"property":"Name","title":{"contains": sub_name}},
            {"property":"Parent","relation":{"contains": parent_id}}
        ]
    }, page_size=10)
    for p in exist:
        try:
            t = p["properties"]["Name"]["title"][0]["plain_text"]
            if _slug(t) == _slug(sub_name):
                return p
        except: pass
    return notion_create_catalog(sub_name, "Subcategory", parent_id)

def notion_create_note(title,text,tags,cat_id,sub_id,files,created,src_url):
    props = {
        "Name": {"title":[{"text":{"content": title[:200] or "Note"}}]},
        "Text": {"rich_text":[{"text":{"content": text or ""}}]},
        "Created": {"date":{"start": created.isoformat()}},
    }
    if tags: props["Tags"] = {"multi_select":[{"name":t} for t in tags]}
    if cat_id: props["Category"] = {"relation":[{"id": cat_id}]}
    if sub_id: props["Subcategory"] = {"relation":[{"id": sub_id}]}
    if src_url: props["Source"] = {"url": src_url}
    payload = {"parent":{"database_id": NOTES_DB_ID}, "properties": props}
    if files:
        payload["properties"]["Files"] = {"files":[{"type":"external","name":f["name"],"external":{"url": f["url"]}} for f in files]}
    r = requests.post(NOTION_PAGES, headers=NOTION_HEADERS, data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    return r.json()

def tg_file_url(bot_token:str, file_path:str) -> str:
    return f"https://api.telegram.org/file/bot{bot_token}/{file_path}"

def src_link(msg) -> str|None:
    if msg.chat and msg.chat.username and msg.message_id:
        return f"https://t.me/{msg.chat.username}/{msg.message_id}"
    return None

# ==== error handler ====
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    log.error("Unhandled error: %s", err)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("❌ Помилка виконання. Перевір логи Railway.")
    except Exception:
        pass

# ==== commands ====
async def start(update: Update, ctx):
    await update.message.reply_text(
        "Бот на Railway ✅ Надішли голосове — я перетворю в текст.\n"
        "Команди:\n"
        "/addcat Назва\n/addsub Підкатегорія в Категорія\n"
        "/find слово | #тег | cat:Назва | sub:Назва\n"
        "В нотатках можеш писати: cat:Фурнітура sub:Ручки #ідеї"
    )

async def diag(update: Update, ctx):
    try:
        info = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo", timeout=15).json()
        await update.message.reply_text("Webhook:\n" + json.dumps(info, ensure_ascii=False, indent=2)[:3500])
    except Exception as e:
        await update.message.reply_text(f"Не вдалося отримати webhook info: {e}")

async def addcat(update: Update, ctx):
    try:
        name = (update.message.text or "").split(maxsplit=1)
        if len(name)<2: return await update.message.reply_text("Використання: /addcat НазваКатегорії")
        page = ensure_category(name[1].strip())
        t = page["properties"]["Name"]["title"][0]["plain_text"]
        await update.message.reply_text(f"✅ Категорія: {t}")
    except Exception as e:
        log.exception("addcat error")
        await update.message.reply_text(f"❌ Notion помилка: {e}")

async def addsub(update: Update, ctx):
    try:
        txt = (update.message.text or "").split(maxsplit=1)
        if len(txt)<2: return await update.message.reply_text("Використання: /addsub Підкатегорія в Категорія")
        m = re.match(r"(.+?)\s+в\s+(.+)", txt[1], re.I)
        if not m: return await update.message.reply_text("Приклад: /addsub Ручки в Фурнітура")
        sub_name = m.group(1).strip(); cat_name = m.group(2).strip()
        page = ensure_subcategory(sub_name, cat_name)
        await update.message.reply_text(f"✅ Підкатегорія: {sub_name} (в {cat_name})")
    except Exception as e:
        log.exception("addsub error")
        await update.message.reply_text(f"❌ Notion помилка: {e}")

async def handle_text(update: Update, ctx):
    try:
        text = update.message.text or ""
        tags = extract_tags(text)
        cat_name, sub_name = parse_cat_sub(text)

        cat_id = sub_id = None
        if cat_name:
            cat = ensure_category(cat_name); cat_id = cat["id"]
        if sub_name:
            sub = ensure_subcategory(sub_name, cat_id or cat_name); sub_id = sub["id"]
            if not cat_id:
                rels = sub["properties"].get("Parent",{}).get("relation",[])
                if rels: cat_id = rels[0]["id"]

        title = text[:120] or "Note"
        notion_create_note(title, text, tags, cat_id, sub_id, None, datetime.datetime.now(), src_link(update.message))
        await update.message.reply_text("💾 Збережено в Notion")
    except Exception as e:
        log.exception("text handler error")
        await update.message.reply_text(f"❌ Помилка: {e}")

async def handle_photo(update: Update, ctx):
    try:
        caption = update.message.caption or ""
        tags = extract_tags(caption)
        cat_name, sub_name = parse_cat_sub(caption)
        cat_id = sub_id = None
        if cat_name: cat = ensure_category(cat_name); cat_id = cat["id"]
        if sub_name:
            sub = ensure_subcategory(sub_name, cat_id or cat_name); sub_id = sub["id"]
            if not cat_id:
                rels = sub["properties"].get("Parent",{}).get("relation",[])
                if rels: cat_id = rels[0]["id"]

        ph = update.message.photo[-1]
        f = await ctx.bot.get_file(ph.file_id)
        file_url = tg_file_url(BOT_TOKEN, f.file_path)
        files = [{"name":"photo.jpg","url":file_url}]
        notion_create_note(caption[:120] or "Photo", caption, tags, cat_id, sub_id, files, datetime.datetime.now(), src_link(update.message))
        await update.message.reply_text("🖼️ Фото збережено в Notion")
    except Exception as e:
        log.exception("photo handler error")
        await update.message.reply_text(f"❌ Помилка: {e}")

async def handle_voice(update: Update, ctx):
    path = None
    try:
        v = update.message.voice
        if not v: return
        tgfile = await ctx.bot.get_file(v.file_id)
        fd, path = tempfile.mkstemp(suffix=".ogg"); os.close(fd)
        await tgfile.download_to_drive(path)
        with open(path, "rb") as f:
            r = client.audio.transcriptions.create(model="whisper-1", file=f)
        text = (r.text or "").strip() if r else ""
        if not text:
            return await update.message.reply_text("Не почув змісту. Скажи ще раз 🙂")

        tags = extract_tags(text)
        cat_name, sub_name = parse_cat_sub(text)
        cat_id = sub_id = None
        if cat_name: cat = ensure_category(cat_name); cat_id = cat["id"]
        if sub_name:
            sub = ensure_subcategory(sub_name, cat_id or cat_name); sub_id = sub["id"]
            if not cat_id:
                rels = sub["properties"].get("Parent",{}).get("relation",[])
                if rels: cat_id = rels[0]["id"]

        notion_create_note(text[:120] or "Voice note", text, tags, cat_id, sub_id, None, datetime.datetime.now(), src_link(update.message))
        await update.message.reply_text("✅ Записав і зберіг у Notion")
    except Exception as e:
        log.exception("voice handler error")
        await update.message.reply_text(f"❌ Помилка: {e}")
    finally:
        try:
            if path and os.path.exists(path): os.remove(path)
        except: pass

async def find_cmd(update: Update, ctx):
    try:
        q = (update.message.text or "").split(maxsplit=1)
        if len(q)<2: return await update.message.reply_text("Використання: /find запит")
        s = q[1].strip()
        if s.startswith("#"):
            flt = {"property":"Tags","multi_select":{"contains": s[1:].lower()}}
        elif s.lower().startswith("cat:"):
            name = s[4:].strip()
            cat = notion_find_catalog_by_name(name)
            if not cat: return await update.message.reply_text("Категорію не знайдено")
            flt = {"property":"Category","relation":{"contains": cat["id"]}}
        elif s.lower().startswith("sub:"):
            name = s[4:].strip()
            sub = notion_find_catalog_by_name(name)
            if not sub: return await update.message.reply_text("Підкатегорію не знайдено")
            flt = {"property":"Subcategory","relation":{"contains": sub["id"]}}
        else:
            flt = {"or":[
                {"property":"Name","title":{"contains": s}},
                {"property":"Text","rich_text":{"contains": s}},
            ]}
        res = notion_query(NOTES_DB_ID, flt, page_size=10)
        if not res: return await update.message.reply_text("Нічого не знайшов.")
        lines = []
        for p in res:
            try: name = p["properties"]["Name"]["title"][0]["plain_text"]
            except: name = "(без назви)"
            try: dt = p["properties"]["Created"]["date"]["start"][:16].replace("T"," ")
            except: dt = ""
            lines.append(f"• {name}  {dt}")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        log.exception("find error")
        await update.message.reply_text(f"❌ Помилка: {e}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("diag", diag))
    app.add_handler(CommandHandler("addcat", addcat))
    app.add_handler(CommandHandler("addsub", addsub))
    app.add_handler(CommandHandler("find", find_cmd))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    path = WEBHOOK_URL.rsplit("/",1)[-1] if WEBHOOK_URL else None
    if WEBHOOK_URL and WEBHOOK_SECRET:
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=path,
                        webhook_url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET,
                        drop_pending_updates=True)
    else:
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    log.info("Starting… CATALOG_DB_ID=%s NOTES_DB_ID=%s", CATALOG_DB_ID, NOTES_DB_ID)
    main()
