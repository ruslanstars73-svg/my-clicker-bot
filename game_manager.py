import random
from datetime import datetime, timedelta


class GameManager:
    def __init__(self, bot):
        self.bot = bot
        self.active_games = {}

    def start_game(self, chat_id, admin_id, amount):
        if chat_id in self.active_games:
            return False, "❌ Игра уже запущена"

        number = random.randint(1, 100)
        game_data = {
            'number': number, 'amount': amount, 'admin_id': admin_id,
            'start_time': datetime.now(), 'end_time': datetime.now() + timedelta(hours=1),
            'players': [], 'winner': None, 'active': True, 'guesses': []
        }
        self.active_games[chat_id] = game_data

        try:
            self.bot.send_message(admin_id, f"🎯 Загадано число: {number}\n💰 Приз: {self.bot.format_currency(amount)}")
        except Exception: pass

        message = f"🎯\n\n@all\nБот загадал число от 1 до 100!\n💰 Приз: {self.bot.format_currency(amount)}\n\n"
        self.bot.send_message(chat_id, message)
        return True, "✅ Игра запущена"

    def stop_game(self, chat_id):
        if chat_id not in self.active_games:
            return False, "❌ Нет активной игры"
        game = self.active_games[chat_id]
        game['active'] = False
        del self.active_games[chat_id]
        return True, "✅ Игра остановлена"

    def check_guess(self, chat_id, user_id, user_name, guess):
        if chat_id not in self.active_games:
            return None, None

        game = self.active_games[chat_id]
        
        # Проверка на таймаут
        if datetime.now() > game['end_time']:
            game['active'] = False
            del self.active_games[chat_id]
            return None, "⏰ Время игры истекло!"

        if not game['active']: return None, None

        try:
            guess_num = int(guess)
            if guess_num < 1 or guess_num > 100:
                return None, "❌ Число должно быть от 1 до 100"
        except ValueError:
            return None, "❌ Введите число"

        if game['winner']:
            return None, f"🏆 Игра завершена, победитель: {game['winner']}"

        if user_id not in game['players']:
            game['players'].append(user_id)

        game['guesses'].append({'user_id': user_id, 'guess': guess_num})

        if guess_num == game['number']:
            game['winner'] = user_name
            game['active'] = False

            user_data = self.bot.db.get_user(user_id)
            if user_data:
                user_data['balance'] += game['amount']
                user_data['total_earned'] += game['amount']
                self.bot.db.update_user(user_id, user_data)

            winner_info = {
                'user_id': user_id, 'name': user_name, 'number': game['number'],
                'amount': game['amount'], 'players': len(game['players']), 'total_guesses': len(game['guesses'])
            }
            del self.active_games[chat_id]
            return True, winner_info

        return None, None

    def is_game_active(self, chat_id):
        if chat_id in self.active_games:
            if datetime.now() > self.active_games[chat_id]['end_time']:
                del self.active_games[chat_id]
                return False
            return self.active_games[chat_id]['active']
        return False