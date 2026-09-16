import logging
import os
import re
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
from catalog import MASTERS, SERVICES, money, service_price

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("rival-bot")
# Не выводим HTTP-запросы Telegram в production-логи: URL Bot API содержит токен.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

KYIV = ZoneInfo("Europe/Kyiv")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
BOOKING_URL = os.getenv(
    "BOOKING_URL", "https://rival.team/ua/book/master"
).strip()

CHOOSE_FLOW, MASTER, SERVICE, DATE, TIME, PHONE = range(6)

UA_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
UA_MONTHS = [
    "",
    "січня",
    "лютого",
    "березня",
    "квітня",
    "травня",
    "червня",
    "липня",
    "серпня",
    "вересня",
    "жовтня",
    "листопада",
    "грудня",
]

STATUS_LABEL = {
    "pending": "Очікує підтвердження",
    "confirmed": "Підтверджено",
    "cancelled": "Скасовано",
}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Записатися", callback_data="book")],
            [InlineKeyboardButton("Мої записи", callback_data="my_bookings")],
            [
                InlineKeyboardButton("Майстри", callback_data="masters"),
                InlineKeyboardButton("Послуги та ціни", callback_data="services"),
            ],
            [InlineKeyboardButton("Контакти", callback_data="contacts")],
            [InlineKeyboardButton("Записатися на сайті", url=BOOKING_URL)],
        ]
    )


def back_main_row():
    return [InlineKeyboardButton("← Головне меню", callback_data="main")]


def fmt_date(iso_date: str) -> str:
    d = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{d.day} {UA_MONTHS[d.month]} {d.year}"


def booking_text(row: dict) -> str:
    master = MASTERS[row["master_id"]]
    service = SERVICES[row["service_id"]]
    price = service_price(row["service_id"], row["master_id"])
    return (
        f"Запис #{row['id']}\n"
        f"{master['name']} · {master['level']}\n"
        f"{service['name']}\n"
        f"{fmt_date(row['visit_date'])}, {row['visit_time']}\n"
        f"{money(price)}\n"
        f"Статус: {STATUS_LABEL.get(row['status'], row['status'])}"
    )


async def send_or_edit(update: Update, text: str, reply_markup=None):
    if update.callback_query:
        q = update.callback_query
        try:
            await q.edit_message_text(text, reply_markup=reply_markup)
        except Exception:
            await q.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=reply_markup)


async def show_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        await update.callback_query.answer()
    user = update.effective_user
    db.upsert_user(user.id, user.username, user.first_name)
    text = (
        "RIVAL BARBERSHOP\n\n"
        "Запис до майстра, послуги та ваші візити — тут."
    )
    await send_or_edit(update, text, main_keyboard())


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_main(update, ctx)


async def cb_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_main(update, ctx)


def masters_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for master_id, master in MASTERS.items():
        rows.append(
            [
                InlineKeyboardButton(
                    f"{master['name']} · {master['level']}",
                    callback_data=f"m:{master_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("Скасувати", callback_data="book_cancel")])
    return InlineKeyboardMarkup(rows)


def services_keyboard(master_id: str | None = None) -> InlineKeyboardMarkup:
    rows = []
    for service_id, service in SERVICES.items():
        label = service["name"]
        if master_id:
            label += f" · {money(service_price(service_id, master_id))}"
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"s:{service_id}")]
        )
    rows.append([InlineKeyboardButton("Скасувати", callback_data="book_cancel")])
    return InlineKeyboardMarkup(rows)


def dates_keyboard(days: int = 14) -> InlineKeyboardMarkup:
    now = datetime.now(KYIV)
    rows, row = [], []
    for i in range(days):
        d = (now + timedelta(days=i)).date()
        label = f"{UA_WEEKDAYS[d.weekday()]} {d.day:02d}.{d.month:02d}"
        row.append(
            InlineKeyboardButton(label, callback_data=f"d:{d.isoformat()}")
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("Скасувати", callback_data="book_cancel")])
    return InlineKeyboardMarkup(rows)


def times_keyboard(visit_date: str) -> InlineKeyboardMarkup:
    now = datetime.now(KYIV)
    chosen = datetime.strptime(visit_date, "%Y-%m-%d").date()
    slots = []
    current = datetime.combine(chosen, time(10, 0), tzinfo=KYIV)
    end = datetime.combine(chosen, time(21, 30), tzinfo=KYIV)

    while current <= end:
        # На сьогодні не показуємо час, який вже минув; залишаємо мінімум 60 хв запасу.
        if chosen != now.date() or current >= now + timedelta(hours=1):
            slots.append(current.strftime("%H:%M"))
        current += timedelta(minutes=30)

    rows, row = [], []
    for slot in slots:
        row.append(InlineKeyboardButton(slot, callback_data=f"t:{slot}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if not rows:
        tomorrow = (chosen + timedelta(days=1)).isoformat()
        rows.append(
            [InlineKeyboardButton("Обрати завтра", callback_data=f"d:{tomorrow}")]
        )
    rows.append([InlineKeyboardButton("Скасувати", callback_data="book_cancel")])
    return InlineKeyboardMarkup(rows)


async def cb_masters_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lines = ["МАЙСТРИ RIVAL", ""]
    for master in MASTERS.values():
        lines.append(f"{master['name']} · {master['level']}")
    await q.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Записатися", callback_data="book")],
                back_main_row(),
            ]
        ),
    )


