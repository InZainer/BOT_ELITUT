#!/usr/bin/env python3
"""
Script to check admin configuration and diagnose issues
"""

import asyncio
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dotenv import load_dotenv
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

async def check_admin_config():
    """Check admin configuration and test admin access"""
    # Load environment
    load_dotenv()
    
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
    ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "")
    
    print("🔍 Admin Configuration Check")
    print("=" * 50)
    
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not set in .env file")
        return
    
    print(f"✅ BOT_TOKEN: {'*' * (len(BOT_TOKEN) - 8) + BOT_TOKEN[-8:] if len(BOT_TOKEN) > 8 else '***'}")
    
    # Parse admin IDs
    ADMIN_IDS = []
    if ADMIN_IDS_STR:
        try:
            ADMIN_IDS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(",") if admin_id.strip()]
            print(f"✅ ADMIN_IDS: {ADMIN_IDS}")
        except ValueError as e:
            print(f"❌ Invalid ADMIN_IDS format: {ADMIN_IDS_STR}")
            print(f"   Error: {e}")
            return
    else:
        print("⚠️  ADMIN_IDS not set")
    
    if ADMIN_CHAT_ID:
        print(f"⚠️  ADMIN_CHAT_ID is set (legacy): {ADMIN_CHAT_ID}")
        print("   Note: ADMIN_CHAT_ID is deprecated, use ADMIN_IDS instead")
    
    if not ADMIN_IDS:
        print("❌ No valid admin IDs configured!")
        print("   Please set ADMIN_IDS in your .env file")
        print("   Example: ADMIN_IDS=123456789,987654321")
        return
    
    print(f"\n🧪 Testing admin access...")
    print("=" * 50)
    
    # Test each admin ID
    bot = Bot(BOT_TOKEN)
    try:
        for admin_id in ADMIN_IDS:
            print(f"\n👤 Testing admin ID: {admin_id}")
            try:
                # Try to get chat info
                chat = await bot.get_chat(admin_id)
                chat_type = getattr(chat, 'type', 'Unknown')
                chat_name = getattr(chat, 'title', getattr(chat, 'username', getattr(chat, 'first_name', 'Unknown')))
                
                print(f"   ✅ Accessible: {chat_type} - {chat_name}")
                
                # Try to send a test message
                try:
                    await bot.send_message(admin_id, "🧪 Тестовое сообщение от бота для проверки доступа")
                    print(f"   ✅ Message sent successfully")
                except Exception as e:
                    print(f"   ⚠️  Message sending failed: {e}")
                    
            except TelegramBadRequest as e:
                error_msg = str(e).lower()
                if "chat not found" in error_msg:
                    print(f"   ❌ Chat not found - ID may be invalid or bot not started by user")
                elif "bot was blocked" in error_msg:
                    print(f"   ❌ Bot was blocked by this user")
                elif "user is deactivated" in error_msg:
                    print(f"   ❌ User account is deactivated")
                else:
                    print(f"   ❌ Bad request: {e}")
                    
            except TelegramForbiddenError as e:
                print(f"   ❌ Forbidden: {e}")
                
            except Exception as e:
                print(f"   ❌ Unexpected error: {e}")
                
    finally:
        await bot.session.close()
    
    print(f"\n📋 Summary")
    print("=" * 50)
    print(f"Total admin IDs configured: {len(ADMIN_IDS)}")
    print(f"Admin IDs: {ADMIN_IDS}")
    
    if ADMIN_CHAT_ID and ADMIN_CHAT_ID not in ADMIN_IDS:
        print(f"⚠️  ADMIN_CHAT_ID ({ADMIN_CHAT_ID}) is not in ADMIN_IDS list")
    
    print(f"\n💡 Troubleshooting tips:")
    print("1. Make sure the bot has been started by each admin user")
    print("2. Check that admin IDs are correct (not usernames)")
    print("3. Ensure admins haven't blocked the bot")
    print("4. Verify the bot token is correct")
    print("5. Check that .env file is in the correct location")

if __name__ == "__main__":
    asyncio.run(check_admin_config())
