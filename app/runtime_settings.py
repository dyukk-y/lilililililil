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
    "MIN_SLOT_GAP_MINUTES": (int, "Мин. зазор между публикациями, мин"),
    "DUPLICATE_REPEAT_LIMIT": (int, "Порог повторов для автоотказа"),
    "DUPLICATE_SIMILARITY_THRESHOLD": (float, "Порог схожести текста (0-1)"),
    "NSFW_EXPLICIT_THRESHOLD": (float, "Порог NSFW-фильтра фото (0-1)"),
    "DELETION_REQUEST_TIMEOUT_HOURS": (int, "Срок ответа на заявку удаления, ч"),
    "MODERATION_TIMEOUT_HOURS": (int, "Срок ручной модерации поста, ч"),
    "AUTHOR_LOOKUP_PRICE_STARS": (int, "Цена расшифровки автора, ⭐"),
    "PRIORITY_BOOST_PRICE_STARS": (int, "Цена ускорения проверки, ⭐"),
    "UNLOCK_PRICE_STARS": (int, "Цена разблокировки (бот/канал/комментарии), ⭐"),
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
            if key in rows:
                try:
                    value = _deserialize(key, rows[key])
                except (ValueError, TypeError):
                    logger.warning(f"Не удалось прочитать настройку {key}='{rows[key]}' из БД, беру значение из .env")
                    value = _default_from_config(key)
            else:
                value = _default_from_config(key)
                await db.execute(
                    "INSERT OR REPLACE INTO settings(key, value, updated_time) VALUES(?,?,?)",
                    (key, _serialize(key, value), str(datetime.now())),
                )

            if key in _INPLACE_LIST_TARGETS:
                target = _INPLACE_LIST_TARGETS[key]()
                target.clear()
                target.extend(value)
                SETTINGS[key] = target
            else:
                SETTINGS[key] = value

        await db.commit()
    logger.info(f"Загружено {len(SETTINGS)} динамических настроек")


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

    if key.endswith("_URL") and not (parsed.startswith("https://") or parsed.startswith("http://")):
        return False, "Ссылка должна начинаться с https:// (или http://)"

    if (key.endswith("_LABEL") or key.endswith("_TEXT")) and not parsed:
        return False, "Текст не может быть пустым"

    if key in _INPLACE_LIST_TARGETS:
        target = _INPLACE_LIST_TARGETS[key]()
        target.clear()
        target.extend(parsed)
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
