import random
import sys
from datetime import datetime

import vk_api

from bot import ClickerBot
from config import GROUP_TOKEN, USER_TOKEN, GROUP_ID, ADMIN_ID


def main():
    if not GROUP_TOKEN or not USER_TOKEN:
        print("=" * 60)
        print("❌ ТОКЕНЫ НЕ НАСТРОЕНЫ")
        print("=" * 60)
        print("Создай файл .env в папке с ботом и добавь туда:")
        print("  GROUP_TOKEN=vk1.a.ваштокен_группы")
        print("  USER_TOKEN=vk1.a.ваштокен_пользователя")
        print("  API_TOKEN=ваштокен_badbotik")
        print()
        print("Пример есть в .env.example")
        sys.exit(1)

    try:
        temp_vk = vk_api.VkApi(token=GROUP_TOKEN)
        temp_vk.get_api().messages.send(
            user_id=ADMIN_ID,
            message=f"Бот снова работает.\nВремя: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}",
            random_id=random.randint(1, 999999999)
        )
    except Exception:
        pass

    bot = ClickerBot(GROUP_TOKEN, USER_TOKEN, GROUP_ID)
    bot.run()


if __name__ == "__main__":
    main()