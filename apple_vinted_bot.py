import asyncio
import html
import logging
import os
import threading
from datetime import datetime

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)

if os.environ.get("APPLE_BOT_TOKEN"):
    os.environ["BOT_TOKEN"] = os.environ["APPLE_BOT_TOKEN"]
os.environ.setdefault("BOT_USER_STATE_FILE", "apple_vinted_profiles.json")

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from apple_vinted_platform import APPLE_VINTED_REGIONS, apple_vinted_loop
from shared import (
    BOT_TOKEN,
    MSK_TZ,
    age_range_label,
    current_user_id,
    keywords_label,
    log,
    parse_age_range,
    parse_keywords,
    parse_price_range,
    register_chat_id,
    save_current_user_state,
    set_current_user,
    set_telegram_loop,
    state,
)


bot_app = None
MARKET = "apple_vinted"


def _price_label():
    return f"{float(state['apple_vinted_min']):g}-{float(state['apple_vinted_max']):g} EUR"


def _age_label():
    return age_range_label(state["apple_vinted_min_age_hours"], state["apple_vinted_max_age_hours"])


def _keywords_label():
    return keywords_label(MARKET)


def _regions_label():
    return " ".join(f".{code}" for code in APPLE_VINTED_REGIONS)


def menu_text():
    stats = state["apple_vinted_stats"]
    status = "Работает" if state["apple_vinted_running"] else "Остановлен"
    last = datetime.now(MSK_TZ).strftime("%H:%M МСК")
    return (
        "<b>Apple Vinted Parser</b>\n"
        f"└ Регионы: <b>{html.escape(_regions_label())}</b>\n\n"
        f"<b>Статус:</b> {status}\n"
        f"<b>Цена:</b> {_price_label()}\n"
        f"<b>Публикация:</b> {_age_label()}\n"
        f"<b>Ключи:</b> {_keywords_label()}\n"
        f"<b>Найдено:</b> {stats['found']} | <b>Циклов:</b> {stats['cycles']}\n"
        f"<b>Обновлено:</b> {last}\n\n"
        "Фильтр пропускает только Apple-технику и режет аксессуары, чехлы, кабели, коробки, "
        "ремешки, сломанное и iCloud-locked."
    )


def menu_kb():
    run_text = "⏹ Остановить" if state["apple_vinted_running"] else "▶ Запустить"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(run_text, callback_data="toggle")],
        [
            InlineKeyboardButton("Цена", callback_data="price"),
            InlineKeyboardButton("Время", callback_data="age"),
            InlineKeyboardButton("Ключи", callback_data="keywords"),
        ],
        [InlineKeyboardButton("Обновить", callback_data="menu")],
    ])


