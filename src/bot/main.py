from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, ReplyKeyboardMarkup,
                           KeyboardButton)
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

from .db import Database
from .loader import ContentLoader, Activity, Guide
from .utils import month_in_season

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
    # Auth flow
    if AUTH_MODE == "phone":
        # placeholder: allow after sharing contact
        kb = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, keyboard=[[KeyboardButton(text="Поделиться телефоном", request_contact=True)]] )
        await message.answer("Для доступа поделитесь номером телефона.", reply_markup=kb)
        return
    else:
        # code auth
        profile = await db.get_user(user.id)
        now = datetime.utcnow()
        if profile and profile.get("access_until") and datetime.fromisoformat(profile["access_until"]) > now:
            await message.answer("Добро пожаловать обратно!", reply_markup=None)
            await show_main_menu(message)
            return
        await message.answer("Добро пожаловать! Введите, пожалуйста, ваш числовой код доступа:")


async def process_code(message: Message, state: FSMContext, db: Database):
    code = message.text.strip() if message.text else ""
    if not code.isdigit():
        await message.answer("Код должен быть числом. Попробуйте ещё раз.")
        return
    ok, house_id = await db.consume_code(int(code), message.from_user.id, ACCESS_DAYS)
    if not ok:
        await message.answer("Код неверный или уже использован. Проверьте и введите снова.")
        return
    await message.answer("Доступ предоставлен!", reply_markup=None)
    await show_main_menu(message)


async def show_main_menu(message: Message):
    house_id = HOUSE_ID  # одна папка контента на бот
    house = loader.load_house(house_id)
    title = house.name if house else "Дом"
    await message.answer(f"{title}. Главное меню:", reply_markup=main_menu_kb())


