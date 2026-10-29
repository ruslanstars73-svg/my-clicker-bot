import json
import sqlite3
from datetime import datetime
from config import ADMIN_ID
from config import DB_PATH

class Database:
    def __init__(self, db_file=DB_PATH):
        self.db_file = db_file
        self.init_db()

    def _to_int(self, val, default=0):
        """Безопасно превращает любое значение в int"""
        if val is None:
            return default
        try:
            return int(float(val)) # float на случай "1000.0" или пробелов
        except (ValueError, TypeError):
            return default

    def _safe_date(self, date_str):
        """Проверяет, является ли строка нормальной датой. Если нет - возвращает None"""
        if not date_str or str(date_str).strip() in ('0', '', 'None'):
            return None
        try:
            datetime.fromisoformat(str(date_str))
            return str(date_str)
        except ValueError:
            return None

    def get_connection(self):
        try:
            conn = sqlite3.connect(self.db_file, timeout=20)
            return conn
        except Exception as e:
            print(f"❌ Ошибка подключения к БД: {e}")
            return None

    def init_db(self):
        conn = self.get_connection()
        if not conn:
            return
        try:
            from config import DEBUG
        except ImportError:
            DEBUG = False

        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")   # ← УСКОРЕНИЕ x5
            cursor.execute("PRAGMA cache_size=-64000;")    # 64 MB кэша
            cursor.execute("PRAGMA temp_store=MEMORY;")

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0, clicks INTEGER DEFAULT 0,
                    last_click TEXT, total_earned INTEGER DEFAULT 0, clicks_since_check INTEGER DEFAULT 0,
                    waiting_captcha INTEGER DEFAULT 0, captcha_word TEXT, captcha_attachment TEXT,
                    has_reviewed INTEGER DEFAULT 0, pending_review INTEGER DEFAULT 0, pending_review_amount TEXT,
                    last_spin TEXT, last_daily_bonus TEXT, daily_bonus_streak INTEGER DEFAULT 0,
                    liked_posts TEXT DEFAULT '[]', click_reward INTEGER DEFAULT NULL,
                    badbotik_token TEXT DEFAULT NULL, captcha_attempts INTEGER DEFAULT 0,
                    last_seen TEXT, notifications_enabled INTEGER DEFAULT 1, last_ping TEXT,
                    prestige_level INTEGER DEFAULT 1, prestige_points INTEGER DEFAULT 0, last_prestige TEXT
                )
            ''')

            # Проверяем версию БД — миграции только если нужно
            DB_VERSION = 10
            try:
                cursor.execute("SELECT value FROM settings WHERE key = 'db_version'")
                row = cursor.fetchone()
                current_version = int(row[0]) if row else 0
            except Exception:
                current_version = 0

            if current_version < DB_VERSION:
                columns_to_add = [
                    ("liked_posts", "TEXT DEFAULT '[]'"), ("click_reward", "INTEGER DEFAULT NULL"),
                    ("badbotik_token", "TEXT DEFAULT NULL"), ("captcha_attempts", "INTEGER DEFAULT 0"),
                    ("last_seen", "TEXT"), ("notifications_enabled", "INTEGER DEFAULT 1"),
                    ("last_ping", "TEXT"), ("prestige_level", "INTEGER DEFAULT 1"),
                    ("prestige_points", "INTEGER DEFAULT 0"), ("last_prestige", "TEXT"),
                    ("last_group_activity_reward", "TEXT DEFAULT NULL")
                ]
                for col_name, col_type in columns_to_add:
                    try:
                        cursor.execute(f"ALTER TABLE users ADD COLUMN {col_name} {col_type}")
                        if DEBUG:
                            print(f"🔧 Добавлена колонка: {col_name}")
                    except sqlite3.OperationalError:
                        pass

                cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('db_version', ?)", (str(DB_VERSION),))

            cursor.execute("UPDATE users SET balance = 0 WHERE balance IS NULL")
            cursor.execute("UPDATE users SET clicks = 0 WHERE clicks IS NULL")
            cursor.execute("UPDATE users SET total_earned = 0 WHERE total_earned IS NULL")
            cursor.execute("UPDATE users SET clicks_since_check = 0 WHERE clicks_since_check IS NULL")
            cursor.execute("UPDATE users SET daily_bonus_streak = 0 WHERE daily_bonus_streak IS NULL")
            cursor.execute("UPDATE users SET prestige_level = 1 WHERE prestige_level IS NULL")
            cursor.execute("UPDATE users SET prestige_points = 0 WHERE prestige_points IS NULL")
            cursor.execute("UPDATE users SET notifications_enabled = 1 WHERE notifications_enabled IS NULL")
            cursor.execute("UPDATE users SET click_reward = NULL WHERE click_reward < 1000")

            cursor.execute('''CREATE TABLE IF NOT EXISTS promocodes (code TEXT PRIMARY KEY, reward INTEGER, max_uses INTEGER, used_count INTEGER DEFAULT 0, activated_by TEXT DEFAULT '[]')''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS extra_admins (user_id INTEGER PRIMARY KEY)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS admin_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT, target_id INTEGER, details TEXT, timestamp TEXT)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY, reason TEXT, banned_by INTEGER, banned_at TEXT)''')
            cursor.execute('''CREATE TABLE IF NOT EXISTS pending_transactions (user_id INTEGER PRIMARY KEY, amount INTEGER, timestamp TEXT)''')

            conn.commit()
            if DEBUG:
                print("✅ БД инициализирована")
        except Exception as e:
            print(f"❌ Ошибка init_db: {e}")
        finally:
            conn.close()

    def get_user(self, user_id):
        conn = self.get_connection()
        if not conn:
            return self.create_user(user_id)
        try:
            conn.row_factory = sqlite3.Row  # Позволяет брать данные по имени колонки
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if not row:
                return self.create_user(user_id)
            
            user_dict = dict(row)
            
            return {
                'user_id': user_dict.get('user_id'), 
                'balance': self._to_int(user_dict.get('balance')), 
                'clicks': self._to_int(user_dict.get('clicks')), 
                'last_click': user_dict.get('last_click'),
                'total_earned': self._to_int(user_dict.get('total_earned')), 
                'clicks_since_check': self._to_int(user_dict.get('clicks_since_check')), 
                'waiting_captcha': bool(user_dict.get('waiting_captcha')),
                'captcha_word': user_dict.get('captcha_word'), 
                'captcha_attachment': user_dict.get('captcha_attachment'), 
                'has_reviewed': bool(user_dict.get('has_reviewed')),
                'pending_review': bool(user_dict.get('pending_review')), 
                'pending_review_amount': user_dict.get('pending_review_amount'), 
                'last_spin': user_dict.get('last_spin'),
                'last_daily_bonus': user_dict.get('last_daily_bonus'), 
                'daily_bonus_streak': self._to_int(user_dict.get('daily_bonus_streak')),
                'liked_posts': json.loads(user_dict.get('liked_posts')) if user_dict.get('liked_posts') else [],
                'click_reward': self._to_int(user_dict.get('click_reward')) if user_dict.get('click_reward') is not None else None,
                'badbotik_token': user_dict.get('badbotik_token'),
                'captcha_attempts': self._to_int(user_dict.get('captcha_attempts')),
                'last_seen': user_dict.get('last_seen'),
                'notifications_enabled': bool(user_dict.get('notifications_enabled')) if user_dict.get('notifications_enabled') is not None else True,
                'last_ping': user_dict.get('last_ping'),
                'prestige_level': self._to_int(user_dict.get('prestige_level'), 1),
                'prestige_points': self._to_int(user_dict.get('prestige_points')),
                'last_prestige': user_dict.get('last_prestige')
            }
        except Exception as e:
            print(f"❌ Ошибка парсинга юзера {user_id}: {e}")
            return self.create_user(user_id)
        finally:
            conn.close()

    def create_user(self, user_id):
        conn = self.get_connection()
        if conn:
            try:
                conn.cursor().execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
                conn.commit()
            except Exception: pass
            finally: conn.close()
        return {
            'user_id': user_id, 'balance': 0, 'clicks': 0, 'last_click': None, 'total_earned': 0,
            'clicks_since_check': 0, 'waiting_captcha': False, 'captcha_word': None, 'captcha_attachment': None,
            'has_reviewed': False, 'pending_review': False, 'pending_review_amount': None, 'last_spin': None,
            'last_daily_bonus': None, 'daily_bonus_streak': 0, 'liked_posts': [], 'click_reward': None,
            'badbotik_token': None, 'captcha_attempts': 0, 'last_seen': None, 'notifications_enabled': True, 'last_ping': None,
            'prestige_level': 1, 'prestige_points': 0, 'last_prestige': None
        }

    def update_user(self, user_id, data):
        conn = self.get_connection()
        if not conn:
            return
        try:
            cursor = conn.cursor()
            safe_click_reward = data.get('click_reward')
            if safe_click_reward is not None and safe_click_reward < 1000:
                safe_click_reward = None

            cursor.execute('''
                UPDATE users SET
                    balance = ?, clicks = ?, last_click = ?, total_earned = ?, clicks_since_check = ?,
                    waiting_captcha = ?, captcha_word = ?, captcha_attachment = ?, has_reviewed = ?,
                    pending_review = ?, pending_review_amount = ?, last_spin = ?, last_daily_bonus = ?,
                    daily_bonus_streak = ?, liked_posts = ?, click_reward = ?, badbotik_token = ?, captcha_attempts = ?,
                    last_seen = ?, notifications_enabled = ?, last_ping = ?
                WHERE user_id = ?
            ''', (
                data.get('balance', 0), data.get('clicks', 0), data.get('last_click'),
                data.get('total_earned', 0), data.get('clicks_since_check', 0),
                1 if data.get('waiting_captcha') else 0, data.get('captcha_word'),
                data.get('captcha_attachment'), 1 if data.get('has_reviewed') else 0,
                1 if data.get('pending_review') else 0, data.get('pending_review_amount'),
                data.get('last_spin'), data.get('last_daily_bonus'),
                data.get('daily_bonus_streak', 0), json.dumps(data.get('liked_posts', [])),
                safe_click_reward, data.get('badbotik_token'),
                data.get('captcha_attempts', 0), data.get('last_seen'),
                1 if data.get('notifications_enabled', True) else 0, data.get('last_ping'),
                user_id
            ))
            conn.commit()
        except Exception as e:
            print(f"❌ Ошибка update_user: {e}")
        finally:
            conn.close()

    # === ОСТАЛЬНЫЕ МЕТОДЫ ===
    def add_pending_tx(self, user_id, amount):
        conn = self.get_connection()
        if not conn: return
        try:
            conn.cursor().execute("INSERT OR REPLACE INTO pending_transactions (user_id, amount, timestamp) VALUES (?, ?, ?)", (user_id, amount, datetime.now().isoformat()))
            conn.commit()
        except Exception: pass
        finally: conn.close()

    def remove_pending_tx(self, user_id):
        conn = self.get_connection()
        if not conn: return
        try:
            conn.cursor().execute("DELETE FROM pending_transactions WHERE user_id = ?", (user_id,))
            conn.commit()
        except Exception: pass
        finally: conn.close()

    def get_all_users(self):
        conn = self.get_connection()
        if not conn: return []
        try: return conn.cursor().execute("SELECT user_id, balance, clicks, total_earned FROM users").fetchall()
        except Exception: return []
        finally: conn.close()

    def get_all_active_users(self):
        conn = self.get_connection()
        if not conn: return []
        try: return [r[0] for r in conn.cursor().execute("SELECT user_id FROM users WHERE user_id > 0").fetchall()]
        except Exception: return []
        finally: conn.close()

    def get_setting(self, key, default=None):
        conn = self.get_connection()
        if not conn: return default
        try:
            row = conn.cursor().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            if row:
                try: return int(row[0])
                except ValueError: return row[0]
        except Exception: pass
        finally: conn.close()
        return default

    def set_setting(self, key, value):
        conn = self.get_connection()
        if not conn: return
        try:
            conn.cursor().execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
            conn.commit()
        except Exception: pass
        finally: conn.close()

    def get_extra_admins(self):
        conn = self.get_connection()
        if not conn: return [ADMIN_ID]
        try:
            rows = conn.cursor().execute("SELECT user_id FROM extra_admins").fetchall()
            return [r[0] for r in rows] if rows else [ADMIN_ID]
        except Exception: return [ADMIN_ID]
        finally: conn.close()

    def add_extra_admin(self, user_id):
        conn = self.get_connection()
        if not conn: return
        try:
            conn.cursor().execute("INSERT OR IGNORE INTO extra_admins (user_id) VALUES (?)", (user_id,))
            conn.commit()
        except Exception: pass
        finally: conn.close()

    def remove_extra_admin(self, user_id):
        conn = self.get_connection()
        if not conn: return
        try:
            conn.cursor().execute("DELETE FROM extra_admins WHERE user_id = ?", (user_id,))
            conn.commit()
        except Exception: pass
        finally: conn.close()

    def add_admin_log(self, admin_id, action, target_id=None, details=None):
        conn = self.get_connection()
        if not conn: return
        try:
            conn.cursor().execute("INSERT INTO admin_logs (admin_id, action, target_id, details, timestamp) VALUES (?, ?, ?, ?, ?)", (admin_id, action, target_id, details, datetime.now().isoformat()))
            conn.commit()
        except Exception: pass
        finally: conn.close()

    def get_admin_logs(self, limit=30):
        conn = self.get_connection()
        if not conn: return []
        try: return conn.cursor().execute("SELECT admin_id, action, target_id, details, timestamp FROM admin_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        except Exception: return []
        finally: conn.close()

    def clear_all_users_except_admin(self, admin_id):
        conn = self.get_connection()
        if not conn: return
        try:
            conn.cursor().execute("DELETE FROM users WHERE user_id != ?", (admin_id,))
            conn.commit()
        except Exception: pass
        finally: conn.close()

    def is_mail_blocked(self, user_id): return self.get_setting(f'mail_blocked_{user_id}', 0) == 1
    def block_mail(self, user_id): self.set_setting(f'mail_blocked_{user_id}', 1)
    def unblock_mail(self, user_id): self.set_setting(f'mail_blocked_{user_id}', 0)

    def ban_user(self, user_id, reason, banned_by):
        conn = self.get_connection()
        if not conn: return False
        try:
            conn.cursor().execute("INSERT OR REPLACE INTO banned_users (user_id, reason, banned_by, banned_at) VALUES (?, ?, ?, ?)", (user_id, reason, banned_by, datetime.now().isoformat()))
            conn.commit()
            return True
        except Exception: return False
        finally: conn.close()

    def unban_user(self, user_id):
        conn = self.get_connection()
        if not conn: return False
        try:
            conn.cursor().execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
            conn.commit()
            return True
        except Exception: return False
        finally: conn.close()

    def is_banned(self, user_id):
        conn = self.get_connection()
        if not conn: return False
        try: return conn.cursor().execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,)).fetchone() is not None
        except Exception: return False
        finally: conn.close()

    def get_promocode(self, code):
        conn = self.get_connection()
        if not conn: return None
        try:
            row = conn.cursor().execute("SELECT * FROM promocodes WHERE code = ?", (code,)).fetchone()
            if row:
                return {'code': row[0], 'reward': row[1], 'max_uses': row[2], 'used_count': row[3], 'activated_by': json.loads(row[4]) if row[4] else []}
        except Exception: pass
        finally: conn.close()
        return None

    def create_promocode(self, code, reward, max_uses):
        conn = self.get_connection()
        if not conn: return
        try:
            conn.cursor().execute("INSERT INTO promocodes (code, reward, max_uses) VALUES (?, ?, ?)", (code, reward, max_uses))
            conn.commit()
        except Exception: pass
        finally: conn.close()

    def update_promocode(self, code, used_count, activated_by):
        conn = self.get_connection()
        if not conn: return
        try:
            conn.cursor().execute("UPDATE promocodes SET used_count = ?, activated_by = ? WHERE code = ?", (used_count, json.dumps(activated_by), code))
            conn.commit()
        except Exception: pass
        finally: conn.close()

    def delete_promocode(self, code):
        conn = self.get_connection()
        if not conn: return
        try:
            conn.cursor().execute("DELETE FROM promocodes WHERE code = ?", (code,))
            conn.commit()
        except Exception: pass
        finally: conn.close()

    def get_all_promocodes(self):
        conn = self.get_connection()
        if not conn: return {}
        try:
            rows = conn.cursor().execute("SELECT * FROM promocodes").fetchall()
            return {r[0]: {'reward': r[1], 'max_uses': r[2], 'used_count': r[3], 'activated_by': json.loads(r[4]) if r[4] else []} for r in rows}
        except Exception: return {}
        finally: conn.close()
