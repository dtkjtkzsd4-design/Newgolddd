import asyncio
import os
import sqlite3
import time
from contextlib import asynccontextmanager

import aiosqlite

from config import DB_PATH

# Жизненный цикл заказа:
#   waiting_proof -> pending -> paid -> lot_review -> completed
#                       \-> rejected        \-> (назад в paid, если нужен новый скриншот)
#   waiting_proof -> cancelled
OPEN_STATUSES = ("waiting_proof", "pending", "paid", "lot_review")
PAID_STATUSES = ("paid", "lot_review", "completed")


@asynccontextmanager
async def _connect():
    conn = await aiosqlite.connect(DB_PATH)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout = 5000")
        yield conn
    finally:
        await conn.close()


async def _ensure_columns(db):
    """Мягкая миграция для баз, созданных старой версией бота."""
    cur = await db.execute("PRAGMA table_info(orders)")
    existing = {r["name"] for r in await cur.fetchall()}
    for col, ddl in (
        ("lot_file_id", "TEXT"),
        ("lot_type", "TEXT"),
        ("lot_price", "INTEGER"),
        ("commission", "REAL"),
    ):
        if col not in existing:
            await db.execute(f"ALTER TABLE orders ADD COLUMN {col} {ddl}")
    # В старой версии финальный статус назывался approved.
    await db.execute("UPDATE orders SET status = 'completed' WHERE status = 'approved'")


