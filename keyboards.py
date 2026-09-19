from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from pricing import fmt_num, fmt_rub


# ---------- ПОКУПАТЕЛЬ ----------

def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💰 Купить голду", callback_data="menu:catalog")
    b.button(text="📈 Курс голды", callback_data="menu:rate")
    b.button(text="🧾 Мои заказы", callback_data="menu:orders")
    b.button(text="🆘 Поддержка", callback_data="menu:support")
    b.button(text="ℹ️ О магазине", callback_data="menu:about")
    if is_admin:
        b.button(text="🛠 Админ-панель", callback_data="menu:admin")
    b.adjust(1)
    return b.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚫 Отменить", callback_data="menu:cancel")
    return b.as_markup()


def order_cancel_kb(order_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🚫 Отменить заказ", callback_data=f"order:cancel:{order_id}")
    return b.as_markup()


def back_to_main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ В меню", callback_data="menu:main")
    return b.as_markup()


def catalog_menu(products, show_custom: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in products:
        b.button(text=f"{p['name']} — {fmt_rub(p['price'])}", callback_data=f"product:{p['id']}")
    if show_custom:
        b.button(text="🧮 Своё количество", callback_data="menu:custom")
    b.button(text="⬅️ В меню", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def rate_kb(has_tiers: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_tiers:
        b.button(text="🧮 Рассчитать свою сумму", callback_data="menu:custom")
    b.button(text="💰 Каталог", callback_data="menu:catalog")
    b.button(text="⬅️ В меню", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def my_orders_kb(open_order) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if open_order is not None and open_order["status"] == "waiting_proof":
        b.button(text="🚫 Отменить заказ", callback_data=f"order:cancel:{open_order['id']}")
    b.button(text="⬅️ В меню", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def delivery_feedback_kb(order_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Всё получил", callback_data=f"deliv:ok:{order_id}")
    b.button(text="⚠️ Голда не пришла", callback_data=f"deliv:bad:{order_id}")
    b.adjust(1)
    return b.as_markup()


# ---------- АДМИН: заявки ----------

def order_review_kb(order_id: int) -> InlineKeyboardMarkup:
    """Проверка оплаты."""
    b = InlineKeyboardBuilder()
    b.button(text="✅ Оплата пришла", callback_data=f"order:approve:{order_id}")
    b.button(text="❌ Отклонить", callback_data=f"order:reject:{order_id}")
    b.adjust(2)
    return b.as_markup()


def lot_review_kb(order_id: int) -> InlineKeyboardMarkup:
    """Выдача голды после скриншота лота."""
    b = InlineKeyboardBuilder()
    b.button(text="✅ Голда передана", callback_data=f"lot:done:{order_id}")
    b.button(text="🔄 Нужен новый скриншот", callback_data=f"lot:retry:{order_id}")
    b.adjust(1)
    return b.as_markup()


def orders_list_kb(orders) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for o in orders:
        if o["status"] == "lot_review":
            text = f"🎯 №{o['id']} — выдать {o['gold_amount']} G"
        else:
            text = f"💳 №{o['id']} — проверить {fmt_rub(o['price'])}"
        b.button(text=text, callback_data=f"order:view:{o['id']}")
    b.button(text="⬅️ Назад", callback_data="adm:back")
    b.adjust(1)
    return b.as_markup()


# ---------- АДМИН: меню ----------

def admin_menu(is_main: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🧾 Заявки", callback_data="adm:orders")
    b.button(text="📦 Товары", callback_data="adm:products")
    b.button(text="📈 Курс голды", callback_data="adm:rate")
    b.button(text="📊 Статистика", callback_data="adm:stats")
    b.button(text="📢 Рассылка", callback_data="adm:broadcast")
    if is_main:
        b.button(text="👥 Администраторы", callback_data="adm:admins")
        b.button(text="⚙ Настройки", callback_data="adm:settings")
        b.button(text="💾 Бэкап базы", callback_data="adm:backup")
    b.button(text="⬅️ В меню", callback_data="menu:main")
    b.adjust(1)
    return b.as_markup()


def products_admin_kb(products) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in products:
        status = "🟢" if p["is_active"] else "🔴"
        b.button(text=f"{status} {p['name']} — {fmt_rub(p['price'])}", callback_data=f"prod:toggle:{p['id']}")
        b.button(text="🗑", callback_data=f"prod:del:{p['id']}")
    b.button(text="➕ Добавить товар", callback_data="prod:add")
    b.button(text="⬅️ Назад", callback_data="adm:back")
    b.adjust(2)
    return b.as_markup()


def rate_admin_kb(tiers) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in tiers:
        b.button(
            text=f"🗑 от {t['min_gold']} G — {fmt_num(t['rate'])} ₽/100 G",
            callback_data=f"rate:del:{t['id']}",
        )
    b.button(text="➕ Добавить / изменить ступень", callback_data="rate:add")
    b.button(text="⬅️ Назад", callback_data="adm:back")
    b.adjust(1)
    return b.as_markup()


def admins_kb(admins) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for a in admins:
        label = f"👑 {a['username'] or a['user_id']}" if a["is_main"] else f"🛡 @{a['username'] or a['user_id']}"
        b.button(text=label, callback_data="noop")
        if not a["is_main"]:
            b.button(text="❌ Снять", callback_data=f"admn:remove:{a['user_id']}")
    b.button(text="➕ Добавить админа", callback_data="admn:add")
    b.button(text="⬅️ Назад", callback_data="adm:back")
    b.adjust(2)
    return b.as_markup()


def settings_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Номер карты", callback_data="set:card_number")
    b.button(text="🏦 Банк", callback_data="set:card_bank")
    b.button(text="👤 Получатель", callback_data="set:card_holder")
    b.button(text="📉 Комиссия Рынка, %", callback_data="set:market_commission")
    b.button(text="🔽 Мин. заказ, G", callback_data="set:min_gold")
    b.button(text="🔼 Макс. заказ, G", callback_data="set:max_gold")
    b.button(text="⏱ Автоотмена без чека, мин", callback_data="set:order_timeout_min")
    b.button(text="🆘 Юзернейм поддержки", callback_data="set:support_username")
    b.button(text="🏷 Название магазина", callback_data="set:shop_name")
    b.button(text="⬅️ Назад", callback_data="adm:back")
    b.adjust(1)
    return b.as_markup()


def back_to_admin_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="adm:back")
    return b.as_markup()
