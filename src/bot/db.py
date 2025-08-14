from __future__ import annotations
import aiosqlite
from datetime import datetime, timedelta
from typing import Optional, Tuple

class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen TEXT,
                access_until TEXT
            )
            """)
            await db.execute("""
            CREATE TABLE IF NOT EXISTS codes (
                code INTEGER PRIMARY KEY,
                house_id TEXT NOT NULL,
                used_by INTEGER,
                used_at TEXT
            )
            """)
            await db.commit()

    async def get_user(self, user_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def upsert_user_access(self, user_id: int, days: int):
        now = datetime.utcnow()
        access_until = now + timedelta(days=days)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO users(user_id, first_seen, access_until) VALUES(?,?,?)\n                 ON CONFLICT(user_id) DO UPDATE SET access_until=excluded.access_until",
                (user_id, now.isoformat(), access_until.isoformat()),
            )
            await db.commit()

    async def consume_code(self, code: int, user_id: int, days: int) -> Tuple[bool, Optional[str]]:
        """Try to mark code as used by this user. Return (ok, house_id)."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM codes WHERE code=?", (code,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return False, None
                if row["used_by"] is not None:
                    return False, None
                house_id = row["house_id"]
            now = datetime.utcnow().isoformat()
            await db.execute("UPDATE codes SET used_by=?, used_at=? WHERE code=?", (user_id, now, code))
            await db.commit()
        await self.upsert_user_access(user_id, days)
        return True, house_id

    async def load_codes_from_csv(self, csv_path: str):
        import csv
        async with aiosqlite.connect(self.path) as db:
            with open(csv_path, newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    code = int(row["code"])
                    house_id = row["house_id"].strip()
                    await db.execute(
                        "INSERT OR IGNORE INTO codes(code, house_id) VALUES(?,?)",
                        (code, house_id)
                    )
            await db.commit()

