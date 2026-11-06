"""Main entry point for oprosbot with Telegram Forum Topics support."""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, Update

from config import get_settings
from database.db_manager import close_db, get_session_factory, init_db
from handlers import (
    admin_poll_router,
    create_poll_router,
    group_events_router,
    inline_router,
    start_router,
    voting_router,
)
from services.scheduler import get_scheduler, resume_scheduled_polls
from utils.health_server import start_health_server
from utils.logging import setup_logging

logger = logging.getLogger("oprosbot")


async def set_bot_commands(bot: Bot) -> None:
    """Configures bot menu commands in Telegram UI."""
    commands = [
        BotCommand(command="create_poll", description="➕ Создать новый опрос"),
        BotCommand(command="my_polls", description="📋 Мои опросы и управление"),
        BotCommand(command="help", description="ℹ️ Инструкция и справка"),
    ]
    await bot.set_my_commands(commands)


async def main() -> None:
    """Main startup routine."""
    try:
        settings = get_settings()
    except Exception as e:
        print(f"CRITICAL: Failed to load configuration: {e}", file=sys.stderr)
        print("Please ensure .env file exists and BOT_TOKEN is set.", file=sys.stderr)
        sys.exit(1)

    setup_logging(settings.LOG_LEVEL)
    logger.info("Starting oprosbot application...")

    logger.info("Connecting to database: %s", settings.DATABASE_URL)
    await init_db(settings.DATABASE_URL)
    session_factory = get_session_factory(settings.DATABASE_URL)

    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    # Outer middleware for transparent update logging
    @dp.update.outer_middleware()
    async def log_update_middleware(handler, event: Update, data: dict):
        if event.message:
            msg = event.message
            u = msg.from_user
            u_info = f"@{u.username}" if u and u.username else f"id:{u.id if u else 'unknown'}"
            chat_info = f"{msg.chat.type}:{msg.chat.id}"
            if msg.message_thread_id:
                chat_info += f" (thread {msg.message_thread_id})"
            logger.info("📩 Message from %s in %s: %r", u_info, chat_info, msg.text or msg.caption or "<media>")
        elif event.callback_query:
            cb = event.callback_query
            u = cb.from_user
            u_info = f"@{u.username}" if u and u.username else f"id:{u.id if u else 'unknown'}"
            logger.info("🔘 Callback from %s: data=%r", u_info, cb.data)
        elif event.my_chat_member:
            m = event.my_chat_member
            logger.info(
                "👥 Chat member status changed in '%s' (%s): %s -> %s",
                m.chat.title,
                m.chat.id,
                m.old_chat_member.status,
                m.new_chat_member.status,
            )
        return await handler(event, data)

    # Dependency injection
    dp["session_factory"] = session_factory

    # Register routers
    dp.include_router(start_router)
    dp.include_router(create_poll_router)
    dp.include_router(voting_router)
    dp.include_router(admin_poll_router)
    dp.include_router(group_events_router)
    dp.include_router(inline_router)

    # APScheduler
    scheduler = get_scheduler()
    scheduler.start()
    logger.info("APScheduler started successfully.")

    # Start HTTP health check server if PORT is configured (Render / cloud hosting)
    health_runner = await start_health_server(settings.PORT)

    bot_info = await bot.get_me()
    logger.info("Logged in as @%s (%s)", bot_info.username, bot_info.full_name)
    await set_bot_commands(bot)

    # Resume scheduled poll auto-closings from database
    await resume_scheduled_polls(bot, session_factory)

    try:
        logger.info("Bot is running and polling for updates...")
        await bot.delete_webhook(drop_pending_updates=True)
        # allowed_updates automatically resolves all used types including my_chat_member
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logger.info("Shutting down oprosbot...")
        if health_runner:
            await health_runner.cleanup()
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await close_db()
        await bot.session.close()
        logger.info("Bot shutdown completed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")
