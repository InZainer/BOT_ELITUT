# utils/validation.py

def validate_code(code):
    # Для примера, принимаем любой 4-значный код
    return code.isdigit() and len(code) == 4