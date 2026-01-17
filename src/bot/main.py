from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
import re
import sys
from pathlib import Path
from typing import Optional, Dict

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, ReplyKeyboardMarkup,
                           KeyboardButton, FSInputFile)
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

from .db import Database
from .loader import ContentLoader, Activity, Guide
from .utils import month_in_season

# FSM States
class AuthStates(StatesGroup):
    waiting_for_code = State()
    waiting_for_photo = State()  # Admin waiting for photo upload

class ConciergeStates(StatesGroup):
    waiting_for_message = State()  # User is in concierge mode, waiting for message
    waiting_for_media = State()    # User can send additional media to their message

# Basic logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("house-bots")

# Globals via env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")  # Multiple admin IDs separated by comma
HOUSE_ID = os.getenv("HOUSE_ID", "house1")
AUTH_MODE = os.getenv("AUTH_MODE", "code")  # code | phone
ACCESS_DAYS = int(os.getenv("ACCESS_DAYS", "30"))
DB_PATH = os.getenv("DB_PATH", "./house-bots.db")

# Security and performance settings
CONCIERGE_MIN_INTERVAL_SECONDS = int(os.getenv("CONCIERGE_MIN_INTERVAL_SECONDS", "2"))
CONCIERGE_WINDOW_SECONDS = int(os.getenv("CONCIERGE_WINDOW_SECONDS", "60"))
CONCIERGE_MAX_MESSAGES_PER_WINDOW = int(os.getenv("CONCIERGE_MAX_MESSAGES_PER_WINDOW", "20"))
MAX_MEDIA_SIZE_MB = int(os.getenv("MAX_MEDIA_SIZE_MB", "16"))

# Parse admin IDs
ADMIN_IDS = []
if ADMIN_IDS_STR:
    try:
        ADMIN_IDS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(",") if admin_id.strip()]
        logger.info(f"Loaded {len(ADMIN_IDS)} admin IDs: {ADMIN_IDS}")
    except ValueError as e:
        logger.error(f"Invalid ADMIN_IDS format: {ADMIN_IDS_STR}. Error: {e}")
        ADMIN_IDS = []

# Keep backward compatibility with old ADMIN_CHAT_ID
ADMIN_CHAT_ID = ADMIN_IDS[0] if ADMIN_IDS else 0

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN is not set. Fill .env before running in production.")

loader = ContentLoader(base_path=Path(__file__).resolve().parent.parent.parent / "content")

# Admin state (in-memory)
ADMIN_REPLY_TARGET: Dict[int, int] = {}  # admin_id -> target_user_id
ADMIN_EDIT_PENDING: Dict[int, str] = {}  # admin_id -> rel_path to write
ADMIN_PHOTO_PENDING: Dict[int, str] = {}  # admin_id -> content_path waiting for photo
ADMIN_VIDEO_PENDING: Dict[int, str] = {}  # admin_id -> content_path waiting for video

# Locked content IDs that require permission
LOCKED_CONTENT_IDS = ["sauna"]  # Add more content IDs here as needed

# In-memory stores for security/rate limits and simple caches
CONCIERGE_RL: Dict[int, Dict[str, int]] = {}
HOUSE_CACHE: Dict[str, dict] = {}
MARKDOWN_CACHE: Dict[tuple, str] = {}


def sanitize_markdown(text: str) -> str:
    """Remove Telegram Markdown special characters to prevent injection when parse mode is enabled."""
    if not text:
        return ""
    return re.sub(r"[*_`\[\]()>~#\+\-=|{}\.!]", "", text)


def get_house_cached(house_id: str):
    cached = HOUSE_CACHE.get(house_id)
    if cached is not None:
        return cached
    house = loader.load_house(house_id)
    HOUSE_CACHE[house_id] = house
    return house


def read_markdown_cached(house_id: str, rel_path: str) -> str:
    key = (house_id, rel_path)
    cached = MARKDOWN_CACHE.get(key)
    if cached is not None:
        return cached
    content = loader.read_markdown(house_id, rel_path)
    MARKDOWN_CACHE[key] = content
    return content


def allow_concierge_message(user_id: int) -> bool:
    now_ts = int(datetime.now(timezone.utc).timestamp())
    rec = CONCIERGE_RL.get(user_id)
    if rec:
        # Enforce min interval
        if now_ts - rec.get("last_ts", 0) < CONCIERGE_MIN_INTERVAL_SECONDS:
            return False
        # Enforce windowed count
        if now_ts - rec.get("first_ts", now_ts) > CONCIERGE_WINDOW_SECONDS:
            # reset window
            CONCIERGE_RL[user_id] = {"first_ts": now_ts, "count": 1, "last_ts": now_ts}
        else:
            if rec.get("count", 0) >= CONCIERGE_MAX_MESSAGES_PER_WINDOW:
                return False
            rec["count"] = rec.get("count", 0) + 1
            rec["last_ts"] = now_ts
    else:
        CONCIERGE_RL[user_id] = {"first_ts": now_ts, "count": 1, "last_ts": now_ts}
    return True

async def ensure_db(db: Database):
    await db.init()

# Keyboards

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_menu_kb():
    btns = [
        [InlineKeyboardButton(text="📍 Как к нам попасть", callback_data="how_to_reach")],
        [InlineKeyboardButton(text="📖 Правила пользования домом", callback_data="rules_house")],
        [InlineKeyboardButton(text="⚙️ Как что работает?", callback_data="howto")],
        [InlineKeyboardButton(text="🎯 Чем заняться?", callback_data="activities")],
        [InlineKeyboardButton(text="🗺️ Карта локаций", callback_data="map")],
        [InlineKeyboardButton(text="💬 Обратная связь", callback_data="feedback")],
        [InlineKeyboardButton(text="👨‍💼 Консьерж (9–21)", callback_data="concierge")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=btns)


def houses_menu_kb():
    """Menu for selecting a house"""
    btns = [
        [InlineKeyboardButton(text="🏡 Дом 1 'Рустик' (28В)", callback_data="house:rustic")],
        [InlineKeyboardButton(text="🏡 Дом 2 'Лофт' (28Б)", callback_data="house:loft")],
        [InlineKeyboardButton(text="🏡 Дом 3 'Икигай' (28А)", callback_data="house:ikigai")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=btns)


def guides_menu_kb(guides: list[Guide]):
    rows = [[InlineKeyboardButton(text=g.title, callback_data=f"guide:{g.id}")] for g in guides]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]])


