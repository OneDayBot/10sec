import os
from telegram.ext import Application, CommandHandler

BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.getenv("PORT", "8000"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL")          # https://<service>.up.railway.app/<SECRET>
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")    # той самий <SECRET>

async def start(update, ctx):
    await update.message.reply_text("Бот на Railway ✅")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    if WEBHOOK_URL and WEBHOOK_SECRET:
        path = WEBHOOK_URL.rsplit("/", 1)[-1]
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=path,
                        webhook_url=WEBHOOK_URL, secret_token=WEBHOOK_SECRET,
                        drop_pending_updates=True)
    else:
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
