import asyncio
import os
import tempfile
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, Message

import database as db
import keyboards as kb
from pricing import fmt_num, fmt_rub, fmt_ts
from states import (
    AdminAdminStates,
    AdminBroadcastStates,
    AdminProductStates,
    AdminRateStates,
    AdminSettingsStates,
    RejectOrderStates,
)
from utils import (
    esc,
    log_event,
    lot_steps,
    lot_instructions,
    mark_processed,
    safe_edit,
    send_order_card,
)

router = Router()

SETTING_LABELS = {
    "card_number": "номер карты",
    "card_holder": "имя получателя",
    "card_bank": "банк",
    "market_commission": "комиссия Рынка, %",
    "min_gold": "минимальный заказ, G",
    "max_gold": "максимальный заказ, G",
    "order_timeout_min": "автоотмена заказа без чека, мин (0 — выкл.)",
    "support_username": "юзернейм поддержки",
    "shop_name": "название магазина",
}

# Числовые настройки: (минимум, максимум)
NUMERIC_SETTINGS = {
    "market_commission": (0, 90),
    "min_gold": (1, 10_000_000),
    "max_gold": (1, 10_000_000),
    "order_timeout_min": (0, 10_080),
}


async def _admin_only(user_id: int) -> bool:
    return await db.is_admin(user_id)


async def _main_admin_only(user_id: int) -> bool:
    return await db.is_main_admin(user_id)


async def _admin_menu_kb(user_id: int):
    return kb.admin_menu(await _main_admin_only(user_id))


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not await _admin_only(message.from_user.id):
        return
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=await _admin_menu_kb(message.from_user.id))


@router.callback_query(F.data == "menu:admin")
async def open_admin_menu(call: CallbackQuery):
    if not await _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав", show_alert=True)
        return
    await safe_edit(call, "🛠 <b>Админ-панель</b>", await _admin_menu_kb(call.from_user.id))
    await call.answer()


@router.callback_query(F.data == "adm:back")
async def back_to_admin(call: CallbackQuery):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    await safe_edit(call, "🛠 <b>Админ-панель</b>", await _admin_menu_kb(call.from_user.id))
    await call.answer()


# ---------- ЗАЯВКИ ----------

@router.callback_query(F.data == "adm:orders")
async def list_orders(call: CallbackQuery):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    orders = await db.get_orders_by_status(("pending", "lot_review"))
    if not orders:
        await call.answer("Нет заявок, требующих действия ✅", show_alert=True)
        return
    await safe_edit(
        call,
        f"🧾 Заявок в работе: <b>{len(orders)}</b>\n"
        "💳 — проверить оплату, 🎯 — выкупить лот и выдать голду.",
        kb.orders_list_kb(orders),
    )
    await call.answer()


@router.callback_query(F.data.startswith("order:view:"))
async def view_order(call: CallbackQuery, bot: Bot):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    order = await db.get_order(int(call.data.split(":")[2]))
    if not order:
        await call.answer("Заказ не найден", show_alert=True)
        return
    await send_order_card(bot, call.from_user.id, order)
    await call.answer()


@router.callback_query(F.data.startswith("order:approve:"))
async def approve_order(call: CallbackQuery, bot: Bot):
    """Оплата подтверждена -> просим покупателя выставить лот."""
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    order_id = int(call.data.split(":")[2])
    if not await db.transition(order_id, ("pending",), "paid", admin_id=call.from_user.id):
        await call.answer("Заказ уже обработан или ещё не оплачен", show_alert=True)
        return
    order = await db.get_order(order_id)

    note = f"\n\n✅ Оплата подтверждена ({esc(call.from_user.full_name)}). Ждём скриншот лота от покупателя."
    try:
        await bot.send_message(order["user_id"], lot_instructions(order))
    except Exception:
        note += "\n⚠️ Не удалось написать покупателю — свяжитесь с ним вручную."
    await mark_processed(call, note)
    await call.answer("Оплата подтверждена ✅")


