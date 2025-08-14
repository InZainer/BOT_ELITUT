# b1_bot.py

import asyncio
import logging
from aiogram import Bot, Dispatcher
from config_test import TG_TOKEN
from database import init_db
from handlers.admin_handlers import admin_router
from handlers.user_handlers import user_router

# Инициализация логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота и диспетчера
bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

# Регистрируем роутеры, в которых содержатся наши хендлеры
dp.include_router(admin_router)
dp.include_router(user_router)

async def main():
    # Инициализация базы данных
    await init_db()

    # Запуск поллинга
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())