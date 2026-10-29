import os
DB_PATH = os.environ.get("DB_PATH", "/app/data/bot_database.db")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ===== ТОКЕНЫ =====
GROUP_TOKEN = os.environ.get("GROUP_TOKEN", "")
USER_TOKEN = os.environ.get("USER_TOKEN", "")
try:
    GROUP_ID = int(os.environ.get("GROUP_ID", "238285284"))
except ValueError:
    GROUP_ID = 238285284

API_URL = "https://api.badbotik.com/api"
API_TOKEN = "c41e9686b288949b175bbb7e026e3331"

# ===== НАСТРОЙКИ (значения по умолчанию, БД перекрывает) =====
CLICK_REWARD = 10000000000
ADMIN_ID = 575107090
MIN_WITHDRAW = 15000000000000
LOG_CHAT_ID = 2000000001
REVIEW_TOPIC_ID = 59112528
LIKE_REWARD = 150 * 10**9  # награда за лайк

BONUS_INTERVAL_HOURS = 24

# RateLimiter (на случай если подключишь)
MAX_CLICKS_PER_MINUTE = 200
RATE_LIMIT_SECONDS = 0.05


MENU_PHOTO = "photo-238285284_457241898"
TOP_PHOTO = "photo-238285284_457241969"
TOPBAL_PHOTO = "photo-238285284_457241957"
SPIN_GIF = "doc-238285284_703630836"
SPIN_WIN_100 = "photo-238285284_457241934"
SPIN_WIN_150 = "photo-238285284_457241935"
SPIN_WIN_300 = "photo-238285284_457241936"
DAILY_BONUS_PHOTO = "photo-238285284_457241981"
PRESTIGE_PHOTO = "photo-238285284_457241948"
POST_GAME_START_PHOTO = "photo-238285284_457241952"
POST_GAME_END_PHOTO = "photo-238285284_457241953"
POST_GAME_REWARD = 5000000000000


STATUSES = [
    (0, "Новичок", "🟢"),
    (100, "Любопытный", "🔵"),
    (500, "Начинающий", "🔷"),
    (1000, "Опытный", "🟣"),
    (5000, "Профессионал", "🔴"),
    (10000, "Эксперт", "🏅"),
    (50000, "Легенда", "👑"),
    (100000, "Бог кликов", "⚡")
]

# ===== РЕЖИМ ТЕСТА =====
# True = бот игнорирует все сообщения, лайки и события (для отладки)
# False = обычная работа
TEST_MODE = False

DEBUG = True  # True = подробные логи в консоль, False = тихий режим