async def cb_services_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lines = ["ПОСЛУГИ ТА ЦІНИ", ""]
    for service in SERVICES.values():
        p = service["prices"]
        lines.append(
            f"{service['name']}\n"
            f"Експерт {money(p['Експерт'])} · "
            f"Старший {money(p['Старший Експерт'])} · "
            f"Топ {money(p['Топ Експерт'])} · "
            f"Амбасадор {money(p['Амбасадор'])}"
        )
    await q.edit_message_text(
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Записатися", callback_data="book")],
                back_main_row(),
            ]
        ),
    )


async def cb_contacts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        "RIVAL BARBERSHOP\n\n"
        "Київ, вул. Шота Руставелі, 31а\n"
        "+38 (073) 534-08-12\n"
        "Щодня 10:00–22:00",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Записатися", callback_data="book")],
                back_main_row(),
            ]
        ),
    )


async def booking_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["booking"] = {}
    await q.edit_message_text(
        "Як вам зручніше записатися?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("До конкретного майстра", callback_data="flow:master")],
                [InlineKeyboardButton("На конкретну дату", callback_data="flow:date")],
                [InlineKeyboardButton("Скасувати", callback_data="book_cancel")],
            ]
        ),
    )
    return CHOOSE_FLOW


async def choose_flow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    flow = q.data.split(":", 1)[1]
    ctx.user_data["booking"]["flow"] = flow

    if flow == "master":
        await q.edit_message_text("Оберіть майстра:", reply_markup=masters_keyboard())
        return MASTER

    await q.edit_message_text(
        "Оберіть бажану дату:", reply_markup=dates_keyboard()
    )
    return DATE


