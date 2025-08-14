#!/usr/bin/env python3
import asyncio
import sys
import pathlib

# Ensure project root is on sys.path when running as a script
ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bot.db import Database

async def main(csv_path: str, db_path: str = "./house-bots.db"):
    db = Database(db_path)
    await db.init()
    await db.load_codes_from_csv(csv_path)
    print("Loaded codes from", csv_path)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/load_codes.py <codes.csv> [db_path]")
        sys.exit(1)
    csv_path = sys.argv[1]
    db_path = sys.argv[2] if len(sys.argv) > 2 else "./house-bots.db"
    asyncio.run(main(csv_path, db_path))

