import os
import sys
import threading
import time


class Watchdog:
    def __init__(self, timeout=300, bot=None):
        self.timeout = timeout
        self.last_activity = time.time()
        self.running = True
        self.bot = bot
        self.notified = False

    def ping(self):
        self.last_activity = time.time()
        self.notified = False

    def start(self):
        def monitor():
            while self.running:
                time.sleep(30)
                if time.time() - self.last_activity > self.timeout:
                    if not self.notified:
                        self.notified = True
                        print(f"⚠️ Бот не отвечал {self.timeout} секунд")
                        if self.bot:
                            try:
                                self.bot.notify_admin("❌ Ошибка", "Бот завис", is_error=True)
                            except Exception:  # noqa: BLE001, S110
                                pass
                        print("🔄 Перезапуск...")
                        time.sleep(2)
                        python = sys.executable
                        os.execl(python, python, *sys.argv)
                else:
                    self.notified = False
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        print("✅ Watchdog запущен")

    def stop(self):
        self.running = False