async def init_db():
    folder = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(folder, exist_ok=True)

    async with _connect() as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            first_seen INTEGER,
            is_banned INTEGER DEFAULT 0
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_main INTEGER DEFAULT 0,
            added_by INTEGER,
            added_at INTEGER
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            amount INTEGER,
            price REAL,
            is_active INTEGER DEFAULT 1
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            product_id INTEGER,
            product_name TEXT,
            gold_amount INTEGER,
            price REAL,
            game_id TEXT,
            status TEXT DEFAULT 'waiting_proof',
            proof_file_id TEXT,
            proof_type TEXT,
            admin_id INTEGER,
            reject_reason TEXT,
            created_at INTEGER,
            updated_at INTEGER,
            lot_file_id TEXT,
            lot_type TEXT,
            lot_price INTEGER,
            commission REAL
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS rate_tiers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            min_gold INTEGER UNIQUE,
            rate REAL
        )""")
        await db.commit()

        await _ensure_columns(db)
        await db.commit()

        defaults = {
            "card_number": "0000 0000 0000 0000",
            "card_holder": "Имя Фамилия",
            "card_bank": "Банк",
            "support_username": "@ishopgold_support",
            "shop_name": "ishopgold",
            # Комиссия Рынка Standoff 2 (в процентах). Меняется из админки, если игра её изменит.
            "market_commission": "20",
            "min_gold": "100",
            "max_gold": "10000",
            # Через сколько минут отменять заказ, по которому не пришёл чек (0 — не отменять).
            "order_timeout_min": "30",
            "rate_updated_at": "0",
        }
        for k, v in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
        await db.commit()


async def ensure_main_admin(user_id: int):
    async with _connect() as db:
        cur = await db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        if row:
            await db.execute("UPDATE admins SET is_main = 1 WHERE user_id = ?", (user_id,))
        else:
            await db.execute(
                "INSERT INTO admins (user_id, is_main, added_by, added_at) VALUES (?, 1, ?, ?)",
                (user_id, user_id, int(time.time())),
            )
        await db.commit()


# ---------- USERS ----------

async def add_user(user_id, username, full_name):
    async with _connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name, first_seen) VALUES (?, ?, ?, ?)",
            (user_id, username, full_name, int(time.time())),
        )
        await db.execute(
            "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
            (username, full_name, user_id),
        )
        await db.commit()


async def get_user(user_id):
    async with _connect() as db:
        cur = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


async def get_all_user_ids():
    async with _connect() as db:
        cur = await db.execute("SELECT user_id FROM users WHERE is_banned = 0")
        rows = await cur.fetchall()
        return [r["user_id"] for r in rows]


async def is_banned(user_id) -> bool:
    async with _connect() as db:
        cur = await db.execute("SELECT is_banned FROM users WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return bool(row and row["is_banned"])


async def set_banned(user_id, banned: bool):
    async with _connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)",
            (user_id, int(time.time())),
        )
        await db.execute(
            "UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if banned else 0, user_id)
        )
        await db.commit()


# ---------- ADMINS ----------

async def is_admin(user_id) -> bool:
    async with _connect() as db:
        cur = await db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        return (await cur.fetchone()) is not None


async def is_main_admin(user_id) -> bool:
    async with _connect() as db:
        cur = await db.execute(
            "SELECT 1 FROM admins WHERE user_id = ? AND is_main = 1", (user_id,)
        )
        return (await cur.fetchone()) is not None


async def get_admin_ids():
    async with _connect() as db:
        cur = await db.execute("SELECT user_id FROM admins")
        rows = await cur.fetchall()
        return [r["user_id"] for r in rows]


async def get_all_admins():
    async with _connect() as db:
        cur = await db.execute("SELECT * FROM admins ORDER BY is_main DESC, added_at ASC")
        return await cur.fetchall()


async def add_admin(user_id, username, added_by):
    async with _connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO admins (user_id, username, is_main, added_by, added_at) "
            "VALUES (?, ?, 0, ?, ?)",
            (user_id, username, added_by, int(time.time())),
        )
        await db.commit()


async def remove_admin(user_id):
    async with _connect() as db:
        await db.execute("DELETE FROM admins WHERE user_id = ? AND is_main = 0", (user_id,))
        await db.commit()


# ---------- SETTINGS ----------

async def get_setting(key):
    async with _connect() as db:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None


async def get_float_setting(key, default):
    value = await get_setting(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


async def set_setting(key, value):
    async with _connect() as db:
        await db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()


async def get_all_settings():
    async with _connect() as db:
        cur = await db.execute("SELECT key, value FROM settings")
        rows = await cur.fetchall()
        return {r["key"]: r["value"] for r in rows}


# ---------- RATE (курс голды) ----------

async def get_tiers():
    async with _connect() as db:
        cur = await db.execute("SELECT * FROM rate_tiers ORDER BY min_gold ASC")
        return await cur.fetchall()


async def add_tier(min_gold: int, rate: float):
    async with _connect() as db:
        await db.execute(
            "INSERT INTO rate_tiers (min_gold, rate) VALUES (?, ?) "
            "ON CONFLICT(min_gold) DO UPDATE SET rate = excluded.rate",
            (min_gold, rate),
        )
        await db.commit()
    await touch_rate_updated()


async def delete_tier(tier_id: int):
    async with _connect() as db:
        await db.execute("DELETE FROM rate_tiers WHERE id = ?", (tier_id,))
        await db.commit()
    await touch_rate_updated()


async def touch_rate_updated():
    await set_setting("rate_updated_at", str(int(time.time())))


# ---------- PRODUCTS ----------

async def get_active_products():
    async with _connect() as db:
        cur = await db.execute("SELECT * FROM products WHERE is_active = 1 ORDER BY amount ASC")
        return await cur.fetchall()


async def get_all_products():
    async with _connect() as db:
        cur = await db.execute("SELECT * FROM products ORDER BY amount ASC")
        return await cur.fetchall()


async def get_product(product_id):
    async with _connect() as db:
        cur = await db.execute("SELECT * FROM products WHERE id = ?", (product_id,))
        return await cur.fetchone()


async def add_product(name, amount, price):
    async with _connect() as db:
        await db.execute(
            "INSERT INTO products (name, amount, price, is_active) VALUES (?, ?, ?, 1)",
            (name, amount, price),
        )
        await db.commit()


async def toggle_product(product_id):
    async with _connect() as db:
        await db.execute("UPDATE products SET is_active = 1 - is_active WHERE id = ?", (product_id,))
        await db.commit()


async def delete_product(product_id):
    async with _connect() as db:
        await db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        await db.commit()


# ---------- ORDERS ----------

async def create_order(user_id, product_id, product_name, gold_amount, price, game_id,
                       commission, lot_price):
    now = int(time.time())
    async with _connect() as db:
        cur = await db.execute(
            """INSERT INTO orders
               (user_id, product_id, product_name, gold_amount, price, game_id, status,
                created_at, updated_at, commission, lot_price)
               VALUES (?, ?, ?, ?, ?, ?, 'waiting_proof', ?, ?, ?, ?)""",
            (user_id, product_id, product_name, gold_amount, price, game_id, now, now,
             commission, lot_price),
        )
        await db.commit()
        return cur.lastrowid


async def get_order(order_id):
    async with _connect() as db:
        cur = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        return await cur.fetchone()


async def get_open_order(user_id):
    """Единственный незавершённый заказ пользователя (одновременно допускается только один)."""
    ph = ",".join("?" * len(OPEN_STATUSES))
    async with _connect() as db:
        cur = await db.execute(
            f"SELECT * FROM orders WHERE user_id = ? AND status IN ({ph}) ORDER BY id DESC LIMIT 1",
            (user_id, *OPEN_STATUSES),
        )
        return await cur.fetchone()


async def transition(order_id, from_statuses, to_status, admin_id=None, reject_reason=None) -> bool:
    """Атомарный переход статуса. Вернёт False, если заказ уже в другом статусе
    (например, его успел обработать другой админ) — двойной обработки не будет."""
    ph = ",".join("?" * len(from_statuses))
    async with _connect() as db:
        cur = await db.execute(
            f"UPDATE orders SET status = ?, admin_id = COALESCE(?, admin_id), "
            f"reject_reason = COALESCE(?, reject_reason), updated_at = ? "
            f"WHERE id = ? AND status IN ({ph})",
            (to_status, admin_id, reject_reason, int(time.time()), order_id, *from_statuses),
        )
        changed = cur.rowcount
        await db.commit()
        return changed == 1


async def attach_proof(order_id, file_id, proof_type) -> bool:
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE orders SET proof_file_id = ?, proof_type = ?, status = 'pending', updated_at = ? "
            "WHERE id = ? AND status = 'waiting_proof'",
            (file_id, proof_type, int(time.time()), order_id),
        )
        changed = cur.rowcount
        await db.commit()
        return changed == 1


async def attach_lot(order_id, file_id, lot_type) -> bool:
    async with _connect() as db:
        cur = await db.execute(
            "UPDATE orders SET lot_file_id = ?, lot_type = ?, status = 'lot_review', updated_at = ? "
            "WHERE id = ? AND status = 'paid'",
            (file_id, lot_type, int(time.time()), order_id),
        )
        changed = cur.rowcount
        await db.commit()
        return changed == 1


async def get_user_orders(user_id):
    async with _connect() as db:
        cur = await db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user_id,)
        )
        return await cur.fetchall()


async def get_orders_by_status(statuses):
    ph = ",".join("?" * len(statuses))
    async with _connect() as db:
        cur = await db.execute(
            f"SELECT * FROM orders WHERE status IN ({ph}) ORDER BY id ASC", tuple(statuses)
        )
        return await cur.fetchall()


async def get_stale_orders(cutoff_ts: int):
    async with _connect() as db:
        cur = await db.execute(
            "SELECT * FROM orders WHERE status = 'waiting_proof' AND created_at < ?", (cutoff_ts,)
        )
        return await cur.fetchall()


async def get_stats():
    ph = ",".join("?" * len(PAID_STATUSES))
    async with _connect() as db:
        async def one(sql, params=()):
            cur = await db.execute(sql, params)
            row = await cur.fetchone()
            return row[0]

        return {
            "users": await one("SELECT COUNT(*) FROM users"),
            "total_orders": await one("SELECT COUNT(*) FROM orders"),
            "paid": await one(f"SELECT COUNT(*) FROM orders WHERE status IN ({ph})", PAID_STATUSES),
            "completed": await one("SELECT COUNT(*) FROM orders WHERE status = 'completed'"),
            "rejected": await one("SELECT COUNT(*) FROM orders WHERE status = 'rejected'"),
            "need_action": await one(
                "SELECT COUNT(*) FROM orders WHERE status IN ('pending', 'lot_review')"
            ),
            "revenue": await one(
                f"SELECT COALESCE(SUM(price), 0) FROM orders WHERE status IN ({ph})", PAID_STATUSES
            ),
            "gold_delivered": await one(
                "SELECT COALESCE(SUM(gold_amount), 0) FROM orders WHERE status = 'completed'"
            ),
        }


# ---------- BACKUP ----------

def _backup_sync(dest_path):
    src = sqlite3.connect(DB_PATH)
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


async def backup_to(dest_path):
    """Консистентная копия базы (безопасна, даже пока бот работает)."""
    await asyncio.to_thread(_backup_sync, dest_path)
