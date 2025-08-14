# handlers/main_helpers.py

from config import ADMINS

# Глобальное хранилище данных пользователей
user_data = {}

def is_admin(user_id):
    return user_id in ADMINS