from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
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

# Basic logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("house-bots")

# Globals via env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)
HOUSE_ID = os.getenv("HOUSE_ID", "house1")
AUTH_MODE = os.getenv("AUTH_MODE", "code")  # code | phone
ACCESS_DAYS = int(os.getenv("ACCESS_DAYS", "30"))
DB_PATH = os.getenv("DB_PATH", "./house-bots.db")

if not BOT_TOKEN:
    logger.warning("BOT_TOKEN is not set. Fill .env before running in production.")

loader = ContentLoader(base_path=Path(__file__).resolve().parent.parent.parent / "content")

# Admin state (in-memory)
ADMIN_REPLY_TARGET: Dict[int, int] = {}  # admin_id -> target_user_id
ADMIN_EDIT_PENDING: Dict[int, str] = {}  # admin_id -> rel_path to write
ADMIN_PHOTO_PENDING: Dict[int, str] = {}  # admin_id -> content_path waiting for photo

async def ensure_db(db: Database):
    await db.init()

# Keyboards

def is_admin(user_id: int) -> bool:
    return ADMIN_CHAT_ID and user_id == ADMIN_CHAT_ID


def main_menu_kb():
    btns = [
        [InlineKeyboardButton(text="Консьерж (9–21)", callback_data="concierge")],
        [InlineKeyboardButton(text="Правила дома", callback_data="rules_house")],
        [InlineKeyboardButton(text="Инвентарь", callback_data="rules_inventory")],
        [InlineKeyboardButton(text="Как это работает?", callback_data="howto")],
        [InlineKeyboardButton(text="Чем заняться?", callback_data="activities")],
        [InlineKeyboardButton(text="Карта локаций", callback_data="map")],
        [InlineKeyboardButton(text="Обратная связь", callback_data="feedback")],
        [InlineKeyboardButton(text="Спецпредложения", callback_data="specials")],
        [InlineKeyboardButton(text="Купить дом", callback_data="buy_house")],
        [InlineKeyboardButton(text="Купить мебель", callback_data="buy_furniture")],
        [InlineKeyboardButton(text="О проекте", callback_data="about")],
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
        await message.answer("Добро пожаловать! Введите, пожалуйста, ваш числовой код доступа:")


async def process_code(message: Message, state: FSMContext, db: Database):
    code = message.text.strip() if message.text else ""
    if not code.isdigit():
        await message.answer("Код должен быть числом. Попробуйте ещё раз.")
        # Keep the state - still waiting for code
        return
    ok, house_id = await db.consume_code(int(code), message.from_user.id, ACCESS_DAYS)
    if not ok:
        await message.answer("Код неверный или уже использован. Проверьте и введите снова.")
        # Keep the state - still waiting for code
        return
    # Success - clear state and show menu
    await state.clear()
    await message.answer("Доступ предоставлен!", reply_markup=None)
    await show_main_menu(message)


async def show_main_menu(message: Message):
    house_id = HOUSE_ID  # одна папка контента на бот
    house = loader.load_house(house_id)
    title = house.name if house else "Дом"
    await message.answer(f"{title}. Главное меню:", reply_markup=main_menu_kb())


async def send_content_with_photo(cb: CallbackQuery, db: Database, content_path: str, text_content: str, reply_markup, parse_mode=ParseMode.MARKDOWN):
    """Helper function to send content with photo if available, fallback to text only"""
    logger.info(f"send_content_with_photo: checking for photo at path '{content_path}'")
    photo_file = await db.get_photo(content_path)
    logger.info(f"send_content_with_photo: db.get_photo returned '{photo_file}'")
    
    if photo_file:
        photos_dir = loader._house_dir(HOUSE_ID) / "photos"
        photo_path = photos_dir / photo_file
        logger.info(f"send_content_with_photo: checking if photo exists at '{photo_path}'")
        logger.info(f"send_content_with_photo: photos_dir exists: {photos_dir.exists()}")
        logger.info(f"send_content_with_photo: photo_path exists: {photo_path.exists()}")
        
        if photo_path.exists():
            logger.info(f"send_content_with_photo: photo found, sending photo with caption")
            try:
                # For aiogram 3.x, use FSInputFile
                input_file = FSInputFile(photo_path)
                await cb.message.answer_photo(input_file, caption=text_content, parse_mode=parse_mode, reply_markup=reply_markup)
                await cb.message.delete()
                logger.info(f"send_content_with_photo: photo sent successfully")
                return
            except Exception as e:
                logger.error(f"send_content_with_photo: error sending photo: {e}")
                logger.exception("Full traceback:")
                # Fallback to text on error
                logger.info(f"send_content_with_photo: falling back to text due to error")
        else:
            logger.warning(f"send_content_with_photo: photo file not found at '{photo_path}'")
            # List available photos for debugging
            if photos_dir.exists():
                available_photos = list(photos_dir.glob("*"))
                logger.info(f"send_content_with_photo: available photos in directory: {[p.name for p in available_photos]}")
    else:
        logger.info(f"send_content_with_photo: no photo found in database for '{content_path}'")
    
    # Fallback to text only
    logger.info(f"send_content_with_photo: falling back to text-only message")
    try:
        # Instead of edit_text, always answer a new message and delete the old one
        await cb.message.answer(text_content, parse_mode=parse_mode, reply_markup=reply_markup)
        await cb.message.delete() # Delete the original message
    except Exception as e:
        logger.error(f"send_content_with_photo: error sending text message fallback: {e}")
        await cb.answer("Ошибка при отправке контента", show_alert=True)


async def callback_router(cb: CallbackQuery, db: Database):
    data = cb.data or ""
    house = loader.load_house(HOUSE_ID)

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
        await cb.message.answer(f"Файлы контента (дом {HOUSE_ID}):\n{listing}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]]))
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
        text = (house.concierge_text if house and house.concierge_text else "Напишите ваш вопрос. Я перешлю администратору.")
        await cb.message.answer(text + "\n\nОтправьте ваш вопрос одним сообщением.\n\n📷 Вы также можете прикрепить фото или видео к вашему вопросу, отправив их отдельным сообщением.", reply_markup=back_kb())
        await cb.message.delete()
        await cb.answer()
        return

    if data == "rules_house":
        md = loader.read_markdown(HOUSE_ID, "texts/rules_house.md")
        await send_content_with_photo(cb, db, "texts/rules_house.md", md, back_kb())
        await cb.answer()
        return

    if data == "rules_inventory":
        md = loader.read_markdown(HOUSE_ID, "texts/rules_inventory.md")
        await send_content_with_photo(cb, db, "texts/rules_inventory.md", md, back_kb())
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
        # Use the common photo handling function
        guide_path = f"guides/{gid}.md"
        await send_content_with_photo(cb, db, guide_path, guide.content_md, 
                                    InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="howto")]]),
                                    ParseMode.MARKDOWN)
        await cb.answer()
        return

    if data == "activities":
        acts = [a for a in loader.list_activities(HOUSE_ID) if month_in_season(a)]
        await cb.message.answer("Чем заняться?", reply_markup=activities_menu_kb(acts))
        await cb.message.delete()
        await cb.answer()
        return

    if data.startswith("activity:"):
        aid = data.split(":", 1)[1]
        act = loader.get_activity(HOUSE_ID, aid)
        if not act:
            await cb.answer("Не найдено", show_alert=False)
            return
        await cb.message.answer(act.to_markdown(), parse_mode=None, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="activities")]]))
        await cb.message.delete()
        await cb.answer()
        return

    if data == "map":
        md = loader.read_markdown(HOUSE_ID, "texts/map.md")
        await send_content_with_photo(cb, db, "texts/map.md", md, back_kb())
        await cb.answer()
        return

    if data == "feedback":
        await cb.message.answer("Оставьте текст отзыва/сообщения. Можете прикрепить фото/видео отдельными сообщениями. В начале напишите: Разрешаю публикацию — да/нет.\n\n📷 Вы также можете прикрепить фото или видео к вашему отзыву, отправив их отдельным сообщением.", reply_markup=back_kb())
        await cb.message.delete()
        await cb.answer()
        return

    if data == "specials":
        md = loader.read_markdown(HOUSE_ID, "texts/specials.md")
        await send_content_with_photo(cb, db, "texts/specials.md", md, back_kb())
        await cb.answer()
        return

    if data == "buy_house":
        md = loader.read_markdown(HOUSE_ID, "texts/buy_house.md")
        await send_content_with_photo(cb, db, "texts/buy_house.md", md, back_kb())
        await cb.answer()
        return

    if data == "buy_furniture":
        md = loader.read_markdown(HOUSE_ID, "texts/buy_furniture.md")
        await send_content_with_photo(cb, db, "texts/buy_furniture.md", md, back_kb())
        await cb.answer()
        return

    if data == "about":
        md = loader.read_markdown(HOUSE_ID, "texts/about.md")
        await send_content_with_photo(cb, db, "texts/about.md", md, back_kb())
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
    if current_state == AuthStates.waiting_for_code.state:
        return await process_code(message, state, db)

    # Check if user is authorized for normal operations
    profile = await db.get_user(message.from_user.id)
    now = datetime.now(timezone.utc)
    authorized = bool(profile and profile.get("access_until") and datetime.fromisoformat(profile["access_until"]) > now)
    
    if not authorized and AUTH_MODE == "code":
        # User is not authorized, ask for code
        await state.set_state(AuthStates.waiting_for_code)
        await message.answer("Для доступа к боту введите, пожалуйста, ваш числовой код доступа:")
        return

    # Concierge question: forward to admin
    if text:
        prefix = text.lower().strip()
        is_consent = "разрешаю публикацию" in prefix
        
        # Determine if this is a concierge message or feedback
        if is_consent:
            # This is feedback, not concierge
            payload = f"Обратная связь от @{message.from_user.username or message.from_user.id}:\n{text}"
            message_type = "обратной связи"
        else:
            # This is a concierge question
            payload = f"Вопрос консьержу от @{message.from_user.username or message.from_user.id}:\n{text}"
            message_type = "консьержу"
        
        if ADMIN_CHAT_ID:
            try:
                await message.bot.send_message(
                    ADMIN_CHAT_ID, payload, 
                    parse_mode=None,  # Disable markdown parsing
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                )
            except Exception as e:
                logger.exception("Failed to send admin message: %s", e)
        
        await message.answer(f"Спасибо! Ваше сообщение {message_type} отправлено администратору.\n\n💡 Вы также можете прикрепить фото или видео к вашему вопросу, отправив их отдельным сообщением.")
        # Вернём пользователя в главное меню
        await show_main_menu(message)


