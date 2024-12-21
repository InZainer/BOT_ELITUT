# handlers/main_menu.py

from aiogram import types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from utils.content_manager import load_content
from handlers.main_helpers import user_data

async def show_main_menu(message: types.Message):
    content_data = load_content()
    user_id = message.from_user.id

    buttons = [[KeyboardButton(text=item)] for item in content_data['main_menu'].keys() if item != "Назад"]
    submenu = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=buttons)

    await message.answer("Добро пожаловать! Выберите нужный пункт меню:", reply_markup=submenu)

async def handle_main_menu(message: types.Message, text: str):
    content_data = load_content()
    user_id = message.from_user.id
    if text in content_data['main_menu']:
        if text == "Как это работает?":
            user_data[user_id]['state'] = 'how_it_works'
            await show_how_it_works_menu(message)
        elif text == "Чем заняться?":
            user_data[user_id]['state'] = 'what_to_do'
            await show_what_to_do_menu(message)
        else:
            item_data = content_data['main_menu'].get(text)
            description = item_data.get('text', 'Нет описания.') if item_data else 'Нет описания.'

            image_path = item_data.get('image') if item_data else None
            if image_path:
                image = FSInputFile(image_path)
                await message.answer_photo(image, caption=description)
            else:
                await message.answer(description)
    else:
        await message.answer("Извините, я не понимаю этот запрос.")

async def show_how_it_works_menu(message: types.Message):
    content_data = load_content()
    buttons = [[KeyboardButton(text=item)] for item in content_data.get('how_it_works', {}).keys()]
    buttons.append([KeyboardButton(text="Назад")])
    submenu = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=buttons)
    await message.answer("Выберите кнопку в разделе 'Как это работает?':", reply_markup=submenu)

async def handle_how_it_works(message: types.Message, text: str):
    content_data = load_content()
    user_id = message.from_user.id
    if text == "Назад":
        user_data[user_id]['state'] = 'main_menu'
        await show_main_menu(message)
    else:
        if text in content_data.get('how_it_works', {}):
            data = content_data['how_it_works'][text]
            instructions = data.get('text', 'Нет инструкции.')
            image_path = data.get('image')
            link = data.get('link')

            if image_path:
                image = FSInputFile(image_path)
                await message.answer_photo(image, caption=instructions)
            else:
                await message.answer(instructions)

            if link:
                await message.answer(f"Видео-инструкция: {link}")
        else:
            await message.answer("Извините, я не понимаю этот запрос.")

async def show_what_to_do_menu(message: types.Message):
    content_data = load_content()
    buttons = [[KeyboardButton(text=item)] for item in content_data.get('what_to_do', {}).keys()]
    buttons.append([KeyboardButton(text="Назад")])
    submenu = ReplyKeyboardMarkup(resize_keyboard=True, keyboard=buttons)
    await message.answer("Выберите действие в разделе 'Чем заняться?':", reply_markup=submenu)

async def handle_what_to_do(message: types.Message, text: str):
    content_data = load_content()
    user_id = message.from_user.id
    if text == "Назад":
        user_data[user_id]['state'] = 'main_menu'
        await show_main_menu(message)
    else:
        if text in content_data.get('what_to_do', {}):
            data = content_data['what_to_do'][text]
            description = data.get('text', 'Нет описания.')
            image_path = data.get('image')

            if image_path:
                image = FSInputFile(image_path)
                await message.answer_photo(image, caption=description)
            else:
                await message.answer(description)
        else:
            await message.answer("Извините, я не понимаю этот запрос.")