@router.callback_query(F.data.startswith("order:reject:"))
async def reject_order_start(call: CallbackQuery, state: FSMContext):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    order_id = int(call.data.split(":")[2])
    order = await db.get_order(order_id)
    if not order:
        await call.answer("Заказ не найден", show_alert=True)
        return
    if order["status"] != "pending":
        await call.answer("Заказ уже обработан", show_alert=True)
        return
    await state.update_data(reject_order_id=order_id)
    await state.set_state(RejectOrderStates.reason)
    await call.message.answer(f"✏️ Укажите причину отклонения заказа №{order_id} (или «-» без причины):")
    await call.answer()


@router.message(RejectOrderStates.reason)
async def reject_order_finish(message: Message, state: FSMContext, bot: Bot):
    if not message.text:
        await message.answer("Отправьте причину текстом или «-».")
        return
    data = await state.get_data()
    order_id = data.get("reject_order_id")
    reason = message.text.strip()
    if reason == "-":
        reason = "не указана"
    await state.clear()

    ok = await db.transition(order_id, ("pending",), "rejected", admin_id=message.from_user.id,
                             reject_reason=reason)
    if not ok:
        await message.answer("Заказ уже обработан другим администратором.")
        return
    order = await db.get_order(order_id)
    try:
        await bot.send_message(
            order["user_id"],
            f"❌ Ваш заказ №{order_id} отклонён.\nПричина: {esc(reason)}\n\n"
            "Если это ошибка — напишите в поддержку.",
        )
    except Exception:
        pass
    await message.answer(f"Заказ №{order_id} отклонён.", reply_markup=await _admin_menu_kb(message.from_user.id))


@router.callback_query(F.data.startswith("lot:done:"))
async def lot_done(call: CallbackQuery, bot: Bot):
    """Админ выкупил лот в игре -> заказ выполнен."""
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    order_id = int(call.data.split(":")[2])
    if not await db.transition(order_id, ("lot_review",), "completed", admin_id=call.from_user.id):
        await call.answer("Заказ уже обработан", show_alert=True)
        return
    order = await db.get_order(order_id)

    note = f"\n\n✅ Голда передана ({esc(call.from_user.full_name)})."
    try:
        await bot.send_message(
            order["user_id"],
            f"🎉 Заказ №{order_id} выполнен! Мы выкупили ваш лот — {order['gold_amount']} G "
            "уже на балансе. Проверьте, пожалуйста, и подтвердите ниже.",
            reply_markup=kb.delivery_feedback_kb(order_id),
        )
    except Exception:
        note += "\n⚠️ Не удалось написать покупателю."
    await mark_processed(call, note)
    await log_event(
        bot,
        f"✅ Заказ №{order_id} выполнен: {order['gold_amount']} G за {fmt_rub(order['price'])} "
        f"(покупатель id{order['user_id']}).",
    )
    await call.answer("Готово ✅")


@router.callback_query(F.data.startswith("lot:retry:"))
async def lot_retry(call: CallbackQuery, bot: Bot):
    """Лот не найден / цена не совпала -> просим новый скриншот."""
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    order_id = int(call.data.split(":")[2])
    if not await db.transition(order_id, ("lot_review",), "paid", admin_id=call.from_user.id):
        await call.answer("Заказ уже обработан", show_alert=True)
        return
    order = await db.get_order(order_id)

    note = f"\n\n🔄 Запросили новый скриншот ({esc(call.from_user.full_name)})."
    try:
        await bot.send_message(
            order["user_id"],
            f"⚠️ По заказу №{order_id} не получилось выкупить лот (не нашли или цена не совпала).\n\n"
            + lot_steps(order),
        )
    except Exception:
        note += "\n⚠️ Не удалось написать покупателю."
    await mark_processed(call, note)
    await call.answer("Запрос отправлен")


# ---------- ТОВАРЫ ----------

@router.callback_query(F.data == "adm:products")
async def products_menu(call: CallbackQuery):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    products = await db.get_all_products()
    await safe_edit(
        call,
        "📦 <b>Товары</b>\n\nНажмите на товар, чтобы включить/выключить его:",
        kb.products_admin_kb(products),
    )
    await call.answer()


