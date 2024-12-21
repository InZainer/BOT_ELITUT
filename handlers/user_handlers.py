# handlers/user_handlers.py

from aiogram import types, Router, F
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from aiogram.filters import Command
from database import add_user
from utils.validation import validate_code
from handlers.main_helpers import is_admin, user_data
from handlers.main_menu import (
    show_main_menu,
    handle_main_menu,
    handle_how_it_works,
    handle_what_to_do
)

user_router = Router()

@user_router.message(Command('start'))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    out_commands = """
    \nКоманды для администратора:\n
    `/mailing Текст + фотография\n`
    `/list_content\n`
    `/add_button Название раздела 'Название кнопки' 'Текст кнопки'\n`
    `/delete_button Название раздела 'Название кнопки'\n`
    `/update_text Название раздела 'Название пункта' 'Новый текст'\n`
    `/update_image Название раздела 'Название пункта'`
    """

    if is_admin(user_id):
        await message.answer("Здравствуйте, администратор! Вы можете использовать команды для управления контентом")
        await message.answer(out_commands, parse_mode="MarkdownV2")
    else:
        await message.answer("Здравствуйте! Пожалуйста, введите ваш уникальный код для доступа к боту.")
        await add_user(message.chat.id)
        user_data[user_id] = {'authenticated': False}

@user_router.message(F.text)
async def handle_code(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()

    if user_id in user_data and not user_data[user_id]['authenticated']:
        code = text
        if validate_code(code):
            user_data[user_id]['authenticated'] = True
            user_data[user_id]['state'] = 'main_menu'
            await show_main_menu(message)
        else:
            await message.answer("Неверный код. Пожалуйста, попробуйте еще раз.")
    elif user_id in user_data and user_data[user_id]['authenticated']:
        current_state = user_data[user_id].get('state', 'main_menu')
        if current_state == 'main_menu':
            await handle_main_menu(message, text)
        elif current_state == 'how_it_works':
            await handle_how_it_works(message, text)
        elif current_state == 'what_to_do':
            await handle_what_to_do(message, text)
    else:
        await message.answer("Пожалуйста, введите /start для начала.")