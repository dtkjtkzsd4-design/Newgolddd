"""Чистые функции расчётов: без Telegram и без базы данных (легко тестировать)."""
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal

MSK = timezone(timedelta(hours=3))


def fmt_num(value) -> str:
    value = float(value)
    return str(int(value)) if value == int(value) else f"{value:g}"


def fmt_rub(value) -> str:
    value = float(value)
    if abs(value - round(value)) < 0.005:
        return f"{int(round(value))} ₽"
    return f"{value:.2f} ₽"


def fmt_ts(ts) -> str:
    if not ts or int(ts) <= 0:
        return "—"
    return datetime.fromtimestamp(int(ts), MSK).strftime("%d.%m %H:%M") + " МСК"


def lot_price(gold: int, commission_pct) -> int:
    """Цена лота на Рынке, при которой владелец лота получит ровно `gold` после комиссии.

    Пример: комиссия 20% -> для получения 100 G лот надо выставить за 125 G.
    """
    pct = Decimal(str(commission_pct))
    if pct < 0 or pct >= 100:
        raise ValueError("Комиссия должна быть в диапазоне 0..99")
    value = Decimal(int(gold)) * 100 / (100 - pct)
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def pick_tier(gold: int, tiers):
    """tiers: список пар (min_gold, rate_per_100). Берём самую высокую подходящую ступень;
    если объём меньше самой низкой ступени — применяем самую низкую."""
    if not tiers:
        return None
    suitable = [t for t in tiers if gold >= t[0]]
    if suitable:
        return max(suitable, key=lambda t: t[0])
    return min(tiers, key=lambda t: t[0])


def price_for(gold: int, tiers):
    """Цена в рублях (округление вверх до целого рубля) или None, если ступеней нет."""
    tier = pick_tier(gold, tiers)
    if tier is None:
        return None
    value = Decimal(int(gold)) * Decimal(str(tier[1])) / 100
    return int(value.to_integral_value(rounding=ROUND_CEILING))
