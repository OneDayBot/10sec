# app.py
# -*- coding: utf-8 -*-
# OneDay Telegram Bot — разові оголошення
# PTB 20.x, Python 3.13
# Логіка: покроковий збір заявки → модерація адміном → публікація в канали

import os
import re
import logging
from datetime import datetime
from html import escape
from typing import Dict, Any, Optional

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    ApplicationBuilder, Application, ContextTypes,
    CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters
)

# ===================== CONFIG =====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger("oneday-bot")

# ⛔️ ПІДСТАВ ТУТ СВІЙ ТОКЕН (краще — через змінну оточення TELEGRAM_BOT_TOKEN)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or "PASTE_YOUR_BOT_TOKEN_HERE"
ADMIN_ID = 924805332  # твій адмін

MAIN_CHANNEL = "@OneDaySK"
CITY_CHANNELS = {
    "Bratislava": "@OneDayBratislava",
    "Košice": "@OneDayKosice",
    "Prešov": "@OneDayPresov",
    "Žilina": "@OneDayZilina",
    "Nitra": "@OneDayNitra",
}

SUPPORTED_CITIES = list(CITY_CHANNELS.keys())

# ===================== STATES =====================
(
    LANG,
    CITY,
    TASK,
    ADDRESS,
    CHOOSING_LOCATION,
    WAITING_LOCATION_CONFIRM,
    DATETIME_SCHEDULE,
    HELPERS,
    PAY_TYPE,
    PAY_VALUE,
    CONTACT,
    PREVIEW,
) = range(100, 112)

# ===================== CALLBACK ACTIONS =====================
ACT_MENU = "menu"
ACT_START_ORDER = "start_order"
ACT_HELP = "help"
ACT_PAY_FIXED = "pay_fixed"
ACT_PAY_HOURLY = "pay_hourly"
ACT_MOD_APPROVE = "mod_approve"
ACT_MOD_REJECT = "mod_reject"

# ===================== HELPERS =====================
def menu_inline() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Створити оголошення", callback_data=ACT_START_ORDER)],
        [InlineKeyboardButton("ℹ️ Допомога", callback_data=ACT_HELP)],
    ])

def city_kbd() -> ReplyKeyboardMarkup:
    rows = [[c] for c in SUPPORTED_CITIES]
    rows.append(["Інше місто (введу вручну)"])
    rows.append(["⬅️ Назад", "🚪 Скасувати"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

def location_request_kbd() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Обрати на мапі", request_location=True)],
         [KeyboardButton("⬅️ Назад"), KeyboardButton("🚪 Скасувати")]],
        resize_keyboard=True, one_time_keyboard=True
    )

def location_confirm_ikbd(lat: float, lon: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Підтвердити", callback_data="loc_confirm")],
        [InlineKeyboardButton("🔁 Обрати іншу", callback_data="loc_change")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="loc_back")],
        [InlineKeyboardButton("🗺️ Відкрити в Картах", url=f"https://maps.google.com/?q={lat},{lon}")],
    ])

def pay_type_kbd() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💶 Фіксована сума", callback_data=ACT_PAY_FIXED)],
        [InlineKeyboardButton("⏱️ Погодинна ставка", callback_data=ACT_PAY_HOURLY)],
    ])

def contact_kbd() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📞 Поділитись контактом", request_contact=True)],
         [KeyboardButton("⬅️ Назад"), KeyboardButton("🚪 Скасувати")]],
        resize_keyboard=True, one_time_keyboard=True
    )