async def cb_router(cb: CallbackQuery, db: Database):
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
        await cb.message.edit_text(f"Файлы контента (дом {HOUSE_ID}):\n{listing}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]]))
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
        await cb.message.edit_text("Главное меню:", reply_markup=main_menu_kb())
        await cb.answer()
        return

    if data == "concierge":
        text = (house.concierge_text if house and house.concierge_text else "Напишите ваш вопрос. Я перешлю администратору.")
        await cb.message.edit_text(text + "\n\nОтправьте ваш вопрос одним сообщением.", reply_markup=back_kb())
        await cb.answer()
        return

    if data == "rules_house":
        md = loader.read_markdown(HOUSE_ID, "texts/rules_house.md")
        await cb.message.edit_text(md, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        await cb.answer()
        return

    if data == "rules_inventory":
        md = loader.read_markdown(HOUSE_ID, "texts/rules_inventory.md")
        await cb.message.edit_text(md, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        await cb.answer()
        return

    if data == "howto":
        guides = loader.list_guides(HOUSE_ID)
        await cb.message.edit_text("Как это работает?", reply_markup=guides_menu_kb(guides))
        await cb.answer()
        return

    if data.startswith("guide:"):
        gid = data.split(":", 1)[1]
        guide = loader.get_guide(HOUSE_ID, gid)
        if not guide:
            await cb.answer("Не найдено", show_alert=False)
            return
        await cb.message.edit_text(guide.content_md, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="howto")]]))
        await cb.answer()
        return

    if data == "activities":
        acts = [a for a in loader.list_activities(HOUSE_ID) if month_in_season(a)]
        await cb.message.edit_text("Чем заняться?", reply_markup=activities_menu_kb(acts))
        await cb.answer()
        return

    if data.startswith("activity:"):
        aid = data.split(":", 1)[1]
        act = loader.get_activity(HOUSE_ID, aid)
        if not act:
            await cb.answer("Не найдено", show_alert=False)
            return
        await cb.message.edit_text(act.to_markdown(), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="activities")]]))
        await cb.answer()
        return

    if data == "map":
        md = loader.read_markdown(HOUSE_ID, "texts/map.md")
        await cb.message.edit_text(md, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        await cb.answer()
        return

    if data == "feedback":
        await cb.message.edit_text("Оставьте текст отзыва/сообщения. Можете прикрепить фото/видео отдельными сообщениями. В начале напишите: Разрешаю публикацию — да/нет.", reply_markup=back_kb())
        await cb.answer()
        return

    if data == "specials":
        md = loader.read_markdown(HOUSE_ID, "texts/specials.md")
        await cb.message.edit_text(md, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        await cb.answer()
        return

    if data == "buy_house":
        md = loader.read_markdown(HOUSE_ID, "texts/buy_house.md")
        await cb.message.edit_text(md, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        await cb.answer()
        return

    if data == "buy_furniture":
        md = loader.read_markdown(HOUSE_ID, "texts/buy_furniture.md")
        await cb.message.edit_text(md, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        await cb.answer()
        return

    if data == "about":
        md = loader.read_markdown(HOUSE_ID, "texts/about.md")
        await cb.message.edit_text(md, parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb())
        await cb.answer()
        return


async def text_router(message: Message, db: Database):
    # route concierge vs feedback vs code entry
    # Ignore admin texts here; admin_router will handle them
    if message.from_user and is_admin(message.from_user.id):
        return
    text = message.text or ""

    # Code entry path when not authorized
    profile = await db.get_user(message.from_user.id)
    now = datetime.utcnow()
    authorized = bool(profile and profile.get("access_until") and datetime.fromisoformat(profile["access_until"]) > now)
    if not authorized and AUTH_MODE == "code":
        return await process_code(message, None, db)

    # Concierge question: forward to admin
    if text:
        prefix = text.lower().strip()
        is_consent = "разрешаю публикацию" in prefix
        # Heuristic: if contains consent or the user clicked feedback before. For MVP, forward everything with meta.
        payload = f"Сообщение от @{message.from_user.username or message.from_user.id}:\n{text}"
        if ADMIN_CHAT_ID:
            try:
                await message.bot.send_message(
                    ADMIN_CHAT_ID, payload,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                )
            except Exception as e:
                logger.exception("Failed to send admin message: %s", e)
        await message.answer("Спасибо! Ваше сообщение отправлено администратору.")
        # Вернём пользователя в главное меню
        await show_main_menu(message)


async def media_router(message: Message):
    # Forward photos/videos to admin
    if ADMIN_CHAT_ID:
        try:
            if message.photo:
                await message.bot.send_photo(
                    ADMIN_CHAT_ID, message.photo[-1].file_id,
                    caption=f"Медиа от @{message.from_user.username or message.from_user.id}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                )
            elif message.video:
                await message.bot.send_video(
                    ADMIN_CHAT_ID, message.video.file_id,
                    caption=f"Видео от @{message.from_user.username or message.from_user.id}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply:{message.from_user.id}")]])
                )
        except Exception as e:
            logger.exception("Failed to forward media: %s", e)
    await message.answer("Принято! Передал администратору.")


async def on_startup(bot: Bot, db: Database):
    logger.info("Bot started for house %s", HOUSE_ID)
    await ensure_db(db)


# Admin: simple content management and reply routing
async def admin_router(message: Message):
    user = message.from_user
    if not user or not is_admin(user.id):
        return

    txt = (message.text or "").strip()

    # If admin is replying to a user (pending target)
    target = ADMIN_REPLY_TARGET.get(user.id)
    if target:
        try:
            if message.text:
                await message.bot.send_message(target, message.text)
            elif message.photo:
                await message.bot.send_photo(target, message.photo[-1].file_id, caption=message.caption)
            elif message.video:
                await message.bot.send_video(target, message.video.file_id, caption=message.caption)
            await message.answer(f"Отправлено пользователю {target}")
        finally:
            ADMIN_REPLY_TARGET.pop(user.id, None)
        return

    # Admin commands
    if txt == "/admin" or txt == "/admin_menu":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Список файлов", callback_data="admin_ls")],
        ])
        await message.answer("Админ-панель:\n- /put <путь> — следующий текст перезапишет файл контента\n- Отправьте текст после /put\n- /ls — показать список файлов", reply_markup=kb)
        return

    if txt.startswith("/put "):
        rel_path = txt.split(" ", 1)[1].strip()
        ADMIN_EDIT_PENDING[user.id] = rel_path
        await message.answer(f"Ок. Пришлите текст, я перезапишу {rel_path}.")
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
        listing = "\n".join(files) if files else "Нет файлов"
        await message.answer(f"Файлы контента (дом {HOUSE_ID}):\n{listing}")
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
        await message.answer(f"Файл {rel} обновлён.")
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
        await cb_router(cb, db)

    async def on_text(message: Message):
        await text_router(message, db)

    # Register handlers
    dp.message.register(on_start, CommandStart())
    dp.message.register(on_menu, Command("menu"))
    dp.callback_query.register(on_callback)
    # Admin router should run before general text forwarding
    dp.message.register(admin_router)

    dp.message.register(on_text, F.text)
    dp.message.register(media_router, F.photo | F.video)

    await on_startup(bot, db)

    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")