def activities_menu_kb(activities: list[Activity]):
    rows = []
    for a in activities:
        rows.append([InlineKeyboardButton(text=a.title, callback_data=f"activity:{a.id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_handler(message: Message, state: FSMContext, db: Database):
    user = message.from_user
    assert user
    # Clear any existing state first
    await state.clear()
    
    # Auth flow
    if AUTH_MODE == "phone":
        # placeholder: allow after sharing contact
        kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, keyboard=[[KeyboardButton(text="Поделиться телефоном", request_contact=True)]] )
        await message.answer("Для доступа поделитесь номером телефона.", reply_markup=kb)
        return
    else:
        # code auth
        profile = await db.get_user(user.id)
        now = datetime.now(timezone.utc)
        if profile and profile.get("access_until") and datetime.fromisoformat(profile["access_until"]) > now:
            await message.answer("Добро пожаловать обратно!", reply_markup=None)
            await show_main_menu(message)
            return
        # Set waiting for code state
        await state.set_state(AuthStates.waiting_for_code)
        current_state_after = await state.get_state()
        logger.info(f"start_handler: set waiting_for_code state for user {user.id}, state after set: {current_state_after}")
        await message.answer("Добро пожаловать! Введите, пожалуйста, ваш числовой код доступа:")


async def process_code(message: Message, state: FSMContext, db: Database):
    try:
        code = message.text.strip() if message.text else ""
        user_id = message.from_user.id if message.from_user else 0
        logger.info(f"process_code: user_id={user_id}, code='{code}'")
        
        if not code.isdigit():
            logger.warning(f"process_code: invalid code format from user {user_id}")
            await message.answer("Код должен быть числом. Попробуйте ещё раз.")
            # Keep the state - still waiting for code
            return
        
        logger.info(f"process_code: attempting to consume code {code} for user {user_id}")
        ok, house_id = await db.consume_code(int(code), user_id, ACCESS_DAYS)
        logger.info(f"process_code: consume_code returned ok={ok}, house_id={house_id}")
        
        if not ok:
            logger.warning(f"process_code: code {code} failed for user {user_id}")
            await message.answer("Код неверный или уже использован. Проверьте и введите снова.")
            # Keep the state - still waiting for code
            return
        
        # Success - clear state and show menu
        logger.info(f"process_code: code {code} successful for user {user_id}, granting access")
        await state.clear()
        await message.answer("Доступ предоставлен!", reply_markup=None)
        await show_main_menu(message)
    except Exception as e:
        logger.exception(f"process_code: unexpected error for user {user_id}: {e}")
        await message.answer("Произошла ошибка при обработке кода. Попробуйте ещё раз или обратитесь к администратору.")


async def show_main_menu(message: Message):
    house_id = HOUSE_ID  # одна папка контента на бот
    house = get_house_cached(house_id)
    title = house.name if house else "Дом"
    await message.answer(f"{title}. Главное меню:", reply_markup=main_menu_kb())


async def send_content_with_media(cb: CallbackQuery, db: Database, content_path: str, text_content: str, reply_markup, parse_mode=ParseMode.MARKDOWN):
    """Helper function to send content with photo/video if available, fallback to text only"""
    logger.info(f"send_content_with_media: checking for media at path '{content_path}'")
    media_info = await db.get_media(content_path)
    
    if media_info:
        media_file, media_type = media_info
        media_dir = loader._house_dir(HOUSE_ID) / "photos"
        media_path = media_dir / media_file
        logger.info(f"send_content_with_media: checking if {media_type} exists at '{media_path}'")
        
        if media_path.exists():
            logger.info(f"send_content_with_media: {media_type} found, sending with caption")
            try:
                input_file = FSInputFile(media_path)
                if media_type == 'video':
                    await cb.message.answer_video(input_file, caption=text_content, parse_mode=parse_mode, reply_markup=reply_markup)
                else:
                    await cb.message.answer_photo(input_file, caption=text_content, parse_mode=parse_mode, reply_markup=reply_markup)
                await cb.message.delete()
                logger.info(f"send_content_with_media: {media_type} sent successfully")
                return
            except Exception as e:
                logger.error(f"send_content_with_media: error sending {media_type}: {e}")
                logger.exception("Full traceback:")
                logger.info(f"send_content_with_media: falling back to text due to error")
        else:
            logger.warning(f"send_content_with_media: {media_type} file not found at '{media_path}'")
    else:
        logger.info(f"send_content_with_media: no media found in database for '{content_path}'")
    
    # Fallback to text only
    logger.info(f"send_content_with_media: falling back to text-only message")
    try:
        await cb.message.answer(text_content, parse_mode=parse_mode, reply_markup=reply_markup)
        await cb.message.delete()
    except Exception as e:
        logger.error(f"send_content_with_media: error sending text message fallback: {e}")
        await cb.answer("Ошибка при отправке контента", show_alert=True)

# Backward compatibility
async def send_content_with_photo(cb: CallbackQuery, db: Database, content_path: str, text_content: str, reply_markup, parse_mode=ParseMode.MARKDOWN):
    """Backward compatibility wrapper"""
    return await send_content_with_media(cb, db, content_path, text_content, reply_markup, parse_mode)


# Concierge functions
async def handle_concierge_start(cb: CallbackQuery, state: FSMContext):
    """Start concierge conversation with proper state management"""
    user_id = cb.from_user.id
    house = get_house_cached(HOUSE_ID)
    
    # Set concierge state
    await state.set_state(ConciergeStates.waiting_for_message)
    
    # Get concierge text from config or use default
    text = (house.concierge_text if house and house.concierge_text else 
            "Вы в режиме консьержа. Напишите ваш вопрос или просьбу.")
    
    # Create concierge keyboard
    concierge_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="concierge_cancel")],
        [InlineKeyboardButton(text="⬅️ В главное меню", callback_data="back_main")]
    ])
    
    await cb.message.answer(
        f"🏨 **Режим консьержа активирован**\n\n"
        f"{text}\n\n"
        f"📝 **Отправьте ваше сообщение одним или несколькими сообщениями**\n"
        f"📷 При необходимости прикрепите фото или видео\n\n"
        f"⏰ Режим работы: 9:00 - 21:00\n"
        f"💬 Все сообщения будут переданы администратору",
        reply_markup=concierge_kb
    )
    await cb.message.delete()
    await cb.answer()
    
    logger.info(f"User {user_id} entered concierge mode")