async def media_router(message: Message):
    # Forward photos/videos to admin
    if ADMIN_CHAT_ID:
        try:
            # Create a safe caption without markdown conflicts
            user_info = f"Медиа от @{message.from_user.username or message.from_user.id}"
            if message.caption:
                # Clean caption from any markdown that might cause parsing errors
                clean_caption = message.caption.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')
                caption = f"{user_info}\n\n{clean_caption}"
            else:
                caption = user_info
            
            if message.photo:
                await message.bot.send_photo(
                    ADMIN_CHAT_ID, message.photo[-1].file_id,
                    caption=caption,
                    parse_mode=None,  # Disable markdown parsing to avoid conflicts
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                )
            elif message.video:
                await message.bot.send_video(
                    ADMIN_CHAT_ID, message.video.file_id,
                    caption=caption,
                    parse_mode=None,  # Disable markdown parsing to avoid conflicts
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                )
        except Exception as e:
            logger.exception("Failed to forward media: %s", e)
            # Try to send without caption if there's still an error
            try:
                if message.photo:
                    await message.bot.send_photo(
                        ADMIN_CHAT_ID, message.photo[-1].file_id,
                        caption=f"Медиа от @{message.from_user.username or message.from_user.id}",
                        parse_mode=None,
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                    )
                elif message.video:
                    await message.bot.send_video(
                        ADMIN_CHAT_ID, message.video.file_id,
                        caption=f"Видео от @{message.from_user.username or message.from_user.id}",
                        parse_mode=None,
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                    )
            except Exception as e2:
                logger.exception("Failed to forward media even without caption: %s", e2)
    
    await message.answer("Принято! Передал администратору.")


