import asyncio
import logging
import time

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, CallbackQuery

import database as db
from config import BOT_TOKEN, DB_PATH, MAIN_ADMIN_ID, ON_RAILWAY, VOLUME_PATH
from handlers import admin, user


class BanMiddleware(BaseMiddleware):
    """Заблокированные пользователи (/ban) бот просто не слышит. Админов бан не касается."""

    async def __call__(self, handler, event, data):
        tg_user = getattr(event, "from_user", None)
        if tg_user and await db.is_banned(tg_user.id) and not await db.is_admin(tg_user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ ограничен", show_alert=True)
            return None
        return await handler(event, data)


async def stale_orders_worker(bot: Bot):
    """Раз в минуту отменяет заказы, по которым слишком долго нет чека."""
    while True:
        try:
            minutes = int(await db.get_float_setting("order_timeout_min", 30))
            if minutes > 0:
                cutoff = int(time.time()) - minutes * 60
                for order in await db.get_stale_orders(cutoff):
                    if await db.transition(order["id"], ("waiting_proof",), "cancelled"):
                        try:
                            await bot.send_message(
                                order["user_id"],
                                f"🚫 Заказ №{order['id']} отменён: чек об оплате не пришёл "
                                f"в течение {minutes} мин. Если вы уже оплатили — напишите в поддержку.",
                            )
                        except Exception:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка в фоновой задаче автоотмены")
        await asyncio.sleep(60)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN (переменная окружения или файл .env)")
    if not MAIN_ADMIN_ID:
        raise SystemExit("Не задан MAIN_ADMIN_ID (переменная окружения или файл .env)")

    await db.init_db()
    await db.ensure_main_admin(MAIN_ADMIN_ID)
    logging.info("База данных: %s", DB_PATH)

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    ban_mw = BanMiddleware()
    dp.message.outer_middleware(ban_mw)
    dp.callback_query.outer_middleware(ban_mw)

    dp.include_router(admin.router)
    dp.include_router(user.router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="rate", description="Курс голды"),
    ])

    if ON_RAILWAY and not VOLUME_PATH and not DB_PATH.startswith("/data"):
        logging.warning("Railway Volume не подключён: база сотрётся при следующем деплое!")
        try:
            await bot.send_message(
                MAIN_ADMIN_ID,
                "⚠️ К сервису на Railway не подключён Volume — база данных (заказы, курс, настройки) "
                "пропадёт при следующем деплое. Подключите Volume и перезапустите бота.",
            )
        except Exception:
            pass

    await bot.delete_webhook(drop_pending_updates=False)  # не теряем чеки, пришедшие во время перезапуска
    worker = asyncio.create_task(stale_orders_worker(bot))
    logging.info("Бот запущен и слушает обновления...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        worker.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
