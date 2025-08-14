import os, tempfile
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from openai import OpenAI

BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.getenv("PORT", "8000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")        # https://<service>.up.railway.app/<SECRET>
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # той самий <SECRET>

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

async def start(update, ctx):
    await update.message.reply_text("Бот на Railway ✅ Надішли голосове — я перетворю в текст.")

async def transcribe_voice(update, ctx):
    v = update.message.voice or update.message.audio
    if not v:
        return
    # 1) скачати у тимчасовий файл
    tgfile = await ctx.bot.get_file(v.file_id)
    fd, path = tempfile.mkstemp(suffix=".ogg")   # файл у /tmp
    os.close(fd)
    await tgfile.download_to_drive(path)

    try:
        # 2) розпізнати через Whisper API
        with open(path, "rb") as f:
            r = client.audio.transcriptions.create(model="whisper-1", file=f)
        text = (r.text or "").strip() if r else ""
        if not text:
            text = "Не почув змісту. Скажи, будь ласка, ще раз 🙂"
        await update.message.reply_text(f"✅ Записав: {text}")
    except Exception as e:
        await update.message.reply_text("❌ Помилка розпізнавання. Спробуй ще раз.")
    finally:
        # 3) гарантовано видалити аудіо
        try: os.remove(path)
        except: pass

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, transcribe_voice))

    path = WEBHOOK_URL.rsplit("/", 1)[-1] if WEBHOOK_URL else None
    if WEBHOOK_URL and WEBHOOK_SECRET:
        app.run_webhook(
            listen="0.0.0.0", port=PORT, url_path=path,
            webhook_url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True
        )
    else:
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