async def choose_master(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    master_id = q.data.split(":", 1)[1]
    if master_id not in MASTERS:
        await q.answer("Майстра не знайдено", show_alert=True)
        return MASTER

    b = ctx.user_data.setdefault("booking", {})
    b["master_id"] = master_id

    if b.get("flow") == "date":
        await q.edit_message_text(
            f"{MASTERS[master_id]['name']} обрано.\nОберіть бажаний час:",
            reply_markup=times_keyboard(b["visit_date"]),
        )
        return TIME

    await q.edit_message_text(
        f"{MASTERS[master_id]['name']} · {MASTERS[master_id]['level']}\n\n"
        "Оберіть послугу:",
        reply_markup=services_keyboard(master_id),
    )
    return SERVICE


async def choose_service(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    service_id = q.data.split(":", 1)[1]
    if service_id not in SERVICES:
        await q.answer("Послугу не знайдено", show_alert=True)
        return SERVICE

    b = ctx.user_data.setdefault("booking", {})
    b["service_id"] = service_id

    if b.get("flow") == "date":
        await q.edit_message_text(
            f"{SERVICES[service_id]['name']} обрано.\nОберіть майстра:",
            reply_markup=masters_keyboard(),
        )
        return MASTER

    await q.edit_message_text(
        f"{SERVICES[service_id]['name']} · "
        f"{money(service_price(service_id, b['master_id']))}\n\n"
        "Оберіть бажану дату:",
        reply_markup=dates_keyboard(),
    )
    return DATE


async def choose_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    visit_date = q.data.split(":", 1)[1]
    try:
        datetime.strptime(visit_date, "%Y-%m-%d")
    except ValueError:
        await q.answer("Некоректна дата", show_alert=True)
        return DATE

    b = ctx.user_data.setdefault("booking", {})
    b["visit_date"] = visit_date

    if b.get("flow") == "date" and "service_id" not in b:
        await q.edit_message_text(
            f"{fmt_date(visit_date)}\n\nОберіть послугу:",
            reply_markup=services_keyboard(),
        )
        return SERVICE

    await q.edit_message_text(
        f"{fmt_date(visit_date)}\n\nОберіть бажаний час:",
        reply_markup=times_keyboard(visit_date),
    )
    return TIME


async def choose_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    visit_time = q.data.split(":", 1)[1]
    if not re.fullmatch(r"\d{2}:\d{2}", visit_time):
        await q.answer("Некоректний час", show_alert=True)
        return TIME

    b = ctx.user_data.setdefault("booking", {})
    b["visit_time"] = visit_time

    master = MASTERS[b["master_id"]]
    service = SERVICES[b["service_id"]]
    price = service_price(b["service_id"], b["master_id"])
    summary = (
        "Перевірте заявку:\n\n"
        f"{master['name']} · {master['level']}\n"
        f"{service['name']}\n"
        f"{fmt_date(b['visit_date'])}, {visit_time}\n"
        f"{money(price)}\n\n"
        "Надішліть номер телефону кнопкою нижче. "
        "Час буде остаточно підтверджено адміністратором."
    )
    await q.edit_message_text(summary)
    await q.message.reply_text(
        "Ваш номер телефону:",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Поділитися номером", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True,
        ),
    )
    return PHONE


async def _save_booking(update: Update, ctx: ContextTypes.DEFAULT_TYPE, phone: str):
    user = update.effective_user
    b = ctx.user_data.get("booking") or {}
    required = {"master_id", "service_id", "visit_date", "visit_time"}
    if not required.issubset(b):
        await update.effective_message.reply_text(
            "Сесію запису втрачено. Почніть ще раз: /start",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    db.upsert_user(user.id, user.username, user.first_name, phone)
    booking_id = db.create_booking(
        user.id,
        b["master_id"],
        b["service_id"],
        b["visit_date"],
        b["visit_time"],
        phone,
    )
    row = db.get_booking(booking_id)

    await update.effective_message.reply_text(
        "Заявку прийнято.\n\n"
        + booking_text(row)
        + "\n\nАдміністратор підтвердить час у цьому чаті.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.effective_message.reply_text(
        "Головне меню:", reply_markup=main_keyboard()
    )

    admin_markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Підтвердити", callback_data=f"adm_confirm:{booking_id}"
                ),
                InlineKeyboardButton(
                    "Скасувати", callback_data=f"adm_cancel:{booking_id}"
                ),
            ]
        ]
    )
    admin_text = (
        "НОВА ЗАЯВКА RIVAL\n\n"
        + booking_text(row)
        + f"\nТелефон: {phone}\n"
        + f"Telegram: @{user.username}" if user.username else ""
    )
    # Через пріоритет операторів формуємо текст окремо, щоб username не обрізав основу.
    admin_text = "НОВА ЗАЯВКА RIVAL\n\n" + booking_text(row) + f"\nТелефон: {phone}"
    if user.username:
        admin_text += f"\nTelegram: @{user.username}"
    admin_text += f"\nUser ID: {user.id}"

    for admin_id in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                chat_id=admin_id,
                text=admin_text,
                reply_markup=admin_markup,
            )
        except Exception as e:
            logger.warning("Cannot notify admin %s: %s", admin_id, e)

    ctx.user_data.pop("booking", None)
    return ConversationHandler.END


async def receive_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    return await _save_booking(update, ctx, contact.phone_number)


async def receive_phone_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text.strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 10:
        await update.message.reply_text(
            "Не схоже на номер телефону. Надішліть номер у форматі +380..."
        )
        return PHONE
    phone = raw if raw.startswith("+") else f"+{digits}"
    return await _save_booking(update, ctx, phone)


async def cancel_booking_flow(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.pop("booking", None)
    await q.edit_message_text("Запис скасовано.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def cb_my_bookings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    rows = db.get_user_bookings(q.from_user.id, limit=10)
    if not rows:
        await q.edit_message_text(
            "У вас поки немає активних записів.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Записатися", callback_data="book")],
                    back_main_row(),
                ]
            ),
        )
        return

    buttons = []
    text_parts = ["МОЇ ЗАПИСИ", ""]
    for row in rows:
        text_parts.append(booking_text(row))
        text_parts.append("")
        if row["status"] in {"pending", "confirmed"}:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"Скасувати запис #{row['id']}",
                        callback_data=f"user_cancel:{row['id']}",
                    )
                ]
            )

    last = rows[-1]
    buttons.append(
        [
            InlineKeyboardButton(
                "Записатися як минулого разу",
                callback_data=f"repeat:{last['id']}",
            )
        ]
    )
    buttons.append(back_main_row())
    await q.edit_message_text(
        "\n".join(text_parts),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_repeat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    booking_id = int(q.data.split(":", 1)[1])
    row = db.get_booking(booking_id)
    if not row or row["telegram_id"] != q.from_user.id:
        await q.answer("Запис не знайдено", show_alert=True)
        return ConversationHandler.END

    ctx.user_data["booking"] = {
        "flow": "master",
        "master_id": row["master_id"],
        "service_id": row["service_id"],
    }
    master = MASTERS[row["master_id"]]
    service = SERVICES[row["service_id"]]
    await q.edit_message_text(
        "Повторюємо минулий запис:\n"
        f"{master['name']} · {service['name']}\n\n"
        "Оберіть нову дату:",
        reply_markup=dates_keyboard(),
    )
    return DATE


async def cb_user_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    booking_id = int(q.data.split(":", 1)[1])
    row = db.get_booking(booking_id)
    if not row or row["telegram_id"] != q.from_user.id:
        await q.answer("Запис не знайдено", show_alert=True)
        return
    row = db.set_booking_status(booking_id, "cancelled")
    await q.edit_message_text(
        "Запис скасовано.\n\n" + booking_text(row),
        reply_markup=main_keyboard(),
    )
    for admin_id in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                admin_id,
                "КЛІЄНТ СКАСУВАВ ЗАПИС\n\n" + booking_text(row),
            )
        except Exception:
            pass


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = db.get_pending_bookings(limit=20)
    if not rows:
        await update.message.reply_text("Нових заявок немає.")
        return
    await update.message.reply_text(f"Очікують підтвердження: {len(rows)}")
    for row in rows:
        await update.message.reply_text(
            booking_text(row),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Підтвердити", callback_data=f"adm_confirm:{row['id']}"
                        ),
                        InlineKeyboardButton(
                            "Скасувати", callback_data=f"adm_cancel:{row['id']}"
                        ),
                    ]
                ]
            ),
        )


