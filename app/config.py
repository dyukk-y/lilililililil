"""
Конфигурация бота.

Все значения читаются из переменных окружения (.env). Никаких реальных
ID чатов/токенов/админов в коде не хранится — используйте .env.example
как шаблон и создайте свой .env (он не должен коммититься в git).
"""
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(
            f"Переменная окружения {name} обязательна. "
            f"Скопируйте .env.example в .env и заполните значения."
        )
    return value


def _required_int(name: str) -> int:
    return int(_required(name))


def _optional_int(name: str, default: int = 0) -> int:
    value = os.getenv(name)
    return int(value) if value else default


def _optional_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value else default


def _optional_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value else default


# ================== ОБЯЗАТЕЛЬНЫЕ ПЕРЕМЕННЫЕ ==================
BOT_TOKEN = _required("BOT_TOKEN")
MAIN_CHANNEL_ID = _required_int("MAIN_CHANNEL_ID")
COMMENTS_CHAT_ID = _optional_int("COMMENTS_CHAT_ID")

# ================== НЕОБЯЗАТЕЛЬНЫЕ ПЕРЕМЕННЫЕ ==================
MODERATORS_CHAT_ID = _optional_int("MODERATORS_CHAT_ID")
MODERATORS_TOPIC_ID = _optional_int("MODERATORS_TOPIC_ID")

ADMINS_CHAT_ID = _optional_int("ADMINS_CHAT_ID")
ADMINS_TOPIC_ID = _optional_int("ADMINS_TOPIC_ID")

# Список ID администраторов через запятую, например: "111111,222222"
_ADMINS_STR = os.getenv("ADMINS", "")
ADMINS = [int(uid.strip()) for uid in _ADMINS_STR.split(",") if uid.strip()]
if not ADMINS:
    raise ValueError(
        "Переменная окружения ADMINS обязательна и должна содержать "
        "хотя бы один Telegram user_id администратора."
    )

MODERATORS: list[int] = []

DB_NAME = os.getenv("DB_NAME", "smotrbot.db")

# Список обязательных подписок подгружается из БД при старте
# (см. app.database.load_subscriptions_from_db). Здесь — лишь
# начальное состояние, список мутируется НА МЕСТЕ (не переприсваивается),
# чтобы все модули, импортировавшие его, видели актуальные данные.
REQUIRED_SUBSCRIPTIONS: list[dict] = []

# ================== АВТОПУБЛИКАЦИЯ ==================
# Все "плавающие" даты/окна публикации считаются по этому часовому поясу,
# независимо от того, в каком часовом поясе находится сам сервер.
TIMEZONE = ZoneInfo(os.getenv("PUBLISH_TIMEZONE", "Asia/Novosibirsk"))

# Окно публикации в течение дня (по TIMEZONE)
PUBLISH_WINDOW_START_HOUR = _optional_int("PUBLISH_WINDOW_START_HOUR", 8)
PUBLISH_WINDOW_END_HOUR = _optional_int("PUBLISH_WINDOW_END_HOUR", 23)
PUBLISH_WINDOW_END_MINUTE = _optional_int("PUBLISH_WINDOW_END_MINUTE", 59)

# Сколько постов максимум публикуется за один день
DAILY_PUBLISH_LIMIT = _optional_int("DAILY_PUBLISH_LIMIT", 10)

# Минимальный зазор между временем публикации двух постов (минуты) —
# чтобы случайные слоты не "слипались" в один момент
MIN_SLOT_GAP_MINUTES = _optional_int("MIN_SLOT_GAP_MINUTES", 20)

# ================== АВТОМОДЕРАЦИЯ ==================
# Если похожий текст (нечёткое сравнение) уже встречался в базе
# столько раз или больше — новый пост автоматически отклоняется.
DUPLICATE_REPEAT_LIMIT = _optional_int("DUPLICATE_REPEAT_LIMIT", 5)
# Порог "похожести" текстов (0..1) для нечёткого сравнения дубликатов.
DUPLICATE_SIMILARITY_THRESHOLD = _optional_float("DUPLICATE_SIMILARITY_THRESHOLD", 0.87)

# ================== РЕЗЕРВНОЕ КОПИРОВАНИЕ БД ==================
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
BACKUP_INTERVAL_MINUTES = _optional_int("BACKUP_INTERVAL_MINUTES", 60)
BACKUP_KEEP_LAST = _optional_int("BACKUP_KEEP_LAST", 48)