def _reset_order(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    context.user_data["order"] = {
        "lang": "uk",
        "city": None,
        "task": None,
        "address": None,
        "location": None,  # {"lat": ..., "lon": ...}
        "datetime": None,
        "helpers": None,
        "pay_type": None,  # "fixed"|"hourly"
        "pay_value": None,
        "contact": None,   # {"phone": "...", "name": "...", "tg": "..."}
        "user": None,      # заповнимо зі старту
    }
    return context.user_data["order"]

def sanitize_phone(text: str) -> Optional[str]:
    digits = re.sub(r"\D", "", text or "")
    if 8 <= len(digits) <= 15:
        return digits
    return None

def format_order(order: Dict[str, Any]) -> str:
    parts = []
    parts.append("📢 <b>Нове оголошення</b>")
    if order.get("city"):
        parts.append(f"🏙️ Місто: <b>{escape(order['city'])}</b>")
    if order.get("address"):
        parts.append(f"📍 Адреса: <b>{escape(order['address'])}</b>")
    if order.get("location"):
        lat = order["location"]["lat"]
        lon = order["location"]["lon"]
        parts.append(f"🗺️ Геоточність: <a href='https://maps.google.com/?q={lat},{lon}'>{lat:.6f}, {lon:.6f}</a>")
    if order.get("task"):
        parts.append(f"🧰 Завдання: <b>{escape(order['task'])}</b>")
    if order.get("datetime"):
        parts.append(f"🗓️ Коли: <b>{escape(order['datetime'])}</b>")
    if order.get("helpers"):
        parts.append(f"👥 К-сть помічників: <b>{order['helpers']}</b>")
    if order.get("pay_type"):
        label = "Фіксована" if order["pay_type"] == "fixed" else "Погодинна"
        parts.append(f"💵 Оплата: <b>{label}</b>")
    if order.get("pay_value"):
        unit = "€ (за все)" if order["pay_type"] == "fixed" else "€/год"
        parts.append(f"   └ сума/ставка: <b>{order['pay_value']} {unit}</b>")
    if order.get("contact"):
        c = order["contact"]
        contact_line = []
        if c.get("name"):
            contact_line.append(escape(c["name"]))
        if c.get("phone"):
            contact_line.append(f"тел: +{c['phone']}")
        if c.get("tg"):
            contact_line.append(f"TG: @{c['tg']}")
        if contact_line:
            parts.append("👤 Контакт: " + " | ".join(contact_line))
    if order.get("user"):
        u = order["user"]
        parts.append(f"— автор: {escape(u.get('name',''))} (id: <code>{u.get('id')}</code>)")
    return "\n".join(parts)

def moderation_kbd(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Схвалити", callback_data=f"{ACT_MOD_APPROVE}:{order_id}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"{ACT_MOD_REJECT}:{order_id}"),
        ]
    ])

# ===================== COMMANDS / MENU =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _reset_order(context)
    user = update.effective_user
    context.user_data["order"]["user"] = {
        "id": user.id,
        "name": user.full_name,
        "tg": user.username or ""
    }
    text = (
        "Вітаю в <b>OneDay</b>!\n\n"
        "Я допоможу створити оголошення на разову роботу і відправлю його на модерацію."
    )
    if update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
        await update.effective_message.reply_text(
            "Напиши мені в особисті повідомлення, щоб створити оголошення."
        )
        return ConversationHandler.END

    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=menu_inline())
    return LANG

async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == ACT_START_ORDER:
        return await ask_city(q, context)
    if q.data == ACT_HELP:
        await q.edit_message_text(
            "ℹ️ Кроки: місто → завдання → адреса → локація → дата/час → кількість помічників → оплата → контакт → підтвердження.",
            reply_markup=menu_inline()
        )
        return LANG
    return LANG

# ===================== FLOW: CITY =====================
async def ask_city(update_or_q, context: ContextTypes.DEFAULT_TYPE):
    if hasattr(update_or_q, "edit_message_text"):
        await update_or_q.edit_message_text("🏙️ Обери місто зі списку або введи вручну:")
        await update_or_q.message.reply_text("Список популярних міст:", reply_markup=city_kbd())
    else:
        await update_or_q.message.reply_text("🏙️ Обери місто зі списку або введи вручну:", reply_markup=city_kbd())
    return CITY

async def on_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text in ("🚪 Скасувати", "/cancel"):
        await update.message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    if text == "⬅️ Назад":
        await update.message.reply_text("Повернув у меню.", reply_markup=ReplyKeyboardRemove())
        await update.message.reply_text("Оберіть дію:", reply_markup=menu_inline())
        return LANG

    context.user_data["order"]["city"] = text
    return await ask_task(update, context)

# ===================== FLOW: TASK =====================
async def ask_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🧰 Коротко опиши завдання (що зробити):",
        reply_markup=ReplyKeyboardRemove()
    )
    return TASK

async def on_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if len(txt) < 5:
        await update.message.reply_text("Опиши трохи детальніше, будь ласка.")
        return TASK
    context.user_data["order"]["task"] = txt
    return await ask_address(update, context)

# ===================== FLOW: ADDRESS =====================
async def ask_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📍 Вкажи адресу (вулиця/будинок/поверх). Якщо важко — напиши «Пропустити».\n"
        "Далі оберемо точку на мапі."
    )
    return ADDRESS

async def on_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if txt.lower() in ("пропустити", "skip"):
        context.user_data["order"]["address"] = None
    else:
        context.user_data["order"]["address"] = txt
    return await ask_location(update, context)

