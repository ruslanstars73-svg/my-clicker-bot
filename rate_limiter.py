import threading
import time

from config import MAX_CLICKS_PER_MINUTE, RATE_LIMIT_SECONDS


class RateLimiter:
    def __init__(self):
        self.clicks = {}
        self.lock = threading.Lock()

    def check_and_add(self, user_id):
        with self.lock:
            now = time.time()
            if user_id not in self.clicks:
                self.clicks[user_id] = []

            self.clicks[user_id] = [t for t in self.clicks[user_id] if now - t < 60]

            if len(self.clicks[user_id]) >= MAX_CLICKS_PER_MINUTE:
                remaining = 60 - (now - self.clicks[user_id][0])
                return False, f"⏰ Слишком много кликов! Подожди {remaining:.0f} секунд"

            if self.clicks[user_id] and (now - self.clicks[user_id][-1]) < RATE_LIMIT_SECONDS:
                return False, f"⏰ Кликай медленнее! Подожди {RATE_LIMIT_SECONDS:.1f} секунду"

            self.clicks[user_id].append(now)
            return True, None