@router.callback_query(F.data == "prod:add")
async def add_product_start(call: CallbackQuery, state: FSMContext):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    await state.set_state(AdminProductStates.name)
    await call.message.answer("Введите название товара (например: «1000 золота»):")
    await call.answer()


@router.message(AdminProductStates.name)
async def add_product_name(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Отправьте название текстом.")
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(AdminProductStates.amount)
    await message.answer("Введите количество золота (целое число):")


@router.message(AdminProductStates.amount)
async def add_product_amount(message: Message, state: FSMContext):
    try:
        amount = int((message.text or "").strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое положительное число.")
        return
    await state.update_data(amount=amount)
    await state.set_state(AdminProductStates.price)
    await message.answer("Введите цену в рублях (например 199 или 199.90):")


@router.message(AdminProductStates.price)
async def add_product_price(message: Message, state: FSMContext):
    try:
        price = float((message.text or "").strip().replace(",", "."))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительное число, например 199 или 199.90")
        return
    data = await state.get_data()
    await db.add_product(data["name"], data["amount"], price)
    await state.clear()
    products = await db.get_all_products()
    await message.answer("✅ Товар добавлен!", reply_markup=kb.products_admin_kb(products))


@router.callback_query(F.data.startswith("prod:toggle:"))
async def toggle_product(call: CallbackQuery):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    await db.toggle_product(int(call.data.split(":")[2]))
    products = await db.get_all_products()
    await safe_edit(
        call,
        "📦 <b>Товары</b>\n\nНажмите на товар, чтобы включить/выключить его:",
        kb.products_admin_kb(products),
    )
    await call.answer()


@router.callback_query(F.data.startswith("prod:del:"))
async def delete_product(call: CallbackQuery):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    await db.delete_product(int(call.data.split(":")[2]))
    products = await db.get_all_products()
    await safe_edit(call, "🗑 Товар удалён.", kb.products_admin_kb(products))
    await call.answer()


# ---------- КУРС ГОЛДЫ ----------

async def _rate_admin_text() -> str:
    tiers = await db.get_tiers()
    lines = ["📈 <b>Курс голды</b> (₽ за 100 G, по ступеням объёма)\n"]
    if tiers:
        for t in tiers:
            lines.append(f"• от {t['min_gold']} G — {fmt_num(t['rate'])} ₽")
    else:
        lines.append("Ступеней пока нет — покупатели видят только готовые товары.")
    lines.append(f"\n🕒 Обновлён: {fmt_ts(await db.get_setting('rate_updated_at'))}")
    lines.append(
        "\nЧтобы добавить или изменить ступень, нажмите «➕» и отправьте два числа: "
        "<code>мин. голды  цена за 100 G</code>, например <code>1000 75</code>. "
        "Ступень с тем же объёмом будет перезаписана. Нажмите на ступень, чтобы удалить её."
    )
    return "\n".join(lines)


@router.callback_query(F.data == "adm:rate")
async def rate_menu(call: CallbackQuery):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    await safe_edit(call, await _rate_admin_text(), kb.rate_admin_kb(await db.get_tiers()))
    await call.answer()


@router.callback_query(F.data == "rate:add")
async def rate_add_start(call: CallbackQuery, state: FSMContext):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    await state.set_state(AdminRateStates.value)
    await call.message.answer(
        "Отправьте два числа через пробел: минимальный объём (G) и цену за 100 G в рублях.\n"
        "Пример: <code>1000 75</code> — от 1000 G голда стоит 75 ₽ за 100 G."
    )
    await call.answer()


@router.message(AdminRateStates.value)
async def rate_add_finish(message: Message, state: FSMContext):
    parts = (message.text or "").replace(",", ".").split()
    try:
        if len(parts) != 2:
            raise ValueError
        min_gold = int(parts[0])
        rate = float(parts[1])
        if min_gold <= 0 or rate <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Не получилось разобрать. Формат: <code>1000 75</code> (два положительных числа).")
        return
    await db.add_tier(min_gold, rate)
    await state.clear()
    await message.answer("✅ Курс обновлён.\n\n" + await _rate_admin_text(),
                         reply_markup=kb.rate_admin_kb(await db.get_tiers()))


@router.callback_query(F.data.startswith("rate:del:"))
async def rate_delete(call: CallbackQuery):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    await db.delete_tier(int(call.data.split(":")[2]))
    await safe_edit(call, await _rate_admin_text(), kb.rate_admin_kb(await db.get_tiers()))
    await call.answer("Ступень удалена")


# ---------- АДМИНИСТРАТОРЫ (главный админ) ----------

@router.callback_query(F.data == "adm:admins")
async def admins_menu(call: CallbackQuery):
    if not await _main_admin_only(call.from_user.id):
        await call.answer("Доступно только главному админу", show_alert=True)
        return
    admins = await db.get_all_admins()
    await safe_edit(call, "👥 <b>Администраторы</b>", kb.admins_kb(admins))
    await call.answer()


@router.callback_query(F.data == "admn:add")
async def add_admin_start(call: CallbackQuery, state: FSMContext):
    if not await _main_admin_only(call.from_user.id):
        await call.answer("Доступно только главному админу", show_alert=True)
        return
    await state.set_state(AdminAdminStates.waiting_user)
    await call.message.answer(
        "Перешлите сюда любое сообщение от пользователя, которого нужно сделать админом, "
        "либо отправьте его user id числом (его можно узнать, например, через @userinfobot)."
    )
    await call.answer()


@router.message(AdminAdminStates.waiting_user)
async def add_admin_finish(message: Message, state: FSMContext, bot: Bot):
    target_id = None
    target_username = None

    origin = getattr(message, "forward_origin", None)
    fwd_user = getattr(origin, "sender_user", None) or getattr(message, "forward_from", None)
    if fwd_user:
        target_id = fwd_user.id
        target_username = fwd_user.username
    elif message.text and message.text.strip().lstrip("-").isdigit():
        target_id = int(message.text.strip())
    else:
        await message.answer("Не получилось определить пользователя. Перешлите его сообщение или отправьте id числом.")
        return

    if await db.is_admin(target_id):
        await message.answer("Этот пользователь уже администратор.")
    else:
        if not target_username:
            try:
                chat = await bot.get_chat(target_id)
                target_username = chat.username
            except Exception:
                pass
        await db.add_admin(target_id, target_username, added_by=message.from_user.id)
        await message.answer(f"✅ Пользователь {target_id} назначен администратором.")
        try:
            await bot.send_message(target_id, "🎉 Вам выдали права администратора!")
        except Exception:
            pass

    await state.clear()
    admins = await db.get_all_admins()
    await message.answer("👥 <b>Администраторы</b>", reply_markup=kb.admins_kb(admins))


@router.callback_query(F.data.startswith("admn:remove:"))
async def remove_admin(call: CallbackQuery, bot: Bot):
    if not await _main_admin_only(call.from_user.id):
        await call.answer("Доступно только главному админу", show_alert=True)
        return
    target_id = int(call.data.split(":")[2])
    if await db.is_main_admin(target_id):
        await call.answer("Нельзя снять главного админа", show_alert=True)
        return
    await db.remove_admin(target_id)
    try:
        await bot.send_message(target_id, "Ваши права администратора были отозваны.")
    except Exception:
        pass
    admins = await db.get_all_admins()
    await safe_edit(call, "👥 <b>Администраторы</b>", kb.admins_kb(admins))
    await call.answer("Права отозваны")


# ---------- НАСТРОЙКИ (главный админ) ----------

async def _settings_text(prefix: str = "") -> str:
    settings = await db.get_all_settings()
    lines = [
        f"{label}: <code>{esc(settings.get(key, '—'))}</code>"
        for key, label in SETTING_LABELS.items()
    ]
    return prefix + "⚙ <b>Настройки магазина</b>\n\n" + "\n".join(lines)


@router.callback_query(F.data == "adm:settings")
async def settings_menu(call: CallbackQuery):
    if not await _main_admin_only(call.from_user.id):
        await call.answer("Доступно только главному админу", show_alert=True)
        return
    await safe_edit(call, await _settings_text(), kb.settings_kb())
    await call.answer()


@router.callback_query(F.data.startswith("set:"))
async def edit_setting_start(call: CallbackQuery, state: FSMContext):
    if not await _main_admin_only(call.from_user.id):
        await call.answer("Доступно только главному админу", show_alert=True)
        return
    key = call.data.split(":", 1)[1]
    if key not in SETTING_LABELS:
        await call.answer()
        return
    await state.update_data(setting_key=key)
    await state.set_state(AdminSettingsStates.value)
    await call.message.answer(f"Введите новое значение для «{SETTING_LABELS[key]}»:")
    await call.answer()


@router.message(AdminSettingsStates.value)
async def edit_setting_finish(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("Отправьте значение текстом.")
        return
    data = await state.get_data()
    key = data.get("setting_key")
    value = message.text.strip()

    if key in NUMERIC_SETTINGS:
        low, high = NUMERIC_SETTINGS[key]
        try:
            number = float(value.replace(",", "."))
            if not low <= number <= high:
                raise ValueError
        except ValueError:
            await message.answer(f"Нужно число от {low} до {high}. Попробуйте ещё раз.")
            return
        value = fmt_num(number)

    await db.set_setting(key, value)
    await state.clear()
    await message.answer(await _settings_text("✅ Настройка обновлена.\n\n"), reply_markup=kb.settings_kb())


# ---------- СТАТИСТИКА ----------

@router.callback_query(F.data == "adm:stats")
async def show_stats(call: CallbackQuery):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    s = await db.get_stats()
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Пользователей: {s['users']}\n"
        f"Всего заказов: {s['total_orders']}\n"
        f"Оплачено: {s['paid']} (из них выдано: {s['completed']})\n"
        f"Отклонено: {s['rejected']}\n"
        f"Ждут вашего действия: {s['need_action']}\n"
        f"Выручка (оплаченные): {fmt_rub(s['revenue'])}\n"
        f"Выдано голды: {s['gold_delivered']} G\n"
    )
    await safe_edit(call, text, kb.back_to_admin_kb())
    await call.answer()


# ---------- РАССЫЛКА ----------

@router.callback_query(F.data == "adm:broadcast")
async def broadcast_start(call: CallbackQuery, state: FSMContext):
    if not await _admin_only(call.from_user.id):
        return await call.answer()
    await state.set_state(AdminBroadcastStates.waiting_message)
    await call.message.answer("✏️ Отправьте сообщение (текст/фото/файл), которое нужно разослать всем пользователям:")
    await call.answer()


@router.message(AdminBroadcastStates.waiting_message)
async def broadcast_finish(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    user_ids = await db.get_all_user_ids()
    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ Рассылка запущена для {len(user_ids)} пользователей...")
    for uid in user_ids:
        try:
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # лимиты Telegram: не больше ~30 сообщений в секунду
    await status_msg.edit_text(f"✅ Рассылка завершена. Отправлено: {sent}, ошибок: {failed}")


# ---------- БЭКАП / БАН ----------

@router.callback_query(F.data == "adm:backup")
async def backup_db(call: CallbackQuery, bot: Bot):
    if not await _main_admin_only(call.from_user.id):
        await call.answer("Доступно только главному админу", show_alert=True)
        return
    await call.answer("Готовлю копию…")
    tmp_path = os.path.join(tempfile.gettempdir(), f"ishopgold_backup_{int(time.time())}.db")
    try:
        await db.backup_to(tmp_path)
        await bot.send_document(
            call.from_user.id,
            FSInputFile(tmp_path, filename=os.path.basename(tmp_path)),
            caption="💾 Резервная копия базы. Храните её приватно — там данные покупателей.",
        )
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if not await _admin_only(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: <code>/ban 123456789</code> (Telegram ID)")
        return
    uid = int(arg)
    if await db.is_admin(uid):
        await message.answer("Администратора банить нельзя.")
        return
    await db.set_banned(uid, True)
    await message.answer(f"🚫 Пользователь {uid} заблокирован в боте.")


@router.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    if not await _admin_only(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: <code>/unban 123456789</code> (Telegram ID)")
        return
    await db.set_banned(int(arg), False)
    await message.answer(f"✅ Пользователь {arg} разблокирован.")