# ===================== FLOW: LOCATION =====================
async def ask_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order = context.user_data.setdefault("order", {})
    order.pop("location", None)

    await update.effective_message.reply_text(
        "📍 Обери локацію на карті: натисни «📍 Обрати на мапі», постав пін і надішли.",
        reply_markup=location_request_kbd()
    )
    return CHOOSING_LOCATION

async def on_location_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    if not loc:
        await update.message.reply_text(
            "Будь ласка, надішли локацію через кнопку «📍 Обрати на мапі».",
            reply_markup=location_request_kbd()
        )
        return CHOOSING_LOCATION

    lat, lon = loc.latitude, loc.longitude
    context.user_data.setdefault("order", {})["location"] = {"lat": lat, "lon": lon}

    await update.message.reply_location(latitude=lat, longitude=lon)
    text = (
        "Ось що ти обрав(ла):\n"
        f"• Широта: {lat:.6f}\n"
        f"• Довгота: {lon:.6f}\n\n"
        "Якщо все ок — підтверджуй."
    )
    await update.message.reply_text(text, reply_markup=location_confirm_ikbd(lat, lon))
    return WAITING_LOCATION_CONFIRM

async def on_location_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data
    if data == "loc_change":
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(
            "Ок, обери іншу точку на мапі.",
            reply_markup=location_request_kbd()
        )
        return CHOOSING_LOCATION

    if data == "loc_back":
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("Повернув до адреси. Можеш уточнити або написати «Пропустити».")
        return ADDRESS

    if data == "loc_confirm":
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("✅ Локацію підтверджено.", reply_markup=ReplyKeyboardRemove())
        return await ask_datetime(q, context)

    await q.message.reply_text("Не впізнав дію. Спробуй ще раз.")
    return WAITING_LOCATION_CONFIRM

# ===================== FLOW: DATETIME =====================
async def ask_datetime(update_or_q, context: ContextTypes.DEFAULT_TYPE):
    send = update_or_q.message.reply_text if hasattr(update_or_q, "message") else update_or_q.edit_message_text
    await send(
        "🗓️ Коли потрібно виконати? Напиши у вільній формі (напр., «сьогодні 16:00», «22.08 о 9:30», «завтра з 10 до 13»)."
    )
    return DATETIME_SCHEDULE

async def on_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    context.user_data["order"]["datetime"] = txt
    return await ask_helpers(update, context)

# ===================== FLOW: HELPERS =====================
async def ask_helpers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👥 Скільки помічників потрібно? (1–4)",
    )
    return HELPERS

async def on_helpers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()
    if not txt.isdigit():
        await update.message.reply_text("Введи число 1–4.")
        return HELPERS
    val = int(txt)
    if val < 1 or val > 4:
        await update.message.reply_text("Вкажи число у діапазоні 1–4.")
        return HELPERS
    context.user_data["order"]["helpers"] = val
    return await ask_pay_type(update, context)

# ===================== FLOW: PAY =====================
async def ask_pay_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "💵 Обери тип оплати:",
        reply_markup=pay_type_kbd()
    )
    return PAY_TYPE

async def on_pay_type_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == ACT_PAY_FIXED:
        context.user_data["order"]["pay_type"] = "fixed"
        await q.edit_message_text("Вкажи фіксовану суму (€) за всю роботу (напр., 60).")
        return PAY_VALUE
    elif q.data == ACT_PAY_HOURLY:
        context.user_data["order"]["pay_type"] = "hourly"
        await q.edit_message_text("Вкажи погодинну ставку (€ / год) (напр., 8).")
        return PAY_VALUE
    else:
        await q.edit_message_text("Оберіть один із варіантів нижче:", reply_markup=pay_type_kbd())
        return PAY_TYPE

async def on_pay_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip().replace(",", ".")
    try:
        val = float(txt)
        if val <= 0:
            raise ValueError
    except Exception:
        await update.message.reply_text("Введи додатне число, наприклад 50 або 8.5")
        return PAY_VALUE
    context.user_data["order"]["pay_value"] = val
    return await ask_contact(update, context)

# ===================== FLOW: CONTACT =====================
async def ask_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📞 Залиш контакт — поділися телефоном кнопкою нижче або напиши номер вручну.",
        reply_markup=contact_kbd()
    )
    return CONTACT

async def on_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    phone = None
    if contact and contact.phone_number:
        phone = sanitize_phone(contact.phone_number)
    else:
        phone = sanitize_phone(update.message.text or "")

    if not phone:
        await update.message.reply_text("Не схоже на номер. Надішли правильний телефон або скористайся кнопкою.")
        return CONTACT

    user = update.effective_user
    context.user_data["order"]["contact"] = {
        "phone": phone,
        "name": user.full_name,
        "tg": user.username or "",
    }
    await update.message.reply_text("Дякую! Формую попередній перегляд…", reply_markup=ReplyKeyboardRemove())
    return await ask_preview(update, context)

