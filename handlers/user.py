from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import database as db
import keyboards as kb
from pricing import fmt_num, fmt_rub, fmt_ts, lot_price, price_for
from states import BuyStates
from utils import (
    esc,
    lot_instructions,
    log_event,
    notify_admins_card,
    notify_admins_text,
    safe_edit,
)

router = Router()

STATUS_LABELS = {
    "waiting_proof": "⏳ ждём чек об оплате",
    "pending": "🔍 проверяем оплату",
    "paid": "💳 оплачен — пришлите скриншот лота",
    "lot_review": "🎯 выкупаем ваш лот",
    "completed": "✅ выполнен",
    "rejected": "❌ отклонён",
    "cancelled": "🚫 отменён",
}


# ---------- helpers ----------

async def _limits() -> tuple[int, int]:
    min_g = int(await db.get_float_setting("min_gold", 100))
    max_g = int(await db.get_float_setting("max_gold", 10000))
    return min_g, max_g


async def _tiers_as_pairs():
    return [(t["min_gold"], t["rate"]) for t in await db.get_tiers()]


async def _rate_text() -> str:
    tiers = await db.get_tiers()
    if not tiers:
        return (
            "📈 <b>Курс голды</b>\n\n"
            "Курс пока не задан. Выберите готовый пакет в каталоге или загляните позже."
        )
    lines = ["📈 <b>Курс голды</b> (цена за 100 G)\n"]
    for t in tiers:
        lines.append(f"• от {t['min_gold']} G — <b>{fmt_num(t['rate'])} ₽</b>")
    lines.append("\nЧем больше берёте за раз, тем выгоднее курс.")
    updated = await db.get_setting("rate_updated_at")
    lines.append(f"🕒 Курс обновлён: {fmt_ts(updated)}")
    return "\n".join(lines)


async def _open_order_alert(call: CallbackQuery) -> bool:
    order = await db.get_open_order(call.from_user.id)
    if order:
        await call.answer(
            f"У вас уже есть незавершённый заказ №{order['id']}. "
            "Завершите его — статус смотрите в «Мои заказы».",
            show_alert=True,
        )
        return True
    return False


# ---------- /start, меню ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)
    is_admin = await db.is_admin(message.from_user.id)
    shop_name = await db.get_setting("shop_name")
    text = (
        f"👋 Добро пожаловать в <b>{esc(shop_name)}</b>!\n\n"
        "Здесь можно быстро и без обмана купить голду для Standoff 2.\n\n"
        "Выберите действие в меню ниже 👇"
    )
    await message.answer(text, reply_markup=kb.main_menu(is_admin))


@router.callback_query(F.data == "menu:main")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    is_admin = await db.is_admin(call.from_user.id)
    await safe_edit(call, "Главное меню:", kb.main_menu(is_admin))
    await call.answer()


@router.callback_query(F.data == "menu:cancel")
async def cancel_action(call: CallbackQuery, state: FSMContext):
    """Отмена на шаге ввода (количество / игровой ID). Заказ ещё не создан."""
    await state.clear()
    is_admin = await db.is_admin(call.from_user.id)
    await safe_edit(call, "Действие отменено. Главное меню:", kb.main_menu(is_admin))
    await call.answer()


# ---------- курс ----------

@router.message(Command("rate"))
async def cmd_rate(message: Message):
    tiers = await db.get_tiers()
    await message.answer(await _rate_text(), reply_markup=kb.rate_kb(bool(tiers)))


@router.callback_query(F.data == "menu:rate")
async def show_rate(call: CallbackQuery):
    tiers = await db.get_tiers()
    await safe_edit(call, await _rate_text(), kb.rate_kb(bool(tiers)))
    await call.answer()


# ---------- каталог и выбор количества ----------