# ================== NSFW-ПРОВЕРКА ФОТО (NudeNet) ==================
# Явная нагота с уверенностью выше порога -> автоматический отказ.
# Любое другое фото (не прошедшее порог явного отказа) НЕ публикуется
# автоматически — всегда уходит на обязательную ручную модерацию,
# т.к. возраст на фото автоматика оценить надёжно не может.
NSFW_EXPLICIT_THRESHOLD = _optional_float("NSFW_EXPLICIT_THRESHOLD", 0.65)

# ================== ЗАЯВКИ НА УДАЛЕНИЕ ПОСТА ==================
# Кому уходит заявка на удаление, если инициатор не автор поста и не
# упомянут в его тексте. ID через запятую.
_DELETION_REVIEWERS_STR = os.getenv("DELETION_REVIEWERS", "784343572,6702947726")
DELETION_REVIEWERS = [int(uid.strip()) for uid in _DELETION_REVIEWERS_STR.split(",") if uid.strip()]
DELETION_REQUEST_TIMEOUT_HOURS = _optional_int("DELETION_REQUEST_TIMEOUT_HOURS", 24)

# ================== ОБЩИЙ СРОК РУЧНОЙ МОДЕРАЦИИ ==================
# Если пост ушёл на ручную модерацию (фото, либо текст без ключевых фраз
# для автопубликации) и за это время никто из модераторов не принял
# решение — пост автоматически отклоняется.
MODERATION_TIMEOUT_HOURS = _optional_int("MODERATION_TIMEOUT_HOURS", 24)

# ================== ПЛАТНАЯ РАСШИФРОВКА АВТОРА (Telegram Stars) ==================
AUTHOR_LOOKUP_PRICE_STARS = _optional_int("AUTHOR_LOOKUP_PRICE_STARS", 100)

# ================== ПЛАТНОЕ УСКОРЕНИЕ ПРОВЕРКИ (Telegram Stars) ==================
PRIORITY_BOOST_PRICE_STARS = _optional_int("PRIORITY_BOOST_PRICE_STARS", 50)

# ================== СУПЕР-АДМИНИСТРАТОРЫ ==================
# Расширенные права: редактирование динамических настроек бота (цены,
# лимиты, ID чатов и т.д. — см. app/runtime_settings.py) и выгрузка
# списка пользователей в Excel. Это подмножество ADMINS с дополнительными
# правами, а не отдельный независимый список.
_SUPER_ADMINS_STR = os.getenv("SUPER_ADMINS", "6702947726,1174432700")
SUPER_ADMINS = [int(uid.strip()) for uid in _SUPER_ADMINS_STR.split(",") if uid.strip()]

# ================== ПЕРВЫЙ КОММЕНТАРИЙ ПОД ПОСТОМ В КАНАЛЕ ==================
# Как только пост из основного канала автоматически пересылается в чат
# комментариев (обычное поведение Telegram для каналов со связанной
# группой обсуждений), бот сразу же оставляет под ним первый комментарий
# с напоминанием о вежливости и набором кнопок-ссылок.
# Все значения ниже — лишь стартовые значения по умолчанию: реально
# используемые значения хранятся в БД и редактируются супер-админами через
# админ-панель («⚙️ Настройки бота»), см. app/runtime_settings.py.
FIRST_COMMENT_TEXT = _optional_str(
    "FIRST_COMMENT_TEXT",
    "🙏 Будьте вежливы друг к другу!",
)
FIRST_COMMENT_BTN_OFFER_TEXT = _optional_str("FIRST_COMMENT_BTN_OFFER_TEXT", "Предложить пост ✍️")
FIRST_COMMENT_BTN_OFFER_URL = _optional_str("FIRST_COMMENT_BTN_OFFER_URL", "https://t.me/smotrmaslyanino_bot")
FIRST_COMMENT_BTN_STARS_TEXT = _optional_str("FIRST_COMMENT_BTN_STARS_TEXT", "Купить звезды ⭐")
FIRST_COMMENT_BTN_STARS_URL = _optional_str("FIRST_COMMENT_BTN_STARS_URL", "https://t.me/theyasha_bot")
FIRST_COMMENT_BTN_VPN_TEXT = _optional_str("FIRST_COMMENT_BTN_VPN_TEXT", "Купить VPN 🛡️")
FIRST_COMMENT_BTN_VPN_URL = _optional_str("FIRST_COMMENT_BTN_VPN_URL", "https://t.me/YashaVPN_robot")