def reply_kb():
    return ReplyKeyboardMarkup(
        [["Меню", "▶ Запустить"], ["⏹ Остановить", "Цена"], ["Время", "Ключи"]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _run_loop(app, user_id):
    set_current_user(user_id)
    apple_vinted_loop(app)


def _start_thread():
    user_id = current_user_id()
    threading.Thread(target=_run_loop, args=(bot_app, user_id), daemon=True).start()


def _stop_parser():
    state["apple_vinted_running"] = False
    state["apple_vinted_run_id"] = state.get("apple_vinted_run_id", 0) + 1


def _activate_user(update):
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    profile = set_current_user(user.id if user else None, chat.id if chat else None)
    return profile


def _autosave(handler):
    async def wrapped(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        _activate_user(update)
        try:
            return await handler(update, ctx)
        finally:
            save_current_user_state()
    return wrapped


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(menu_text(), parse_mode="HTML", reply_markup=reply_kb())
    await update.message.reply_text("Панель управления", parse_mode="HTML", reply_markup=menu_kb())


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["awaiting"] = None
    _stop_parser()
    await update.message.reply_text("⏹ Apple Vinted остановлен", reply_markup=reply_kb())


async def cmd_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["awaiting"] = "apple_vinted_price_range"
    await update.message.reply_text(
        "Введи диапазон цены Apple Vinted в евро\n"
        f"Сейчас: <b>{_price_label()}</b>\n\n"
        "Например: <code>50-1200</code>",
        parse_mode="HTML",
        reply_markup=reply_kb(),
    )


async def cmd_age(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["awaiting"] = "apple_vinted_age_range"
    await update.message.reply_text(
        "Введи время публикации в часах\n"
        f"Сейчас: <b>{_age_label()}</b>\n\n"
        "Например: <code>24</code> или <code>1-12</code>",
        parse_mode="HTML",
        reply_markup=reply_kb(),
    )


async def cmd_keywords(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["awaiting"] = "apple_vinted_keywords"
    await update.message.reply_text(
        "Введи ключевые слова через запятую\n"
        f"Сейчас: <b>{_keywords_label()}</b>\n\n"
        "Например: <code>iphone 13, macbook m1, airpods pro</code>\n"
        "Чтобы очистить: <code>-</code>",
        parse_mode="HTML",
        reply_markup=reply_kb(),
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = getattr(q, "from_user", None)
    chat_id = q.message.chat_id if q.message else None
    set_current_user(user.id if user else None, chat_id)
    register_chat_id(chat_id)
    data = q.data

    async def edit(text=None):
        await q.edit_message_text(text or menu_text(), parse_mode="HTML", reply_markup=menu_kb())

    if data == "menu":
        await edit()
        return

    if data == "toggle":
        if state["apple_vinted_running"]:
            _stop_parser()
        else:
            state["apple_vinted_run_id"] = state.get("apple_vinted_run_id", 0) + 1
            state["apple_vinted_running"] = True
            _start_thread()
        await edit()
        return

    if data == "price":
        state["awaiting"] = "apple_vinted_price_range"
        await edit(
            "Введи диапазон цены Apple Vinted в евро\n"
            f"Сейчас: <b>{_price_label()}</b>\n\n"
            "Например: <code>50-1200</code>"
        )
        return

    if data == "age":
        state["awaiting"] = "apple_vinted_age_range"
        await edit(
            "Введи время публикации в часах\n"
            f"Сейчас: <b>{_age_label()}</b>\n\n"
            "Например: <code>24</code> или <code>1-12</code>"
        )
        return

    if data == "keywords":
        state["awaiting"] = "apple_vinted_keywords"
        await edit(
            "Введи ключевые слова через запятую\n"
            f"Сейчас: <b>{_keywords_label()}</b>\n\n"
            "Например: <code>iphone 13, macbook m1, airpods pro</code>\n"
            "Чтобы очистить: <code>-</code>"
        )
        return


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    register_chat_id(message.chat_id)
    raw_text = message.text or ""
    text = raw_text.strip()
    button_text = text.lower()
    awaiting = state.get("awaiting")

    if button_text in ("меню", "menu", "/start"):
        state["awaiting"] = None
        await message.reply_text(menu_text(), parse_mode="HTML", reply_markup=menu_kb())
        return

    if button_text in ("▶ запустить", "запустить", "start"):
        if not state["apple_vinted_running"]:
            state["apple_vinted_run_id"] = state.get("apple_vinted_run_id", 0) + 1
            state["apple_vinted_running"] = True
            _start_thread()
        await message.reply_text(menu_text(), parse_mode="HTML", reply_markup=menu_kb())
        return

    if button_text in ("⏹ остановить", "остановить", "стоп", "stop"):
        _stop_parser()
        state["awaiting"] = None
        await message.reply_text("⏹ Apple Vinted остановлен", reply_markup=reply_kb())
        return

    if button_text in ("цена", "price", "/price"):
        await cmd_price(update, ctx)
        return

    if button_text in ("время", "age", "/age"):
        await cmd_age(update, ctx)
        return

    if button_text in ("ключи", "keywords", "/keywords"):
        await cmd_keywords(update, ctx)
        return

    if awaiting == "apple_vinted_keywords":
        state["apple_vinted_keywords"] = parse_keywords(text)
        state["awaiting"] = None
        await message.reply_text(
            f"✅ Ключи: <b>{_keywords_label()}</b>\n\n{menu_text()}",
            parse_mode="HTML",
            reply_markup=menu_kb(),
        )
        return

    if awaiting == "apple_vinted_price_range":
        try:
            min_price, max_price = parse_price_range(text.replace(",", "."), is_int=False)
        except ValueError:
            await message.reply_text("Нужен диапазон цены. Например: 50-1200", reply_markup=reply_kb())
            return
        state["apple_vinted_min"] = min_price
        state["apple_vinted_max"] = max_price
        state["awaiting"] = None
        await message.reply_text(
            f"✅ Цена: <b>{_price_label()}</b>\n\n{menu_text()}",
            parse_mode="HTML",
            reply_markup=menu_kb(),
        )
        return

    if awaiting == "apple_vinted_age_range":
        try:
            min_age, max_age = parse_age_range(text)
        except ValueError:
            await message.reply_text("Нужно число часов или диапазон. Например: 24 или 1-12", reply_markup=reply_kb())
            return
        state["apple_vinted_min_age_hours"] = min_age
        state["apple_vinted_max_age_hours"] = max_age
        state["awaiting"] = None
        await message.reply_text(
            f"✅ Время публикации: <b>{_age_label()}</b>\n\n{menu_text()}",
            parse_mode="HTML",
            reply_markup=menu_kb(),
        )
        return

    await message.reply_text(menu_text(), parse_mode="HTML", reply_markup=menu_kb())


async def setup_bot_commands(app):
    set_telegram_loop(asyncio.get_running_loop())
    await app.bot.set_my_commands([
        BotCommand("start", "Запустить Apple Vinted bot"),
        BotCommand("stop", "Остановить парсинг"),
        BotCommand("price", "Диапазон цен"),
        BotCommand("age", "Время публикации"),
        BotCommand("keywords", "Ключевые слова"),
    ])


def main():
    global bot_app
    if not BOT_TOKEN:
        raise RuntimeError("Set APPLE_BOT_TOKEN or BOT_TOKEN")
    log.info("Starting Apple Vinted bot")
    log.info("Apple Vinted regions: %s", ", ".join(APPLE_VINTED_REGIONS))
    log.info("Token source: %s", "APPLE_BOT_TOKEN" if os.environ.get("APPLE_BOT_TOKEN") else "BOT_TOKEN")
    bot_app = Application.builder().token(BOT_TOKEN).post_init(setup_bot_commands).build()
    bot_app.add_handler(CommandHandler("start", _autosave(cmd_start)))
    bot_app.add_handler(CommandHandler("stop", _autosave(cmd_stop)))
    bot_app.add_handler(CommandHandler("price", _autosave(cmd_price)))
    bot_app.add_handler(CommandHandler("age", _autosave(cmd_age)))
    bot_app.add_handler(CommandHandler("keywords", _autosave(cmd_keywords)))
    bot_app.add_handler(CallbackQueryHandler(_autosave(on_callback)))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _autosave(on_message)))
    log.info("Apple Vinted bot polling started. Open Telegram and send /start")
    bot_app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