async def handle_concierge_message(message: Message, state: FSMContext, db: Database):
    """Handle message in concierge mode"""
    user = message.from_user
    text = message.text or ""
    # Simple rate limit to reduce spam
    if not allow_concierge_message(user.id):
        await message.answer("Слишком часто. Подождите пару секунд и попробуйте снова.")
        return
    
    logger.info(f"Processing concierge message from user {user.id}: {text[:50]}...")
    
    # Send message to admins
    if ADMIN_IDS:
        try:
            user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
            if user.first_name:
                user_info = f"{user.first_name} ({user_info})"
            
            payload = f"🏨 Сообщение консьержу\n\n"\
                     f"👤 От: {user_info}\n"\
                     f"⏰ Время: {datetime.now().strftime('%H:%M:%S')}\n\n"\
                     f"💬 Сообщение:\n{text}"
            
            # Send to all admins with reply button
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✉️ Ответить", callback_data=f"admin_reply:{user.id}")]
            ])
            
            success_count = 0
            for admin_id in ADMIN_IDS:
                try:
                    await message.bot.send_message(
                        admin_id, payload,
                        parse_mode=None,  # Отключаем парсинг Markdown для безопасности
                        reply_markup=admin_kb
                    )
                    success_count += 1
                    logger.info(f"Successfully sent concierge message to admin {admin_id}")
                except Exception as e:
                    logger.error(f"Failed to send concierge message to admin {admin_id}: {e}")
            
            if success_count > 0:
                # Confirm to user
                await message.answer(
                    f"✅ **Сообщение отправлено администратору!**\n\n"
                    f"📧 Ваше сообщение передано {success_count} администратору(ам)\n"
                    f"⏱️ Ожидайте ответа в рабочее время (9:00-21:00)\n\n"
                    f"💡 Вы можете отправить дополнительные сообщения или медиафайлы",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📱 Завершить диалог", callback_data="concierge_finish")],
                        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")]
                    ])
                )
                # Keep user in concierge mode for additional messages
                await state.set_state(ConciergeStates.waiting_for_media)
            else:
                await message.answer(
                    "❌ **Ошибка отправки сообщения**\n\n"
                    "К сожалению, не удалось доставить сообщение администраторам. "
                    "Попробуйте позже или обратитесь через другие каналы связи.",
                    reply_markup=back_kb()
                )
                await state.clear()
        except Exception as e:
            logger.exception("Failed to process concierge message: %s", e)
            await message.answer(
                "❌ Произошла ошибка при отправке сообщения. Попробуйте позже.",
                reply_markup=back_kb()
            )
            await state.clear()
    else:
        await message.answer(
            "⚠️ Администраторы сейчас недоступны. Попробуйте позже.",
            reply_markup=back_kb()
        )
        await state.clear()


async def handle_concierge_media(message: Message, state: FSMContext):
    """Handle media in concierge mode"""
    user = message.from_user
    # Rate limit media as well
    if not allow_concierge_message(user.id):
        await message.answer("Слишком часто. Подождите пару секунд и попробуйте снова.")
        return
    
    logger.info(f"Processing concierge media from user {user.id}")
    
    if ADMIN_IDS:
        try:
            user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
            if user.first_name:
                user_info = f"{user.first_name} ({user_info})"
            
            caption = f"🏨 **Медиафайл от консьержа**\n\n"\
                     f"👤 От: {user_info}\n"\
                     f"⏰ Время: {datetime.now().strftime('%H:%M:%S')}"
            
            if message.caption:
                caption += f"\n\n📝 **Описание:**\n{message.caption}"
            
            # Admin keyboard
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✉️ Ответить", callback_data=f"admin_reply:{user.id}")]
            ])
            
            success_count = 0
            for admin_id in ADMIN_IDS:
                try:
                    if message.photo:
                        # Enforce simple size constraint if available
                        if hasattr(message.photo[-1], 'file_size') and message.photo[-1].file_size and message.photo[-1].file_size > MAX_MEDIA_SIZE_MB * 1024 * 1024:
                            logger.warning("Photo too large, skipping forwarding")
                            continue
                        await message.bot.send_photo(
                            admin_id, message.photo[-1].file_id,
                            caption=sanitize_markdown(caption),
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=admin_kb
                        )
                    elif message.video:
                        if hasattr(message.video, 'file_size') and message.video.file_size and message.video.file_size > MAX_MEDIA_SIZE_MB * 1024 * 1024:
                            logger.warning("Video too large, skipping forwarding")
                            continue
                        await message.bot.send_video(
                            admin_id, message.video.file_id,
                            caption=sanitize_markdown(caption),
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=admin_kb
                        )
                    elif message.document:
                        await message.bot.send_document(
                            admin_id, message.document.file_id,
                            caption=sanitize_markdown(caption),
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=admin_kb
                        )
                    success_count += 1
                    logger.info(f"Successfully sent concierge media to admin {admin_id}")
                except Exception as e:
                    logger.error(f"Failed to send concierge media to admin {admin_id}: {e}")
            
            if success_count > 0:
                await message.answer(
                    f"✅ **Медиафайл отправлен!**\n\n"
                    f"📧 Файл передан {success_count} администратору(ам)\n"
                    f"💡 Можете отправить еще сообщения или завершить диалог",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📱 Завершить диалог", callback_data="concierge_finish")],
                        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back_main")]
                    ])
                )
            else:
                await message.answer(
                    "❌ Ошибка при отправке медиафайла. Попробуйте позже.",
                    reply_markup=back_kb()
                )
                await state.clear()
        except Exception as e:
            logger.exception("Failed to process concierge media: %s", e)
            await message.answer(
                "❌ Произошла ошибка. Попробуйте позже.",
                reply_markup=back_kb()
            )
            await state.clear()
    else:
        await message.answer(
            "⚠️ Администраторы недоступны.",
            reply_markup=back_kb()
        )
        await state.clear()