@router.callback_query(F.data == "menu:catalog")
async def show_catalog(call: CallbackQuery):
    products = await db.get_active_products()
    tiers = await db.get_tiers()
    if not products and not tiers:
        await call.answer("Каталог пока пуст, загляните позже 🙏", show_alert=True)
        return
    await safe_edit(call, "💰 <b>Каталог голды</b>\n\nВыберите пакет:",
                    kb.catalog_menu(products, show_custom=bool(tiers)))
    await call.answer()


@router.callback_query(F.data.startswith("product:"))
async def choose_product(call: CallbackQuery, state: FSMContext):
    if await _open_order_alert(call):
        return
    product_id = int(call.data.split(":")[1])
    product = await db.get_product(product_id)
    if not product or not product["is_active"]:
        await call.answer("Этот товар недоступен", show_alert=True)
        return
    await state.clear()
    await state.update_data(product_id=product_id)
    await state.set_state(BuyStates.waiting_game_id)
    await safe_edit(
        call,
        f"Вы выбрали: <b>{esc(product['name'])}</b>\n"
        f"Голды: <b>{product['amount']} G</b>\n"
        f"Цена: <b>{fmt_rub(product['price'])}</b>\n\n"
        "✏️ Введите ваш <b>игровой ID (никнейм) в Standoff 2</b> — по нему мы сверим заказ:",
        kb.cancel_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "menu:custom")
async def custom_start(call: CallbackQuery, state: FSMContext):
    if await _open_order_alert(call):
        return
    if not await db.get_tiers():
        await call.answer("Курс пока не задан — выберите пакет из каталога", show_alert=True)
        return
    min_g, max_g = await _limits()
    await state.clear()
    await state.set_state(BuyStates.waiting_custom_amount)
    await safe_edit(
        call,
        f"✏️ Сколько голды вам нужно? Отправьте число от <b>{min_g}</b> до <b>{max_g}</b>:",
        kb.cancel_kb(),
    )
    await call.answer()


@router.message(BuyStates.waiting_custom_amount)
async def custom_amount(message: Message, state: FSMContext):
    raw = (message.text or "").replace(" ", "")
    if not raw.isdigit():
        await message.answer("Введите количество голды числом, например 1500.")
        return
    gold = int(raw)
    min_g, max_g = await _limits()
    if gold < min_g or gold > max_g:
        await message.answer(f"Количество должно быть от {min_g} до {max_g} G.")
        return
    price = price_for(gold, await _tiers_as_pairs())
    if price is None:
        await state.clear()
        await message.answer("Курс сейчас недоступен, попробуйте позже.")
        return
    await state.update_data(custom_gold=gold, custom_price=price, product_id=None)
    await state.set_state(BuyStates.waiting_game_id)
    await message.answer(
        f"🧮 <b>{gold} G = {fmt_rub(price)}</b>\n\n"
        "✏️ Введите ваш <b>игровой ID (никнейм) в Standoff 2</b> — по нему мы сверим заказ:",
        reply_markup=kb.cancel_kb(),
    )


# ---------- создание заказа ----------

