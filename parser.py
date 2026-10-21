import re

import vk_api  # noqa: F401  (нужен только как зависимость для типов)


class MessageParser:
    def __init__(self, bot=None):
        self.bot = bot

    def extract_user_id(self, text):
        patterns = [
            r'\[id(\d+)\|',
            r'vk/.com/id(\d+)',
            r'vk/.ru/id(\d+)',
            r'@([a-zA-Z0-9_]+)',
            r'id(\d+)'
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                if match.group(0).startswith('@'):
                    username = match.group(1)
                    try:
                        if self.bot is not None:
                            user = self.bot.vk.users.get(user_ids=username)
                        else:
                            from config import GROUP_TOKEN
                            vk = vk_api.VkApi(token=GROUP_TOKEN)
                            user = vk.get_api().users.get(user_ids=username)
                        if user:
                            return user[0]['id']
                    except Exception:
                        pass
                else:
                    return int(match.group(1))
        return None

    @staticmethod
    def extract_all_links(text):
        patterns = [
            r'https?://[^\s]+',
            r'vk\.com/[^\s]+',
            r'vk\.ru/[^\s]+',
            r'vk\.me/[^\s]+'
        ]
        links = []
        for pattern in patterns:
            matches = re.findall(pattern, text)
            links.extend(matches)
        return list(set(links))

    @staticmethod
    def extract_mentions(text):
        pattern = r'@([a-zA-Z0-9_]+)'
        return re.findall(pattern, text)