async def on_startup(bot: Bot, db: Database):
    logger.info("Bot started for house %s", HOUSE_ID)
    await ensure_db(db)


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

📁 **/ls** - Показать все файлы контента
Пример: просто напишите `/ls`

📝 **/put <путь>** - Изменить файл
Как использовать:
1️⃣ Напишите `/put texts/about.md`
2️⃣ Отправьте новый текст отдельным сообщением

📷 **/photo <путь>** - Добавить фото к контенту
Как использовать:
1️⃣ Напишите `/photo texts/about.md`
2️⃣ Отправьте фото отдельным сообщением
💡 Если фото уже есть - оно заменится

🗑️ **/delpic <путь>** - Удалить фото контента
Пример: `/delpic texts/about.md`

⚙️ **Примеры путей:**
• `texts/about.md` - О проекте
• `texts/rules_house.md` - Правила дома
• `guides/sauna.md` - Гид по бане
• `activities.yaml` - Список активностей

📊 **Статистика:** Коды работают многоразово ✅""".format(house_id=HOUSE_ID)
        
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

    if txt.startswith("/delpic "):
        content_path = txt.split(" ", 1)[1].strip()
        deleted = await db.delete_photo(content_path)
        if deleted:
            # Also delete the physical file if exists
            photos_dir = loader._house_dir(HOUSE_ID) / "photos"
            photo_file = await db.get_photo(content_path)  # This will return None now since we deleted it
            # Try to find and delete the old photo file
            for photo_path in photos_dir.glob(f"{content_path.replace('/', '_')}.*"):
                try:
                    photo_path.unlink()
                    logger.info(f"Deleted photo file: {photo_path}")
                except Exception as e:
                    logger.error(f"Failed to delete photo file {photo_path}: {e}")
            await message.answer(
                f"✅ **Фото удалено!**\n\n"
                f"📁 Контент: {content_path}\n"
                f"🗑️ Фото больше не привязано к этому контенту.",
                parse_mode=None
            )
        else:
            await message.answer(
                f"⚠️ **Фото не найдено**\n\n"
                f"📁 Контент: {content_path}\n"
                f"🔍 К этому контенту не привязано фото.",
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


async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp = Dispatcher(storage=MemoryStorage())
    db = Database(DB_PATH)

    # Wrapper handlers that close over db and pass state correctly
    async def on_start(message: Message, state: FSMContext):
        await start_handler(message, state, db)

    async def on_menu(message: Message, state: FSMContext):
        await start_handler(message, state, db)

    async def on_callback(cb: CallbackQuery):
        await callback_router(cb, db)

    async def on_text(message: Message, state: FSMContext):
        await text_router(message, state, db)

    # Register handlers in correct order
    dp.message.register(on_start, CommandStart())
    dp.message.register(on_menu, Command("menu"))
    dp.callback_query.register(on_callback)
    
    dp.message.register(on_text, F.text)
    
    async def on_media(message: Message):
        # Check if admin is uploading photo for content
        if message.from_user and is_admin(message.from_user.id) and message.photo:
            await admin_router(message, db)
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

