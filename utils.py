import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

import database as db
import keyboards as kb
from config import LOG_CHAT_ID
from pricing import fmt_num, fmt_rub, lot_price


def esc(value) -> str:
    """Экранирование пользовательского текста для parse_mode=HTML.
    Без этого ник вроде «<b>» ломает сообщение, и админ вообще не получит заявку."""
    return html.escape("" if value is None else str(value), quote=False)


# ---------- отправка ----------

async def safe_edit(call: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None):
    try:
        await call.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        # Сообщение нельзя отредактировать (например, это фото) — шлём новое.
        await call.message.answer(text, reply_markup=reply_markup)


async def send_media(bot: Bot, chat_id: int, file_id: str, media_type: str,
                     caption: str | None = None, reply_markup: InlineKeyboardMarkup | None = None):
    if media_type == "photo":
        return await bot.send_photo(chat_id, file_id, caption=caption, reply_markup=reply_markup)
    return await bot.send_document(chat_id, file_id, caption=caption, reply_markup=reply_markup)


async def mark_processed(call: CallbackQuery, note: str):
    """Дописывает пометку к сообщению админа и убирает кнопки."""
    msg = call.message
    try:
        if msg.photo or msg.document:
            await msg.edit_caption(caption=msg.html_text + note, reply_markup=None)
        else:
            await msg.edit_text(msg.html_text + note, reply_markup=None)
    except TelegramBadRequest:
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        await msg.answer(note.strip())


async def log_event(bot: Bot, text: str):
    if not LOG_CHAT_ID:
        return
    try:
        await bot.send_message(LOG_CHAT_ID, text)
    except Exception:
        logging.warning("Не удалось записать событие в лог-чат", exc_info=True)


# ---------- данные заказа ----------

def order_commission(order) -> float:
    return order["commission"] if order["commission"] is not None else 20.0


def order_lot_price(order) -> int:
    if order["lot_price"]:
        return int(order["lot_price"])
    return lot_price(order["gold_amount"], order_commission(order))


def buyer_label(user_id, buyer) -> str:
    if buyer is None:
        return f"id{user_id}"
    username = f"@{esc(buyer['username'])}" if buyer["username"] else "—"
    return f"{esc(buyer['full_name'])} ({username}, id{user_id})"


def payment_caption(order, buyer) -> str:
    return (
        f"🆕 <b>Заказ №{order['id']}</b> — проверка оплаты\n"
        f"Покупатель: {buyer_label(order['user_id'], buyer)}\n"
        f"Товар: {esc(order['product_name'])} — {order['gold_amount']} G\n"
        f"Игровой ID: <code>{esc(order['game_id'])}</code>\n"
        f"Сумма: <b>{fmt_rub(order['price'])}</b>\n\n"
        "Сверьте поступление в приложении банка, а не только скриншот."
    )


def lot_caption(order, buyer) -> str:
    return (
        f"🎯 <b>Заказ №{order['id']}</b> — выдача голды\n"
        f"Покупатель: {buyer_label(order['user_id'], buyer)}\n"
        f"Игровой ID: <code>{esc(order['game_id'])}</code>\n"
        f"Выдать: <b>{order['gold_amount']} G</b>\n"
        f"Лот должен стоять: <b>{order_lot_price(order)} G</b> "
        f"(комиссия Рынка {fmt_num(order_commission(order))}%)\n\n"
        "Перед покупкой сверьте цену лота в игре с суммой выше. "
        "Купили — нажмите «Голда передана»."
    )


def lot_steps(order) -> str:
    gold = order["gold_amount"]
    return (
        f"Передадим вам <b>{gold} G</b> через Рынок Standoff 2:\n"
        "1️⃣ Выставите на Рынок любой недорогой скин из своего инвентаря.\n"
        f"2️⃣ Цена лота — ровно <b>{order_lot_price(order)} G</b> "
        f"(комиссия Рынка {fmt_num(order_commission(order))}% уже учтена, на баланс придёт {gold} G).\n"
        "3️⃣ Сделайте скриншот, на котором видно скин и цену лота, и пришлите его сюда.\n"
        "4️⃣ Не снимайте лот, пока не получите сообщение, что заказ выполнен.\n\n"
        "Цена должна быть точной — с другой ценой лот не выкупим, придётся выставлять заново."
    )


def lot_instructions(order) -> str:
    return f"✅ Оплата по заказу №{order['id']} подтверждена!\n\n" + lot_steps(order)


# ---------- карточки для админов ----------

async def send_order_card(bot: Bot, chat_id: int, order):
    buyer = await db.get_user(order["user_id"])
    status = order["status"]
    if status == "lot_review" and order["lot_file_id"]:
        await send_media(bot, chat_id, order["lot_file_id"], order["lot_type"],
                         lot_caption(order, buyer), kb.lot_review_kb(order["id"]))
    elif status == "pending" and order["proof_file_id"]:
        await send_media(bot, chat_id, order["proof_file_id"], order["proof_type"],
                         payment_caption(order, buyer), kb.order_review_kb(order["id"]))
    else:
        await bot.send_message(chat_id, f"Заказ №{order['id']}: статус — {order['status']}")


async def notify_admins_card(bot: Bot, order):
    for admin_id in await db.get_admin_ids():
        try:
            await send_order_card(bot, admin_id, order)
        except Exception:
            logging.warning("Не удалось отправить заказ №%s админу %s", order["id"], admin_id,
                            exc_info=True)


async def notify_admins_text(bot: Bot, text: str):
    for admin_id in await db.get_admin_ids():
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logging.warning("Не удалось написать админу %s", admin_id, exc_info=True)