@router.message(BuyStates.waiting_game_id)
async def get_game_id(message: Message, state: FSMContext):
    game_id = (message.text or "").strip()
    if not game_id or len(game_id) > 64:
        await message.answer("Пожалуйста, введите корректный игровой ID (до 64 символов).")
        return

    open_order = await db.get_open_order(message.from_user.id)
    if open_order:
        await state.clear()
        await message.answer(f"У вас уже есть незавершённый заказ №{open_order['id']}.")
        return

    data = await state.get_data()
    if data.get("custom_gold"):
        name = f"{data['custom_gold']} G"
        amount = int(data["custom_gold"])
        price = data["custom_price"]
        product_id = None
    else:
        product = await db.get_product(data.get("product_id"))
        if not product:
            await state.clear()
            await message.answer("Товар больше недоступен, начните заново через /start")
            return
        name, amount, price, product_id = product["name"], product["amount"], product["price"], product["id"]

    commission = await db.get_float_setting("market_commission", 20)
    lot = lot_price(amount, commission)
    order_id = await db.create_order(
        user_id=message.from_user.id,
        product_id=product_id,
        product_name=name,
        gold_amount=amount,
        price=price,
        game_id=game_id,
        commission=commission,
        lot_price=lot,
    )
    await state.clear()

    card_number = await db.get_setting("card_number")
    card_holder = await db.get_setting("card_holder")
    card_bank = await db.get_setting("card_bank")
    timeout = int(await db.get_float_setting("order_timeout_min", 30))

    text = (
        f"🧾 Заказ №{order_id} создан!\n\n"
        f"Товар: <b>{esc(name)}</b> ({amount} G)\n"
        f"Игровой ID: <code>{esc(game_id)}</code>\n"
        f"К оплате: <b>{fmt_rub(price)}</b>\n\n"
        "💳 Переведите сумму на карту:\n"
        f"Номер: <code>{esc(card_number)}</code>\n"
        f"Банк: {esc(card_bank)}\n"
        f"Получатель: {esc(card_holder)}\n\n"
        "После оплаты отправьте сюда <b>скриншот или файл чека</b>.\n\n"
        "ℹ️ <b>Как вы получите голду.</b> Игра не позволяет переводить голду напрямую, "
        "поэтому используем Рынок: после проверки оплаты вы выставите любой недорогой скин "
        f"за <b>{lot} G</b> (комиссия Рынка {fmt_num(commission)}% уже учтена), пришлёте "
        f"скриншот лота, а мы его выкупим — и вам придёт ровно {amount} G."
    )
    if timeout > 0:
        text += f"\n\n⏱ Если чек не придёт в течение {timeout} мин., заказ отменится автоматически."
    await message.answer(text, reply_markup=kb.order_cancel_kb(order_id))


@router.callback_query(F.data.startswith("order:cancel:"))
async def cancel_order_cb(call: CallbackQuery, state: FSMContext):
    order_id = int(call.data.split(":")[2])
    order = await db.get_order(order_id)
    if not order or order["user_id"] != call.from_user.id:
        await call.answer("Заказ не найден", show_alert=True)
        return
    if await db.transition(order_id, ("waiting_proof",), "cancelled"):
        await state.clear()
        is_admin = await db.is_admin(call.from_user.id)
        await safe_edit(call, f"Заказ №{order_id} отменён. Главное меню:", kb.main_menu(is_admin))
        await call.answer()
    else:
        await call.answer(
            "Этот заказ уже нельзя отменить — он в работе. Если что-то не так, напишите в поддержку.",
            show_alert=True,
        )


# ---------- чек и скриншот лота (работает по данным из БД, переживает перезапуск бота) ----------

@router.message(StateFilter(None), F.photo | F.document)
async def on_media(message: Message, bot: Bot):
    order = await db.get_open_order(message.from_user.id)
    if not order:
        await message.answer("Сейчас я не жду файлов. Чтобы купить голду, откройте меню: /start")
        return

    if message.photo:
        file_id, media_type = message.photo[-1].file_id, "photo"
    else:
        file_id, media_type = message.document.file_id, "document"

    status = order["status"]
    order_id = order["id"]
    is_admin = await db.is_admin(message.from_user.id)

    if status == "waiting_proof":
        if not await db.attach_proof(order_id, file_id, media_type):
            return
        await message.answer(
            f"✅ Чек по заказу №{order_id} получен и отправлен на проверку.\n"
            "Обычно это занимает 15–30 минут. Вам придёт уведомление 🔔",
            reply_markup=kb.main_menu(is_admin),
        )
        await notify_admins_card(bot, await db.get_order(order_id))

    elif status == "paid":
        if not await db.attach_lot(order_id, file_id, media_type):
            return
        await message.answer(
            f"🎯 Скриншот лота по заказу №{order_id} получен. Выкупаем его — "
            "не снимайте лот, скоро напишем.",
            reply_markup=kb.main_menu(is_admin),
        )
        await notify_admins_card(bot, await db.get_order(order_id))

    else:  # pending / lot_review
        await message.answer(
            f"Заказ №{order_id} уже на проверке — дополнительных файлов не нужно. Скоро вернёмся 🙏"
        )


