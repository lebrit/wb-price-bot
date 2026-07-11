from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .config import Settings
from .database import Database
from .handlers import HandlerContext, create_router
from .monitor import PriceMonitor
from .security import SessionCipher
from .wildberries import AccountWildberriesClient, MpstatsPriceClient, PublicWildberriesClient

logger = logging.getLogger(__name__)


async def run_bot(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    database = Database(settings.database_path)
    await database.initialize()
    cipher = SessionCipher(settings.session_encryption_key)
    public_client = PublicWildberriesClient(settings)
    account_client = AccountWildberriesClient(settings)
    licensed_client = MpstatsPriceClient(settings)
    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    monitor = PriceMonitor(
        settings=settings,
        database=database,
        bot=bot,
        public_client=public_client,
        account_client=account_client,
        licensed_client=licensed_client,
        cipher=cipher,
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(
        create_router(
            HandlerContext(
                settings=settings,
                database=database,
                public_client=public_client,
                account_client=account_client,
                licensed_client=licensed_client,
                cipher=cipher,
                monitor=monitor,
            )
        )
    )
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Открыть главное меню"),
            BotCommand(command="add", description="Добавить товар"),
            BotCommand(command="list", description="Мои товары"),
            BotCommand(command="account", description="Аккаунт Wildberries"),
            BotCommand(command="settings", description="Регион и уведомления"),
            BotCommand(command="import", description="Массовый импорт"),
            BotCommand(command="export", description="Экспорт CSV/JSON"),
            BotCommand(command="folders", description="Папки и теги"),
            BotCommand(command="users", description="Заявки пользователей (админ)"),
            BotCommand(command="status", description="Состояние сервиса"),
            BotCommand(command="help", description="Помощь"),
            BotCommand(command="cancel", description="Отменить ввод"),
        ]
    )
    me = await bot.get_me()
    logger.info("Запущен Telegram-бот @%s", me.username)
    monitor_task = asyncio.create_task(monitor.run(), name="price-monitor")
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        monitor.stop()
        try:
            await asyncio.wait_for(monitor_task, timeout=30)
        except TimeoutError:
            monitor_task.cancel()
        await public_client.close()
        await licensed_client.close()
        await bot.session.close()
        await database.close()