async def callback_router(cb: CallbackQuery, state: FSMContext, db: Database):
    data = cb.data or ""
    house = get_house_cached(HOUSE_ID)

    # Admin panel callbacks
    if data == "admin_ls":
        if not is_admin(cb.from_user.id):
            await cb.answer("Недостаточно прав", show_alert=True)
            return
        base = loader._house_dir(HOUSE_ID)
        files = []
        for sub in [base / "texts", base / "guides"]:
            if sub.exists():
                for p in sorted(sub.glob("**/*")):
                    if p.is_file():
                        files.append(str(p.relative_to(base)))
        if (base / "activities.yaml").exists():
            files.append("activities.yaml")
        listing = "\n".join(files) if files else "Нет файлов"
        await cb.message.answer(
            f"Файлы контента (дом {HOUSE_ID}):\n{listing}", 
            parse_mode=None,  # Disable markdown parsing for file names
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]])
        )
        await cb.message.delete()
        await cb.answer()
        return

    # Admin reply button
    if data.startswith("admin_reply:"):
        if not is_admin(cb.from_user.id):
            await cb.answer("Недостаточно прав", show_alert=True)
            return
        target_str = data.split(":", 1)[1]
        try:
            target_user = int(target_str)
        except ValueError:
            await cb.answer("Некорректный адресат", show_alert=False)
            return
        ADMIN_REPLY_TARGET[cb.from_user.id] = target_user
        await cb.message.answer(f"Введите ответ пользователю {target_user}. Ваше следующее сообщение будет отправлено ему.")
        await cb.answer()
        return

    if data == "back_main":
        await cb.message.answer("Главное меню:", reply_markup=main_menu_kb())
        await cb.message.delete()
        await cb.answer()
        return

    if data == "concierge":
        # Start concierge conversation with FSM state
        await handle_concierge_start(cb, state)
        return
    
    # Concierge callbacks
    if data == "concierge_cancel":
        await state.clear()
        await cb.message.answer("❌ Режим консьержа отменен.", reply_markup=main_menu_kb())
        await cb.message.delete()
        await cb.answer()
        return
    
    if data == "concierge_finish":
        await state.clear()
        await cb.message.answer("📱 **Диалог завершен**\n\nСпасибо за обращение! Возвращайтесь, если понадобится помощь.", reply_markup=main_menu_kb())
        await cb.message.delete()
        await cb.answer()
        return

    if data == "how_to_reach":
        md = read_markdown_cached(HOUSE_ID, "texts/how_to_reach.md")
        await cb.message.answer(md, parse_mode=ParseMode.MARKDOWN, reply_markup=houses_menu_kb())
        await cb.message.delete()
        await cb.answer()
        return
    
    if data.startswith("house:"):
        house_name = data.split(":", 1)[1]
        house_file_map = {
            "rustic": "texts/house_rustic.md",
            "loft": "texts/house_loft.md",
            "ikigai": "texts/house_ikigai.md"
        }
        
        if house_name in house_file_map:
            md = read_markdown_cached(HOUSE_ID, house_file_map[house_name])
            back_to_houses_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К выбору дома", callback_data="how_to_reach")],
                [InlineKeyboardButton(text="🏠 В главное меню", callback_data="back_main")]
            ])
            await send_content_with_photo(cb, db, house_file_map[house_name], md, back_to_houses_kb)
        else:
            await cb.answer("Не найдено", show_alert=False)
        await cb.answer()
        return

    if data == "rules_house":
        md = read_markdown_cached(HOUSE_ID, "texts/rules_house.md")
        await send_content_with_photo(cb, db, "texts/rules_house.md", md, back_kb())
        await cb.answer()
        return

    if data == "howto":
        guides = loader.list_guides(HOUSE_ID)
        await cb.message.answer("Как это работает?", reply_markup=guides_menu_kb(guides))
        await cb.message.delete()
        await cb.answer()
        return

    if data.startswith("guide:"):
        gid = data.split(":", 1)[1]
        guide = loader.get_guide(HOUSE_ID, gid)
        if not guide:
            await cb.answer("Не найдено", show_alert=False)
            return
        
        # Check if content is locked and user has permission
        if gid in LOCKED_CONTENT_IDS:
            user_id = cb.from_user.id
            is_admin_user = is_admin(user_id)
            has_access = await db.has_permission(user_id, gid)
            logger.info(f"Access check for guide '{gid}': user_id={user_id}, is_admin={is_admin_user}, has_permission={has_access}")
            
            if not has_access and not is_admin_user:
                logger.warning(f"Access denied for user {user_id} to guide '{gid}'")
                await cb.answer("🔒 Доступ ограничен. Обратитесь к администратору для получения доступа.", show_alert=True)
                return
            else:
                logger.info(f"Access granted for user {user_id} to guide '{gid}'")
        
        # Use the common media handling function
        guide_path = f"guides/{gid}.md"
        await send_content_with_media(cb, db, guide_path, guide.content_md, 
                                    InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="howto")]]),
                                    ParseMode.MARKDOWN)
        await cb.answer()
        return

    if data == "activities":
        user_id = cb.from_user.id
        is_admin_user = is_admin(user_id)
        acts = [a for a in loader.list_activities(HOUSE_ID) if month_in_season(a)]
        
        logger.info(f"Activities menu: user_id={user_id}, is_admin={is_admin_user}, total_activities={len(acts)}, LOCKED_CONTENT_IDS={LOCKED_CONTENT_IDS}")
        
        # Filter out locked activities if user doesn't have permission
        if not is_admin_user:
            filtered_acts = []
            for act in acts:
                if act.id not in LOCKED_CONTENT_IDS:
                    filtered_acts.append(act)
                    logger.debug(f"Activity '{act.id}' is not locked, adding to menu")
                else:
                    # Check if user has permission
                    has_access = await db.has_permission(user_id, act.id)
                    logger.info(f"Locked activity '{act.id}': user {user_id} has_permission={has_access}")
                    if has_access:
                        filtered_acts.append(act)
                        logger.info(f"Activity '{act.id}' added to menu (user has permission)")
                    else:
                        logger.info(f"Activity '{act.id}' filtered out (user has no permission)")
            acts = filtered_acts
            logger.info(f"Filtered activities count: {len(acts)}")
        else:
            logger.info(f"Admin user, showing all {len(acts)} activities")
        
        await cb.message.answer("Чем заняться?", reply_markup=activities_menu_kb(acts))
        await cb.message.delete()
        await cb.answer()
        return

    if data.startswith("activity:"):
        aid = data.split(":", 1)[1]
        logger.info(f"Processing activity callback: aid='{aid}', LOCKED_CONTENT_IDS={LOCKED_CONTENT_IDS}")
        act = loader.get_activity(HOUSE_ID, aid)
        if not act:
            await cb.answer("Не найдено", show_alert=False)
            return
        
        # Check if content is locked and user has permission
        user_id = cb.from_user.id
        is_admin_user = is_admin(user_id)
        is_locked = aid in LOCKED_CONTENT_IDS
        
        logger.info(f"Activity '{aid}' access check: user_id={user_id}, is_admin={is_admin_user}, is_locked={is_locked}, LOCKED_CONTENT_IDS={LOCKED_CONTENT_IDS}")
        
        if is_locked:
            has_access = await db.has_permission(user_id, aid)
            logger.info(f"Permission check result for user {user_id} and content '{aid}': has_permission={has_access}")
            
            # Block access if user is not admin and doesn't have permission
            if not is_admin_user and not has_access:
                logger.warning(f"❌ ACCESS DENIED: user {user_id} attempted to access locked activity '{aid}' without permission")
                await cb.answer("🔒 Доступ ограничен. Обратитесь к администратору для получения доступа.", show_alert=True)
                return
            else:
                reason = "admin" if is_admin_user else "has permission"
                logger.info(f"✅ ACCESS GRANTED: user {user_id} can access '{aid}' ({reason})")
        else:
            logger.info(f"✅ Activity '{aid}' is not locked, access granted")
        
        # Show content only if access is granted
        await cb.message.answer(act.to_markdown(), parse_mode=None, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="activities")]]))
        await cb.message.delete()
        await cb.answer()
        return

    if data == "map":
        md = read_markdown_cached(HOUSE_ID, "texts/map.md")
        await send_content_with_photo(cb, db, "texts/map.md", md, back_kb())
        await cb.answer()
        return

    if data == "feedback":
        feedback_text = (
            "💬 **Обратная связь**\n\n"
            "Оставьте текст вашего отзыва или сообщения.\n\n"
            "❗ **Важно:** В начале сообщения укажите:\n"
            "🔹 **Разрешаю публикацию — да** или **нет**\n\n"
            "📷 Вы также можете прикрепить фото или видео, отправив их отдельным сообщением."
        )
        await cb.message.answer(feedback_text, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        await cb.message.delete()
        await cb.answer()
        return


async def text_router(message: Message, state: FSMContext, db: Database):
    # Handle admin messages first
    if message.from_user and is_admin(message.from_user.id):
        logger.info(f"Processing admin message: {message.text[:50] if message.text else ''}...")
        await admin_router(message, db)
        return
    
    # route concierge vs feedback vs code entry    
    current_state = await state.get_state()
    text = message.text or ""
    logger.info(f"text_router: user_id={message.from_user.id}, state={current_state}, text='{text[:50]}...'")

    # If user is in waiting_for_code state, process the code
    # In aiogram 3.x, state can be None, a State object, or a string
    # Check if state matches waiting_for_code state
    is_waiting_for_code = (
        current_state == AuthStates.waiting_for_code or 
        current_state == AuthStates.waiting_for_code.state or
        (isinstance(current_state, str) and "waiting_for_code" in current_state)
    )
    
    if is_waiting_for_code:
        logger.info(f"text_router: user {message.from_user.id} is in waiting_for_code state, processing code")
        return await process_code(message, state, db)
    
    # If user is in concierge mode, handle the message appropriately
    if current_state == ConciergeStates.waiting_for_message.state:
        return await handle_concierge_message(message, state, db)
    elif current_state == ConciergeStates.waiting_for_media.state:
        # In this state, user can send additional text messages
        return await handle_concierge_message(message, state, db)

    # Check if user is authorized for normal operations
    profile = await db.get_user(message.from_user.id)
    now = datetime.now(timezone.utc)
    authorized = bool(profile and profile.get("access_until") and datetime.fromisoformat(profile["access_until"]) > now)
    
    if not authorized and AUTH_MODE == "code":
        # User is not authorized, ask for code
        await state.set_state(AuthStates.waiting_for_code)
        await message.answer("Для доступа к боту введите, пожалуйста, ваш числовой код доступа:")
        return

    # Only forward messages that are explicitly concierge questions or feedback
    # Check if this looks like a concierge question or feedback
    text_lower = text.lower().strip()
    
    # Check for explicit concierge/feedback indicators
    is_concierge_question = any(keyword in text_lower for keyword in [
        "вопрос", "помощь", "помогите", "как", "где", "когда", "что", "почему",
        "консьерж", "консьержу", "администратор", "админу"
    ])
    
    is_feedback = "разрешаю публикацию" in text_lower or any(keyword in text_lower for keyword in [
        "отзыв", "жалоба", "предложение", "идея", "комментарий", "мнение"
    ])
    
    # Only forward if it's clearly a concierge question or feedback
    if is_concierge_question or is_feedback:
        # Determine if this is a concierge message or feedback
        if is_feedback:
            # This is feedback, not concierge
            payload = f"Обратная связь от @{message.from_user.username or message.from_user.id}:\n{text}"
            message_type = "обратной связи"
        else:
            # This is a concierge question
            payload = f"Вопрос консьержу от @{message.from_user.username or message.from_user.id}:\n{text}"
            message_type = "консьержу"
        
        if ADMIN_IDS:
            try:
                # Send to all admins
                for admin_id in ADMIN_IDS:
                    try:
                        await message.bot.send_message(
                            admin_id, payload, 
                            parse_mode=None,  # Disable markdown parsing
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                        )
                        logger.info(f"Successfully sent {message_type} to admin {admin_id}")
                    except Exception as e:
                        logger.error(f"Failed to send message to admin {admin_id}: {e}")
                        # Continue trying other admins
                        
            except Exception as e:
                logger.exception("Failed to send admin message: %s", e)
        
        await message.answer(f"Спасибо! Ваше сообщение {message_type} отправлено администратору.\n\n💡 Вы также можете прикрепить фото или видео к вашему вопросу, отправив их отдельным сообщением.")
        # Вернём пользователя в главное меню
        await show_main_menu(message)
    else:
        # This is just a regular message, don't forward to admin
        # Just show the main menu
        await show_main_menu(message)


async def media_router(message: Message):
    # Forward photos/videos to admin
    if ADMIN_IDS:
        logger.info(f"Forwarding media from user {message.from_user.id} to {len(ADMIN_IDS)} admins: {ADMIN_IDS}")
        try:
            # Create a safe caption without markdown conflicts
            user_info = f"Медиа от @{message.from_user.username or message.from_user.id}"
            if message.caption:
                # Clean caption from any markdown that might cause parsing errors
                clean_caption = message.caption.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')
                caption = f"{user_info}\n\n{clean_caption}"
            else:
                caption = user_info
            
            # Send to all admins
            success_count = 0
            for admin_id in ADMIN_IDS:
                try:
                    if message.photo:
                        if hasattr(message.photo[-1], 'file_size') and message.photo[-1].file_size and message.photo[-1].file_size > MAX_MEDIA_SIZE_MB * 1024 * 1024:
                            logger.warning("Photo too large, skipping forwarding")
                            continue
                        await message.bot.send_photo(
                            admin_id, message.photo[-1].file_id,
                            caption=caption,
                            parse_mode=None,  # Disable markdown parsing to avoid conflicts
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                        )
                    elif message.video:
                        if hasattr(message.video, 'file_size') and message.video.file_size and message.video.file_size > MAX_MEDIA_SIZE_MB * 1024 * 1024:
                            logger.warning("Video too large, skipping forwarding")
                            continue
                        await message.bot.send_video(
                            admin_id, message.video.file_id,
                            caption=caption,
                            parse_mode=None,  # Disable markdown parsing to avoid conflicts
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                        )
                    success_count += 1
                    logger.info(f"Successfully sent media to admin {admin_id}")
                except Exception as e:
                    logger.error(f"Failed to send media to admin {admin_id}: {e}")
                    # Continue trying other admins
                    
            if success_count == 0:
                logger.error(f"Failed to send media to any admin. All {len(ADMIN_IDS)} attempts failed.")
            else:
                logger.info(f"Successfully sent media to {success_count}/{len(ADMIN_IDS)} admins")
                    
        except Exception as e:
            logger.exception("Failed to forward media: %s", e)
            # Try to send without caption if there's still an error
            try:
                success_count = 0
                for admin_id in ADMIN_IDS:
                    try:
                        if message.photo:
                            await message.bot.send_photo(
                                admin_id, message.photo[-1].file_id,
                                caption=f"Медиа от @{message.from_user.username or message.from_user.id}",
                                parse_mode=None,
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                            )
                        elif message.video:
                            await message.bot.send_video(
                                admin_id, message.video.file_id,
                                caption=f"Видео от @{message.from_user.username or message.from_user.id}",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                            )
                        success_count += 1
                    except Exception as e2:
                        logger.error(f"Failed to send media to admin {admin_id} even without caption: {e2}")
                if success_count == 0:
                    logger.error(f"Failed to send media to any admin even without caption")
            except Exception as e2:
                logger.exception("Failed to forward media even without caption: {e2}")
    else:
        logger.warning("No admin IDs configured, cannot forward media")
    
    await message.answer("Принято! Передал администраторам.")


async def check_admin_config():
    """Check admin configuration and log issues"""
    logger.info(f"Admin configuration check:")
    logger.info(f"  ADMIN_IDS_STR: '{ADMIN_IDS_STR}'")
    logger.info(f"  Parsed ADMIN_IDS: {ADMIN_IDS}")
    logger.info(f"  ADMIN_CHAT_ID (backward compat): {ADMIN_CHAT_ID}")
    
    if not ADMIN_IDS:
        logger.error("No admin IDs configured! Bot will not be able to forward messages to admins.")
        logger.error("Please set ADMIN_IDS in your .env file (e.g., ADMIN_IDS=123456789,987654321)")
        return False
    
    # Test if we can send a test message to each admin
    logger.info("Testing admin message delivery...")
    from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
    
    for admin_id in ADMIN_IDS:
        try:
            # Create a temporary bot instance to test message sending
            test_bot = Bot(BOT_TOKEN)
            try:
                # Try to get chat info to verify the admin ID is valid
                chat = await test_bot.get_chat(admin_id)
                logger.info(f"✅ Admin {admin_id} is accessible: {chat.type} - {getattr(chat, 'title', getattr(chat, 'username', 'Unknown'))}")
            except TelegramBadRequest as e:
                if "chat not found" in str(e).lower():
                    logger.error(f"❌ Admin {admin_id}: Chat not found - this ID may be invalid or the bot hasn't been started by this user")
                elif "bot was blocked" in str(e).lower():
                    logger.error(f"❌ Admin {admin_id}: Bot was blocked by this user")
                else:
                    logger.error(f"❌ Admin {admin_id}: Bad request - {e}")
            except TelegramForbiddenError as e:
                logger.error(f"❌ Admin {admin_id}: Forbidden - {e}")
            except Exception as e:
                logger.error(f"❌ Admin {admin_id}: Unexpected error - {e}")
            finally:
                await test_bot.session.close()
        except Exception as e:
            logger.error(f"❌ Failed to test admin {admin_id}: {e}")
    
    return True


async def on_startup(bot: Bot, db: Database):
    logger.info("Bot started for house %s", HOUSE_ID)
    await ensure_db(db)
    
    # Check admin configuration
    await check_admin_config()


# Admin: simple content management and reply routing
async def admin_router(message: Message, db: Database):
    user = message.from_user
    if not user or not is_admin(user.id):
        logger.info(f"admin_router: not admin user_id={user.id if user else None}")
        return False  # Continue to other handlers

    txt = (message.text or "").strip()

    # If admin is replying to a user (pending target)
    target = ADMIN_REPLY_TARGET.get(user.id)
    if target:
        try:
            if message.text:
                await message.bot.send_message(target, f"Вам пришло сообщение от консьержа!\n\n{message.text}")
            elif message.photo:
                caption = f"Вам пришло сообщение от консьержа!\n\n{message.caption or ''}"
                await message.bot.send_photo(target, message.photo[-1].file_id, caption=caption)
            elif message.video:
                caption = f"Вам пришло сообщение от консьержа!\n\n{message.caption or ''}"
                await message.bot.send_video(target, message.video.file_id, caption=caption)
            await message.answer(f"Отправлено пользователю {target}")
        finally:
            ADMIN_REPLY_TARGET.pop(user.id, None)
        return

    # Admin commands
    if txt == "/admin" or txt == "/admin_menu":
        help_text = """🔧 **Админ-панель для дома {house_id}**

📁 /ls - Показать все файлы контента
Пример: просто напишите `/ls`

📝 /put <путь> - Изменить файл
Как использовать:
1️⃣ Напишите `/put texts/about.md`
2️⃣ Отправьте новый текст отдельным сообщением

📷 /photo <путь> - Добавить фото к контенту
Как использовать:
1️⃣ Напишите `/photo texts/about.md`
2️⃣ Отправьте фото отдельным сообщением
💡 Если фото уже есть - оно заменится

🎥 /video <путь> - Добавить видео к контенту
Как использовать:
1️⃣ Напишите `/video guides/sauna.md`
2️⃣ Отправьте видео отдельным сообщением
💡 Если видео уже есть - оно заменится

🗑️ /delpic <путь> - Удалить фото/видео контента
Пример: `/delpic texts/about.md`

🔓 /grant <user_id> <content_id> - Выдать доступ к заблокированному контенту
Пример: `/grant 123456789 sauna`

🔒 /revoke <user_id> <content_id> - Отозвать доступ к контенту
Пример: `/revoke 123456789 sauna`

📋 /permissions <user_id> - Показать все разрешения пользователя
Пример: `/permissions 123456789`

⚙️ Примеры путей:
• `texts/about.md` - О проекте
• `texts/rules_house.md` - Правила дома
• `guides/sauna.md` - Гид по бане
• `activities.yaml` - Список активностей""".format(house_id=HOUSE_ID)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📁 Список файлов", callback_data="admin_ls")],
        ])
        await message.answer(help_text, parse_mode=None, reply_markup=kb)
        return

    if txt.startswith("/put "):
        rel_path = txt.split(" ", 1)[1].strip()
        ADMIN_EDIT_PENDING[user.id] = rel_path
        
        # Check if file exists to give better feedback
        base = loader._house_dir(HOUSE_ID)
        target_file = base / rel_path
        
        if target_file.exists():
            current_content = target_file.read_text(encoding='utf-8')
            preview = current_content[:300] + ('...' if len(current_content) > 300 else '')
            status = f"⚙️ Редактирование файла: {rel_path}\n\n📄 Текущий контент:\n{preview}\n\n📝 Отправьте новый текст (одним сообщением):"
        else:
            status = f"➕ Создание нового файла: {rel_path}\n\n📝 Отправьте содержимое (одним сообщением):"
            
        await message.answer(status, parse_mode=None)
        return

    if txt == "/ls":
        # list common files
        base = loader._house_dir(HOUSE_ID)
        files = []
        for sub in [base / "texts", base / "guides"]:
            if sub.exists():
                for p in sorted(sub.glob("**/*")):
                    if p.is_file():
                        files.append(str(p.relative_to(base)))
        if (base / "activities.yaml").exists():
            files.append("activities.yaml")
        
        if files:
            listing = "\n".join(f"📄 {f}" for f in files)
            response = f"📁 **Файлы контента (дом {HOUSE_ID}):**\n\n{listing}\n\nℹ️ Для редактирования используйте:\n`/put <путь>`"
        else:
            response = f"⚠️ Нет файлов в доме {HOUSE_ID}"
        
        await message.answer(response, parse_mode=None)
        return

    if txt.startswith("/photo "):
        content_path = txt.split(" ", 1)[1].strip()
        ADMIN_PHOTO_PENDING[user.id] = content_path
        await message.answer(
            f"📷 Добавление фото для: {content_path}\n\n"
            f"📤 Отправьте фотографию следующим сообщением.\n"
            f"💡 Если фото уже существует, оно будет заменено.",
            parse_mode=None
        )
        return

    if txt.startswith("/video "):
        content_path = txt.split(" ", 1)[1].strip()
        ADMIN_VIDEO_PENDING[user.id] = content_path
        await message.answer(
            f"🎥 Добавление видео для: {content_path}\n\n"
            f"📤 Отправьте видео следующим сообщением.\n"
            f"💡 Если видео уже существует, оно будет заменено.",
            parse_mode=None
        )
        return

    if txt.startswith("/delpic "):
        content_path = txt.split(" ", 1)[1].strip()
        # Get media info before deleting
        media_info = await db.get_media(content_path)
        deleted = await db.delete_photo(content_path)
        if deleted:
            # Also delete the physical file if exists
            photos_dir = loader._house_dir(HOUSE_ID) / "photos"
            if media_info:
                media_file, media_type = media_info
                media_path = photos_dir / media_file
                try:
                    if media_path.exists():
                        media_path.unlink()
                        logger.info(f"Deleted {media_type} file: {media_path}")
                except Exception as e:
                    logger.error(f"Failed to delete {media_type} file {media_path}: {e}")
            else:
                # Fallback: try to find and delete by pattern
                for media_path in photos_dir.glob(f"{content_path.replace('/', '_')}.*"):
                    try:
                        media_path.unlink()
                        logger.info(f"Deleted media file: {media_path}")
                    except Exception as e:
                        logger.error(f"Failed to delete media file {media_path}: {e}")
            media_type_name = media_info[1] if media_info else "медиа"
            await message.answer(
                f"✅ **{media_type_name.capitalize()} удалено!**\n\n"
                f"📁 Контент: {content_path}\n"
                f"🗑️ {media_type_name.capitalize()} больше не привязано к этому контенту.",
                parse_mode=None
            )
        else:
            await message.answer(
                f"⚠️ **Медиа не найдено**\n\n"
                f"📁 Контент: {content_path}\n"
                f"🔍 К этому контенту не привязано медиа.",
                parse_mode=None
            )
        return

    # If pending edit path and admin sends text
    pending = ADMIN_EDIT_PENDING.get(user.id)
    if pending and message.text:
        rel = pending
        # secure write
        base = loader._house_dir(HOUSE_ID)
        target = (base / rel).resolve()
        if base.resolve() not in target.parents and base.resolve() != target:
            await message.answer("Некорректный путь")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(message.text, encoding="utf-8")
        ADMIN_EDIT_PENDING.pop(user.id, None)
        
        # Get file size for feedback
        file_size = len(message.text.encode('utf-8'))
        await message.answer(
            f"✅ **Файл успешно обновлён!**\n\n"
            f"📄 Файл: `{rel}`\n"
            f"📊 Размер: {file_size} байт\n"
            f"⚙️ Изменения применены немедленно!",
            parse_mode=None
        )
        return

    # If pending photo and admin sends photo
    if message.photo and user.id in ADMIN_PHOTO_PENDING:
        content_path = ADMIN_PHOTO_PENDING[user.id]
        photo = message.photo[-1]  # Get highest resolution
        
        try:
            # Download the photo
            file_info = await message.bot.get_file(photo.file_id)
            file_extension = file_info.file_path.split('.')[-1] if file_info.file_path else 'jpg'
            
            # Generate filename based on content path
            safe_name = content_path.replace('/', '_').replace('\\', '_')
            photo_filename = f"{safe_name}.{file_extension}"
            
            # Create photos directory
            photos_dir = loader._house_dir(HOUSE_ID) / "photos"
            photos_dir.mkdir(exist_ok=True)
            
            # Download and save photo
            photo_path = photos_dir / photo_filename
            await message.bot.download_file(file_info.file_path, photo_path)
            
            # Save to database
            await db.add_photo(content_path, photo_filename)
            
            # Clean up pending state
            ADMIN_PHOTO_PENDING.pop(user.id, None)
            
            await message.answer(
                f"✅ **Фото успешно добавлено!**\n\n"
                f"📁 Контент: {content_path}\n"
                f"📷 Файл: {photo_filename}\n"
                f"📊 Размер: {photo.file_size if photo.file_size else 'неизвестно'} байт\n"
                f"🎯 Фото будет показываться пользователям при просмотре этого контента!",
                parse_mode=None
            )
            logger.info(f"Photo saved for {content_path}: {photo_filename}")
            
        except Exception as e:
            logger.exception(f"Failed to save photo for {content_path}: {e}")
            await message.answer(
                f"❌ **Ошибка при сохранении фото**\n\n"
                f"📁 Контент: {content_path}\n"
                f"🔧 Попробуйте еще раз или обратитесь к разработчику.\n"
                f"Ошибка: {str(e)}",
                parse_mode=None
            )
        return

    # If pending video and admin sends video
    if message.video and user.id in ADMIN_VIDEO_PENDING:
        content_path = ADMIN_VIDEO_PENDING[user.id]
        video = message.video
        
        try:
            # Download the video
            file_info = await message.bot.get_file(video.file_id)
            file_extension = file_info.file_path.split('.')[-1] if file_info.file_path else 'mp4'
            
            # Generate filename based on content path
            safe_name = content_path.replace('/', '_').replace('\\', '_')
            video_filename = f"{safe_name}.{file_extension}"
            
            # Create photos directory (used for all media)
            photos_dir = loader._house_dir(HOUSE_ID) / "photos"
            photos_dir.mkdir(exist_ok=True)
            
            # Download and save video
            video_path = photos_dir / video_filename
            await message.bot.download_file(file_info.file_path, video_path)
            
            # Save to database
            await db.add_video(content_path, video_filename)
            
            # Clean up pending state
            ADMIN_VIDEO_PENDING.pop(user.id, None)
            
            await message.answer(
                f"✅ **Видео успешно добавлено!**\n\n"
                f"📁 Контент: {content_path}\n"
                f"🎥 Файл: {video_filename}\n"
                f"📊 Размер: {video.file_size if video.file_size else 'неизвестно'} байт\n"
                f"🎯 Видео будет показываться пользователям при просмотре этого контента!",
                parse_mode=None
            )
            logger.info(f"Video saved for {content_path}: {video_filename}")
            
        except Exception as e:
            logger.exception(f"Failed to save video for {content_path}: {e}")
            await message.answer(
                f"❌ **Ошибка при сохранении видео**\n\n"
                f"📁 Контент: {content_path}\n"
                f"🔧 Попробуйте еще раз или обратитесь к разработчику.\n"
                f"Ошибка: {str(e)}",
                parse_mode=None
            )
        return

    # Permission management commands
    if txt.startswith("/grant "):
        parts = txt.split(" ", 2)
        if len(parts) < 3:
            await message.answer("❌ Неверный формат. Используйте: `/grant <user_id> <content_id>`\nПример: `/grant 123456789 sauna`", parse_mode=None)
            return
        try:
            target_user_id = int(parts[1])
            content_id = parts[2].strip()
            granted = await db.grant_permission(target_user_id, content_id, user.id)
            if granted:
                await message.answer(
                    f"✅ **Доступ выдан!**\n\n"
                    f"👤 Пользователь: {target_user_id}\n"
                    f"📁 Контент: {content_id}\n"
                    f"🔓 Теперь пользователь может просматривать этот контент.",
                    parse_mode=None
                )
            else:
                await message.answer("❌ Ошибка при выдаче доступа.", parse_mode=None)
        except ValueError:
            await message.answer("❌ Неверный формат user_id. Должно быть число.", parse_mode=None)
        return

    if txt.startswith("/revoke "):
        parts = txt.split(" ", 2)
        if len(parts) < 3:
            await message.answer("❌ Неверный формат. Используйте: `/revoke <user_id> <content_id>`\nПример: `/revoke 123456789 sauna`", parse_mode=None)
            return
        try:
            target_user_id = int(parts[1])
            content_id = parts[2].strip()
            revoked = await db.revoke_permission(target_user_id, content_id)
            if revoked:
                await message.answer(
                    f"✅ **Доступ отозван!**\n\n"
                    f"👤 Пользователь: {target_user_id}\n"
                    f"📁 Контент: {content_id}\n"
                    f"🔒 Пользователь больше не может просматривать этот контент.",
                    parse_mode=None
                )
            else:
                await message.answer("⚠️ Доступ не найден или уже отозван.", parse_mode=None)
        except ValueError:
            await message.answer("❌ Неверный формат user_id. Должно быть число.", parse_mode=None)
        return

    if txt.startswith("/permissions "):
        parts = txt.split(" ", 1)
        if len(parts) < 2:
            await message.answer("❌ Неверный формат. Используйте: `/permissions <user_id>`\nПример: `/permissions 123456789`", parse_mode=None)
            return
        try:
            target_user_id = int(parts[1])
            permissions = await db.list_user_permissions(target_user_id)
            if permissions:
                perm_list = "\n".join([f"• {p['content_id']} (выдано: {p['granted_at'][:10]})" for p in permissions])
                await message.answer(
                    f"📋 **Разрешения пользователя {target_user_id}:**\n\n{perm_list}",
                    parse_mode=None
                )
            else:
                await message.answer(f"📋 У пользователя {target_user_id} нет специальных разрешений.", parse_mode=None)
        except ValueError:
            await message.answer("❌ Неверный формат user_id. Должно быть число.", parse_mode=None)
        return


