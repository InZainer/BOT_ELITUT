# handlers/admin_handlers.py

import re
import logging
import os
import json
from aiogram import types, Router, Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from aiogram.filters import Command
from database import get_all_user_ids
from utils.content_manager import load_content, save_content
from handlers.main_helpers import is_admin

logger = logging.getLogger(__name__)

admin_router = Router()

# Глобальное хранилище данных для рассылки
mailing_data = {}

@admin_router.message(Command('mailing'))
async def cmd_mailing(message: types.Message, bot: Bot):
    if is_admin(message.from_user.id):
        # Удаляем команду /mailing из текста, если она есть
        mailing_text = message.text.replace("/mailing", '').strip() if message.text else message.caption
        # Проверяем, что есть хотя бы текст или фото
        if not mailing_text and not message.photo:
            await message.answer("Используйте команду с текстом или картинкой.")
            return

        # Сохраняем данные для рассылки
        mailing_data[message.from_user.id] = {
            'text': mailing_text,
            'photo': message.photo[-1].file_id if message.photo else None,
            'confirm_message_id': None  # Будем хранить ID сообщения с подтверждением
        }

        # Создаем кнопки для подтверждения
        confirm_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да", callback_data="confirm_mailing"),
                    InlineKeyboardButton(text="Нет", callback_data="cancel_mailing")
                ]
            ]
        )

        # Отображаем сообщение для подтверждения
        if mailing_data[message.from_user.id]['photo']:
            sent_message = await message.answer_photo(
                photo=mailing_data[message.from_user.id]['photo'],
                caption=f"Вы уверены, что хотите отправить следующее сообщение всем пользователям?\n\n{mailing_text}",
                reply_markup=confirm_kb
            )
        else:
            sent_message = await message.answer(
                f"Вы уверены, что хотите отправить следующее сообщение всем пользователям?\n\n{mailing_text}",
                reply_markup=confirm_kb
            )

        # Сохраняем ID сообщения для подтверждения
        mailing_data[message.from_user.id]['confirm_message_id'] = sent_message.message_id
    else:
        await message.answer("У вас нет прав для выполнения этой команды.")

@admin_router.callback_query(lambda c: c.data in ['confirm_mailing', 'cancel_mailing'])
async def process_callback_button(callback_query: CallbackQuery, bot: Bot):
    await bot.answer_callback_query(callback_query.id)
    admin_id = callback_query.from_user.id

    if admin_id not in mailing_data:
        await bot.send_message(admin_id, "Нет данных для рассылки.")
        return

    confirm_message_id = mailing_data[admin_id].get('confirm_message_id')

    if callback_query.data == 'confirm_mailing':
        # Получаем данные для рассылки
        data = mailing_data.get(admin_id)
        if not data:
            await bot.send_message(admin_id, "Нет данных для рассылки.")
            return

        mailing_text = data['text']
        photo_file_id = data['photo']

        # Получаем список chat_id из базы данных
        user_ids = await get_all_user_ids()

        # Отправляем сообщение каждому пользователю
        for user_id in user_ids:
            try:
                if photo_file_id:
                    # Отправляем фото с подписью, если оно есть
                    await bot.send_photo(user_id, photo_file_id, caption=mailing_text)
                elif mailing_text:
                    # Отправляем текст
                    await bot.send_message(user_id, mailing_text)
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

        await bot.send_message(admin_id, "Рассылка завершена!")
    else:
        await bot.send_message(admin_id, "Рассылка отменена.")

    # Удаляем сообщение с подтверждением
    if confirm_message_id:
        try:
            await bot.delete_message(chat_id=admin_id, message_id=confirm_message_id)
        except Exception as e:
            logger.error(f"Не удалось удалить сообщение с подтверждением: {e}")

    # Очищаем данные для рассылки
    mailing_data.pop(admin_id, None)

