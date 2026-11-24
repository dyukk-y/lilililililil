"""
Динамически изменяемые настройки бота.

Большинство "тонких" параметров (цены, лимиты, ID чатов, пороги
автомодерации) читаются не напрямую из .env через app.config, а отсюда —
это позволяет двум супер-администраторам (см. app.config.SUPER_ADMINS)
менять их прямо в админ-панели без перезапуска бота и без правки .env.

Как это работает:
- При первом старте бота (или для новых ключей после обновления) значение
  берётся из .env (через app.config) как значение по умолчанию и
  сохраняется в таблицу settings.
- При следующих запусках значение читается из БД — правки .env для уже
  сохранённых ключей перестают на них влиять (это ожидаемо: раз админ
  один раз поменял цену через бота, она не должна "откатываться" при
  каждом деплое).
- В памяти всегда держится актуальный снимок (мутируется на месте, как и
  REQUIRED_SUBSCRIPTIONS в app.config) — чтение через get() не ходит в БД.

Что НЕ входит в динамические настройки (осознанно, остаётся только в .env):
- BOT_TOKEN — секрет, менять "на лету" через бота небезопасно и бессмысленно.
- DB_NAME — путь к файлу БД, менять на ходу технически не имеет смысла.
- PUBLISH_TIMEZONE — влияет на интерпретацию уже сохранённых расписаний,
  смена "на лету" может запутать уже запланированные публикации.
- BACKUP_DIR — путь на файловой системе.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Tuple

import aiosqlite

from app import config as _cfg
from app.config import DB_NAME

logger = logging.getLogger(__name__)


def _parse_int_list(value: str) -> List[int]:
    return [int(x.strip()) for x in value.split(",") if x.strip()]


def _format_int_list(value: List[int]) -> str:
    return ",".join(str(x) for x in value)


# key -> (тип, человекочитаемое название)
# Порядок влияет на порядок отображения в админ-панели.
SETTINGS_SCHEMA: Dict[str, Tuple[type, str]] = {
    "MAIN_CHANNEL_ID": (int, "ID основного канала"),
    "COMMENTS_CHAT_ID": (int, "ID чата комментариев"),
    "MODERATORS_CHAT_ID": (int, "ID чата модераторов"),
    "MODERATORS_TOPIC_ID": (int, "ID темы модераторов"),
    "ADMINS_CHAT_ID": (int, "ID чата администраторов"),
    "ADMINS_TOPIC_ID": (int, "ID темы администраторов"),
    "ADMINS": (list, "Список ID администраторов"),
    "DELETION_REVIEWERS": (list, "Рецензенты заявок на удаление"),
    "PUBLISH_WINDOW_START_HOUR": (int, "Начало окна публикации (час, 0-23)"),
    "PUBLISH_WINDOW_END_HOUR": (int, "Конец окна публикации (час, 0-23)"),
    "PUBLISH_WINDOW_END_MINUTE": (int, "Конец окна публикации (минута, 0-59)"),
    "DAILY_PUBLISH_LIMIT": (int, "Лимит публикаций в день"),
    "USER_DAILY_POST_LIMIT": (int, "Лимит постов пользователя в день"),
    "MAX_POST_LENGTH": (int, "Максимальная длина поста"),
    "MIN_SLOT_GAP_MINUTES": (int, "Мин. зазор между публикациями, мин"),
    "DUPLICATE_REPEAT_LIMIT": (int, "Порог повторов для автоотказа"),
    "DUPLICATE_SIMILARITY_THRESHOLD": (float, "Порог схожести текста (0-1)"),
    "DUPLICATE_LOOKBACK_HOURS": (int, "Окно проверки повторов, ч"),
    "PHOTO_DUPLICATE_LOOKBACK_HOURS": (int, "Окно проверки повторов фото, ч"),
    "AI_SHADOW_MODE": (int, "Теневой режим ИИ (0/1)"),
    "NSFW_EXPLICIT_THRESHOLD": (float, "Порог NSFW-фильтра фото (0-1)"),
    "DELETION_REQUEST_TIMEOUT_HOURS": (int, "Срок ответа на заявку удаления, ч"),
    "MODERATION_TIMEOUT_HOURS": (int, "Срок ручной модерации поста, ч"),
    "AUTHOR_LOOKUP_PRICE_STARS": (int, "Цена расшифровки автора, ⭐"),
    "PRIORITY_BOOST_PRICE_STARS": (int, "Цена ускорения проверки, ⭐"),
    "UNLOCK_PRICE_STARS": (int, "Цена разблокировки (бот/канал/комментарии), ⭐"),
    "PIN_PRICE_STARS_PER_HOUR": (int, "Цена закрепа своего поста за 1 час, ⭐"),
    "NOTIFY_AUTHOR_ON_PUBLISH": (int, "Сообщать автору о публикации (0/1)"),
    "RICH_MESSAGES": (int, "Таблицы в админ-панели (0/1)"),
    "AUTOPILOT_TEXT": (int, "Автопилот для текста (0/1)"),
    "AUTOPILOT_DECIDE_HOURS": (int, "Через сколько часов решает автопилот"),
    "AUTOPILOT_TRUSTED_PHOTOS": (int, "Автопубликация фото доверенных (0/1)"),
    "TRUSTED_MIN_PUBLISHED": (int, "Доверенный автор: мин. публикаций"),
    "AUTOBAN_ENABLED": (int, "Автобан за повторные нарушения (0/1)"),
    "AUTOBAN_VIOLATIONS": (int, "Нарушений за сутки до автобана"),
    "BACKUP_INTERVAL_MINUTES": (int, "Интервал резервного копирования, мин"),
    "BACKUP_KEEP_LAST": (int, "Сколько резервных копий хранить"),
    "INTRO_COMMENT_TEXT": (str, "Текст первого комментария под постом"),
    "INTRO_COMMENT_BTN1_LABEL": (str, "Кнопка 1 — текст"),
    "INTRO_COMMENT_BTN1_URL": (str, "Кнопка 1 — ссылка"),
    "INTRO_COMMENT_BTN2_LABEL": (str, "Кнопка 2 — текст"),
    "INTRO_COMMENT_BTN2_URL": (str, "Кнопка 2 — ссылка"),
    "INTRO_COMMENT_BTN3_LABEL": (str, "Кнопка 3 — текст"),
    "INTRO_COMMENT_BTN3_URL": (str, "Кнопка 3 — ссылка"),
}

# Группировка настроек для компактного меню в админ-панели.
SETTINGS_CATEGORIES: List[Tuple[str, List[str]]] = [
    ("💬 Чаты и роли", [
        "MAIN_CHANNEL_ID", "COMMENTS_CHAT_ID", "MODERATORS_CHAT_ID", "MODERATORS_TOPIC_ID",
        "ADMINS_CHAT_ID", "ADMINS_TOPIC_ID", "ADMINS", "DELETION_REVIEWERS",
    ]),
    ("🗓 Публикация", [
        "PUBLISH_WINDOW_START_HOUR", "PUBLISH_WINDOW_END_HOUR", "PUBLISH_WINDOW_END_MINUTE",
        "DAILY_PUBLISH_LIMIT", "USER_DAILY_POST_LIMIT", "MAX_POST_LENGTH", "MIN_SLOT_GAP_MINUTES",
        "NOTIFY_AUTHOR_ON_PUBLISH",
    ]),
    ("🛡 Автомодерация", [
        "DUPLICATE_REPEAT_LIMIT", "DUPLICATE_SIMILARITY_THRESHOLD", "DUPLICATE_LOOKBACK_HOURS",
        "PHOTO_DUPLICATE_LOOKBACK_HOURS", "AI_SHADOW_MODE", "NSFW_EXPLICIT_THRESHOLD",
        "MODERATION_TIMEOUT_HOURS", "DELETION_REQUEST_TIMEOUT_HOURS",
    ]),
    ("🤖 Автопилот", ["AUTOPILOT_TEXT", "AUTOPILOT_DECIDE_HOURS", "AUTOPILOT_TRUSTED_PHOTOS",
                      "TRUSTED_MIN_PUBLISHED", "AUTOBAN_ENABLED", "AUTOBAN_VIOLATIONS"]),
    ("⭐ Цены", ["AUTHOR_LOOKUP_PRICE_STARS", "PRIORITY_BOOST_PRICE_STARS", "UNLOCK_PRICE_STARS",
               "PIN_PRICE_STARS_PER_HOUR"]),
    ("🗄 Копии и вид", ["BACKUP_INTERVAL_MINUTES", "BACKUP_KEEP_LAST", "RICH_MESSAGES"]),
]


def category_of(key: str) -> int | None:
    for idx, (_, keys) in enumerate(SETTINGS_CATEGORIES):
        if key in keys:
            return idx
    return None


# Текущие значения в памяти (заполняется в load_settings()).
SETTINGS: Dict[str, Any] = {}


def _serialize(key: str, value: Any) -> str:
    value_type = SETTINGS_SCHEMA[key][0]
    if value_type is list:
        return _format_int_list(value)
    return str(value)


def _deserialize(key: str, raw: str) -> Any:
    value_type = SETTINGS_SCHEMA[key][0]
    if value_type is list:
        return _parse_int_list(raw)
    if value_type is int:
        return int(raw)
    if value_type is float:
        return float(raw)
    return raw


def _default_from_config(key: str) -> Any:
    """Стартовое значение — то, что уже вычислено в app.config из .env."""
    return getattr(_cfg, key)


# Списковые настройки, для которых в app.config уже есть мутируемый-на-месте
# список (тот же паттерн, что и REQUIRED_SUBSCRIPTIONS) — вместо создания
# нового списка при каждом set_value() мы очищаем и заполняем ТОТ ЖЕ объект,
# поэтому все модули, сделавшие "from app.config import ADMINS" при
# импорте, продолжают видеть актуальные данные без единой правки в них.
_INPLACE_LIST_TARGETS = {
    "ADMINS": lambda: _cfg.ADMINS,
    "DELETION_REVIEWERS": lambda: _cfg.DELETION_REVIEWERS,
}


async def load_settings() -> None:
    """Вызывается один раз при старте бота (после init_db)."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT key, value FROM settings")
        rows = {row[0]: row[1] for row in await cur.fetchall()}

        for key in SETTINGS_SCHEMA:
            # ADMINS — критическая настройка доступа. Если список явно задан
            # в .env, он является источником истины при старте. Иначе старая
            # версия могла оставить в SQLite прежний список администраторов,
            # из-за чего новые админы из .env получали "нет доступа".
            # После загрузки актуальный список синхронизируется обратно в БД.
            if key == "ADMINS" and str(getattr(_cfg, "_ADMINS_STR", "")).strip():
                value = _default_from_config(key)
                await db.execute(
                    "INSERT OR REPLACE INTO settings(key, value, updated_time) VALUES(?,?,?)",
                    (key, _serialize(key, value), str(datetime.now())),
                )
            elif key in rows:
                try:
                    value = _deserialize(key, rows[key])
                except (ValueError, TypeError):
                    logger.warning(
                        "Не удалось прочитать настройку %s=%r из БД, беру значение из .env",
                        key, rows[key],
                    )
                    value = _default_from_config(key)
            else:
                value = _default_from_config(key)
                await db.execute(
                    "INSERT OR REPLACE INTO settings(key, value, updated_time) VALUES(?,?,?)",
                    (key, _serialize(key, value), str(datetime.now())),
                )

            if key in _INPLACE_LIST_TARGETS:
                target = _INPLACE_LIST_TARGETS[key]()
                # ВАЖНО: копия ДО clear(). Значение по умолчанию — это тот
                # же самый объект списка из app.config, и раньше clear()
                # опустошал его, а extend() добавлял уже пустой список. Итог:
                # ADMINS в памяти всегда был пуст (уведомления о платном
                # приоритете не уходили ни одному администратору), а
                # DELETION_REVIEWERS — пуст после первого запуска.
                new_items = list(value)
                target.clear()
                target.extend(new_items)
                SETTINGS[key] = target
            else:
                SETTINGS[key] = value

        await db.commit()
    logger.info("Загружено %s динамических настроек", len(SETTINGS))