async def main():
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is missing. Exiting.")
        sys.exit(1)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = Dispatcher(storage=MemoryStorage())
    db = Database(DB_PATH)

    # Wrapper handlers that close over db and pass state correctly
    async def on_start(message: Message, state: FSMContext):
        await start_handler(message, state, db)

    async def on_menu(message: Message, state: FSMContext):
        await start_handler(message, state, db)

    async def on_callback(cb: CallbackQuery, state: FSMContext):
        await callback_router(cb, state, db)

    async def on_code_entry(message: Message, state: FSMContext):
        """Handler specifically for code entry state"""
        logger.info(f"on_code_entry: user {message.from_user.id} sent text in waiting_for_code state")
        await process_code(message, state, db)
    
    async def on_text(message: Message, state: FSMContext):
        await text_router(message, state, db)

    # Register handlers in correct order
    dp.message.register(on_start, CommandStart())
    dp.message.register(on_menu, Command("menu"))
    dp.callback_query.register(on_callback)
    
    # Register code entry handler with state filter FIRST (higher priority)
    dp.message.register(on_code_entry, F.text, StateFilter(AuthStates.waiting_for_code))
    
    # Then register general text handler
    dp.message.register(on_text, F.text)
    
    async def on_media(message: Message, state: FSMContext):
        # Check if admin is uploading photo/video for content
        if message.from_user and is_admin(message.from_user.id):
            if message.photo and message.from_user.id in ADMIN_PHOTO_PENDING:
                await admin_router(message, db)
            elif message.video and message.from_user.id in ADMIN_VIDEO_PENDING:
                await admin_router(message, db)
            else:
                # Admin sending media to user (reply mode)
                await admin_router(message, db)
        else:
            # Check if user is in concierge mode
            current_state = await state.get_state()
            if current_state in [ConciergeStates.waiting_for_message.state, ConciergeStates.waiting_for_media.state]:
                await handle_concierge_media(message, state)
            else:
                await media_router(message)
    
    dp.message.register(on_media, F.photo | F.video)

    await on_startup(bot, db)

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")