# ===================== PREVIEW & MODERATION =====================
async def ask_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order = context.user_data["order"]
    text = format_order(order)

    await update.effective_message.reply_text("Будь ласка, перевір оголошення:")
    if order.get("location"):
        await update.effective_message.reply_location(
            latitude=order["location"]["lat"], longitude=order["location"]["lon"]
        )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
    await update.effective_message.reply_text("✅ Відправляю на модерацію. Чекай на підтвердження.")

    order_id = int(datetime.now().timestamp())
    context.user_data["last_order_id"] = order_id

    admin_text = "🛡️ <b>Модерація оголошення</b>\n\n" + text
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_text,
            parse_mode=ParseMode.HTML,
            reply_markup=moderation_kbd(order_id),
            disable_web_page_preview=True
        )
    except Exception as e:
        log.exception("Не вдалось надіслати адмінам: %s", e)

    await update.effective_message.reply_text("Надіслано на модерацію. Дякую!", reply_markup=menu_inline())
    return PREVIEW

async def on_mod_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await q.edit_message_text("Ти не адмін цієї публікації.")
        return PREVIEW

    data = q.data or ""
    action, _, tail = data.partition(":")
    try:
        order_id = int(tail.strip())
    except Exception:
        order_id = None

    # Для простоти беремо останнє замовлення (демо)
    order = context.user_data.get("order")
    if not order:
        await q.edit_message_text("Замовлення не знайдено (сеанс міг скинутись).")
        return PREVIEW

    if action == ACT_MOD_APPROVE:
        await q.edit_message_text("✅ Схвалено. Публікую…")
        await publish_order(context, order)
    elif action == ACT_MOD_REJECT:
        await q.edit_message_text("❌ Відхилено. (Без публікації)")
    else:
        await q.edit_message_text("Невідома дія.")
    return PREVIEW

async def publish_order(context: ContextTypes.DEFAULT_TYPE, order: Dict[str, Any]):
    text = format_order(order)
    try:
        if order.get("location"):
            await context.bot.send_location(
                chat_id=MAIN_CHANNEL,
                latitude=order["location"]["lat"],
                longitude=order["location"]["lon"]
            )
        await context.bot.send_message(
            chat_id=MAIN_CHANNEL,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
    except Exception as e:
        log.exception("Публікація у головний канал: %s", e)

    city = order.get("city", "")
    city_channel = CITY_CHANNELS.get(city)
    if city_channel:
        try:
            if order.get("location"):
                await context.bot.send_location(
                    chat_id=city_channel,
                    latitude=order["location"]["lat"],
                    longitude=order["location"]["lon"]
                )
            await context.bot.send_message(
                chat_id=city_channel,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        except Exception as e:
            log.exception("Публікація у міський канал: %s", e)

# ===================== CANCEL / FALLBACKS =====================
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Не впізнав команду. Натисни кнопки нижче.", reply_markup=menu_inline())
    return LANG

# ===================== APP SETUP =====================
def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Встав токен у TELEGRAM_BOT_TOKEN або прямо в коді.")

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("menu", start)],
        states={
            LANG: [
                CallbackQueryHandler(on_menu_click, pattern=rf"^({ACT_START_ORDER}|{ACT_HELP})$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, unknown),
            ],
            CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_city)],
            TASK: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_task)],
            ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_address)],
            CHOOSING_LOCATION: [
                MessageHandler(filters.LOCATION, on_location_received),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    lambda u, c: u.message.reply_text(
                        "Щоб продовжити, натисни «📍 Обрати на мапі».",
                        reply_markup=location_request_kbd()
                    ) or CHOOSING_LOCATION
                ),
            ],
            WAITING_LOCATION_CONFIRM: [
                CallbackQueryHandler(on_location_confirm_cb, pattern=r"^loc_(confirm|change|back)$"),
            ],
            DATETIME_SCHEDULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_datetime)],
            HELPERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_helpers)],
            PAY_TYPE: [CallbackQueryHandler(on_pay_type_cb, pattern=rf"^({ACT_PAY_FIXED}|{ACT_PAY_HOURLY})$")],
            PAY_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_pay_value)],
            CONTACT: [
                MessageHandler(filters.CONTACT, on_contact),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_contact),
            ],
            PREVIEW: [
                CallbackQueryHandler(on_mod_action, pattern=rf"^({ACT_MOD_APPROVE}|{ACT_MOD_REJECT}):\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, unknown),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    log.info("OneDay bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
