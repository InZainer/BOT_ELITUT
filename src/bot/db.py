from __future__ import annotations
import aiosqlite
from datetime import datetime, timedelta, timezone
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
                house_id TEXT NOT NULL
            )
            """)
            # Track code usage for analytics without blocking reuse
            await db.execute("""
            CREATE TABLE IF NOT EXISTS code_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                used_at TEXT NOT NULL,
                FOREIGN KEY (code) REFERENCES codes (code)
            )
            """)
            # Store photos associated with content
            await db.execute("""
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_path TEXT NOT NULL,
                photo_file TEXT NOT NULL,
                added_at TEXT NOT NULL,
                UNIQUE(content_path)
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
        now = datetime.now(timezone.utc)
        access_until = now + timedelta(days=days)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO users(user_id, first_seen, access_until) VALUES(?,?,?)\n                 ON CONFLICT(user_id) DO UPDATE SET access_until=excluded.access_until",
                (user_id, now.isoformat(), access_until.isoformat()),
            )
            await db.commit()

    async def consume_code(self, code: int, user_id: int, days: int) -> Tuple[bool, Optional[str]]:
        """Check if code is valid and grant access. Code can be used by multiple users. Return (ok, house_id)."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM codes WHERE code=?", (code,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return False, None
                house_id = row["house_id"]
            
            # Log code usage for analytics (without blocking reuse)
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT INTO code_usage(code, user_id, used_at) VALUES(?,?,?)",
                (code, user_id, now)
            )
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

    async def add_photo(self, content_path: str, photo_file: str):
        """Add or replace photo for content."""
        # Normalize path to use forward slashes
        normalized_path = content_path.replace('\\', '/')
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO photos(content_path, photo_file, added_at) VALUES(?,?,?)",
                (normalized_path, photo_file, now)
            )
            await db.commit()

    async def get_photo(self, content_path: str) -> Optional[str]:
        """Get photo filename for content."""
        # Normalize path to use forward slashes for lookup
        normalized_path = content_path.replace('\\', '/')
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT photo_file FROM photos WHERE content_path=?", (normalized_path,)) as cur:
                row = await cur.fetchone()
                return row["photo_file"] if row else None

    async def delete_photo(self, content_path: str) -> bool:
        """Delete photo for content. Returns True if photo was deleted."""
        # Normalize path to use forward slashes
        normalized_path = content_path.replace('\\', '/')
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("DELETE FROM photos WHERE content_path=?", (normalized_path,))
            await db.commit()
            return cursor.rowcount > 0

    async def list_photos(self):
        """List all photos."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT content_path, photo_file FROM photos ORDER BY content_path") as cur:
                return [dict(row) async for row in cur]
