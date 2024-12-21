# utils/content_manager.py

import json
import os

CONTENT_FILE = 'content.json'

def load_content():
    if not os.path.exists(CONTENT_FILE):
        return {}
    with open(CONTENT_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_content(content_data):
    with open(CONTENT_FILE, 'w', encoding='utf-8') as f:
        json.dump(content_data, f, ensure_ascii=False, indent=4)