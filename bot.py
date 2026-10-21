import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta

import requests
import vk_api
from PIL import Image, ImageDraw, ImageFont
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

from config import (
    GROUP_TOKEN, USER_TOKEN, GROUP_ID, API_TOKEN, API_URL,
    CLICK_REWARD, ADMIN_ID, MIN_WITHDRAW, LOG_CHAT_ID, REVIEW_TOPIC_ID,
    BONUS_INTERVAL_HOURS, MENU_PHOTO, TOP_PHOTO, SPIN_GIF,
    SPIN_WIN_100, SPIN_WIN_150, SPIN_WIN_300, DAILY_BONUS_PHOTO, STATUSES,
    PRESTIGE_PHOTO, POST_GAME_START_PHOTO, POST_GAME_END_PHOTO, POST_GAME_REWARD, TOPBAL_PHOTO, LIKE_REWARD, TEST_MODE, DEBUG
)
from database import Database
from decorators import retry_on_error, safe_execute, command_handler, sanitize_log
from game_manager import GameManager
from parser import MessageParser
from rate_limiter import RateLimiter
from watchdog1 import Watchdog

# ===== НАСТРОЙКИ ПРЕСТИЖА =====
PRESTIGE_MULTIPLIERS = {1: 1.0, 2: 1.3, 3: 1.5, 4: 2.0, 5: 2.5}
PRESTIGE_REQS = {
    2: (1000, 15 * 10**12),
    3: (1200, 18 * 10**12),
    4: (1440, 22 * 10**12),
    5: (1728, 26 * 10**12)
}

# FIX #7: запрещённые в тестовом режиме команды
TEST_MODE_FORBIDDEN_EXACT = {
    '/db_clear', '/deleteall', '/clearall', '/resetall', '/resetclicks',
    '/restart', '/off', '/postgame',
}
TEST_MODE_FORBIDDEN_PREFIX = ('/ras ', '/post ', '/game ')