@admin_router.message(Command('list_content'))
async def cmd_list_content(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет прав для выполнения этой команды.")
        return

    # Проверяем, существует ли файл content.json
    try:
        content_data = load_content()
    except FileNotFoundError:
        await message.answer("Файл content.json не найден.")
        return
    except json.JSONDecodeError:
        await message.answer("Ошибка чтения файла content.json.")
        return

    # Формируем ответ с содержимым
    response = "Список содержимого:\n\n"
    
    for section, buttons in content_data.items():
        response += f"*{section}*\n"
        for button_name, button_content in buttons.items():
            text = button_content.get("text", "Нет текста")
            image = button_content.get("image", "Нет изображения")
            response += f"  - *{button_name}*\n"
            response += f"    Текст: `{text}`\n"
            response += f"    Изображение: `{image}`\n\n"

    # Если контента нет
    if response == "Список содержимого:\n\n":
        response = "Контент отсутствует."

    # Отправляем ответ
    await message.answer(response, parse_mode="MarkdownV2")

@admin_router.message(Command('add_button'))
async def cmd_add_button(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет прав для выполнения этой команды.")
        return

    # Формат команды: /add_button Название_раздела 'Название_кнопки' 'Текст_кнопки'
    pattern = r"^/add_button\s+(\S+)\s+'([^']+)'\s+'(.+)'$"
    match = re.match(pattern, message.text)

    if not match:
        await message.answer("Используйте формат: /add_button Название_раздела 'Название_кнопки' 'Текст_кнопки'")
        return

    section, button_name, button_text = match.groups()

    # Загрузка текущего контента
    content_data = load_content()

    # Добавление новой кнопки в content_data
    if section not in content_data:
        content_data[section] = {}

    if button_name in content_data[section]:
        await message.answer(f"Кнопка '{button_name}' уже существует в разделе '{section}'.")
        return

    content_data[section][button_name] = {
        "text": button_text,
        "image": ""  # Оставляем image пустым
    }

    # Сохранение изменений
    save_content(content_data)

    await message.answer(f"Кнопка '{button_name}' успешно добавлена в раздел '{section}'.")

@admin_router.message(Command('delete_button'))
async def cmd_remove_button(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет прав для выполнения этой команды.")
        return

    # Формат команды: /delete_button Название_раздела 'Название_кнопки'
    pattern = r"^/delete_button\s+(\S+)\s+'([^']+)'$"
    match = re.match(pattern, message.text)

    if not match:
        await message.answer("Используйте формат: /delete_button Название_раздела 'Название_кнопки'")
        return

    section, button_name = match.groups()

    # Загрузка текущего контента
    content_data = load_content()

    # Проверка наличия раздела и кнопки
    if section in content_data and button_name in content_data[section]:
        del content_data[section][button_name]

        # Если раздел пустой после удаления кнопки, удаляем и его
        if not content_data[section]:
            del content_data[section]

        save_content(content_data)
        await message.answer(f"Кнопка '{button_name}' удалена из раздела '{section}'.")
    else:
        await message.answer(f"Кнопка '{button_name}' не найдена в разделе '{section}'.")

@admin_router.message(Command('update_text'))
async def cmd_update_text(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет прав для выполнения этой команды.")
        return

    # Формат команды: /update_text Название_раздела 'Название_пункта' 'Новый_текст'
    pattern = r"^/update_text\s+(\S+)\s+'([^']+)'\s+'(.+)'$"
    match = re.match(pattern, message.text)

    if not match:
        await message.answer("Используйте формат: /update_text Название_раздела 'Название_пункта' 'Новый_текст'")
        return

    section, item_name, new_text = match.groups()

    # Загрузка текущего контента
    content_data = load_content()

    if section in content_data and item_name in content_data[section]:
        content_data[section][item_name]['text'] = new_text
        save_content(content_data)
        await message.answer(f"Текст для '{item_name}' в разделе '{section}' обновлен.")
    else:
        await message.answer(f"Неверный раздел или пункт меню. Раздел: '{section}', Пункт: '{item_name}' не найден.")

@admin_router.message(Command('update_image'))
async def cmd_update_image(message: types.Message, bot: Bot):
    if not is_admin(message.from_user.id):
        await message.answer("У вас нет прав для выполнения этой команды.")
        return

    if not message.photo:
        await message.answer("Пожалуйста, прикрепите изображение к сообщению.")
        return

    # Формат команды: /update_image Название_раздела 'Название_пункта'
    # Предполагается, что раздел и пункт передаются в caption
    parts = message.caption.split(' ', 2) if message.caption else []
    if len(parts) < 3:
        await message.answer("Используйте формат: /update_image Название_раздела 'Название_пункта'")
        return

    section = parts[1]
    item_name = parts[2].strip("'\"")

    # Загрузка текущего контента
    content_data = load_content()

    if section in content_data and item_name in content_data[section]:
        photo = message.photo[-1]
        try:
            # Получаем информацию о файле из TG
            file_info = await bot.get_file(photo.file_id)

            # Наличие папки images
            os.makedirs('images', exist_ok=True)

            # Скачиваем файл
            file_path = f"images/{photo.file_unique_id}.jpg"
            await bot.download_file(file_info.file_path, destination=file_path)

            # Обновляем путь к фотографии
            content_data[section][item_name]['image'] = file_path
            save_content(content_data)

            await message.answer(f"Изображение для '{item_name}' в разделе '{section}' успешно обновлено.")
        except Exception as e:
            await message.answer(f"Ошибка при загрузке изображения: {e}")
    else:
        await message.answer("Неверный раздел или пункт меню.")