def get(key: str) -> Any:
    """Текущее значение настройки."""
    if key not in SETTINGS:
        # На случай обращения до load_settings() (не должно происходить в
        # проде, но не должно и падать намертво) — берём дефолт из .env.
        return _default_from_config(key)
    return SETTINGS[key]


async def set_value(key: str, raw_value: str) -> Tuple[bool, str]:
    """Устанавливает новое значение настройки. Возвращает (успех, сообщение)."""
    if key not in SETTINGS_SCHEMA:
        return False, f"Неизвестная настройка: {key}"

    raw_value = raw_value.strip()

    try:
        parsed = _deserialize(key, raw_value)
    except (ValueError, TypeError):
        value_type = SETTINGS_SCHEMA[key][0]
        type_hint = {"int": "целое число", "float": "число", "list": "ID через запятую"}.get(value_type.__name__, "текст")
        return False, f"Неверный формат. Ожидается: {type_hint}"

    # Жёсткие диапазоны защищают scheduler, платежи и модерацию от
    # логически невозможных значений.
    ranges = {
        "PUBLISH_WINDOW_START_HOUR": (0, 23),
        "PUBLISH_WINDOW_END_HOUR": (0, 23),
        "PUBLISH_WINDOW_END_MINUTE": (0, 59),
        "DAILY_PUBLISH_LIMIT": (1, 10000),
        "USER_DAILY_POST_LIMIT": (1, 100),
        "MAX_POST_LENGTH": (5, 4096),
        "MIN_SLOT_GAP_MINUTES": (0, 1440),
        "DUPLICATE_REPEAT_LIMIT": (1, 100),
        "DUPLICATE_SIMILARITY_THRESHOLD": (0.0, 1.0),
        "DUPLICATE_LOOKBACK_HOURS": (1, 720),
        "PHOTO_DUPLICATE_LOOKBACK_HOURS": (1, 720),
        "AI_SHADOW_MODE": (0, 1),
        "NSFW_EXPLICIT_THRESHOLD": (0.0, 1.0),
        "DELETION_REQUEST_TIMEOUT_HOURS": (1, 720),
        "MODERATION_TIMEOUT_HOURS": (1, 720),
        "AUTHOR_LOOKUP_PRICE_STARS": (1, 1_000_000),
        "PRIORITY_BOOST_PRICE_STARS": (1, 1_000_000),
        "UNLOCK_PRICE_STARS": (1, 1_000_000),
        "PIN_PRICE_STARS_PER_HOUR": (1, 100_000),
        "NOTIFY_AUTHOR_ON_PUBLISH": (0, 1),
        "RICH_MESSAGES": (0, 1),
        "AUTOPILOT_TEXT": (0, 1),
        "AUTOPILOT_DECIDE_HOURS": (1, 72),
        "AUTOPILOT_TRUSTED_PHOTOS": (0, 1),
        "TRUSTED_MIN_PUBLISHED": (1, 1000),
        "AUTOBAN_ENABLED": (0, 1),
        "AUTOBAN_VIOLATIONS": (2, 50),
        "BACKUP_INTERVAL_MINUTES": (1, 10080),
        "BACKUP_KEEP_LAST": (1, 1000),
    }
    if key in ranges:
        lo, hi = ranges[key]
        if not (lo <= parsed <= hi):
            return False, f"Значение должно быть от {lo} до {hi}"

    if key.endswith("_URL") and not (parsed.startswith("https://") or parsed.startswith("http://")):
        return False, "Ссылка должна начинаться с https:// (или http://)"

    if (key.endswith("_LABEL") or key.endswith("_TEXT")) and not parsed:
        return False, "Текст не может быть пустым"
    if key.endswith("_LABEL") and len(parsed) > 32:
        return False, "Текст кнопки слишком длинный — максимум 32 символа, чтобы он полностью помещался"

    # Пустой список администраторов = никто не сможет зайти в админ-панель.
    if key == "ADMINS" and not parsed:
        return False, "Список администраторов не может быть пустым"
    if key == "MAIN_CHANNEL_ID" and parsed == 0:
        return False, "ID основного канала обязателен"

    if key in ("PUBLISH_WINDOW_START_HOUR", "PUBLISH_WINDOW_END_HOUR", "PUBLISH_WINDOW_END_MINUTE"):
        start_hour = parsed if key == "PUBLISH_WINDOW_START_HOUR" else get("PUBLISH_WINDOW_START_HOUR")
        end_hour = parsed if key == "PUBLISH_WINDOW_END_HOUR" else get("PUBLISH_WINDOW_END_HOUR")
        end_minute = parsed if key == "PUBLISH_WINDOW_END_MINUTE" else get("PUBLISH_WINDOW_END_MINUTE")
        # Ночное окно вроде 23:00→08:00 поддерживается scheduler'ом.
        if start_hour == end_hour and end_minute == 0:
            return False, "Окно публикации не может иметь нулевую длительность"

    if key in _INPLACE_LIST_TARGETS:
        target = _INPLACE_LIST_TARGETS[key]()
        new_items = list(parsed)
        target.clear()
        target.extend(new_items)
        SETTINGS[key] = target
    else:
        SETTINGS[key] = parsed

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings(key, value, updated_time) VALUES(?,?,?)",
            (key, _serialize(key, parsed), str(datetime.now())),
        )
        await db.commit()
    logger.info(f"Настройка {key} изменена на {parsed!r}")
    return True, "OK"


def get_label(key: str) -> str:
    return SETTINGS_SCHEMA.get(key, (None, key))[1]


def display_value(key: str) -> str:
    value = get(key)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "—"
    if value is None:
        return "—"
    return str(value)


def all_keys() -> List[str]:
    return list(SETTINGS_SCHEMA.keys())
