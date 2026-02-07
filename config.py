# ================== CONFIG ==================
BOT_TOKEN = "7142196746:AAHBbpyXISw4XCxtvSzDItojFcFc97wKDZY"

MAIN_CHANNEL_ID = -1002120185316
COMMENTS_CHAT_ID = -1002070054707  # ID чата для комментариев

MODERATORS_CHAT_ID = -1002156153932
MODERATORS_TOPIC_ID = 70619

ADMINS_CHAT_ID = -1002104468174
ADMINS_TOPIC_ID = 76774

ADMINS = [6702947726, 1171717255]
MODERATORS = []

DB_NAME = "smotrbot.db"

# ================== ОБЯЗАТЕЛЬНЫЕ КАНАЛЫ/БОТЫ ДЛЯ ПОДПИСКИ ==================
REQUIRED_SUBSCRIPTIONS = [
    {
        "type": "channel",
        "id": "-1002120185316",
        "username": "@smotrmaslyanino",
        "name": "Смотр Маслянино",
        "url": "https://t.me/smotrmaslyanino"
    },
    {
        "type": "bot",
        "id": "8589490953",
        "username": "@smotrmaslyaninostars_bot",
        "name": "Магазин Звёзд",
        "url": "https://t.me/smotrmaslyaninostars_bot"
    }
]