@router.message(StateFilter(None), F.text, ~F.text.startswith("/"))
async def fallback_text(message: Message):
    order = await db.get_open_order(message.from_user.id)
    if order and order["status"] == "waiting_proof":
        await message.answer("Пришлите, пожалуйста, скриншот или файл чека об оплате 📎")
    elif order and order["status"] == "paid":
        await message.answer("Пришлите скриншот выставленного лота (скин и цена должны быть видны) 📸")
    else:
        await message.answer("Не понял вас 🤔 Откройте меню: /start")


# ---------- подтверждение получения голды ----------

@router.callback_query(F.data.startswith("deliv:"))
async def delivery_feedback(call: CallbackQuery, bot: Bot):
    _, action, raw_id = call.data.split(":")
    order = await db.get_order(int(raw_id))
    if not order or order["user_id"] != call.from_user.id or order["status"] != "completed":
        await call.answer("Заказ не найден", show_alert=True)
        return
    if action == "ok":
        await safe_edit(call, f"🎉 Спасибо за покупку! Заказ №{order['id']} закрыт. Будем рады видеть снова.")
    else:
        support = await db.get_setting("support_username")
        await safe_edit(
            call,
            f"Приняли. Передали заказ №{order['id']} администраторам — с вами свяжутся. "
            f"Можно также написать в поддержку: {esc(support)}",
        )
        await notify_admins_text(
            bot,
            f"⚠️ Покупатель сообщает, что голда по заказу №{order['id']} не пришла "
            f"(id{order['user_id']}, игровой ID {esc(order['game_id'])}). Проверьте выдачу.",
        )
        await log_event(bot, f"⚠️ Спор по заказу №{order['id']}: голда не пришла (со слов покупателя).")
    await call.answer()


# ---------- мои заказы / поддержка / о магазине ----------

@router.callback_query(F.data == "menu:orders")
async def my_orders(call: CallbackQuery):
    orders = await db.get_user_orders(call.from_user.id)
    if not orders:
        await call.answer("У вас пока нет заказов", show_alert=True)
        return
    lines = ["🧾 <b>Ваши заказы:</b>\n"]
    for o in orders:
        lines.append(
            f"№{o['id']} — {esc(o['product_name'])} ({o['gold_amount']} G) — "
            f"{fmt_rub(o['price'])} — {STATUS_LABELS.get(o['status'], o['status'])}"
        )
    open_order = await db.get_open_order(call.from_user.id)
    if open_order and open_order["status"] == "paid":
        lines.append("\n" + lot_instructions(open_order))
    elif open_order and open_order["status"] == "waiting_proof":
        lines.append(f"\nДля заказа №{open_order['id']} пришлите скриншот или файл чека об оплате.")
    await safe_edit(call, "\n".join(lines), kb.my_orders_kb(open_order))
    await call.answer()


@router.callback_query(F.data == "menu:support")
async def support(call: CallbackQuery):
    support_username = await db.get_setting("support_username")
    await safe_edit(call, f"🆘 По любым вопросам пишите: {esc(support_username)}", kb.back_to_main_kb())
    await call.answer()


@router.callback_query(F.data == "menu:about")
async def about(call: CallbackQuery):
    shop_name = await db.get_setting("shop_name")
    await safe_edit(
        call,
        f"ℹ️ <b>{esc(shop_name)}</b> — магазин голды для Standoff 2.\n"
        "Ручная проверка платежей, выдача через Рынок со скриншотом лота, честные цены.",
        kb.back_to_main_kb(),
    )
    await call.answer()
