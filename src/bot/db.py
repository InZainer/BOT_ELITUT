from __future__ import annotations
import aiosqlite
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

logger = logging.getLogger("house-bots")

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
            
            # Store photos and videos associated with content
            await db.execute("""
            CREATE TABLE IF NOT EXISTS photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_path TEXT NOT NULL,
                media_file TEXT NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'photo',
                added_at TEXT NOT NULL,
                UNIQUE(content_path)
            )
            """)
            
            # Migration: Rename photo_file to media_file if needed
            async with db.execute("PRAGMA table_info(photos)") as cur:
                columns = await cur.fetchall()
                column_names = [col[1] for col in columns]
                
                # Check if old schema exists (has photo_file but not media_file)
                if 'photo_file' in column_names and 'media_file' not in column_names:
                    logger.info("Migrating photos table from photo_file to media_file")
                    await db.execute("""
                        CREATE TABLE photos_new (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            content_path TEXT NOT NULL,
                            media_file TEXT NOT NULL,
                            media_type TEXT NOT NULL DEFAULT 'photo',
                            added_at TEXT NOT NULL,
                            UNIQUE(content_path)
                        )
                    """)
                    await db.execute("""
                        INSERT INTO photos_new (id, content_path, media_file, media_type, added_at)
                        SELECT id, content_path, photo_file, 'photo', added_at FROM photos
                    """)
                    await db.execute("DROP TABLE photos")
                    await db.execute("ALTER TABLE photos_new RENAME TO photos")
                    logger.info("Migration completed")
            
            # Store user permissions for locked content
            await db.execute("""
            CREATE TABLE IF NOT EXISTS user_permissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                content_id TEXT NOT NULL,
                granted_at TEXT NOT NULL,
                granted_by INTEGER,
                UNIQUE(user_id, content_id),
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
            """)
            await db.commit()

    async def get_user(self, user_id: int) -> Optional[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def upsert_user_access(self, user_id: int, days: int = 9999):
        """Register or update user access."""
        now = datetime.now(timezone.utc)
        access_until = now + timedelta(days=days)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO users(user_id, first_seen, access_until) VALUES(?,?,?)\n                 ON CONFLICT(user_id) DO UPDATE SET access_until=excluded.access_until",
                (user_id, now.isoformat(), access_until.isoformat()),
            )
            await db.commit()

    async def add_photo(self, content_path: str, photo_file: str):
        """Add or replace photo for content."""
        await self.add_media(content_path, photo_file, 'photo')
    
    async def add_video(self, content_path: str, video_file: str):
        """Add or replace video for content."""
        await self.add_media(content_path, video_file, 'video')
    
    async def add_media(self, content_path: str, media_file: str, media_type: str = 'photo'):
        """Add or replace media (photo or video) for content."""
        # Normalize path to use forward slashes
        normalized_path = content_path.replace('\\', '/')
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO photos(content_path, media_file, media_type, added_at) VALUES(?,?,?,?)",
                (normalized_path, media_file, media_type, now)
            )
            await db.commit()

    async def get_photo(self, content_path: str) -> Optional[str]:
        """Get photo filename for content."""
        result = await self.get_media(content_path)
        return result[0] if result and result[1] == 'photo' else None
    
    async def get_video(self, content_path: str) -> Optional[str]:
        """Get video filename for content."""
        result = await self.get_media(content_path)
        return result[0] if result and result[1] == 'video' else None
    
    async def get_media(self, content_path: str) -> Optional[tuple[str, str]]:
        """Get media filename and type for content. Returns (filename, media_type) or None."""
        # Normalize path to use forward slashes for lookup
        normalized_path = content_path.replace('\\', '/')
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT media_file, media_type FROM photos WHERE content_path=?", (normalized_path,)) as cur:
                row = await cur.fetchone()
                return (row["media_file"], row["media_type"]) if row else None

    async def delete_photo(self, content_path: str) -> bool:
        """Delete photo/video for content. Returns True if media was deleted."""
        # Normalize path to use forward slashes
        normalized_path = content_path.replace('\\', '/')
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("DELETE FROM photos WHERE content_path=?", (normalized_path,))
            await db.commit()
            return cursor.rowcount > 0
    
    async def grant_permission(self, user_id: int, content_id: str, granted_by: Optional[int] = None) -> bool:
        """Grant permission to user for locked content. Returns True if granted."""
        now = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.path) as db:
            try:
                await db.execute(
                    "INSERT OR REPLACE INTO user_permissions(user_id, content_id, granted_at, granted_by) VALUES(?,?,?,?)",
                    (user_id, content_id, now, granted_by)
                )
                await db.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to grant permission: {e}")
                return False
    
    async def revoke_permission(self, user_id: int, content_id: str) -> bool:
        """Revoke permission from user for locked content. Returns True if revoked."""
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "DELETE FROM user_permissions WHERE user_id=? AND content_id=?",
                (user_id, content_id)
            )
            await db.commit()
            return cursor.rowcount > 0
    
    async def has_permission(self, user_id: int, content_id: str) -> bool:
        """Check if user has permission for locked content."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT 1 FROM user_permissions WHERE user_id=? AND content_id=?",
                (user_id, content_id)
            ) as cur:
                row = await cur.fetchone()
                return row is not None
    
    async def list_user_permissions(self, user_id: int):
        """List all permissions for a user."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT content_id, granted_at FROM user_permissions WHERE user_id=?",
                (user_id,)
            ) as cur:
                return [dict(row) async for row in cur]

    async def list_photos(self):
        """List all photos."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT content_path, media_file, media_type FROM photos ORDER BY content_path") as cur:
                return [dict(row) async for row in cur]