async def cb_admin_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.answer("Немає доступу", show_alert=True)
        return

    action, raw_id = q.data.split(":", 1)
    booking_id = int(raw_id)
    status = "confirmed" if action == "adm_confirm" else "cancelled"
    row = db.set_booking_status(booking_id, status)
    if not row:
        await q.answer("Запис не знайдено", show_alert=True)
        return

    prefix = "ПІДТВЕРДЖЕНО" if status == "confirmed" else "СКАСОВАНО"
    await q.edit_message_text(f"{prefix}\n\n" + booking_text(row))

    client_text = (
        "Ваш запис підтверджено. До зустрічі у RIVAL!\n\n"
        if status == "confirmed"
        else "На жаль, цей запис скасовано. Оберіть інший час через меню.\n\n"
    )
    try:
        await ctx.bot.send_message(
            row["telegram_id"],
            client_text + booking_text(row),
            reply_markup=main_keyboard(),
        )
    except Exception as e:
        logger.warning("Cannot notify client: %s", e)


async def unknown_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Відкрийте головне меню командою /start."
    )


def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    db.init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    booking_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(booking_entry, pattern=r"^book$"),
            CallbackQueryHandler(cb_repeat, pattern=r"^repeat:\d+$"),
        ],
        states={
            CHOOSE_FLOW: [
                CallbackQueryHandler(choose_flow, pattern=r"^flow:(master|date)$")
            ],
            MASTER: [
                CallbackQueryHandler(choose_master, pattern=r"^m:[a-z0-9_]+$")
            ],
            SERVICE: [
                CallbackQueryHandler(choose_service, pattern=r"^s:[a-z0-9_]+$")
            ],
            DATE: [
                CallbackQueryHandler(choose_date, pattern=r"^d:\d{4}-\d{2}-\d{2}$")
            ],
            TIME: [
                CallbackQueryHandler(choose_time, pattern=r"^t:\d{2}:\d{2}$")
            ],
            PHONE: [
                MessageHandler(filters.CONTACT, receive_contact),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone_text),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_booking_flow, pattern=r"^book_cancel$"),
            CommandHandler("start", cmd_start),
        ],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("id", cmd_id))

    app.add_handler(booking_conv)

    app.add_handler(CallbackQueryHandler(cb_main, pattern=r"^main$"))
    app.add_handler(CallbackQueryHandler(cb_masters_info, pattern=r"^masters$"))
    app.add_handler(CallbackQueryHandler(cb_services_info, pattern=r"^services$"))
    app.add_handler(CallbackQueryHandler(cb_contacts, pattern=r"^contacts$"))
    app.add_handler(CallbackQueryHandler(cb_my_bookings, pattern=r"^my_bookings$"))
    app.add_handler(CallbackQueryHandler(cb_user_cancel, pattern=r"^user_cancel:\d+$"))
    app.add_handler(
        CallbackQueryHandler(
            cb_admin_action, pattern=r"^adm_(confirm|cancel):\d+$"
        )
    )

    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    return app


def main():
    if not BOT_TOKEN:
        # Даём Railway успешно поднять сервис ещё до того, как добавлен секрет.
        # После установки BOT_TOKEN сервис будет перезапущен и бот начнёт polling.
        import time as _time
        logger.warning("BOT_TOKEN is not set yet; waiting for configuration")
        while True:
            _time.sleep(3600)

    app = build_app()
    logger.info("RIVAL bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