class ClickerBot:
    def __init__(self, group_token, user_token, group_id):
        try:
            test_vk = vk_api.VkApi(token=group_token)
            test_vk.get_api().groups.getById(group_id=group_id)
            print("токен работает")
        except Exception as e:
            print(f"❌ Ошибка группового токена: {e}")
            sys.exit(1)

        try:
            self.vk_user_session = vk_api.VkApi(token=user_token)
            self.vk_user = self.vk_user_session.get_api()
            print("сессия создана")
        except Exception as e:
            print(f"❌ Ошибка токена: {e}")
            sys.exit(1)

        try:
            self.group_token = group_token
            self.group_id = group_id
            self.vk_session = vk_api.VkApi(token=group_token)
            self.longpoll = VkBotLongPoll(self.vk_session, group_id)
            self.vk = self.vk_session.get_api()
            self.db = Database()
            self.bot_enabled = True
            self.api_last_request_time = 0
            self.api_lock = threading.Lock()            

            self.withdraw_locks = {}
            self.promo_lock = threading.Lock()

            self.min_withdraw = MIN_WITHDRAW
            self.log_chat_id = LOG_CHAT_ID
            self.click_reward = CLICK_REWARD

            self.load_settings()
            self.game_manager = GameManager(self)
            self.parser = MessageParser(self)
            self.rate_limiter = RateLimiter()

            self.click_buffer = {}
            self.click_buffer_lock = threading.Lock()
            self.last_flush = time.time()
            self.flush_interval = 5
            self.flush_threshold = 20

            self.name_cache = {}
            self.last_edit_time = {}
            self.daily_msg_ids = {}

            self.captcha_font = None
            try:
                current_dir = os.path.dirname(os.path.abspath(__file__))
                for path in [
                    os.path.join(current_dir, "ofont.ru_Roboto.ttf"),
                    "ofont.ru_Roboto.ttf",
                    "C:/Windows/Fonts/arialbd.ttf",
                    "C:/Windows/Fonts/arial.ttf",
                ]:
                    try:
                        if os.path.exists(path):
                            self.captcha_font = ImageFont.truetype(path, 72)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            self.post_game_active = False
            self.post_game_number = None
            self.post_game_post_id = None

            print("✅ Бот инициализирован")
        except Exception as e:
            print(f"ошибка запуска: {e}")
            print(sanitize_log(traceback.format_exc()))
            sys.exit(1)

        self.watchdog = Watchdog(timeout=7200, bot=self)
        self.watchdog.start()

    def restart_bot(self):
        print("🔄 Перезапуск бота...")
        self.flush_clicks()
        python = sys.executable
        subprocess.Popen([python] + sys.argv)
        os._exit(0)

    # FIX #3: flush не теряет данные при ошибке БД
    def flush_user(self, user_id):
        with self.click_buffer_lock:
            data = self.click_buffer.get(user_id)
        if data is None:
            return True
        try:
            self.db.update_user(user_id, data)
        except Exception as e:
            print(f"⚠️ Ошибка flush_user({user_id}): {e}")
            return False
        with self.click_buffer_lock:
            if self.click_buffer.get(user_id) is data:
                self.click_buffer.pop(user_id, None)
        return True

    # FIX #3: flush не теряет данные при ошибке БД
    def flush_clicks(self):
        with self.click_buffer_lock:
            snapshot = list(self.click_buffer.items())
        for uid, data in snapshot:
            try:
                self.db.update_user(uid, data)
            except Exception as e:
                print(f"⚠️ Ошибка flush_clicks({uid}): {e}")
                continue
            with self.click_buffer_lock:
                if self.click_buffer.get(uid) is data:
                    self.click_buffer.pop(uid, None)
        self.last_flush = time.time()

    def get_buffered_user(self, user_id):
        with self.click_buffer_lock:
            if user_id in self.click_buffer:
                return self.click_buffer[user_id]
        return self.db.get_user(user_id)

    # FIX #1: проверка pending-транзакции
    def _has_pending_tx(self, user_id):
        try:
            if hasattr(self.db, 'get_pending_tx'):
                return self.db.get_pending_tx(user_id) is not None
        except Exception:
            pass
        try:
            conn = self.db.get_connection()
            if not conn:
                return False
            cursor = conn.cursor()
            for table in ('pending_txs', 'pending_tx'):
                try:
                    cursor.execute(f"SELECT 1 FROM {table} WHERE user_id = ? LIMIT 1", (user_id,))
                    if cursor.fetchone():
                        conn.close()
                        return True
                except Exception:
                    pass
            conn.close()
        except Exception:
            pass
        return False

    # ===== СТАТУСЫ =====
    def get_status(self, clicks):
        status_name = "Новичок"
        status_emoji = "🟢"
        for threshold, name, emoji in reversed(STATUSES):
            if clicks >= threshold:
                status_name = name
                status_emoji = emoji
                break
        return status_name, status_emoji

    def get_next_threshold(self, clicks):
        for threshold, name, emoji in STATUSES:
            if clicks < threshold:
                return threshold, name, emoji
        return None, None, None

    def get_progress_bar(self, current, target, length=20):
        if target <= 0:
            return "[" + "█" * length + "]"
        percent = current / target
        filled = int(length * percent)
        empty = length - filled
        bar = "█" * filled + "░" * empty
        return f"[{bar}] {current}/{target} ({int(percent*100)}%)"

    # ===== ПОСТЫ =====
    def check_new_posts(self):
        try:
            enabled = self.db.get_setting('post_notify_enabled', 0)
            if not enabled:
                return
            posts = self.vk_user.wall.get(owner_id=-GROUP_ID, count=1)
            if not posts['items']:
                return
            last_post = posts['items'][0]
            last_post_id = last_post['id']
            saved_post_id = self.db.get_setting('last_post_id', 0)
            if last_post_id != saved_post_id:
                self.db.set_setting('last_post_id', last_post_id)
                post_url = f"https://vk.com/wall-{GROUP_ID}_{last_post_id}"
                notification = f"Оповещение о новом посте в боте!\nВкл/Откл оповещение: «postnotify». \n\n{post_url}\n\n👀 Заходи, читай, ставь лайк!"
                users = self.db.get_all_active_users()
                sent = 0
                for user_id in users:
                    if self.db.is_mail_blocked(user_id) or self.db.is_banned(user_id):
                        continue
                    try:
                        self.send_message(user_id, notification)
                        sent += 1
                        time.sleep(0.34)
                    except Exception:
                        pass
                self.db.add_admin_log(ADMIN_ID, 'оповещение_о_посте', None, f'Пост {last_post_id}, отправлено {sent}')
                print(f"успешно! {last_post_id} отправлено {sent} игрокам")
        except Exception as e:
            print(f"Error: {e}")

    @safe_execute()
    def load_settings(self):
        val = self.db.get_setting('min_withdraw', MIN_WITHDRAW)
        if val is not None:
            self.min_withdraw = val
        val = self.db.get_setting('log_chat_id', LOG_CHAT_ID)
        if val is not None:
            self.log_chat_id = val
        val = self.db.get_setting('click_reward', CLICK_REWARD)
        if val is not None:
            self.click_reward = val

    @safe_execute(default_return=False)
    def is_admin(self, user_id):
        try:
            extra_admins = self.db.get_extra_admins()
            return user_id == ADMIN_ID or user_id in extra_admins
        except Exception:
            return user_id == ADMIN_ID

    @safe_execute(default_return=False)
    def is_private_chat(self, peer_id):
        try:
            return peer_id > 0 and peer_id <= 2000000000
        except Exception:
            return False

    def format_currency(self, num):
        try:
            if num < 1000:
                return f"{int(num)}$"
            elif num < 1000000:
                val = num / 1000
                return f"{int(val)}к$" if val == int(val) else f"{val:.1f}к$".replace('.0к$', 'к$')
            elif num < 1000000000:
                val = num / 1000000
                return f"{int(val)}м$" if val == int(val) else f"{val:.1f}м$".replace('.0м$', 'м$')
            elif num < 1000000000000:
                val = num / 1000000000
                return f"{int(val)}мк$" if val == int(val) else f"{val:.1f}мк$".replace('.0мк$', 'мк$')
            elif num < 1000000000000000:
                val = num / 1000000000000
                return f"{int(val)}мм$" if val == int(val) else f"{val:.1f}мм$".replace('.0мм$', 'мм$')
            else:
                val = num / 1000000000000000
                return f"{int(val)}ммк$" if val == int(val) else f"{val:.1f}ммк$".replace('.0ммк$', 'ммк$')
        except Exception:
            return f"{num}$"

    def parse_amount(self, amount_str):
        try:
            amount_str = amount_str.lower().strip()
            if amount_str.endswith('мм'):
                return int(float(amount_str[:-2]) * 1000000000000)
            elif amount_str.endswith('мк'):
                return int(float(amount_str[:-2]) * 1000000000)
            else:
                return int(float(amount_str))
        except Exception:
            return 0

    def format_click_reward(self, value):
        try:
            if value >= 1000000000000:
                return f"{value // 1000000000000}мм$"
            elif value >= 1000000000:
                return f"{value // 1000000000}мк$"
            else:
                return f"{value}$"
        except Exception:
            return f"{value}$"

    @safe_execute()
    def set_click_reward(self, new_amount):
        self.click_reward = new_amount
        self.db.set_setting('click_reward', new_amount)

    def get_click_reward(self):
        return self.click_reward

    def get_user_click_reward(self, user_id):
        user = self.db.get_user(user_id)
        if user and user.get('click_reward') is not None:
            return user['click_reward']
        return self.get_click_reward()

    # FIX #2: flush перед прямой модификацией
    def set_user_click_reward(self, user_id, amount):
        self.flush_user(user_id)
        user = self.db.get_user(user_id)
        if user:
            user['click_reward'] = amount if amount >= 1000 else None
            self.db.update_user(user_id, user)
            return True
        return False

    def notify_admin(self, title, message, is_error=True):
        try:
            if is_error:
                icon = "❌"
                prefix = "error"
            else:
                icon = "🔄"
                prefix = "ПЕРЕЗАПУСК"
            full_message = f"{icon} **{prefix}** {icon}\n\n📌 {title}\n📝 {message}\n⏰ Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
            self.send_message(ADMIN_ID, full_message)
        except Exception as e:
            print(f"❌ Error: {e}")

    def ban_in_group(self, user_id, reason=""):
        try:
            self.vk.groups.ban(group_id=GROUP_ID, user_id=user_id, comment=reason[:100] if reason else "Нарушение правил")
            return True, None
        except vk_api.exceptions.ApiError as e:
            if e.code == 232:
                return False, "Пользователь уже забанен в сообществе."
            try:
                self.vk_user.groups.ban(group_id=GROUP_ID, user_id=user_id, reason=1, comment=reason[:100] if reason else "Нарушение правил")
                return True, None
            except Exception as e2:
                return False, f"Не удалось (Группа: {str(e)[:100]}, Юзер: {str(e2)[:100]})"
        except Exception as e:
            return False, f"Неизвестная ошибка: {e}"

    def unban_in_group(self, user_id):
        try:
            self.vk_user.groups.unban(group_id=GROUP_ID, user_id=user_id)
            return True
        except Exception as e:
            print(f"❌ Ошибка разбана в группе: {e}")
            return False

    # ===== СИНХРОНИЗАЦИЯ ДИАЛОГОВ =====
    def sync_all_dialogs(self):
        print("🔄 Начинаю...")
        offset = 0
        count = 200
        total_synced = 0
        try:
            while True:
                response = self.vk.messages.getConversations(count=count, offset=offset)
                items = response.get('items', [])
                if not items:
                    break
                for item in items:
                    peer = item.get('conversation', {}).get('peer', {})
                    if peer.get('type') == 'user':
                        user_id = peer.get('id')
                        user = self.db.get_user(user_id)
                        if not user.get('last_seen'):
                            self.db.update_user(user_id, user)
                            total_synced += 1
                offset += count
                time.sleep(0.34)
        except Exception as e:
            print(f"⚠️ Ошибка синхронизации: {e}")
        print(f"✅ Синхронизация завершена! Добавлено/обновлено: {total_synced} диалогов.")
        if total_synced > 0:
            self.send_message(ADMIN_ID, f"Синхронизация: обновлено {total_synced} диалогов")

    # ===== АСИНХРОННЫЕ РАССЫЛКИ =====
    @retry_on_error(max_retries=5, delay=3)
    def _async_send_post(self, admin_id, post_attachment, custom_text):
        users = self.db.get_all_active_users()
        sent = 0
        blocked = 0
        errors = 0
        message = custom_text if custom_text else "новый пост!"
        for user_id in users:
            if self.db.is_mail_blocked(user_id) or self.db.is_banned(user_id):
                blocked += 1
                continue
            try:
                self.vk.messages.send(user_id=user_id, message=message, random_id=random.randint(1, 999999999), attachment=post_attachment)
                sent += 1
                time.sleep(0.34)
            except Exception:
                errors += 1
        result = f"Отправлено: {sent}\nБлок: {blocked}\nError: {errors}"
        self.db.add_admin_log(admin_id, 'рассылка_поста', None, result)
        self.send_message(admin_id, result)

    def send_post_to_all(self, admin_id, post_attachment, custom_text=""):
        threading.Thread(target=self._async_send_post, args=(admin_id, post_attachment, custom_text), daemon=True).start()

    @retry_on_error(max_retries=5, delay=3)
    def _async_send_mail(self, admin_id, message_text):
        try:
            users = self.db.get_all_active_users()
            sent = 0
            blocked = 0
            errors = 0
            for user_id in users:
                if self.db.is_mail_blocked(user_id) or self.db.is_banned(user_id):
                    blocked += 1
                    continue
                try:
                    self.send_message(user_id, f"Рассылка: \n\n{message_text}")
                    sent += 1
                    time.sleep(0.34)
                except Exception:
                    errors += 1
            result = f"Отправлено: {sent}\nЗаблокировано: {blocked}\nОшибок: {errors}"
            self.db.add_admin_log(admin_id, 'рассылка', None, f"отправлено={sent}, заблокировано={blocked}, ошибок={errors}")
            self.send_message(admin_id, result)
        except Exception as e:
            self.send_message(admin_id, f"❌ Ошибка рассылки: {e}")

    def send_mail_to_all(self, admin_id, message_text):
        threading.Thread(target=self._async_send_mail, args=(admin_id, message_text), daemon=True).start()

    def generate_captcha_word(self):
        words = ['СОЛНЦЕ', 'НЕБО', 'ТРАВА', 'ВОДА', 'ОГОНЬ', 'ВЕТЕР', 'ДОЖДЬ', 'СНЕГ', 'ГОРА', 'РЕКА', 'ЛЕС', 'ПОЛЕ', 'ДОМ', 'КОТ', 'ПЁС', 'СОВА', 'ЛУНА', 'ЗВЕЗДА']
        return random.choice(words)

    @retry_on_error(max_retries=5, delay=3)
    def generate_captcha_image(self, word):
        for attempt in range(5):
            try:
                img = Image.new('RGB', (800, 300), color=(255, 255, 255))
                draw = ImageDraw.Draw(img)
                font = self.captcha_font if self.captcha_font else ImageFont.load_default()
                text = word.upper()
                draw.text((50, 110), text, font=font, fill=(0, 0, 0))
                draw.rectangle([(10, 10), (790, 290)], outline=(0, 0, 0), width=3)
                for _ in range(300):
                    draw.point((random.randint(15, 785), random.randint(15, 285)),
                               fill=(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
                for _ in range(8):
                    draw.line((random.randint(15, 785), random.randint(15, 285),
                               random.randint(15, 785), random.randint(15, 285)),
                              fill=(random.randint(100, 200), random.randint(100, 200), random.randint(100, 200)), width=2)
                img_buffer = io.BytesIO()
                img.save(img_buffer, format='PNG')
                img_bytes = img_buffer.getvalue()
                if not img_bytes or len(img_bytes) < 500:
                    time.sleep(1 + attempt)
                    continue
                upload_server = self.vk.photos.getMessagesUploadServer(group_id=GROUP_ID)
                response = requests.post(upload_server['upload_url'],
                                         files={'photo': ('captcha.png', img_bytes, 'image/png')}, timeout=15)
                photo_info = response.json()
                if not all(photo_info.get(k) for k in ('photo', 'server', 'hash')):
                    time.sleep(1 + attempt)
                    continue
                photo = self.vk.photos.saveMessagesPhoto(
                    photo=photo_info['photo'], server=photo_info['server'], hash=photo_info['hash'])[0]
                return f"photo{photo['owner_id']}_{photo['id']}"
            except Exception as e:
                print(f"⚠️ Ошибка капчи (попытка {attempt+1}/5): {e}")
                if attempt < 4:
                    time.sleep(1 + attempt)
        return None

    @retry_on_error(max_retries=5, delay=3)
    def add_click(self, user_id, peer_id):
        try:
            allowed, error_msg = self.rate_limiter.check_and_add(user_id)
            if not allowed:
                return False, error_msg
            with self.click_buffer_lock:
                if user_id in self.click_buffer:
                    user = self.click_buffer[user_id]
                else:
                    user = self.db.get_user(user_id)
                    self.click_buffer[user_id] = user
            if user['waiting_captcha']:
                return False, "🔐 Сначала пройди проверку! Напиши слово с картинки"
            now = datetime.now()
            # Проверку по времени убрали, так как RateLimiter уже защищает от спама
            base_reward = user.get('click_reward')
            if base_reward is None or base_reward < 1000:
                base_reward = self.get_click_reward()
            prestige_lvl = user.get('prestige_level', 1)
            multiplier = PRESTIGE_MULTIPLIERS.get(prestige_lvl, 1.0)
            click_reward = int(base_reward * multiplier)
            old_status, _ = self.get_status(user['clicks'])
            user['balance'] += click_reward
            user['clicks'] += 1
            user['total_earned'] += click_reward
            user['last_click'] = now.isoformat()
            user['clicks_since_check'] += 1
            new_status, new_emoji = self.get_status(user['clicks'])
            if user['clicks_since_check'] >= 12:
                captcha_word = self.generate_captcha_word()
                captcha_attachment = self.generate_captcha_image(captcha_word)
                user['waiting_captcha'] = True
                user['captcha_word'] = captcha_word.lower()
                user['captcha_attachment'] = captcha_attachment
                # Генерируем отрицательное число, чтобы следующая капча выпала через 20-30 кликов
                user['clicks_since_check'] = -random.randint(8, 18)
                self.flush_user(user_id)
                if captcha_attachment:
                    self.send_message(peer_id, f"✅ Зачисление +{self.format_click_reward(click_reward)} но:\n\nНапиши слово, которое видишь на картинке", attachment=captcha_attachment)
                else:
                    self.send_message(peer_id, f"✅ Зачисление +{self.format_click_reward(click_reward)} но:\n\n🔐 Проверка: Напиши слово '{captcha_word}' чтобы продолжить кликать")
                return True, None
            if old_status != new_status:
                self.send_message(peer_id, f"🎉 Вау, Ты достиг нового статуса: {new_status} {new_emoji}\n\nПродолжай кликать для следующего статуса!")
            if len(self.click_buffer) >= self.flush_threshold or (time.time() - self.last_flush > self.flush_interval):
                self.flush_clicks()
            return True, f"✅ Зачислены +{self.format_click_reward(click_reward)}! Кликов: {user['clicks']}\nБаланс: {self.format_currency(user['balance'])}"
        except Exception as e:
            print(f"❌ Ошибка add_click: {e}")
            return False, "❌ Ошибка, попробуй позже"

    @retry_on_error(max_retries=5, delay=3)
    def spin_bonus(self, user_id, peer_id):
        try:
            self.flush_user(user_id)
            user = self.db.get_user(user_id)
            now = datetime.now()
            last_spin = user.get('last_spin')
            if last_spin and last_spin != '0':
                try:
                    last_time = datetime.fromisoformat(last_spin)
                except ValueError:
                    last_time = None
                if last_time:
                    time_passed = now - last_time
                    if time_passed < timedelta(hours=BONUS_INTERVAL_HOURS):
                        remaining = timedelta(hours=BONUS_INTERVAL_HOURS) - time_passed
                        hours = remaining.seconds // 3600
                        minutes = (remaining.seconds % 3600) // 60
                        seconds = remaining.seconds % 60
                        return False, None, f"⏰ ты уже крутил барабан!\nСледующее вращение через: {hours}ч {minutes}м {seconds}с"
            rand = random.randint(1, 100)
            if rand <= 10:
                win_amount = 300000000000
                win_photo = SPIN_WIN_300
                result_text = "джекпот! +300мк$! ✨"
            elif rand <= 50:
                win_amount = 150000000000
                win_photo = SPIN_WIN_150
                result_text = "выпало: +150мк$!"
            else:
                win_amount = 100000000000
                win_photo = SPIN_WIN_100
                result_text = "выпало: +100мк$!"
            msg_id = self.send_message(peer_id, "🎰 барабан крутится...", attachment=SPIN_GIF)
            time.sleep(2)
            if win_amount > 0:
                user['balance'] += win_amount
                user['total_earned'] += win_amount
                result_text = f"{result_text}\n💰 Новый баланс: {self.format_currency(user['balance'])}"
            else:
                result_text = f"{result_text}\n💰 Баланс не изменился: {self.format_currency(user['balance'])}"
            user['last_spin'] = now.isoformat()
            self.db.update_user(user_id, user)
            if msg_id:
                self.edit_message(peer_id, msg_id, result_text, attachment=win_photo)
            return True, win_amount, result_text
        except Exception as e:
            print(f"❌ Ошибка spin_bonus: {e}")
            return False, None, "❌ Ошибка, попробуй позже"

    # FIX #6: спин в отдельном потоке — не блокирует LongPoll
    def _spin_bonus_thread(self, user_id, peer_id):
        try:
            success, win_amount, result_text = self.spin_bonus(user_id, peer_id)
            if not success and result_text:
                self.send_back_keyboard(peer_id, result_text)
        except Exception as e:
            print(f"❌ Ошибка фонового спина: {e}")

    # FIX #6: вывод в отдельном потоке — не блокирует LongPoll
    def _withdraw_thread(self, user_id, peer_id):
        try:
            success, result = self.withdraw_all(user_id, peer_id)
            if result:
                self.send_message(peer_id, f"❌ {result}")
        except Exception as e:
            print(f"❌ Ошибка фонового вывода: {e}")

    @retry_on_error(max_retries=5, delay=3)
    def edit_message(self, peer_id, message_id, text, attachment=None):
        try:
            self.vk.messages.edit(peer_id=peer_id, message_id=message_id, message=text, attachment=attachment)
        except Exception as e:
            print(f"❌ Ошибка редактирования: {e}")
            raise

    # FIX #23: правильный сброс streak после 7 дня
    def get_daily_bonus_data(self, user_id):
        try:
            self.flush_user(user_id)
            user = self.get_buffered_user(user_id)
            now = datetime.now()
            last_bonus = user.get('last_daily_bonus')
            current_streak = user.get('daily_bonus_streak', 0)
            next_available_time = None
            if last_bonus and last_bonus != '0':
                try:
                    last_time = datetime.fromisoformat(last_bonus)
                except ValueError:
                    last_time = None
                if last_time:
                    time_passed = now - last_time
                    if time_passed < timedelta(hours=BONUS_INTERVAL_HOURS):
                        can_claim = False
                        streak = current_streak
                        next_available_time = last_time + timedelta(hours=BONUS_INTERVAL_HOURS)
                    else:
                        can_claim = True
                        if time_passed < timedelta(hours=36):
                            # FIX #23: после 7 дня начинается новый цикл с 1
                            streak = 1 if current_streak >= 7 else current_streak + 1
                        else:
                            streak = 1
                        next_available_time = None
                else:
                    can_claim = True
                    streak = 1
            else:
                can_claim = True
                streak = 1
            rewards = {1: 100*10**9, 2: 150*10**9, 3: 250*10**9, 4: 300*10**9,
                       5: 350*10**9, 6: 400*10**9, 7: 500*10**9}
            reward = rewards.get(streak, 100 * 10**9)
            return {'can_claim': can_claim, 'streak': streak, 'reward': reward,
                    'rewards': rewards, 'next_available_time': next_available_time}
        except Exception:
            return {'can_claim': True, 'streak': 1, 'reward': 100 * 10**9,
                    'rewards': {}, 'next_available_time': None}

    @retry_on_error(max_retries=5, delay=3)
    def claim_daily_bonus(self, user_id, peer_id):
        try:
            self.flush_user(user_id)
            user = self.db.get_user(user_id)
            bonus_data = self.get_daily_bonus_data(user_id)
            if not bonus_data['can_claim']:
                next_time = bonus_data['next_available_time']
                if next_time:
                    remaining = next_time - datetime.now()
                    hours = remaining.seconds // 3600
                    minutes = (remaining.seconds % 3600) // 60
                    seconds = remaining.seconds % 60
                    next_streak = bonus_data['streak'] + 1 if bonus_data['streak'] < 7 else 1
                    return False, f"Бонус ещё не доступен!\n(День {next_streak}) будет через: {hours}ч {minutes}м {seconds}с"
                return False, "❌ Бонус ещё не доступен!"
            reward = bonus_data['reward']
            streak = bonus_data['streak']
            user['balance'] += reward
            user['total_earned'] += reward
            user['last_daily_bonus'] = datetime.now().isoformat()
            user['daily_bonus_streak'] = streak
            self.db.update_user(user_id, user)
            return True, f"🎁 Ежедневный бонус (День {streak}) получен!\n➕ +{self.format_currency(reward)}\nБаланс: {self.format_currency(user['balance'])}"
        except Exception as e:
            return False, f"❌ Ошибка: {e}"

    @retry_on_error(max_retries=5, delay=3)
    def send_log(self, user_id, amount_formatted):
        if self.log_chat_id == 0:
            return
        try:
            user_info = self.vk.users.get(user_ids=user_id)[0]
            log_text = f"""#вывод

👨 Пользователь: [id{user_id}|{user_info['first_name']} {user_info['last_name']}]
💰 Сумма вывода: {amount_formatted}"""
            self.vk.messages.send(peer_id=self.log_chat_id, message=log_text, random_id=random.randint(1, 999999999))
        except Exception as e:
            print(f"❌ Ошибка лога: {e}")

    @retry_on_error(max_retries=2, delay=3)
    def send_review_to_forum(self, user_id, amount, comment):
        try:
            user_info = self.vk.users.get(user_ids=user_id)[0]
            review_text = f"""New Feedback:

• От:  [id{user_id}|{user_info['first_name']} {user_info['last_name']}]
• Сумма вывода: {amount}
💬 Коммент: {comment}

Вывод осуществлен!"""
            self.vk_user.board.createComment(group_id=GROUP_ID, topic_id=REVIEW_TOPIC_ID, message=review_text, from_group=False)
            print("✅ Отзыв успешно отправлен в обсуждения.")
        except vk_api.exceptions.ApiError as e:
            print(f"❌ Не удалось отправить отзыв (Ошибка токена: {e}). Пересылаю админу.")
            try:
                self.send_message(ADMIN_ID, f"⚠️ **ОТЗЫВ НЕ ОТПРАВЛЕН В ТОПИК (Токен умер)**\n\n{review_text}")
            except Exception as inner_e:
                print(f"❌ Даже админу не удалось переслать: {inner_e}")
        except Exception as e:
            print(f"❌ Ошибка отправки отзыва: {e}")

    # FIX #1, #13: идемпотентность + блок при pending_review + не возвращать баланс после успешной оплаты
    @command_handler
    def withdraw_all(self, user_id, peer_id):
        self.flush_user(user_id)
        if user_id not in self.withdraw_locks:
            self.withdraw_locks[user_id] = threading.Lock()
        if not self.withdraw_locks[user_id].acquire(blocking=False):
            return False, "⏳ Вывод уже обрабатывается. Пожалуйста, подождите."

        balance = 0
        paid = False
        try:
            user = self.get_buffered_user(user_id)

            # FIX #13: блокируем вывод до написания отзыва
            if user.get('pending_review'):
                return False, "📝 Сначала напишите отзыв о предыдущем выводе! Ссылка в предыдущем сообщении."

            # FIX #1: защита от повторного вывода поверх незавершённой транзакции
            if self._has_pending_tx(user_id):
                return False, "⏳ У вас уже есть незавершённый вывод. Свяжитесь с администратором."

            balance = user['balance'] or 0
            if balance <= 0:
                return False, "❌ Вывод невозможен при балансе меньше 1$"
            if balance < self.min_withdraw:
                return False, f"❌ Минимальная сумма: {self.format_currency(self.min_withdraw)}. Твой баланс: {self.format_currency(balance)}"

            # Проверка баланса бота
            try:
                r = self.safe_api_request(f"{API_URL}/balance", {'token': API_TOKEN}, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    bot_balance = data.get('answer', {}).get('balance', 0)
                    if balance > bot_balance:
                        return False, f"❌ Недостаточно средств на балансе бота! Доступно: {self.format_currency(bot_balance)}\nСвяжитесь с администратором."
                else:
                    return False, "Ошибка проверки баланса бота. Попробуйте позже."
            except Exception as e:
                return False, f"❌ Ошибка API при проверке баланса: {e}"

            # Резервируем: пишем в БД ДО вызова API
            user['balance'] = 0
            self.db.update_user(user_id, user)
            self.db.add_pending_tx(user_id, balance)

            # Блок с запросом на выплату
            max_retries = 3
            last_error = None
            for attempt in range(max_retries):
                try:
                    r = self.safe_api_request(f"{API_URL}/pay",
                                      {'token': API_TOKEN, 'user': user_id, 'count': balance}, timeout=15)
                    
                    # Если ответ 200, выходим из цикла и обрабатываем результат
                    if r.status_code == 200:
                        break
                    
                    # Если пришла ошибка 429 (Too Many Requests), ждем и пробуем снова
                    elif r.status_code == 429:
                        wait_time = (attempt + 1) * 2  # Ждем 2, 4, 6 секунд
                        print(f"⚠️ Rate limit (429). Попытка {attempt+1}/{max_retries}. Ждем {wait_time} сек.")
                        time.sleep(wait_time)
                        last_error = "Слишком много запросов к API"
                        continue # Переходим к следующей попытке
                    
                    # Другие ошибки API (не 429) — прерываем цикл, чтобы вернуть деньги
                    else:
                        last_error = f"HTTP {r.status_code}"
                        break
                        
                except Exception as e:
                    last_error = str(e)[:60]
                    time.sleep(1) # Небольшая пауза при сетевой ошибке

            # Если после всех попыток успеха не было — возвращаем деньги
            if r.status_code != 200:
                user['balance'] = balance
                self.db.update_user(user_id, user)
                try:
                    self.db.remove_pending_tx(user_id)
                except Exception:
                    pass
                return False, f"❌ Ошибка API: {last_error}. Деньги возвращены на баланс."

            if r.status_code == 200:
                data = r.json()
                if data.get('answer') == 'success':
                    paid = True
                    try:
                        self.db.remove_pending_tx(user_id)
                    except Exception as e:
                        print(f"⚠️ remove_pending_tx: {e}")
                    
                    formatted_amount = self.format_currency(balance)
                    
                    # Уведомления — в отдельном try, чтобы сбой уведомления не вернул баланс
                    try:
                        self.send_log(user_id, formatted_amount)
                        keyboard = {"one_time": False, "buttons": [[{"action": {"type": "text", "label": "🔙 Вернуться в меню"}, "color": "negative"}]]}
                        
                        if not user.get('has_reviewed'):
                            link = f"https://vk.com/topic-{GROUP_ID}_{REVIEW_TOPIC_ID}"
                            self.send_message(peer_id, f"😍 Вы вывели: {formatted_amount}!\n💳 Деньги уже у вас на балансе в @badbotik!\n\n📝 Если хотите, можете оставить отзыв тут: {link}", keyboard=keyboard)
                            user['has_reviewed'] = True # Ставим флаг, чтобы больше не предлагал
                            self.db.update_user(user_id, user)
                        else:
                            self.send_message(peer_id, f"😍 Вы вывели: {formatted_amount}!\n💳 Деньги уже у вас на балансе в @badbotik!", keyboard=keyboard)
                    except Exception as e:
                        print(f"⚠️ Ошибка уведомления после вывода: {e}")
                    
                    return True, None
            else:
                user['balance'] = balance
                self.db.update_user(user_id, user)
                try:
                    self.db.remove_pending_tx(user_id)
                except Exception:
                    pass
                return False, f"❌ API ошибка {r.status_code}. Деньги возвращены на баланс."

        except Exception as e:
            if paid:
                # FIX #1: оплата прошла — НЕ возвращаем баланс
                print(f"⚠️ Исключение после успешной выплаты user {user_id}: {e}")
                return True, None
            try:
                user = self.db.get_user(user_id)
                if user['balance'] == 0:
                    user['balance'] = balance
                    self.db.update_user(user_id, user)
                    self.db.remove_pending_tx(user_id)
            except Exception:
                pass
            return False, f"❌ Ошибка: {str(e)[:50]}. Деньги возвращены на баланс."
        finally:
            self.withdraw_locks[user_id].release()

    @retry_on_error(max_retries=5, delay=3)
    def get_user_name(self, user_id):
        try:
            user = self.vk.users.get(user_ids=user_id)[0]
            name = f"{user['first_name']} {user['last_name']}".replace('[', '').replace(']', '')
            return f"[id{user_id}|{name}]"
        except Exception:
            return f"[id{user_id}|Пользователь]"

    # ===== ИГРА В КОММЕНТАРИЯХ =====
    @command_handler
    def handle_postgame_command(self, user_id, peer_id):
        if not self.is_admin(user_id):
            self.send_message(peer_id, "error")
            return
        if self.post_game_active:
            self.send_message(peer_id, "Дождитесь окончания игры.")
            return
        self.post_game_number = random.randint(1, 100)
        self.post_game_active = True
        try:
            post_text = f"🎯 Игра угадай число\n\nЯ загадал число от 1 до 100. Пиши своё число в комментарии!\n\n💰 Приз: {self.format_currency(POST_GAME_REWARD)}"
            response = self.vk.wall.post(owner_id=-GROUP_ID, message=post_text, attachments=POST_GAME_START_PHOTO, from_group=1)
            self.post_game_post_id = response['post_id']
            self.send_message(peer_id, f"Успешно!\nЗагаданное число: {self.post_game_number}\nhttps://vk.com/wall-{GROUP_ID}_{self.post_game_post_id}")
        except Exception as e:
            print(f"Ошибка создания поста: {e}")
            self.send_message(peer_id, f"Не удалось создать пост: {e}")
            self.post_game_active = False

    # FIX #15: не завершаем игру, если победитель не найден в БД; flush перед начислением
    def handle_wall_reply(self, event):
        if not self.post_game_active or not self.post_game_post_id:
            return
        try:
            post_id = event.object.post_id
            from_id = event.object.from_id
            comment_id = event.object.id
            text = event.object.text.strip()
            if int(post_id) != int(self.post_game_post_id):
                return
            try:
                guess = int(text)
            except ValueError:
                return
            if guess == self.post_game_number:
                # Проверяем пользователя ДО выключения игры
                user = self.db.get_user(from_id)
                if not user:
                    # Не игрок — игра продолжается
                    return
                if self.db.is_banned(from_id):
                    return

                self.post_game_active = False

                self.flush_user(from_id)
                user = self.db.get_user(from_id)
                user['balance'] += POST_GAME_REWARD
                user['total_earned'] += POST_GAME_REWARD
                self.db.update_user(from_id, user)

                user_name = self.get_user_name(from_id)
                self.vk.wall.createComment(
                    owner_id=-GROUP_ID,
                    post_id=self.post_game_post_id,
                    reply_to_comment=comment_id,
                    message=f"🎉 {user_name}, ты угадал число {self.post_game_number}!\nТы получаешь {self.format_currency(POST_GAME_REWARD)} на баланс!"
                )
                try:
                    new_text = f"🎉 Игра окончена!\n\nПобедитель: {user_name} (угадал число {self.post_game_number}).\n\nЖдите следующую игру!"
                    response = self.vk.wall.post(owner_id=-GROUP_ID, message=new_text, attachments=POST_GAME_END_PHOTO, from_group=1)
                    new_post_id = response['post_id']
                    new_post_url = f"https://vk.com/wall-{GROUP_ID}_{new_post_id}"
                    self.vk.wall.createComment(
                        owner_id=-GROUP_ID,
                        post_id=self.post_game_post_id,
                        message=f"🏆 Итоги игры и победитель здесь: {new_post_url}"
                    )
                except Exception as e:
                    print(f"⚠️ Не удалось создать пост с итогами: {e}")
                self.send_message(ADMIN_ID, f"Игра в комментариях завершена!\nПобедитель: {user_name}\nЧисло: {self.post_game_number}")
        except Exception as e:
            print(f"❌ Ошибка в обработке комментария: {e}")


        # ===== ЗАПУСК =====
    def run(self):
        print("\n" + "=" * 50)
        if TEST_MODE:
            print("🧪 РЕЖИМ ТЕСТА АКТИВЕН")
            print("🧪 Бот реагирует ТОЛЬКО на команды админа.")
            print("🧪 Разрушительные/массовые команды заблокированы.")
            print("🧪 Чтобы выключить тест: TEST_MODE = False в config.py")
        else:
            print("🚀 Фарм | Бот Нищий запущен.")
        print("=" * 50)

        if not TEST_MODE:
            threading.Thread(target=self.check_new_posts, daemon=True).start()
            threading.Thread(target=self._api_status_checker, daemon=True).start()
            threading.Thread(target=self._ping_inactive_users, daemon=True).start()
            threading.Thread(target=self.sync_all_dialogs, daemon=True).start()

        error_counter = 0
        while True:
            try:
                for event in self.longpoll.listen():
                    error_counter = 0
                    if TEST_MODE:
                        allowed = False
                        if event.type == VkBotEventType.MESSAGE_NEW:
                            sender = event.object.message.get('from_id')
                            if sender and self.is_admin(sender):
                                allowed = True
                        elif event.type == VkBotEventType.MESSAGE_EVENT:
                            sender = event.object.user_id
                            if sender and self.is_admin(sender):
                                allowed = True
                        if not allowed:
                            continue

                    if event.type == VkBotEventType.MESSAGE_NEW:
                        try:
                            self.handle_message(event)
                        except Exception as e:
                            print(f"❌ Ошибка в handle_message: {e}")
                            error_counter += 1
                            if error_counter >= 5:
                                raise Exception("Too many errors")
                    elif event.type == VkBotEventType.BOARD_POST_NEW:
                        self.handle_board_post(event)
                    elif event.type == VkBotEventType.MESSAGE_EVENT:
                        self.handle_message_event(event)
                    elif event.type == VkBotEventType.WALL_REPLY_NEW:
                        self.handle_wall_reply(event)
                    elif event.type == "like_add":
                        self.handle_like_add(event)

            except vk_api.exceptions.ApiError as e:
                print(f"❌ VK API ошибка {e.code}: {e}")
                if e.code == 15:
                    if not TEST_MODE:
                        self.notify_admin("Токен умер", "Бот остановлен", is_error=True)
                    sys.exit(1)
                elif e.code == 9:
                    time.sleep(60)
                else:
                    time.sleep(30)
            except requests.exceptions.ConnectionError:
                print("❌ Нет соединения")
                time.sleep(60)
            except Exception as e:
                print(f"❌ Критическая ошибка: {e}")
                error_counter += 1
                time.sleep(30)
                if error_counter >= 3:
                    if not TEST_MODE:
                        self.notify_admin("Аварийный перезапуск", str(e), is_error=False)
                    self.restart_bot()

            for attempt in range(10):
                try:
                    print("🔄 Пересоздание сессии VK LongPoll...")
                    time.sleep(5)
                    self.vk_session = vk_api.VkApi(token=self.group_token)
                    self.longpoll = VkBotLongPoll(self.vk_session, self.group_id)
                    self.vk = self.vk_session.get_api()
                    print("✅ Переподключено")
                    error_counter = 0
                    break
                except Exception as e:
                    print(f"⚠️ Ошибка переподключения ({attempt+1}/10): {e}")
                    time.sleep(15)

    # ===== ФОРМИРОВАНИЕ МЕНЮ =====
    # Награда за лайк поста группы (защита от повторного лайка, бана, блокировки)
    def handle_like_add(self, event):
        try:
            liker_id = event.object.liker_id
            object_type = event.object.object_type
            object_owner_id = event.object.object_owner_id
            object_id = event.object.object_id

            if object_type != 'post':
                return
            if int(object_owner_id) != -GROUP_ID:
                return
            if liker_id <= 0:
                return
            if self.db.is_banned(liker_id) or self.db.is_mail_blocked(liker_id):
                return

            self.flush_user(liker_id)
            user = self.db.get_user(liker_id)
            if not user:
                return

            liked_posts = user.get('liked_posts') or []
            post_id_str = str(object_id)
            if post_id_str in liked_posts:
                return

            reward = LIKE_REWARD
            user['balance'] += reward
            user['total_earned'] += reward
            liked_posts.append(post_id_str)
            user['liked_posts'] = liked_posts
            self.db.update_user(liker_id, user)

            self.send_message(
                liker_id,
                f"❤️Йоу, cпасибо за лайк!\nТы получаешь {self.format_currency(reward)} на баланс!"
            )
            self.send_message(ADMIN_ID, f"{self.get_user_name(liker_id)} поставил лайк на пост")
        except Exception as e:
            print(f"❌ Ошибка в handle_like_add: {e}")

    def get_menu_data(self, user_id):
        user = self.get_buffered_user(user_id)
        balance = self.format_currency(user['balance'])
        clicks = user['clicks']
        status_name, status_emoji = self.get_status(clicks)
        user_name = self.get_user_name(user_id)
        creator_name = self.get_user_name(ADMIN_ID)
        p_lvl = user.get('prestige_level', 1)
        p_mult = PRESTIGE_MULTIPLIERS.get(p_lvl, 1.0)

        badbotik_text = ""
        if self.is_admin(user_id):
            badbotik_balance = self.get_badbotik_balance()
            if badbotik_balance is not None:
                badbotik_text = f"\n👑 Баланс @badbotik: {self.format_currency(badbotik_balance)}"

        if user['balance'] < 1000000000:
            greeting = "Ты только в начале пути, фармер!"
            action = "Жми *Кликнуть*, чтобы заработать первые миллионы."
        elif user['balance'] < 1000000000000000:
            greeting = "Неплохо накопилю, уже чувствуешь себя богачом?"
            action = "Не забывай выводить деньги в @badbotik или качать престиж!"
        else:
            greeting = "ого, да ты настоящий монстр!"
            action = "Может хватит кликать и пора выводить?"

        text = f"""👋 {greeting}

👤 Игрок: {user_name}
💰 Баланс: {balance}
👆 Клики: {clicks}
🏆 Статус: {status_name} {status_emoji}
🔮 Престиж: {p_lvl} (x{p_mult}){badbotik_text}

💡 {action}

👑 Создатель: {creator_name}"""

        keyboard = {
            "one_time": False,
            "buttons": [
                [{"action": {"type": "text", "label": "👆 Кликнуть карпика"}, "color": "positive"}],
                [{"action": {"type": "text", "label": "💸 Вывести все"}, "color": "primary"},
                 {"action": {"type": "text", "label": "🔥 Бонус"}, "color": "secondary"}],
                [{"action": {"type": "text", "label": "💰 Бонус дня"}, "color": "secondary"},
                 {"action": {"type": "text", "label": "🐥 Престиж"}, "color": "primary"}],
                [{"action": {"type": "text", "label": "📊 Статистика и Помощь"}, "color": "negative"}]
            ]
        }
        return text, MENU_PHOTO, keyboard

    def get_prestige_data(self, user_id):
        self.flush_user(user_id)
        user = self.db.get_user(user_id)
        lvl = user.get('prestige_level', 1) or 1
        pts = user.get('prestige_points', 0) or 0
        mult = PRESTIGE_MULTIPLIERS.get(lvl, 1.0)
        clicks = user.get('clicks', 0) or 0
        balance = user.get('balance', 0) or 0
        try:
            photo = PRESTIGE_PHOTO
        except NameError:
            photo = MENU_PHOTO

        if lvl >= 5:
            text = "🔮 Престиж\n\nТы достиг максимального (5) уровня престижа!\nМножитель: x2.5. Ты легенда!"
            inline_kb = {"inline": True, "buttons": [[{"action": {"type": "callback", "label": "🔙 В меню", "payload": json.dumps({"command": "back_to_menu"})}, "color": "negative"}]]}
            return text, photo, inline_kb

        next_lvl = lvl + 1
        next_mult = PRESTIGE_MULTIPLIERS.get(next_lvl, mult)
        req_clicks, req_balance = PRESTIGE_REQS.get(next_lvl, (0, 0))

        last_prestige = user.get('last_prestige')
        cooldown_text = ""
        can_prestige = True
        if last_prestige and last_prestige != '0':
            try:
                last_time = datetime.fromisoformat(last_prestige)
                time_passed = datetime.now() - last_time
                if time_passed < timedelta(days=7):
                    can_prestige = False
                    remaining = timedelta(days=7) - time_passed
                    days = remaining.days
                    hours = remaining.seconds // 3600
                    cooldown_text = f"\n⏰ Следующий престиж доступен через: {days}д {hours}ч"
            except ValueError:
                pass

        clicks_ok = clicks >= req_clicks
        balance_ok = balance >= req_balance

        text = f"🔮 Престиж - возможность прокачать свой множитель клика\n\n"
        text += f"Текущий престиж: {lvl} (множитель x{mult})\n"
        text += f"Кликов накопилось: {clicks:,} (нужно {req_clicks:,})\n"
        text += f"Баланс: {self.format_currency(balance)} (нужно {self.format_currency(req_balance)})\n\n"

        inline_kb = {"inline": True, "buttons": []}
        if not clicks_ok or not balance_ok:
            text += "❌ Недостаточно ресурсов"
            inline_kb["buttons"].append([{"action": {"type": "callback", "label": "🔙 В меню", "payload": json.dumps({"command": "back_to_menu"})}, "color": "negative"}])
            return text, photo, inline_kb
        if not can_prestige:
            text += f"Ты уже повышал престиж недавно!{cooldown_text}"
            inline_kb["buttons"].append([{"action": {"type": "callback", "label": "🔙 В меню", "payload": json.dumps({"command": "back_to_menu"})}, "color": "negative"}])
            return text, photo, inline_kb

        burn_balance = int(balance * 0.8)
        balance_left = balance - burn_balance

        text += "Что ты получишь:\n"
        text += f"• Уровень престижа: {lvl} → {next_lvl}\n"
        text += f"• Множитель клика: x{mult} → x{next_mult} (навсегда!)\n"
        text += f"• Очков престижа: +1\n\n"
        text += "Что потеряешь:\n"
        text += f"• Клики: {clicks:,} → {clicks - req_clicks:,} (-{req_clicks:,})\n"
        text += f"• Баланс: {self.format_currency(balance)} → {self.format_currency(balance_left)} (-80%)\n"
        text += "• Индивидуальная сумма клика: сбросится\n\n"
        inline_kb["buttons"].append([{"action": {"type": "callback", "label": "✅ Подтвердить", "payload": json.dumps({"command": "confirm_prestige"})}, "color": "positive"}])
        inline_kb["buttons"].append([{"action": {"type": "callback", "label": "🔙 Отмена", "payload": json.dumps({"command": "back_to_menu"})}, "color": "negative"}])
        return text, photo, inline_kb

    def get_stats_inline_kb(self):
        return {"inline": True, "buttons": [
            [{"action": {"type": "callback", "label": "🏆 Топ", "payload": json.dumps({"command": "show_top"})}, "color": "secondary"},
             {"action": {"type": "callback", "label": "📊 Прогресс", "payload": json.dumps({"command": "show_progress"})}, "color": "secondary"}],
            [{"action": {"type": "callback", "label": "📝 Отзывы", "payload": json.dumps({"command": "show_reviews"})}, "color": "secondary"},
             {"action": {"type": "callback", "label": "❓ Помощь", "payload": json.dumps({"command": "show_help"})}, "color": "secondary"}],
            [{"action": {"type": "open_link", "label": "💬 Чатик", "link": "https://vk.me/join/p9LwzG5HJnn2mO3ODuJgPzrUpBUhN4oodjE="}}],
            [{"action": {"type": "callback", "label": "🔙 В меню", "payload": json.dumps({"command": "back_to_menu"})}, "color": "negative"}]
        ]}

    def get_stats_back_kb(self):
        return {"inline": True, "buttons": [
            [{"action": {"type": "callback", "label": "🔙 Назад", "payload": json.dumps({"command": "back_to_stats"})}, "color": "secondary"}]
        ]}

    # ===== ОТПРАВКА СООБЩЕНИЙ =====
    @retry_on_error(max_retries=5, delay=3)
    def send_message(self, peer_id, text, attachment=None, keyboard=None, inline_keyboard=None, reply_to=None, template=None):
        if DEBUG:
            print(f"📤 Отправка: {text[:50]}...")
        if not text:
            text = "✅"
        try:
            kb_to_send = None
            if inline_keyboard:
                kb_to_send = json.dumps(inline_keyboard, ensure_ascii=False)
            elif keyboard:
                kb_to_send = json.dumps(keyboard, ensure_ascii=False)
            params = {
                "peer_id": peer_id,
                "message": text,
                "random_id": random.randint(1, 999999999),
                "attachment": attachment,
                "keyboard": kb_to_send,
                "reply_to": reply_to if reply_to else None
            }
            if template:
                params["template"] = json.dumps(template, ensure_ascii=False)
            return self.vk.messages.send(**params)
        except vk_api.exceptions.ApiError as e:
            if e.code == 901:
                return None
            elif e.code == 9:
                time.sleep(10)
                return self.send_message(peer_id, text, attachment, keyboard, inline_keyboard, reply_to, template)
            else:
                raise
        except Exception:
            raise

    def safe_api_request(self, url, payload, timeout=15):
        """Безопасный запрос к API Badbotik с глобальным кулдауном"""
        with self.api_lock:
            now = time.time()
            # Ждем, если с прошлого запроса прошло меньше 2 секунд
            if now - self.api_last_request_time < 2.0:
                wait = 2.0 - (now - self.api_last_request_time)
                time.sleep(wait)
            
            # Обновляем время ПОСЛЕ ожидания, прямо перед отправкой
            self.api_last_request_time = time.time()
        
        try:
            r = requests.post(url, json=payload, timeout=timeout, headers={'Content-Type': 'application/json'})
            return r
        except Exception as e:
            print(f"❌ Ошибка сети при запросе к API: {e}")
            return None        

    def safe_edit(self, user_id, peer_id, msg_id, text, attachment=None, keyboard=None, inline_keyboard=None):
        now = time.time()
        last_user = self.last_edit_time.get(user_id, 0)
        if now - last_user < 1.5:
            return False
        peer_key = f"peer_{peer_id}"
        last_peer = self.last_edit_time.get(peer_key, 0)
        if now - last_peer < 0.2:
            return False
        try:
            kb_to_send = None
            if inline_keyboard:
                kb_to_send = json.dumps(inline_keyboard, ensure_ascii=False)
            elif keyboard:
                kb_to_send = json.dumps(keyboard, ensure_ascii=False)
            params = {"peer_id": peer_id, "message_id": msg_id, "message": text}
            if attachment:
                params["attachment"] = attachment
            if kb_to_send:
                params["keyboard"] = kb_to_send
            self.vk.messages.edit(**params)
            self.last_edit_time[user_id] = now
            self.last_edit_time[peer_key] = now
            return True
        except vk_api.exceptions.ApiError as e:
            if e.code == 9:
                print(f"⚠️ Flood control на edit, блок на 5 сек")
                self.last_edit_time[user_id] = now + 5
                self.last_edit_time[peer_key] = now + 5
                return False
            print(f"❌ Ошибка редактирования: {e}")
            return False

    def send_back_keyboard(self, peer_id, text, attachment=None):
        inline_kb = {"inline": True, "buttons": [[{"action": {"type": "callback", "label": "🔙 Вернуться в меню", "payload": json.dumps({"command": "back_to_menu"})}, "color": "negative"}]]}
        self.send_message(peer_id, text, attachment=attachment, inline_keyboard=inline_kb)

    def send_keyboard(self, peer_id, user_id):
        text, photo, keyboard = self.get_menu_data(user_id)
        self.send_message(peer_id, text, attachment=photo, keyboard=keyboard)

    # FIX #23: корректное отображение при streak=7
    def send_daily_bonus(self, peer_id, user_id):
        try:
            old_msg_id = self.daily_msg_ids.pop(user_id, None)
            if old_msg_id:
                try:
                    self.vk.messages.delete(message_ids=old_msg_id, delete_for_all=True)
                except Exception:
                    pass

            bonus_data = self.get_daily_bonus_data(user_id)
            can_claim = bonus_data['can_claim']
            streak = bonus_data['streak']
            next_time = bonus_data['next_available_time']

            raw_photo = DAILY_BONUS_PHOTO
            photo_id = raw_photo.replace("photo", "", 1) if raw_photo.startswith("photo") else raw_photo

            rewards_text = {1: "100мк$", 2: "150мк$", 3: "250мк$", 4: "300мк$",
                            5: "350мк$", 6: "400мк$", 7: "500мк$"}

            elements = []
            cycle_complete = (streak >= 7 and not can_claim)

            for day in range(1, 8):
                if cycle_complete:
                    desc = f"✅ Получено\n+{rewards_text[day]}"
                    btn = [{"action": {"type": "callback", "label": "✅ Получено", "payload": json.dumps({"command": "dummy_daily", "day": day})}, "color": "secondary"}]
                elif day < streak:
                    desc = f"✅ Получено\n+{rewards_text[day]}"
                    btn = [{"action": {"type": "callback", "label": "✅ Получено", "payload": json.dumps({"command": "dummy_daily", "day": day})}, "color": "secondary"}]
                elif day == streak and can_claim:
                    desc = f"🎁 Доступно!\n+{rewards_text[day]}"
                    btn = [{"action": {"type": "callback", "label": "Забрать", "payload": json.dumps({"command": "claim_daily"})}, "color": "positive"}]
                elif day == streak and not can_claim:
                    desc = f"✅ Получено\n+{rewards_text[day]}"
                    btn = [{"action": {"type": "callback", "label": "✅ Получено", "payload": json.dumps({"command": "dummy_daily", "day": day})}, "color": "secondary"}]
                elif day == streak + 1 and not can_claim and next_time:
                    remaining = next_time - datetime.now()
                    hours = remaining.seconds // 3600
                    minutes = (remaining.seconds % 3600) // 60
                    desc = f"Через {hours}ч {minutes}м\nПолучишь +{rewards_text[day]}"
                    wait_phrases = [f"⏰ Жди, Карп сказал", f"⏰ Карп следит за тобой",
                                    f"⏰ {hours}ч — приказ Карпа", f"⏰ Терпи, Карп в тебя верит",
                                    f"⏰ Жди {hours}ч {minutes}м", f"⏰ Скоро! {hours}ч 🔥"]
                    btn = [{"action": {"type": "callback", "label": random.choice(wait_phrases), "payload": json.dumps({"command": "dummy_daily", "day": day})}, "color": "secondary"}]
                else:
                    desc = f"Награда +{rewards_text[day]}"
                    locked_phrases = ["🔒 Карп не пустил", "🔒 Карп закрыл", "🔒 Карп сказал нет",
                                      "🔒 Карп спит", "🔒 Это к Карпу", "🔒 Жди Карпа",
                                      "🔒 Рано, брат", "🔒 Не время, воин", "🔒 Закрыто, брат",
                                      "🔒 Не сегодня", "🔒 Иди кликай", "🔒 Спи, рано", "🔒 ???"]
                    btn = [{"action": {"type": "callback", "label": random.choice(locked_phrases), "payload": json.dumps({"command": "dummy_daily", "day": day})}, "color": "negative"}]
                elements.append({"title": f"День {day}", "description": desc, "photo_id": photo_id, "buttons": btn})

            if cycle_complete and next_time:
                remaining = next_time - datetime.now()
                hours = remaining.seconds // 3600
                minutes = (remaining.seconds % 3600) // 60
                header = f"🎁 Цикл пройден! Новый цикл через {hours}ч {minutes}м (начнётся с Дня 1)"
            else:
                header = f"🎁 Ежедневка! ({streak}/7)"

            template = {"type": "carousel", "elements": elements}
            result = self.send_message(peer_id, header, template=template)
            if result:
                self.daily_msg_ids[user_id] = result
        except Exception as e:
            print(f"❌ Ошибка send_daily_bonus: {e}")

    def send_stats_menu(self, peer_id):
        self.send_message(peer_id, "📊 Выбери раздел:", inline_keyboard=self.get_stats_inline_kb())

    def send_prestige_menu(self, peer_id, user_id):
        text, photo, inline_kb = self.get_prestige_data(user_id)
        self.send_message(peer_id, text, attachment=photo, inline_keyboard=inline_kb)

    # ===== АДМИН-КОМАНДЫ =====
    @command_handler
    def send_admin_commands(self, peer_id):
        click_reward = self.get_click_reward()
        commands_text = f"""

• /game [сумма] - запустить игру "Угадай число"
• /gameoff - остановить игру
• /post [ссылка] [текст] - разослать пост всем
• /Ras [текст] - разослать текст всем
• /rastest [текст] - тест (только вам)
• /blockras [ID] - запретить рассылку
• /unblockras [ID] - разрешить рассылку
• /blocklist - список заблокированных
• /sync - синхронизировать всех диалогов в БД
• /postgame - запустить игру в комментариях

🔧 Управление:
• /off - выключить бота
• /on - включить бота
• /restart - перезапустить
• /addadmin [ID] - добавить админа
• /removeadmin [ID] - удалить админа
• /listadmin - список админов
• /adminlogs - логи
• /ping - проверить состояние бота
• /setmin [сумма] - изменить минимум вывода
• /setclick [сумма] - изменить глобальную сумму клика
• /newclick [ссылка/ID] [сумма] - установить сумму клика для конкретного пользователя
• /give [сумма] - выдать себе

Текущая глобальная сумма клика: {self.format_click_reward(click_reward)}

• /clear [ID] - очистить баланс (клики сохраняются)
• /clearall - очистить баланс всех (клики сохраняются)
• /resetclicks - сбросить клики у всех (баланс сохраняется)
• /delete [ID] - удалить игрока
• /deleteall - удалить всех
• /ban [ссылка/ID] [причина] - забанить
• /unban [ссылка/ID] - разбанить

• /reset [ID] - сбросить капчу
• /resetall - сбросить всем
• addpromo [код] [активаций] [сумма] - создать
• delpromo [код] - удалить
• listpromo - список
• /stats - статистика бота
• /list - список игроков
• /setlog [ID] - установить чат логов
• /getlog - показать чат

🗄️ БД:
• /db - настройки БД
• /db_backup - бэкап
• /db_stats - статистика
• /db_clear - очистить всех
• /db_optimize - оптимизировать

• /postnotify - вкл/выкл оповещения
"""
        self.send_message(peer_id, commands_text)

    @command_handler
    def send_db_settings(self, peer_id):
        try:
            db_size = os.path.getsize("bot_database.db") if os.path.exists("bot_database.db") else 0
            db_size_mb = round(db_size / 1024 / 1024, 2)
            users_count = len(self.db.get_all_users())
            promos_count = len(self.db.get_all_promocodes())
            admins_count = len(self.db.get_extra_admins())
            text = f"""
Файл: bot_database.db
Размер: {db_size_mb} MB

Статистика:
👥 Пользователей: {users_count}
🎫 Промокодов: {promos_count}
👑 Доп. админов: {admins_count}

• /db_backup - создать бэкап БД
• /db_stats - статистика БД
• /db_clear - очистить всех пользователей
• /db_optimize - оптимизировать БД"""
            self.send_message(peer_id, text)
        except Exception as e:
            print(f"❌ Ошибка send_db_settings: {e}")

    @command_handler
    def db_backup(self, peer_id):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"bot_database_backup_{timestamp}.db"
            shutil.copy2("bot_database.db", backup_name)
            self.send_message(peer_id, f"📦 Бэкап создан: {backup_name}\nФайл сохранён в папке с ботом")
        except Exception as e:
            self.send_message(peer_id, f"❌ Ошибка бэкапа: {e}")

    @command_handler
    def db_stats(self, peer_id):
        try:
            conn = self.db.get_connection()
            if not conn:
                self.send_message(peer_id, "❌ Ошибка подключения к БД")
                return
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            total_users = cursor.fetchone()[0]
            cursor.execute("SELECT SUM(balance) FROM users")
            total_balance = cursor.fetchone()[0] or 0
            cursor.execute("SELECT SUM(clicks) FROM users")
            total_clicks = cursor.fetchone()[0] or 0
            cursor.execute("SELECT user_id, balance, clicks FROM users ORDER BY balance DESC LIMIT 5")
            top_users = cursor.fetchall()
            cursor.execute("SELECT COUNT(*) FROM promocodes")
            total_promos = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM extra_admins")
            total_admins = cursor.fetchone()[0]
            db_size = os.path.getsize("bot_database.db") if os.path.exists("bot_database.db") else 0
            db_size_mb = round(db_size / 1024 / 1024, 2)
            conn.close()
            text = f"""
Размер: {db_size_mb} MB

Пользователи:
Всего: {total_users}
Общий баланс: {self.format_currency(total_balance)}
Всего кликов: {total_clicks}

🏆 Топ 5 по балансу:
"""
            for i, (uid, bal, clicks) in enumerate(top_users, 1):
                name = self.get_user_name(uid)
                text += f"{i}. {name} - {self.format_currency(bal)} (кликов: {clicks})\n"
            text += f"\n🎫 Промокодов: {total_promos}\n👑 Доп. админов: {total_admins}"
            self.send_message(peer_id, text)
        except Exception as e:
            self.send_message(peer_id, f"❌ Ошибка: {e}")

    @command_handler
    def db_clear(self, peer_id):
        keyboard = {"one_time": False, "buttons": [[{"action": {"type": "text", "label": "✅ ДА, ОЧИСТИТЬ"}, "color": "negative"}, {"action": {"type": "text", "label": "🔙 НАЗАД"}, "color": "secondary"}]]}
        self.send_message(peer_id, "⚠️ ВНИМАНИЕ! Ты уверен, что хочешь удалить ВСЕХ пользователей?\nЭто действие необратимо!", keyboard=keyboard)

    @command_handler
    def db_clear_confirm(self, peer_id):
        try:
            self.db.clear_all_users_except_admin(ADMIN_ID)
            with self.click_buffer_lock:
                self.click_buffer.clear()
            self.send_message(peer_id, "✅ Все пользователи (кроме главного админа) удалены!")
        except Exception as e:
            self.send_message(peer_id, f"❌ Ошибка: {e}")

    @command_handler
    def db_optimize(self, peer_id):
        try:
            old_size = os.path.getsize("bot_database.db") if os.path.exists("bot_database.db") else 0
            conn = self.db.get_connection()
            if not conn:
                self.send_message(peer_id, "❌ Ошибка подключения к БД")
                return
            cursor = conn.cursor()
            cursor.execute("VACUUM")
            conn.commit()
            conn.close()
            new_size = os.path.getsize("bot_database.db") if os.path.exists("bot_database.db") else 0
            old_mb = round(old_size / 1024 / 1024, 2)
            new_mb = round(new_size / 1024 / 1024, 2)
            self.send_message(peer_id, f"⚡ База данных оптимизирована!\nБыло: {old_mb} MB\nСтало: {new_mb} MB")
        except Exception as e:
            self.send_message(peer_id, f"❌ Ошибка оптимизации: {e}")

    # ===== API БОТА НИЩИЙ =====
    def get_badbotik_balance(self):
        try:
            r = self.safe_api_request(f"{API_URL}/balance", {'token': API_TOKEN}, timeout=10)
            if r.status_code == 200:
                return r.json().get('answer', {}).get('balance', 0)
        except Exception:
            pass
        return None

    @command_handler
    def handle_botbalance_command(self, user_id, peer_id):
        if not self.is_admin(user_id):
            self.send_message(peer_id, "Error")
            return
        try:
            r = requests.post(f"{API_URL}/balance", json={'token': API_TOKEN}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                balance = data.get('answer', {}).get('balance', 0)
                bot_id = data.get('answer', {}).get('id', 0)
                self.send_message(peer_id, f"Баланс: {self.format_currency(balance)}\nID: @id{bot_id}")
            else:
                self.send_message(peer_id, "Ошибка получения баланса")
        except Exception as e:
            self.send_message(peer_id, f"Ошибка API: {e}")

    @command_handler
    def handle_payments_command(self, user_id, peer_id):
        if not self.is_admin(user_id):
            self.send_message(peer_id, "Error")
            return
        try:
            r = self.safe_api_request(f"{API_URL}/getHistory", {'token': API_TOKEN, 'count': 10}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                history = data.get('answer', {}).get('history', [])
                if not history:
                    self.send_message(peer_id, "Нет выплат")
                    return
                text = "Последние выводы:\n\n"
                total = 0
                for item in history:
                    user_name = self.get_user_name(item['id'])
                    amount = self.format_currency(item['amount'])
                    time_str = datetime.fromtimestamp(item['time']).strftime('%d.%m %H:%M')
                    text += f"{user_name} -> {amount} ({time_str})\n"
                    total += item['amount']
                text += f"\nВсего выведено: {self.format_currency(total)}"
                self.send_message(peer_id, text)
            else:
                self.send_message(peer_id, "Ошибка получения истории")
        except Exception as e:
            self.send_message(peer_id, f"Ошибка: {e}")

    @command_handler
    def handle_withdrawstats_command(self, user_id, peer_id):
        if not self.is_admin(user_id):
            self.send_message(peer_id, "Error")
            return
        try:
            r = self.safe_api_request(f"{API_URL}/getHistory", {'token': API_TOKEN, 'count': 10}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                history = data.get('answer', {}).get('history', [])
                if not history:
                    self.send_message(peer_id, "Нет данных для статистики")
                    return
                total = sum(item['amount'] for item in history)
                count = len(history)
                avg = total // count if count > 0 else 0
                max_amount = max(item['amount'] for item in history) if history else 0
                min_amount = min(item['amount'] for item in history) if history else 0
                text = f"Статистика выводов:\n\nВсего выведено: {self.format_currency(total)}\nКоличество выводов: {count}\nСредний чек: {self.format_currency(avg)}\nМаксимальный: {self.format_currency(max_amount)}\nМинимальный: {self.format_currency(min_amount)}"
                self.send_message(peer_id, text)
            else:
                self.send_message(peer_id, "Ошибка получения статистики")
        except Exception as e:
            self.send_message(peer_id, f"Ошибка: {e}")

    @command_handler
    def handle_api_command(self, user_id, peer_id):
        if not self.is_admin(user_id):
            self.send_message(peer_id, "Error")
            return
        text = """Доступные методы API бота Нищий:

1. /botbalance - проверить баланс бота Нищий
2. /payments - показать последние 10 выплат через API
3. /withdrawstats - статистика по выводам (сумма, количество, средний чек)
"""
        self.send_message(peer_id, text)

    def check_bot_balance(self):
        try:
            r = self.safe_api_request(f"{API_URL}/balance", {'token': API_TOKEN}, timeout=10)
            if r.status_code == 200:
                balance = r.json().get('answer', {}).get('balance', 0)
                if balance < 10000000000000:
                    self.send_message(ADMIN_ID, f"Внимание! Баланс бота Нищий: {self.format_currency(balance)}. Рекомендуется пополнить.")
                return balance
        except Exception:
            pass
        return None

    @command_handler
    def handle_newclick_command(self, admin_id, peer_id, text):
        if not self.is_admin(admin_id):
            self.send_message(peer_id, "Error")
            return
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            self.send_message(peer_id, "/newclick [ссылка/ID] [сумма]")
            return
        target_input = parts[1]
        amount_str = parts[2]
        target_id = self.parser.extract_user_id(target_input)
        if not target_id:
            self.send_message(peer_id, "Не удалось определить пользователя")
            return
        amount = self.parse_amount(amount_str)
        if amount <= 0:
            self.send_message(peer_id, "Сумма должна быть больше 0 (формат: 1мк, 2мм)")
            return
        if self.set_user_click_reward(target_id, amount):
            self.db.add_admin_log(admin_id, 'изменение_индивидуальной_суммы_клика', target_id, f'{self.format_click_reward(amount)}')
            self.send_message(peer_id, f"Установлена сумма клика: {self.format_click_reward(amount)}")
        else:
            self.send_message(peer_id, "❌ Ошибка при обновлении")

    # FIX #9: /ping только для админа
    @command_handler
    def handle_ping_command(self, user_id, peer_id):
        if not self.is_admin(user_id):
            self.send_message(peer_id, "Error")
            return
        try:
            start = time.time()
            self.vk.users.get(user_ids=ADMIN_ID)
            vk_time = time.time() - start
            db_ok = True
            try:
                conn = self.db.get_connection()
                if conn:
                    conn.cursor().execute("SELECT 1")
                    conn.close()
                else:
                    db_ok = False
            except Exception:
                db_ok = False
            lp_ok = self.longpoll is not None
            wd_ok = self.watchdog.running
            status = "✅ Всё работает"
            if not db_ok:
                status = "⚠️ Проблема с БД"
            if not lp_ok:
                status = "⚠️ Проблема с LongPoll"
            if not wd_ok:
                status = "⚠️ Watchdog не работает"
            message = f"""🏓PONG

• API VK: {vk_time:.3f} сек (OK)
• База данных: {'✅' if db_ok else '❌'}
• LongPoll: {'✅' if lp_ok else '❌'}
• Watchdog: {'✅' if wd_ok else '❌'}
• Бот включен: {'✅' if self.bot_enabled else '❌'}

📦 Всего пользователей: {len(self.db.get_all_users())}
⏰ Время сервера: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}

Статус: {status}"""
            self.send_message(peer_id, message)
        except Exception as e:
            self.send_message(peer_id, f"❌ Ошибка при выполнении ping: {e}")

    @command_handler
    def handle_post_command(self, user_id, peer_id, text):
        if not self.is_admin(user_id):
            self.send_message(peer_id, "Error")
            return
        post_attachment = self.extract_post_from_text(text)
        if not post_attachment:
            self.send_message(peer_id, "Не удалось распознать пост\nОтправьте ссылку вида: vk.com/wall-238285284_123456")
            return
        custom_text = re.sub(r'^/post\s*', '', text, flags=re.IGNORECASE)
        patterns = [r'(?:https?://)?(?:www\.)?vk\.(?:com|ru)/wall[_-]?\d+_\d+', r'wall[_-]?\d+_\d+', r'vk\.com/wall[_-]?\d+_\d+']
        for pattern in patterns:
            custom_text = re.sub(pattern, '', custom_text, flags=re.IGNORECASE)
        custom_text = custom_text.strip()
        self.send_message(peer_id, "В процессе...")
        self.send_post_to_all(user_id, post_attachment, custom_text)

    @command_handler
    def handle_posttest_command(self, user_id, peer_id, text):
        if not self.is_admin(user_id):
            self.send_message(peer_id, "Error")
            return
        post_attachment = self.extract_post_from_text(text)
        if not post_attachment:
            self.send_message(peer_id, "Не удалось распознать пост")
            return
        custom_text = re.sub(r'^/posttest\s*', '', text, flags=re.IGNORECASE)
        patterns = [r'(?:https?://)?(?:www\.)?vk\.(?:com|ru)/wall[_-]?\d+_\d+', r'wall[_-]?\d+_\d+', r'vk\.com/wall[_-]?\d+_\d+']
        for pattern in patterns:
            custom_text = re.sub(pattern, '', custom_text, flags=re.IGNORECASE)
        custom_text = custom_text.strip()
        try:
            message = custom_text if custom_text else "test"
            self.vk.messages.send(user_id=user_id, message=message, random_id=random.randint(1, 999999999), attachment=post_attachment)
            self.send_message(peer_id, "Тестовый пост отправлен!")
        except Exception as e:
            self.send_message(peer_id, f"❌ Ошибка: {e}")

    def extract_post_from_text(self, text):
        text = text.strip()
        patterns = [r'(?:https?://)?(?:www\.)?vk\.(?:com|ru)/wall[_-]?(\d+)_(\d+)', r'wall[_-]?(\d+)_(\d+)', r'vk\.com/wall[_-]?(\d+)_(\d+)']
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                owner_id = match.group(1)
                post_id = match.group(2)
                try:
                    owner_int = int(owner_id)
                    if owner_int > 0:
                        owner_id = f"-{owner_int}"
                except Exception:
                    pass
                return f"wall{owner_id}_{post_id}"
        return None

    # FIX #24: используем переданный текст, а не "test"
    @command_handler
    def handle_rastest_command(self, user_id, peer_id, text):
        if not self.is_admin(user_id):
            self.send_message(peer_id, "Error")
            return
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            self.send_message(peer_id, "/rastest [текст]")
            return
        message_text = parts[1].strip()
        if len(message_text) < 3:
            self.send_message(peer_id, "❌ Текст слишком короткий (минимум 3 символа)")
            return
        try:
            self.vk.messages.send(user_id=user_id, message=message_text, random_id=random.randint(1, 999999999))
            self.send_message(peer_id, "Тестовое сообщение отправлено!")
        except Exception as e:
            self.send_message(peer_id, f"❌ Ошибка: {e}")

    def send_message_with_reply(self, peer_id, reply_to_id, text):
        try:
            self.vk.messages.send(peer_id=peer_id, message=text, random_id=random.randint(1, 999999999), reply_to=reply_to_id)
        except Exception as e:
            print(f"❌ Ошибка реплая: {e}")
            self.send_message(peer_id, text)

    # ===== ИГРЫ В ЧАТЕ =====
    @command_handler
    def handle_game_command(self, user_id, peer_id, text):
        if not self.is_admin(user_id):
            self.send_message(peer_id, "Только админ может запускать игру")
            return
        parts = text.split()
        if len(parts) != 2:
            self.send_message(peer_id, "/game [сумма]")
            return
        amount = self.parse_amount(parts[1])
        if amount <= 0:
            self.send_message(peer_id, "❌ Сумма должна быть больше 0")
            return
        if self.game_manager.is_game_active(peer_id):
            self.send_message(peer_id, "❌ В этом чате уже запущена игра")
            return
        success, result = self.game_manager.start_game(peer_id, user_id, amount)
        self.send_message(peer_id, result)

    @command_handler
    def handle_gameoff_command(self, user_id, peer_id):
        if not self.is_admin(user_id):
            self.send_message(peer_id, "❌ Только админ может остановить игру")
            return
        success, result = self.game_manager.stop_game(peer_id)
        self.send_message(peer_id, result)

    @command_handler
    def handle_game_guess(self, event):
        user_id = event.object.message['from_id']
        peer_id = event.object.message['peer_id']
        text = event.object.message['text'].strip()
        if not self.game_manager.is_game_active(peer_id):
            return False
        try:
            user_info = self.vk.users.get(user_ids=user_id)[0]
            user_name = f"{user_info['first_name']} {user_info['last_name']}"
        except Exception:
            user_name = f"Пользователь {user_id}"
        result, data = self.game_manager.check_guess(peer_id, user_id, user_name, text)
        if result is None and data:
            self.send_message_with_reply(peer_id, event.object.message['id'], data)
            return True
        elif result is True:
            winner_name = data['name']
            number = data['number']
            amount = data['amount']
            players_count = data['players']
            total_guesses = data['total_guesses']
            mention = f"[id{user_id}|{winner_name}]"
            reply_text = f"🎉 {mention} отгадал число {number} и получает - {self.format_currency(amount)}!\n\n• Участников: {players_count}\n• Всего попыток: {total_guesses}\n• Приз: {self.format_currency(amount)}"
            self.send_message_with_reply(peer_id, event.object.message['id'], reply_text)
            self.db.add_admin_log(ADMIN_ID, 'победа_в_игре', user_id, f'число={number}, приз={amount}')
            return True
        elif result is None and data is None:
            return True
        return False

    # ===== АДМИН-КОМАНДЫ (бан/разбан/очистка) =====
    @command_handler
    def ban_user_command(self, admin_id, peer_id, target_id, reason):
        if target_id == admin_id:
            self.send_message(peer_id, "/ban (ссылка) (причина)")
            return
        if target_id == ADMIN_ID:
            self.send_message(peer_id, "Error")
            return
        self.flush_user(target_id)
        if self.db.ban_user(target_id, reason, admin_id):
            success_vk, vk_error = self.ban_in_group(target_id, reason)
            self.db.add_admin_log(admin_id, 'бан', target_id, reason)
            blacklist_url = f"https://vk.com/manage/blacklist?gid={GROUP_ID}"
            if success_vk:
                self.send_message(peer_id, "успешно!")
            else:
                self.send_message(peer_id, f"(Error: {vk_error})\n\n[Кликни сюда, чтобы забанить его в сообществе вручную]({blacklist_url})")
            try:
                self.send_message(target_id, f"🤡 Вы заблокированы в боте по причине: {reason}")
            except Exception:
                pass
        else:
            self.send_message(peer_id, "❌ Ошибка при бане в базе данных")

    @command_handler
    def unban_user_command(self, admin_id, peer_id, target_id):
        self.flush_user(target_id)
        if self.db.unban_user(target_id):
            self.unban_in_group(target_id)
            self.db.add_admin_log(admin_id, 'разбан', target_id, None)
            target_name = self.get_user_name(target_id)
            self.send_message(peer_id, f"Успешно!\n👤 {target_name}")
            try:
                self.send_message(target_id, f"✅ Вы разблокированы в боте")
            except Exception:
                pass
        else:
            self.send_message(peer_id, "Пользователь не был в бане")

    @command_handler
    def clear_user_balance(self, admin_id, peer_id, target_id):
        self.flush_user(target_id)
        user_data = self.db.get_user(target_id)
        if user_data:
            old = self.format_currency(user_data['balance'])
            user_data['balance'] = 0
            self.db.update_user(target_id, user_data)
            self.db.add_admin_log(admin_id, 'очистка_баланса', target_id, f'было {old}')
            self.send_message(peer_id, f"Успешно! (было: {old}). Клики сохранены.")
        else:
            self.send_message(peer_id, "Игрок не найден")

    @command_handler
    def reset_user_captcha(self, admin_id, peer_id, target_id):
        self.flush_user(target_id)
        user_data = self.db.get_user(target_id)
        if user_data:
            user_data['waiting_captcha'] = False
            user_data['captcha_word'] = None
            user_data['captcha_attachment'] = None
            user_data['clicks_since_check'] = 0
            self.db.update_user(target_id, user_data)
            self.db.add_admin_log(admin_id, 'сброс_капчи', target_id)
            self.send_message(peer_id, "готово")
        else:
            self.send_message(peer_id, "❌ Игрок не найден")

    @command_handler
    def delete_user_command(self, admin_id, peer_id, target_id):
        self.flush_user(target_id)
        conn = self.db.get_connection()
        if conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users WHERE user_id = ?", (target_id,))
            conn.commit()
            conn.close()
            with self.click_buffer_lock:
                self.click_buffer.pop(target_id, None)
            self.db.add_admin_log(admin_id, 'удаление_игрока', target_id)
            self.send_message(peer_id, "готово")
        else:
            self.send_message(peer_id, "❌ Ошибка удаления")

    def process_links(self, text):
        links = self.parser.extract_all_links(text)
        if links:
            for link in links:
                if any(bad in link.lower() for bad in ['phishing', 'spam', 'virus']):
                    return True, "⚠️ Обнаружена подозрительная ссылка!"
        return False, None

    def handle_mentions(self, text):
        return self.parser.extract_mentions(text)

    # FIX #7: блокировка разрушительных команд в TEST_MODE
    def _is_test_mode_forbidden(self, text_lower):
        if not TEST_MODE:
            return False
        if text_lower in TEST_MODE_FORBIDDEN_EXACT:
            return True
        for prefix in TEST_MODE_FORBIDDEN_PREFIX:
            if text_lower.startswith(prefix):
                return True
        return False

    # ===== ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ =====
    @command_handler
    def handle_message(self, event):
        self.watchdog.ping()

        try:
            user_id = event.object.message['from_id']
            peer_id = event.object.message['peer_id']
            text = event.object.message['text'].strip()
            text_lower = text.lower()
        except Exception:
            return

        if not text:
            return
        if len(text) > 1000:
            self.send_message(peer_id, "❌ Сообщение слишком длинное.")
            return
        if self.db.is_banned(user_id) and not self.is_admin(user_id):
            self.send_message(peer_id, "🚫 Доступ запрещён.")
            return

        # FIX #7: запрет разрушительных команд в тестовом режиме
        if self._is_test_mode_forbidden(text_lower):
            self.send_message(peer_id, "🧪 В тестовом режиме эта команда запрещена")
            return

        is_suspicious, warning = self.process_links(text)
        if is_suspicious and not self.is_admin(user_id):
            self.send_message(peer_id, warning)
            return
        self.handle_mentions(text)

        # ===== АДМИН-КОМАНДЫ =====
        if text_lower == '/botbalance':
            self.handle_botbalance_command(user_id, peer_id); return
        if text_lower == '/payments':
            self.handle_payments_command(user_id, peer_id); return
        if text_lower == '/withdrawstats':
            self.handle_withdrawstats_command(user_id, peer_id); return
        if text_lower == '/api':
            self.handle_api_command(user_id, peer_id); return
        if text_lower.startswith('/newclick'):
            self.handle_newclick_command(user_id, peer_id, text); return
        # FIX #9: ping теперь с проверкой админа внутри
        if text_lower == '/ping':
            self.handle_ping_command(user_id, peer_id); return
        if text_lower == '/sync' and self.is_admin(user_id):
            self.send_message(peer_id, "🔄 Ожидайте...")
            threading.Thread(target=self.sync_all_dialogs, daemon=True).start(); return
        # FIX #10: /postnotify только для админа
        if text_lower == '/postnotify':
            if not self.is_admin(user_id):
                self.send_message(peer_id, "Error")
                return
            current = self.db.get_setting('post_notify_enabled', 1)
            new_val = 0 if current == 1 else 1
            self.db.set_setting('post_notify_enabled', new_val)
            status = "включено" if new_val == 1 else "выключено"
            self.send_message(peer_id, f"Оповещения о новых постах: {status}")
            return
        if text_lower == '/postgame':
            self.handle_postgame_command(user_id, peer_id); return
        if text_lower.startswith('/posttest'):
            self.handle_posttest_command(user_id, peer_id, text); return
        if text_lower.startswith('/post'):
            self.handle_post_command(user_id, peer_id, text); return
        if text_lower.startswith('/rastest'):
            self.handle_rastest_command(user_id, peer_id, text); return
        if text_lower.startswith('/gameoff'):
            self.handle_gameoff_command(user_id, peer_id); return
        if text_lower.startswith('/game'):
            self.handle_game_command(user_id, peer_id, text); return
        if self.game_manager.is_game_active(peer_id):
            try:
                guess = int(text)
                if 1 <= guess <= 100:
                    result = self.handle_game_guess(event)
                    if result:
                        return
            except ValueError:
                pass

        if self.is_admin(user_id):
            if text_lower == '/clearall':
                users = self.db.get_all_users()
                count = 0
                for user in users:
                    uid = user[0]
                    self.flush_user(uid)
                    user_data = self.db.get_user(uid)
                    if user_data:
                        user_data['balance'] = 0
                        self.db.update_user(uid, user_data)
                        count += 1
                self.db.add_admin_log(user_id, 'очистка_баланса_всем', None, f'{count} игроков')
                self.send_message(peer_id, "Успешно")
                return

            if text_lower.startswith('/clear ') or text_lower.startswith('/clear\t'):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    self.send_message(peer_id, "/clear [ID/ссылка]")
                    return
                target_id = self.parser.extract_user_id(parts[1].strip())
                if target_id:
                    self.clear_user_balance(user_id, peer_id, target_id)
                else:
                    self.send_message(peer_id, "❌ Не удалось определить пользователя")
                return

            if text_lower == '/resetall':
                users = self.db.get_all_users()
                count = 0
                for user in users:
                    uid = user[0]
                    self.flush_user(uid)
                    user_data = self.db.get_user(uid)
                    if user_data:
                        user_data['waiting_captcha'] = False
                        user_data['captcha_word'] = None
                        user_data['captcha_attachment'] = None
                        user_data['clicks_since_check'] = 0
                        self.db.update_user(uid, user_data)
                        count += 1
                self.db.add_admin_log(user_id, 'сброс_капчи_всем', None, f'{count} игроков')
                self.send_message(peer_id, "Успешно")
                return

            if text_lower.startswith('/reset ') or text_lower.startswith('/reset\t'):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    self.send_message(peer_id, "/reset [ID/ссылка]")
                    return
                target_id = self.parser.extract_user_id(parts[1].strip())
                if target_id:
                    self.reset_user_captcha(user_id, peer_id, target_id)
                else:
                    self.send_message(peer_id, "❌ Не удалось определить пользователя")
                return

            if text_lower == '/resetclicks':
                users = self.db.get_all_users()
                count = 0
                for user in users:
                    uid = user[0]
                    self.flush_user(uid)
                    user_data = self.db.get_user(uid)
                    if user_data:
                        user_data['clicks'] = 0
                        self.db.update_user(uid, user_data)
                        count += 1
                self.db.add_admin_log(user_id, 'сброс_кликов_всем', None, f'{count} игроков')
                self.send_message(peer_id, "Успешно!")
                return

            if text_lower.startswith('/ban'):
                target_id = self.parser.extract_user_id(text)
                if target_id:
                    parts = text.split(maxsplit=2)
                    reason = parts[2] if len(parts) > 2 else "Без причины"
                    self.ban_user_command(user_id, peer_id, target_id, reason)
                else:
                    self.send_message(peer_id, "/ban [ссылка] [причина]")
                return

            if text_lower.startswith('/unban'):
                target_id = self.parser.extract_user_id(text)
                if target_id:
                    self.unban_user_command(user_id, peer_id, target_id)
                else:
                    self.send_message(peer_id, "/unban [ID/ссылка]")
                return

            if text_lower.startswith('/delete'):
                parts = text.split()
                if len(parts) == 1:
                    self.send_message(peer_id, "/delete [ID/ссылка] - удалить игрока")
                    return
                target_id = self.parser.extract_user_id(' '.join(parts[1:]))
                if target_id:
                    self.delete_user_command(user_id, peer_id, target_id)
                else:
                    self.send_message(peer_id, "❌ Не удалось определить пользователя")
                return

            if text_lower.startswith('/blockras'):
                parts = text.split()
                if len(parts) == 1:
                    self.send_message(peer_id, "/blockras [ID/ссылка]")
                    return
                target_id = self.parser.extract_user_id(' '.join(parts[1:]))
                if target_id:
                    self.db.block_mail(target_id)
                    self.db.add_admin_log(user_id, 'блокировка_рассылки', target_id)
                    self.send_message(peer_id, "Успешно")
                else:
                    self.send_message(peer_id, "❌ Не удалось определить пользователя")
                return

            if text_lower.startswith('/unblockras'):
                parts = text.split()
                if len(parts) == 1:
                    self.send_message(peer_id, "/unblockras [ID/ссылка]")
                    return
                target_id = self.parser.extract_user_id(' '.join(parts[1:]))
                if target_id:
                    self.db.unblock_mail(target_id)
                    self.db.add_admin_log(user_id, 'разблокировка_рассылки', target_id)
                    self.send_message(peer_id, "Успешно")
                else:
                    self.send_message(peer_id, "❌ Не удалось определить пользователя")
                return

            if text_lower == '/blocklist':
                conn = self.db.get_connection()
                if not conn:
                    self.send_message(peer_id, "Отсутствует")
                    return
                cursor = conn.cursor()
                cursor.execute("SELECT key FROM settings WHERE key LIKE 'mail_blocked_%'")
                rows = cursor.fetchall()
                conn.close()
                if not rows:
                    self.send_message(peer_id, "📋 Нет заблокированных пользователей")
                    return
                blocked_list = "Список:\n\n"
                for row in rows:
                    uid = row[0].replace('mail_blocked_', '')
                    try:
                        blocked_list += f"• {self.get_user_name(int(uid))}\n"
                    except Exception:
                        blocked_list += f"• Пользователь {uid}\n"
                self.send_message(peer_id, blocked_list)
                return

            if text_lower.startswith('/ras'):
                parts = text.split(maxsplit=1)
                if len(parts) != 2:
                    self.send_message(peer_id, "/Ras [текст]")
                    return
                message_text = parts[1].strip()
                if len(message_text) < 5:
                    self.send_message(peer_id, "Текст слишком короткий)")
                    return
                self.send_message(peer_id, "Начинаю текстовую рассылку...")
                self.send_mail_to_all(user_id, message_text)
                return

            if text_lower == '/stats':
                users = self.db.get_all_users()
                total_users = len(users)
                total_clicks = sum(u[2] for u in users) if users else 0
                total_balance = sum(u[1] for u in users) if users else 0
                waiting = 0
                for user in users:
                    u = self.db.get_user(user[0])
                    if u and u.get('waiting_captcha'):
                        waiting += 1
                stats = f"""🐊Игроков: {total_users}
Кликов: {total_clicks}
Баланс: {self.format_currency(total_balance)}
Мин. вывод: {self.format_currency(self.min_withdraw)}
Сумма клика: {self.format_click_reward(self.get_click_reward())}
Ожидают капчу: {waiting}"""
                self.send_message(peer_id, stats)
                return

            if text_lower == '/list':
                users = self.db.get_all_users()
                if not users:
                    self.send_message(peer_id, "Отсутствует")
                    return
                players = "Список:\n\n"
                for uid, balance, clicks, _ in users[:20]:
                    name = self.get_user_name(uid)
                    players += f"{name} - {self.format_currency(balance)} (кликов: {clicks})\n"
                self.send_message(peer_id, players)
                return

            if text_lower == '/admin' or text_lower == 'админ команды':
                self.send_admin_commands(peer_id); return
            if text_lower == '/db':
                self.send_db_settings(peer_id); return
            if text_lower == '/db_backup':
                self.db_backup(peer_id); return
            if text_lower == '/db_stats':
                self.db_stats(peer_id); return
            if text_lower == '/db_clear':
                self.db_clear(peer_id); return
            if text == 'да' and self.is_admin(user_id):
                self.db_clear_confirm(peer_id); return
            if text_lower == '/db_optimize':
                self.db_optimize(peer_id); return

            if text_lower == '/off':
                self.bot_enabled = False
                self.db.add_admin_log(user_id, 'выключение_бота')
                self.send_message(peer_id, "готово!"); return
            if text_lower == '/on':
                self.bot_enabled = True
                self.db.add_admin_log(user_id, 'включение_бота')
                self.send_message(peer_id, "готово!"); return

            if text_lower == '/restart':
                self.db.add_admin_log(user_id, 'перезапуск_бота')
                self.send_message(peer_id, "🔄 Перезапуск...")
                time.sleep(1)
                self.restart_bot()
                return

            if text_lower == '/adminlogs':
                logs = self.db.get_admin_logs(30)
                if not logs:
                    self.send_message(peer_id, "отсутствуют")
                    return
                log_text = "лист действий:\n\n"
                for admin_id, action, target_id, details, timestamp in logs:
                    time_str = datetime.fromisoformat(timestamp).strftime('%d.%m %H:%M')
                    admin_name = self.get_user_name(admin_id)
                    log_text += f"{time_str} | {admin_name} | {action}"
                    if target_id:
                        log_text += f" | ID: {target_id}"
                    if details:
                        log_text += f" | {details}"
                    log_text += "\n"
                self.send_message(peer_id, log_text); return

            if text_lower.startswith('/addadmin'):
                parts = text.split()
                if len(parts) != 2:
                    self.send_message(peer_id, "/addadmin [ID]"); return
                try:
                    new_admin = int(parts[1])
                    extra_admins = self.db.get_extra_admins()
                    if new_admin in extra_admins:
                        self.send_message(peer_id, "❌ Пользователь уже является админом"); return
                    if new_admin == ADMIN_ID:
                        self.send_message(peer_id, "❌ Это главный админ"); return
                    if self.db.is_banned(new_admin):
                        self.send_message(peer_id, "❌ Пользователь забанен"); return
                    self.db.add_extra_admin(new_admin)
                    self.db.add_admin_log(user_id, 'добавление_админа', new_admin)
                    self.send_message(peer_id, "готово")
                except Exception:
                    self.send_message(peer_id, "❌ Неверный ID")
                return

            if text_lower.startswith('/removeadmin'):
                parts = text.split()
                if len(parts) != 2:
                    self.send_message(peer_id, "/removeadmin [ID]"); return
                try:
                    remove_admin = int(parts[1])
                    extra_admins = self.db.get_extra_admins()
                    if remove_admin not in extra_admins:
                        self.send_message(peer_id, "❌ Пользователь не является админом"); return
                    self.db.remove_extra_admin(remove_admin)
                    self.db.add_admin_log(user_id, 'удаление_админа', remove_admin)
                    self.send_message(peer_id, "успешно")
                except Exception:
                    self.send_message(peer_id, "❌ Неверный ID")
                return

            if text_lower == '/listadmin':
                extra_admins = self.db.get_extra_admins()
                if not extra_admins:
                    self.send_message(peer_id, "Нет дополнительных админов"); return
                admins_list = "Дополнительные админы:\n"
                for admin in extra_admins:
                    admins_list += f"• {self.get_user_name(admin)}\n"
                self.send_message(peer_id, admins_list); return

            if text_lower.startswith('/setlog'):
                parts = text.split()
                if len(parts) != 2:
                    self.send_message(peer_id, f"Текущий чат логов: {self.log_chat_id}"); return
                try:
                    self.log_chat_id = int(parts[1])
                    self.db.set_setting('log_chat_id', self.log_chat_id)
                    self.db.add_admin_log(user_id, 'изменение_чата_логов', None, str(self.log_chat_id))
                    self.send_message(peer_id, f"Чат логов: {self.log_chat_id}")
                except Exception:
                    self.send_message(peer_id, "❌ Неверный ID")
                return

            if text_lower == '/getlog':
                self.send_message(peer_id, f"Чат логов: {self.log_chat_id}"); return

            if text_lower.startswith('/setmin'):
                parts = text.split()
                if len(parts) != 2:
                    self.send_message(peer_id, f"💸 Текущий минимум: {self.format_currency(self.min_withdraw)}"); return
                amount_str = parts[1]
                if amount_str.endswith('мм'):
                    new_min = int(float(amount_str[:-2]) * 1000000000000)
                elif amount_str.endswith('мк'):
                    new_min = int(float(amount_str[:-2]) * 1000000000)
                else:
                    try:
                        new_min = int(amount_str)
                    except Exception:
                        self.send_message(peer_id, "❌ Неверный формат"); return
                if new_min <= 0:
                    self.send_message(peer_id, "❌ Минимум должен быть > 0"); return
                old_min = self.min_withdraw
                self.min_withdraw = new_min
                self.db.set_setting('min_withdraw', self.min_withdraw)
                self.db.add_admin_log(user_id, 'изменение_мин_вывода', None, f'{old_min} -> {new_min}')
                self.send_message(peer_id, f"✅ Минимум: {self.format_currency(self.min_withdraw)}"); return

            if text_lower.startswith('/setclick'):
                parts = text.split()
                if len(parts) != 2:
                    current = self.get_click_reward()
                    self.send_message(peer_id, f"💰 Текущая глобальная сумма клика: {self.format_click_reward(current)}"); return
                try:
                    new_amount = self.parse_amount(parts[1])
                    if new_amount < 1000000000:
                        self.send_message(peer_id, "❌ Сумма слишком маленькая. Минимум: 1мк$"); return
                    if new_amount > 999000000000000:
                        self.send_message(peer_id, "❌ Сумма слишком большая. Максимум: 999мм$"); return
                    old_amount = self.get_click_reward()
                    self.set_click_reward(new_amount)
                    self.db.add_admin_log(user_id, 'изменение_глобальной_суммы_клика', None, f"{self.format_click_reward(old_amount)} -> {self.format_click_reward(new_amount)}")
                    self.send_message(peer_id, f"готово!\nБыло: {self.format_click_reward(old_amount)}\nСтало: {self.format_click_reward(new_amount)}")
                except Exception as e:
                    self.send_message(peer_id, f"❌ Ошибка: {e}")
                return

            if text_lower.startswith('/give'):
                parts = text.split()
                if len(parts) != 2:
                    self.send_message(peer_id, "/give [сумма]"); return
                amount_str = parts[1]
                if amount_str.endswith('мм'):
                    amount = int(float(amount_str[:-2]) * 1000000000000)
                elif amount_str.endswith('мк'):
                    amount = int(float(amount_str[:-2]) * 1000000000)
                else:
                    try:
                        amount = int(amount_str)
                    except Exception:
                        self.send_message(peer_id, "❌ Неверный формат"); return
                if amount <= 0:
                    self.send_message(peer_id, "❌ Сумма должна быть > 0"); return
                self.flush_user(user_id)
                admin = self.db.get_user(user_id)
                if admin:
                    admin['balance'] += amount
                    admin['total_earned'] += amount
                    self.db.update_user(user_id, admin)
                    self.db.add_admin_log(user_id, 'выдача_денег', None, f'{self.format_currency(amount)}')
                    self.send_message(peer_id, f"Выдано {self.format_currency(amount)}")
                else:
                    self.send_message(peer_id, "❌ Админ не найден")
                return

            if text_lower == '/deleteall':
                try:
                    conn = self.db.get_connection()
                    if conn:
                        conn.cursor().execute("DELETE FROM users")
                        conn.commit()
                        conn.close()
                except Exception:
                    pass
                with self.click_buffer_lock:
                    self.click_buffer.clear()
                self.db.add_admin_log(user_id, 'удаление_всех_игроков')
                self.send_message(peer_id, "успешно!"); return

            if text_lower.startswith('addpromo'):
                parts = text.split()
                if len(parts) != 4:
                    self.send_message(peer_id, "addpromo [название] [кол-во активаций] [сумма]"); return
                code = parts[1].upper()
                try:
                    max_uses = int(parts[2])
                except Exception:
                    self.send_message(peer_id, "Количество активаций должно быть числом"); return
                amount_str = parts[3]
                if amount_str.endswith('мм'):
                    reward = int(float(amount_str[:-2]) * 1000000000000)
                elif amount_str.endswith('мк'):
                    reward = int(float(amount_str[:-2]) * 1000000000)
                else:
                    try:
                        reward = int(amount_str)
                    except Exception:
                        self.send_message(peer_id, "Неверный формат суммы"); return
                if reward <= 0 or max_uses <= 0:
                    self.send_message(peer_id, "❌ Награда и количество активаций должны быть > 0"); return
                if self.db.get_promocode(code):
                    self.send_message(peer_id, f"Промокод {code} уже существует"); return
                self.db.create_promocode(code, reward, max_uses)
                self.db.add_admin_log(user_id, 'создание_промокода', None, f'{code} на {self.format_currency(reward)}')
                self.send_message(peer_id, "успешно!"); return

            if text_lower.startswith('delpromo'):
                parts = text.split()
                if len(parts) != 2:
                    self.send_message(peer_id, "delpromo [код]"); return
                code = parts[1].upper()
                if not self.db.get_promocode(code):
                    self.send_message(peer_id, f"Промокод {code} не найден"); return
                self.db.delete_promocode(code)
                self.db.add_admin_log(user_id, 'удаление_промокода', None, code)
                self.send_message(peer_id, "успешно!"); return

            if text_lower == 'listpromo':
                promocodes = self.db.get_all_promocodes()
                if not promocodes:
                    self.send_message(peer_id, "Нет активных промокодов"); return
                promo_list = "промо:\n\n"
                for code, data in promocodes.items():
                    promo_list += f"{code} - {self.format_currency(data['reward'])} (осталось: {data['max_uses'] - data['used_count']}/{data['max_uses']})\n"
                self.send_message(peer_id, promo_list); return

        # ===== КОМАНДЫ ТОЛЬКО ДЛЯ БЕСЕД =====
        is_chat = peer_id > 2000000000
        if is_chat:
            reply_msg_id = None
            cmid = event.object.message.get('conversation_message_id')
            if cmid:
                try:
                    res = self.vk.messages.getByConversationMessageId(peer_id=peer_id, conversation_message_ids=cmid)
                    if res['items']:
                        reply_msg_id = res['items'][0]['id']
                except Exception as e:
                    print(f"⚠️ Не удалось получить message_id: {e}")

            if text_lower in ['топик']:
                users = self.db.get_all_users()
                filtered = [u for u in users if u[0] > 0 and u[0] != GROUP_ID]
                sorted_users = sorted(filtered, key=lambda x: x[1], reverse=True)[:5]
                response = "🏆 Топик по балику в кликере:\n\n"
                for i, (uid, balance, clicks, _) in enumerate(sorted_users, 1):
                    try:
                        user_info = self.vk.users.get(user_ids=uid)[0]
                        name = f"{user_info['first_name']} {user_info['last_name']}".replace('[', '').replace(']', '')
                    except Exception:
                        name = f"Пользователь {uid}"
                    response += f"{i}. [id{uid}|{name}] — {self.format_currency(balance)}\n"
                self.send_message(peer_id, response, attachment=TOPBAL_PHOTO, reply_to=reply_msg_id)
                return

            if text_lower in ['инфа', '/info']:
                user = self.db.get_user(user_id)
                if not user:
                    self.send_message(peer_id, "❌ Ошибка, попробуй позже", reply_to=reply_msg_id)
                    return
                sn, se = self.get_status(user['clicks'])
                p_lvl = user.get('prestige_level', 1)
                p_mult = PRESTIGE_MULTIPLIERS.get(p_lvl, 1.0)
                text = (f"👤 Имя: {self.get_user_name(user_id)}\n"
                        f"💰 Баланс: {self.format_currency(user['balance'])}\n"
                        f"👆 Клики: {user['clicks']}\n"
                        f"🏆 Статус: {sn} {se}\n"
                        f"🔮 Престиж: {p_lvl} (x{p_mult})")
                self.send_message(peer_id, text, reply_to=reply_msg_id)
                return

        if not self.is_private_chat(peer_id):
            return

        print(f"\n📨 [{datetime.now().strftime('%H:%M:%S')}] От {user_id}: {text[:50]}")

        user = self.db.get_user(user_id)
        if not user:
            self.send_message(peer_id, "❌ Ошибка, попробуй позже"); return

        menu_buttons = [
            '🔙 вернуться в меню', 'вернуться в меню', 'назад',
            '👆 Кликнуть карпика', '💸 вывести все', '🔥 бонус',
            '💰 бонус дня', '🐥 престиж', '📊 статистика и помощь',
            '🏆 топ', '📊 прогресс', '❓ помощь', 'начать', 'start', '/start',
            '🎁 забрать бонус', 'забрать бонус'
        ]

        if user.get('waiting_captcha'):
            expected_word = user.get('captcha_word')
            if text_lower == expected_word:
                user['waiting_captcha'] = False
                user['captcha_word'] = None
                user['captcha_attachment'] = None
                self.db.update_user(user_id, user)
                self.send_message(peer_id, "Можешь продолжать кликать!✅")
            else:
                self.send_message(peer_id, f"❌ Неверное слово! Попробуй ещё раз")
            return

        code_upper = text.upper()
        with self.promo_lock:
            promo = self.db.get_promocode(code_upper)
            if promo:
                if promo['used_count'] >= promo['max_uses']:
                    self.send_message(peer_id, f"промокод {code_upper} больше не активен:("); return
                if str(user_id) in promo['activated_by']:
                    self.send_message(peer_id, f"Ты уже активировал промокод {code_upper}"); return
                self.flush_user(user_id)
                user = self.db.get_user(user_id)
                promo['used_count'] += 1
                promo['activated_by'].append(str(user_id))
                self.db.update_promocode(code_upper, promo['used_count'], promo['activated_by'])
                user['balance'] += promo['reward']
                user['total_earned'] += promo['reward']
                self.db.update_user(user_id, user)
                self.send_message(peer_id, f"Вы получили +{self.format_currency(promo['reward'])}\n💰 Баланс: {self.format_currency(user['balance'])}")
                return

        if text in ['🔙 Вернуться в меню', 'Вернуться в меню', 'вернуться в меню', 'назад']:
            self.send_keyboard(peer_id, user_id); return

        if not self.is_admin(user_id) and not self.bot_enabled:
            self.send_message(peer_id, "🔧 Бот на техработах"); return

        if text_lower in ['начать', 'start', '/start']:
            self.send_keyboard(peer_id, user_id)
        elif text_lower in ['помощь', '/help', '❓ помощь']:
            help_text = f"""
🎮 Команды:
• 👆 Кликнуть - заработать {self.format_click_reward(self.get_click_reward())}
• 💸 Вывести все - моментальный вывод средств
• 🎰 Бонус - крутить барабан
• 🎁 Бонус дня - ежедневный бонус за 7 дней подряд
• 🔮 Престиж - прокачать множитель клика
• 🏆 Топ - топ 10 игроков по кликам
• 📊 Прогресс - показать прогресс до следующего статуса
• 📝 Отзывы - обсуждения с отзывами о выводах

💸 Минимальная сумма вывода: {self.format_currency(self.min_withdraw)}"""
            self.send_message(peer_id, help_text, inline_keyboard=self.get_stats_back_kb())
        elif text_lower in ['отзывы']:
            self.send_message(peer_id, "📝 Отзывы здесь: https://vk.com/topic-238285284_59112528", inline_keyboard=self.get_stats_back_kb())
        elif text_lower in ['кликнуть', '👆 кликнуть карпика']:
            success, result = self.add_click(user_id, peer_id)
            if result:
                self.send_message(peer_id, result)
        elif text_lower in ['вывести все', '💸 вывести все']:
            # FIX #6: вывод в отдельном потоке
            threading.Thread(target=self._withdraw_thread, args=(user_id, peer_id), daemon=True).start()
        elif text_lower in ['бонус', '🔥 бонус']:
            # FIX #6: спин в отдельном потоке
            threading.Thread(target=self._spin_bonus_thread, args=(user_id, peer_id), daemon=True).start()
        elif text_lower in ['бонус дня', '💰 бонус дня']:
            self.send_daily_bonus(peer_id, user_id)
        elif text_lower in ['престиж', '🐥 престиж']:
            self.send_prestige_menu(peer_id, user_id)
        elif text_lower in ['📊 статистика и помощь', 'статистика и помощь']:
            self.send_stats_menu(peer_id)
        elif text == '🎁 забрать бонус' or text == 'забрать бонус':
            success, result = self.claim_daily_bonus(user_id, peer_id)
            self.send_back_keyboard(peer_id, result)
            if success:
                self.send_daily_bonus(peer_id, user_id)
            return
        elif text_lower in ['прогресс', 'progress', '📊 прогресс']:
            user = self.db.get_user(user_id)
            clicks = user['clicks']
            status_name, status_emoji = self.get_status(clicks)
            next_threshold, next_name, next_emoji = self.get_next_threshold(clicks)
            text = f"📊 Прогресс\n\n"
            text += f"{status_emoji} Ранг: {status_name}\n"
            text += f"🖱️ Кликов: {clicks}\n\n"
            if next_threshold:
                bar = self.get_progress_bar(clicks, next_threshold)
                text += f"{next_emoji} Следующий ранг: {next_name}\n{bar}\n\n"
            else:
                text += f"🎉 Отлично!\nТы достиг максимального статуса!\n"
            self.send_message(peer_id, text, inline_keyboard=self.get_stats_back_kb())
        elif text_lower in ['топ', '🏆 топ']:
            users = self.db.get_all_users()
            if not users:
                self.send_message(peer_id, "отсутствует"); return
            filtered_users = [u for u in users if u[0] > 0 and u[0] != GROUP_ID]
            if not filtered_users:
                self.send_message(peer_id, "отсутствует"); return
            sorted_users = sorted(filtered_users, key=lambda x: x[2], reverse=True)
            top_10 = sorted_users[:10]
            response_text = "🏆 Топ по кликам\n\n"
            for i, (uid, balance, clicks, _) in enumerate(top_10, 1):
                try:
                    user_info = self.vk.users.get(user_ids=uid)[0]
                    full_name = f"{user_info['first_name']} {user_info['last_name']}".replace('[', '').replace(']', '')
                except Exception:
                    full_name = f"Пользователь {uid}"
                status_name, status_emoji = self.get_status(clicks)
                balance_str = f"{balance:,}".replace(",", ".")
                if not balance_str.endswith("$"):
                    balance_str = f"{balance_str}$"
                clicks_str = f"{clicks:,}".replace(",", ".")
                response_text += f"{i}. {status_emoji} [id{uid}|{full_name}] ({status_name}) - {clicks_str} кликов ({balance_str})\n"
            self.send_message(peer_id, response_text, attachment=TOP_PHOTO, inline_keyboard=self.get_stats_back_kb())
        else:
            self.send_keyboard(peer_id, user_id)

    # ===== ФОНОВЫЕ ЗАДАЧИ =====
    def _api_status_checker(self):
        api_ok = True
        while True:
            try:
                r = self.safe_api_request(f"{API_URL}/balance", {'token': API_TOKEN}, timeout=10)
                if r.status_code != 200:
                    raise Exception(f"HTTP {r.status_code}")
                data = r.json()
                if 'answer' not in data or 'balance' not in data['answer']:
                    raise Exception("Неверный формат ответа API")
                if not api_ok:
                    self.send_message(ADMIN_ID, "✅ API бота Нищий снова работает!")
                    api_ok = True
            except Exception as e:
                if api_ok:
                    self.send_message(ADMIN_ID, f"API нищего недоступно или изменилось!\nerror: {e}")
                    api_ok = False
            time.sleep(1200)

    def _ping_inactive_users(self):
        while True:
            time.sleep(43200)
            try:
                conn = self.db.get_connection()
                if not conn:
                    continue
                cursor = conn.cursor()
                three_days_ago = (datetime.now() - timedelta(days=3)).isoformat()
                cursor.execute("""
                    SELECT user_id FROM users
                    WHERE last_seen < ?
                    AND last_seen IS NOT NULL
                    AND notifications_enabled = 1
                    AND (last_ping IS NULL OR last_ping < ?)
                """, (three_days_ago, (datetime.now() - timedelta(days=7)).isoformat()))
                users = [row[0] for row in cursor.fetchall()]
                conn.close()
                for uid in users:
                    if self.db.is_mail_blocked(uid) or self.db.is_banned(uid):
                        continue
                    try:
                        kb = {"inline": True, "buttons": [[{"action": {"type": "callback", "label": "Отписаться от уведомлений", "payload": json.dumps({"command": "unsubscribe_pings"})}, "color": "secondary"}]]}
                        self.send_message(uid, "👋 Привет! Мы скучаем. Тебя ждут ежедневные бонусы и много денег в нашем кликере! Заходи скорей пока Карпик добрый.", inline_keyboard=kb)
                        self.flush_user(uid)
                        user = self.db.get_user(uid)
                        user['last_ping'] = datetime.now().isoformat()
                        self.db.update_user(uid, user)
                        time.sleep(0.5)
                    except Exception:
                        pass
            except Exception as e:
                print(f"❌ Ошибка в пинг-чекере: {e}")

    def handle_board_post(self, event):
        pass # Отключено

    def handle_message_event(self, event):
        try:
            user_id = event.object.user_id
            peer_id = event.object.peer_id
            if isinstance(event.object.payload, str):
                payload = json.loads(event.object.payload)
            else:
                payload = event.object.payload
            command = payload.get('command')

            msg_id = None
            if command in ('back_to_menu', 'show_progress', 'show_help', 'back_to_stats'):
                try:
                    res = self.vk.messages.getByConversationMessageId(peer_id=peer_id, conversation_message_ids=event.object.conversation_message_id)
                    if res['items']:
                        msg_id = res['items'][0]['id']
                except Exception as e:
                    print(f"Ошибка получения message_id: {e}")

            def safe_answer(text):
                try:
                    self.vk.messages.sendMessageEventAnswer(
                        event_id=event.object.event_id, user_id=user_id, peer_id=peer_id,
                        event_data=json.dumps({"type": "show_snackbar", "text": text}))
                except vk_api.exceptions.ApiError:
                    pass

            if command == 'back_to_menu':
                safe_answer("Главное меню")
                if msg_id:
                    text, photo, kb = self.get_menu_data(user_id)
                    success = self.safe_edit(user_id, peer_id, msg_id, text, attachment=photo, keyboard=kb)
                    if not success:
                        self.send_keyboard(peer_id, user_id)
                else:
                    self.send_keyboard(peer_id, user_id)

            elif command == 'back_to_stats':
                safe_answer("В меню статистики")
                if msg_id:
                    success = self.safe_edit(user_id, peer_id, msg_id, "📊 Выбери раздел:", inline_keyboard=self.get_stats_inline_kb())
                    if not success:
                        self.send_stats_menu(peer_id)
                else:
                    self.send_stats_menu(peer_id)

            elif command == 'dummy_daily':
                day = payload.get('day', 0)
                user = self.get_buffered_user(user_id)
                streak = user.get('daily_bonus_streak', 0) or 0
                bonus_data = self.get_daily_bonus_data(user_id)
                can_claim = bonus_data['can_claim']
                next_time = bonus_data['next_available_time']

                if streak >= 7 and not can_claim:
                    msg = "✅ Цикл пройден! Жди новый цикл."
                elif day < streak:
                    msg = f"✅ День {day} уже получен! Забери следующий."
                elif day == streak and not can_claim:
                    msg = f"⏰ День {day} уже забран. Жди следующий!"
                elif day == streak + 1 and not can_claim and next_time:
                    remaining = next_time - datetime.now()
                    hours = remaining.seconds // 3600
                    minutes = (remaining.seconds % 3600) // 60
                    msg = random.choice([f"⏰ Жди {hours}ч {minutes}м, Карп следит", f"⏰ {hours}ч {minutes}м — приказ Карпа", f"⏰ Скоро! {hours}ч {minutes}м"])
                elif day == streak + 1 and can_claim:
                    msg = f"🎁 День {day} уже доступен! Жми «Забрать»"
                elif day > streak:
                    need = day - streak
                    msg = f"🔒 Сначала забери день {streak + 1}" if need == 1 else f"🔒 Откроется через {need} дней. Карп ждёт!"
                else:
                    msg = "🔒 Рано, брат. Кликай пока!"
                safe_answer(msg)

            elif command == 'claim_daily':
                success, result = self.claim_daily_bonus(user_id, peer_id)
                safe_answer(result)
                if success:
                    self.send_daily_bonus(peer_id, user_id)

            elif command == 'confirm_prestige':
                self.flush_user(user_id)
                user = self.db.get_user(user_id)
                lvl = user.get('prestige_level') or 1
                clicks = user.get('clicks') or 0
                balance = user.get('balance') or 0
                next_lvl = lvl + 1
                if next_lvl > 5:
                    return
                req_clicks, req_balance = PRESTIGE_REQS.get(next_lvl, (0, 0))
                if clicks < req_clicks or balance < req_balance:
                    safe_answer("Недостаточно ресурсов!")
                    return
                last_prestige = user.get('last_prestige')
                if last_prestige and last_prestige != '0':
                    try:
                        if datetime.now() - datetime.fromisoformat(last_prestige) < timedelta(days=7):
                            safe_answer("Кулдаун 7 дней ещё не прошел!")
                            return
                    except ValueError:
                        pass
                user['prestige_level'] = next_lvl
                user['prestige_points'] += 1
                user['clicks'] -= req_clicks
                user['balance'] = balance - int(balance * 0.8)
                user['click_reward'] = None
                user['last_prestige'] = datetime.now().isoformat()
                self.db.update_user(user_id, user)
                mult = PRESTIGE_MULTIPLIERS.get(next_lvl, 1.0)
                safe_answer(f"Успешно! Твой множитель x{mult}")
                self.send_keyboard(peer_id, user_id)

            elif command == 'show_top':
                safe_answer("Открываю топ...")
                users = self.db.get_all_users()
                filtered = [u for u in users if u[0] > 0 and u[0] != GROUP_ID]
                sorted_users = sorted(filtered, key=lambda x: x[2], reverse=True)[:10]
                response_text = "Топ по кликам:\n\n"
                for i, (uid, balance, clicks, _) in enumerate(sorted_users, 1):
                    try:
                        user_info = self.vk.users.get(user_ids=uid)[0]
                        full_name = f"{user_info['first_name']} {user_info['last_name']}".replace('[', '').replace(']', '')
                    except Exception:
                        full_name = f"Пользователь {uid}"
                    status_name, status_emoji = self.get_status(clicks)
                    response_text += f"{i}. {status_emoji} [id{uid}|{full_name}] ({status_name}) - {clicks:,} кликов\n"
                self.send_message(peer_id, response_text, attachment=TOP_PHOTO, inline_keyboard=self.get_stats_back_kb())

            elif command == 'show_progress':
                safe_answer("Открываю прогресс...")
                user = self.db.get_user(user_id)
                clicks = user['clicks']
                status_name, status_emoji = self.get_status(clicks)
                next_threshold, next_name, next_emoji = self.get_next_threshold(clicks)
                text = f"📊 Прогресс\n\n{status_emoji} Ранг: {status_name}\n🖱️ Кликов: {clicks}\n\n"
                if next_threshold:
                    text += f"{next_emoji} Цель: {next_name}\n{self.get_progress_bar(clicks, next_threshold)}\n\n"
                else:
                    text += f"🎉 Ты достиг максимального статуса!\n"
                inline_kb = self.get_stats_back_kb()
                if msg_id:
                    success = self.safe_edit(user_id, peer_id, msg_id, text, inline_keyboard=inline_kb)
                    if not success:
                        self.send_message(peer_id, text, inline_keyboard=inline_kb)
                else:
                    self.send_message(peer_id, text, inline_keyboard=inline_kb)

            elif command == 'show_reviews':
                safe_answer("Открываю отзывы...")
                self.send_message(peer_id, "📝 Отзывы здесь: https://vk.com/topic-238285284_59112528", inline_keyboard=self.get_stats_back_kb())

            elif command == 'show_help':
                safe_answer("Открываю помощь...")
                text = f"""Команды:

• Кликнуть - заработать {self.format_click_reward(self.get_click_reward())}
• Вывести все - моментальный вывод средств
• Бонус - крутить барабан
• Бонус дня - ежедневный бонус за 7 дней подряд
• Престиж - прокачать множитель клика
• Топ - топ 10 игроков по кликам
• Прогресс - показать прогресс до следующего статуса
• Отзывы - обсуждения с отзывами о выводах

💸 Минимальная сумма вывода: {self.format_currency(self.min_withdraw)}"""
                inline_kb = self.get_stats_back_kb()
                if msg_id:
                    success = self.safe_edit(user_id, peer_id, msg_id, text, inline_keyboard=inline_kb)
                    if not success:
                        self.send_message(peer_id, text, inline_keyboard=inline_kb)
                else:
                    self.send_message(peer_id, text, inline_keyboard=inline_kb)

            elif command == 'unsubscribe_pings':
                self.flush_user(user_id)
                user = self.db.get_user(user_id)
                user['notifications_enabled'] = False
                self.db.update_user(user_id, user)
                safe_answer("Вы успешно отписались от уведомлений.")

        except Exception as e:
            print(f"❌ Ошибка в обработке инлайн-кнопки: {e}")

    # ===== ЗАПУСК =====
    def run(self):
        print("\n" + "=" * 50)
        if TEST_MODE:
            print("🧪 РЕЖИМ ТЕСТА АКТИВЕН")
            print("🧪 Бот реагирует ТОЛЬКО на команды админа.")
            print("🧪 Разрушительные/массовые команды заблокированы.")
            print("🧪 Чтобы выключить тест: TEST_MODE = False в config.py")
        else:
            print("🚀 Фарм | Бот Нищий запущен.")
        print("=" * 50)

        if not TEST_MODE:
            threading.Thread(target=self.check_new_posts, daemon=True).start()
            threading.Thread(target=self._api_status_checker, daemon=True).start()
            threading.Thread(target=self._ping_inactive_users, daemon=True).start()
            threading.Thread(target=self.sync_all_dialogs, daemon=True).start()

        error_counter = 0
        while True:
            try:
                for event in self.longpoll.listen():
                    error_counter = 0
                    if TEST_MODE:
                        allowed = False
                        if event.type == VkBotEventType.MESSAGE_NEW:
                            sender = event.object.message.get('from_id')
                            if sender and self.is_admin(sender):
                                allowed = True
                        elif event.type == VkBotEventType.MESSAGE_EVENT:
                            sender = event.object.user_id
                            if sender and self.is_admin(sender):
                                allowed = True
                        if not allowed:
                            continue

                    if event.type == VkBotEventType.MESSAGE_NEW:
                        try:
                            self.handle_message(event)
                        except Exception as e:
                            print(f"❌ Ошибка в handle_message: {e}")
                            error_counter += 1
                            if error_counter >= 5:
                                raise Exception("Too many errors")
                    elif event.type == VkBotEventType.BOARD_POST_NEW:
                        self.handle_board_post(event)
                    elif event.type == VkBotEventType.MESSAGE_EVENT:
                        self.handle_message_event(event)
                    elif event.type == VkBotEventType.WALL_REPLY_NEW:
                        self.handle_wall_reply(event)
                    elif event.type == "like_add":
                        self.handle_like_add(event)

            except vk_api.exceptions.ApiError as e:
                print(f"❌ VK API ошибка {e.code}: {e}")
                if e.code == 15:
                    if not TEST_MODE:
                        self.notify_admin("Токен умер", "Бот остановлен", is_error=True)
                    sys.exit(1)
                elif e.code == 9:
                    time.sleep(60)
                else:
                    time.sleep(30)
            except requests.exceptions.ConnectionError:
                print("❌ Нет соединения")
                time.sleep(60)
            except Exception as e:
                print(f"❌ Критическая ошибка: {e}")
                error_counter += 1
                time.sleep(30)
                if error_counter >= 3:
                    if not TEST_MODE:
                        self.notify_admin("Аварийный перезапуск", str(e), is_error=False)
                    self.restart_bot()

            for attempt in range(10):
                try:
                    print("🔄 Пересоздание сессии VK LongPoll...")
                    time.sleep(5)
                    self.vk_session = vk_api.VkApi(token=self.group_token)
                    self.longpoll = VkBotLongPoll(self.vk_session, self.group_id)
                    self.vk = self.vk_session.get_api()
                    print("✅ Переподключено")
                    error_counter = 0
                    break
                except Exception as e:
                    print(f"⚠️ Ошибка переподключения ({attempt+1}/10): {e}")
                    time.sleep(15)