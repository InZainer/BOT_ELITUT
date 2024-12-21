# database.py

import aiosqlite

DB_NAME = "users.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY)''')
        await db.commit()

async def add_user(chat_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
        await db.commit()

async def get_all_user_ids():
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT chat_id FROM users")
        rows = await cursor.fetchall()
        return [row[0] for row in rows]