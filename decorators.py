import functools
import sqlite3
import sys
import time
import traceback
import re
import vk_api
from config import GROUP_TOKEN, USER_TOKEN, API_TOKEN

def sanitize_log(text):
    """Скрывает токены из логов"""
    for token in [GROUP_TOKEN, USER_TOKEN, API_TOKEN]:
        if token and len(token) > 10:
            text = text.replace(token, "[HIDDEN_TOKEN]")
    # Также скрываем любые токены по паттерну
    text = re.sub(r'vk1\.a\.[a-zA-Z0-9_\-]+', '[HIDDEN_VK_TOKEN]', text)
    return text

def safe_execute(default_return=None):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                print(sanitize_log(f"❌ Ошибка в {func.__name__}: {e}"))
                print(sanitize_log(traceback.format_exc()))
                return default_return
        return wrapper
    return decorator

def command_handler(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except Exception as e:
            error_text = sanitize_log(traceback.format_exc())
            print(f"❌ Ошибка в команде {func.__name__}: {sanitize_log(str(e))}\n{error_text}")
            try:
                peer_id = kwargs.get('peer_id') or (args[1] if len(args) > 1 else None)
                if peer_id:
                    self.send_message(peer_id, "❌ Произошла внутренняя ошибка. Админ уведомлен.")
            except Exception: pass
            try:
                self.notify_admin("Ошибка команды", f"Команда: {func.__name__}\nОшибка: {sanitize_log(str(e))[:300]}", is_error=True)
            except Exception: pass
            return None
    return wrapper

def retry_on_error(max_retries=5, delay=3):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            import requests as _rq
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except vk_api.exceptions.ApiError as e:
                    if e.code == 9: time.sleep(delay * (attempt + 1) * 2)
                    elif e.code == 14: time.sleep(15)
                    elif e.code == 6: time.sleep(delay * (attempt + 1) * 3)
                    elif e.code == 901: return None
                    elif e.code == 15: sys.exit(1)
                    else:
                        if attempt < max_retries - 1: time.sleep(delay * (attempt + 1))
                        else: raise
                except _rq.exceptions.Timeout:
                    time.sleep(delay * (attempt + 1))
                except _rq.exceptions.ConnectionError:
                    time.sleep(delay * (attempt + 1))
                except Exception as e:
                    if attempt < max_retries - 1: time.sleep(delay * (attempt + 1))
                    else: raise
            return None
        return wrapper
    return decorator