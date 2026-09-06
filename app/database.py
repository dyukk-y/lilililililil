"""
Слой работы с базой данных.

Все функции открывают короткоживущее соединение aiosqlite на каждый вызов —
для нагрузки уровня одного Telegram-бота (десятки/сотни запросов в секунду)
это нормально и проще, чем пул соединений, при условии что включён WAL
(см. init_db) и на таблицах есть нужные индексы.
"""
import asyncio
import logging
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite

from app.config import DB_NAME, REQUIRED_SUBSCRIPTIONS, TIMEZONE
from app.runtime_settings import get as get_setting

logger = logging.getLogger(__name__)


# ================== СТАНДАРТНЫЙ СПИСОК СТОП-СЛОВ ==================
# Базовые корни мата и распространённых оскорблений. Загружается в таблицу
# publication_blacklist один раз при первом старте (если таблица пуста),
# дальше администратор полностью управляет списком через админ-панель
# (добавление/удаление слов) — этот список лишь стартовая точка.
DEFAULT_BLACKLIST_WORDS = [
    # маты (корни — ловят и однокоренные слова за счёт поиска по подстроке)
    "хуй", "хуе", "хуя", "хуё", "пизд", "ебат", "ебал", "ебан", "ебуч", "ёб ",
    "мудак", "мудо", "сука", "сучк", "бляд", "блять", "гандон", "залуп",
    "пидор", "пидар", "пидр", "долбоеб", "долбаеб", "сволоч", "ублюд",
    "чмо ", "хер ", "трахат", "шлюх", "манда", "мразь",
    # оскорбления
    "идиот", "дебил", "дурак", "тупиц", "урод", "кретин", "придурок",
    "дегенерат", "тварь", "скотина", "быдло", "лох ",
]


# ================== ВСПОМОГАТЕЛЬНОЕ: НОРМАЛИЗАЦИЯ ТЕКСТА ==================
_EMOJI_PATTERN = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2700-\u27BF]+"
)


def normalize_text(text: str) -> str:
    """Приводит текст поста к виду, удобному для нечёткого сравнения:
    убирает эмодзи, пунктуацию, лишние пробелы, регистр."""
    if not text:
        return ""
    text = text.lower()
    text = _EMOJI_PATTERN.sub("", text)
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ================== ИНИЦИАЛИЗАЦИЯ ==================
async def init_db() -> None:
    """Создаёт таблицы, индексы и включает WAL-режим для конкурентного доступа."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            reg_date TEXT,
            is_subscribed INTEGER DEFAULT 0,
            comments_count INTEGER DEFAULT 0,
            mentions_count INTEGER DEFAULT 0,
            trust_score REAL DEFAULT 0
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS posts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            text TEXT,
            photo TEXT,
            time TEXT,
            status TEXT DEFAULT 'moderation',
            moderator_id INTEGER,
            moderation_time TEXT,
            reject_reason TEXT,
            message_id_moderators INTEGER,
            message_id_admins INTEGER,
            chat_id_moderators INTEGER,
            chat_id_admins INTEGER,
            scheduled_time TEXT,
            auto_status TEXT,
            text_norm TEXT,
            channel_message_id INTEGER,
            review_deadline TEXT,
            comment_posted INTEGER DEFAULT 0,
            publish_attempts INTEGER DEFAULT 0,
            last_publish_error TEXT,
            publishing_started_at TEXT,
            comment_claimed_at TEXT,
            published_at TEXT,
            photo_hash TEXT,
            ai_score INTEGER,
            ai_confidence REAL,
            ai_decision TEXT,
            ai_reason TEXT,
            ocr_text TEXT
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS bans(
            user_id INTEGER PRIMARY KEY,
            reason TEXT,
            ban_time TEXT,
            admin_id INTEGER,
            admin_username TEXT
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS publication_blacklist(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT UNIQUE,
            added_by INTEGER,
            added_time TEXT
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            data TEXT,
            time TEXT
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS required_subscriptions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sub_type TEXT NOT NULL,
            sub_id TEXT NOT NULL,
            username TEXT NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            added_by INTEGER,
            added_time TEXT,
            UNIQUE(sub_type, sub_id)
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS deletion_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            requester_id INTEGER NOT NULL,
            reason TEXT,
            status TEXT DEFAULT 'pending_admin',
            created_time TEXT,
            expire_time TEXT,
            decided_by INTEGER,
            decided_time TEXT,
            admin_refs TEXT
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS star_purchases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            post_id INTEGER,
            amount_stars INTEGER,
            telegram_charge_id TEXT,
            time TEXT
        )""")
        # Повторная "расшифровка автора" одного и того же поста одним и тем же
        # пользователем не должна тарифицироваться дважды (см. get_star_purchase) —
        # это гарантируется частичным уникальным индексом только для kind='author_reveal'.
        # Для kind='priority_boost' повторные покупки разрешены (человек может
        # ускорить публикацию несколько раз).
        await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_star_purchases_reveal_unique
        ON star_purchases(user_id, post_id) WHERE kind='author_reveal'
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_time TEXT
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS ai_feedback(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER,
            ai_decision TEXT,
            human_decision TEXT NOT NULL,
            ai_score INTEGER,
            ai_confidence REAL,
            trust_before REAL,
            trust_delta REAL,
            created_time TEXT
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS ai_corrections(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            admin_id INTEGER NOT NULL,
            correction TEXT NOT NULL,
            created_time TEXT
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS auto_approve_phrases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phrase TEXT UNIQUE,
            added_by INTEGER,
            added_time TEXT
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS auto_approve_phrase_disabled(
            phrase TEXT PRIMARY KEY,
            disabled_by INTEGER,
            disabled_time TEXT
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS advertising_posts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            source_chat_id INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            preview_chat_id INTEGER,
            preview_message_id INTEGER,
            control_chat_id INTEGER,
            control_message_id INTEGER,
            channel_message_id INTEGER,
            duration_hours INTEGER NOT NULL,
            pin_duration_hours INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            published_at TEXT,
            expires_at TEXT,
            pin_expires_at TEXT,
            status TEXT DEFAULT 'draft',
            error TEXT,
            ad_type TEXT DEFAULT 'post',
            subscription_type TEXT,
            subscription_id TEXT,
            subscription_username TEXT,
            subscription_name TEXT,
            subscription_url TEXT,
            subscription_hours INTEGER DEFAULT 0,
            broadcast_count INTEGER DEFAULT 0
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS advertising_subscriptions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id INTEGER NOT NULL,
            sub_type TEXT NOT NULL,
            sub_id TEXT NOT NULL,
            username TEXT DEFAULT '',
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            UNIQUE(ad_id, sub_type, sub_id)
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS advertising_broadcasts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_id INTEGER NOT NULL,
            sequence_no INTEGER NOT NULL,
            scheduled_at TEXT NOT NULL,
            sent_at TEXT,
            status TEXT DEFAULT 'scheduled',
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            UNIQUE(ad_id, sequence_no)
        )""")

        # Идемпотентная доставка рекламных рассылок: один пользователь не
        # должен получить одну и ту же рассылку повторно после временной ошибки
        # или перезапуска процесса.
        await db.execute("""
        CREATE TABLE IF NOT EXISTS advertising_broadcast_deliveries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broadcast_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            attempts INTEGER DEFAULT 0,
            sent_at TEXT,
            last_error TEXT,
            UNIQUE(broadcast_id, user_id)
        )""")

        await db.commit()

        # Для тех, кто обновляется со старой версии БД (созданной до появления
        # scheduled_time/auto_status/text_norm/comments_count/mentions_count) —
        # добавляем недостающие колонки, не трогая существующие данные.
        await _ensure_schema_migrations(db)

        # Индексы под самые частые запросы (статус поста, посты пользователя за период,
        # очередь публикации, выборка по времени бана и т.д.) — без них каждый такой
        # SELECT на большой таблице posts/logs превращается в полное сканирование.
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_user_time ON posts(user_id, time)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_scheduled ON posts(status, scheduled_time)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_channel_msg ON posts(channel_message_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_bans_ban_time ON bans(ban_time)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_logs_time ON logs(time)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_deletion_status ON deletion_requests(status, expire_time)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_review_deadline ON posts(status, auto_status, review_deadline)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_star_purchases_user ON star_purchases(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_text_norm ON posts(text_norm)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_photo_hash ON posts(photo_hash)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_published_at ON posts(published_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_posts_ai_decision ON posts(ai_decision)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_time ON ai_feedback(created_time)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_post ON ai_feedback(post_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_corrections_time ON ai_corrections(created_time)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_advertising_status_expiry ON advertising_posts(status, expires_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_advertising_pin_expiry ON advertising_posts(status, pin_expires_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_advertising_admin ON advertising_posts(admin_id, created_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_sub_expiry ON advertising_subscriptions(status, expires_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_broadcast_due ON advertising_broadcasts(status, scheduled_at)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ad_broadcast_delivery ON advertising_broadcast_deliveries(broadcast_id, status)")
        # Если процесс был убит во время отправки рассылки, незавершённые
        # операции безопасно возвращаются в очередь после перезапуска.
        await db.execute("UPDATE advertising_broadcasts SET status='scheduled' WHERE status='sending'")
        await db.execute("UPDATE advertising_broadcast_deliveries SET status='pending' WHERE status='sending'")
        # Идемпотентность платежей: сначала оставляем самую раннюю запись
        # каждого charge_id, чтобы индекс можно было безопасно добавить и на
        # старую БД с историческими дублями.
        await db.execute("""
            DELETE FROM star_purchases
            WHERE telegram_charge_id IS NOT NULL AND telegram_charge_id != ''
              AND id NOT IN (
                  SELECT MIN(id) FROM star_purchases
                  WHERE telegram_charge_id IS NOT NULL AND telegram_charge_id != ''
                  GROUP BY telegram_charge_id
              )
        """)
        await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_star_charge_unique ON star_purchases(telegram_charge_id) WHERE telegram_charge_id IS NOT NULL AND telegram_charge_id != ''")

        await db.commit()
        logger.info("База данных инициализирована")
        await load_subscriptions_from_db()
        await seed_default_blacklist()
        await seed_default_auto_approve_phrases()


async def _ensure_schema_migrations(db: aiosqlite.Connection) -> None:
    """Добавляет колонки, которых не было в более старых версиях БД."""
    async def _columns(table: str) -> set:
        cur = await db.execute(f"PRAGMA table_info({table})")
        return {row[1] for row in await cur.fetchall()}

    posts_cols = await _columns("posts")
    for col, coltype in (
        ("scheduled_time", "TEXT"),
        ("auto_status", "TEXT"),
        ("text_norm", "TEXT"),
        ("channel_message_id", "INTEGER"),
        ("review_deadline", "TEXT"),
        ("comment_posted", "INTEGER DEFAULT 0"),
        ("publish_attempts", "INTEGER DEFAULT 0"),
        ("last_publish_error", "TEXT"),
        ("publishing_started_at", "TEXT"),
        ("comment_claimed_at", "TEXT"),
        ("published_at", "TEXT"),
        ("photo_hash", "TEXT"),
        ("ai_score", "INTEGER"),
        ("ai_confidence", "REAL"),
        ("ai_decision", "TEXT"),
        ("ai_reason", "TEXT"),
        ("ocr_text", "TEXT"),
        ("publish_next_retry_at", "TEXT"),
    ):
        if col not in posts_cols:
            await db.execute(f"ALTER TABLE posts ADD COLUMN {col} {coltype}")
            logger.info(f"Миграция: добавлена колонка posts.{col}")

    ads_cols = await _columns("advertising_posts")
    if "ad_type" not in ads_cols:
        await db.execute("ALTER TABLE advertising_posts ADD COLUMN ad_type TEXT DEFAULT 'post'")
        logger.info("Миграция: добавлена колонка advertising_posts.ad_type")
    ads_cols = await _columns("advertising_posts")
    for col, ddl in (("subscription_type", "TEXT"), ("subscription_id", "TEXT"), ("subscription_username", "TEXT"), ("subscription_name", "TEXT"), ("subscription_url", "TEXT"), ("subscription_hours", "INTEGER DEFAULT 0"), ("broadcast_count", "INTEGER DEFAULT 0")):
        if col not in ads_cols:
            await db.execute(f"ALTER TABLE advertising_posts ADD COLUMN {col} {ddl}")
            logger.info("Миграция: добавлена колонка advertising_posts.%s", col)

    users_cols = await _columns("users")
    for col, coltype in (
        ("comments_count", "INTEGER DEFAULT 0"),
        ("mentions_count", "INTEGER DEFAULT 0"),
        ("trust_score", "REAL DEFAULT 0"),
    ):
        if col not in users_cols:
            await db.execute(f"ALTER TABLE users ADD COLUMN {col} {coltype}")
            logger.info(f"Миграция: добавлена колонка users.{col}")

    await db.commit()

    # Бэкфилл text_norm для постов, созданных до появления дедупликации
    cur = await db.execute("SELECT id, text FROM posts WHERE text_norm IS NULL")
    rows = await cur.fetchall()
    for post_id, text in rows:
        await db.execute(
            "UPDATE posts SET text_norm=? WHERE id=?", (normalize_text(text or ""), post_id)
        )
    if rows:
        await db.commit()
        logger.info(f"Миграция: пересчитан text_norm для {len(rows)} постов")


async def seed_default_blacklist() -> None:
    """Загружает стандартный список стоп-слов (маты/оскорбления), только если
    чёрный список публикаций ещё пуст — чтобы не затирать правки администратора
    при последующих перезапусках."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM publication_blacklist")
        count = (await cur.fetchone())[0]
        if count > 0:
            return

        now = str(datetime.now())
        added = 0
        for word in DEFAULT_BLACKLIST_WORDS:
            try:
                await db.execute(
                    "INSERT INTO publication_blacklist(keyword, added_by, added_time) VALUES(?,?,?)",
                    (word, None, now),
                )
                added += 1
            except aiosqlite.IntegrityError:
                pass
        await db.commit()
        if added:
            logger.info(f"Загружен стандартный список из {added} стоп-слов (маты/оскорбления)")


# Стандартные "ключевые фразы" — если текст поста содержит хотя бы одну из
# них (без учёта регистра), пост считается кандидатом на полностью
# автоматическую публикацию (при условии, что прошёл остальные проверки:
# стоп-слова, дубликаты, осмысленность текста). Если ни одной фразы нет —
# пост уходит на обязательную ручную модерацию, как и посты с фото.
DEFAULT_AUTO_APPROVE_PHRASES = [
    # Идентификация человека
    "что за", "что за мальчик", "что за девочка", "что за парень",
    "что за девушка", "что за мальчики", "что за девочки",
    "расскажите про", "расскажите о", "расскажите кто",
    "расскажите кто такой", "расскажите кто такая", "расскажите о нем",
    "расскажите о нём", "кто это", "кто она", "кто он", "кто такой",
    "кто такая", "кто знает", "подскажите кто", "как зовут",
    # Контакты / знакомство
    "срочно дайте юз", "юз в лс", "юзернейм в лс", "контакты в лс",
    "дайте юз", "дайте юзернейм", "можно юз", "можно инст",
    "хочу познакомиться", "хочу познакомится", "было приятно познакомиться",
    "понравилась", "понравился", "понравилась девушка", "понравился парень",
    # Поиск / встреча
    "разыскивается", "ищу эту", "ищу этого", "ищу эту девушку",
    "ищу этого парня", "видели её", "видели ее", "видели его",
    "где найти", "где можно встретить", "кто едет в", "кто будет в",
    "у нас в", "с кем можно",
]


async def seed_default_auto_approve_phrases() -> None:
    """Добавляет недостающие системные фразы, не затирая существующие записи.

    INSERT OR IGNORE безопасен для обновлений: уже настроенные фразы и
    добавленные администратором записи не изменяются. Если администратор
    удалил системную фразу, она попадает в таблицу отключений и не возвращается
    при следующем запуске.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        now = str(datetime.now())
        added = 0
        for phrase in DEFAULT_AUTO_APPROVE_PHRASES:
            disabled = await db.execute(
                "SELECT 1 FROM auto_approve_phrase_disabled WHERE phrase=?", (phrase,)
            )
            if await disabled.fetchone():
                continue
            cur = await db.execute(
                "INSERT OR IGNORE INTO auto_approve_phrases(phrase, added_by, added_time) VALUES(?,?,?)",
                (phrase, None, now),
            )
            added += cur.rowcount or 0
        await db.commit()
        if added:
            logger.info(f"Добавлено {added} новых системных фраз для автопубликации")


async def has_auto_approve_trigger(text: str) -> bool:
    """True, если текст содержит хотя бы одну из ключевых фраз-триггеров
    автопубликации (см. auto_approve_phrases)."""
    text_lower = (text or "").lower()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT phrase FROM auto_approve_phrases")
        rows = await cur.fetchall()
    return any(row[0] in text_lower for row in rows)


async def add_auto_approve_phrase(phrase: str, admin_id: int) -> bool:
    phrase_clean = phrase.strip().lower()
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("DELETE FROM auto_approve_phrase_disabled WHERE phrase=?", (phrase_clean,))
            await db.execute(
                "INSERT INTO auto_approve_phrases(phrase, added_by, added_time) VALUES(?,?,?)",
                (phrase_clean, admin_id, str(datetime.now())),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_auto_approve_phrase(phrase: str, admin_id: Optional[int] = None) -> None:
    phrase_clean = phrase.strip().lower()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM auto_approve_phrases WHERE phrase=?", (phrase_clean,))
        if phrase_clean in DEFAULT_AUTO_APPROVE_PHRASES:
            await db.execute(
                "INSERT OR REPLACE INTO auto_approve_phrase_disabled(phrase, disabled_by, disabled_time) VALUES(?,?,?)",
                (phrase_clean, admin_id, str(datetime.now())),
            )
        await db.commit()


async def get_auto_approve_phrases(page: int = 1, per_page: int = 10) -> Tuple[list, int]:
    offset = (page - 1) * per_page
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT phrase, added_by, added_time FROM auto_approve_phrases ORDER BY phrase LIMIT ? OFFSET ?",
            (per_page, offset),
        )
        rows = await cur.fetchall()
        cur_count = await db.execute("SELECT COUNT(*) FROM auto_approve_phrases")
        total = (await cur_count.fetchone())[0]
        return rows, total


async def load_subscriptions_from_db() -> None:
    """Загружает список обязательных подписок из БД (мутирует список на месте)."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("""
            SELECT sub_type, sub_id, username, name, url
            FROM required_subscriptions
            ORDER BY id
        """)
        rows = await cur.fetchall()

        if rows:
            REQUIRED_SUBSCRIPTIONS.clear()
            for row in rows:
                REQUIRED_SUBSCRIPTIONS.append({
                    "type": row[0],
                    "id": row[1],
                    "username": row[2],
                    "name": row[3],
                    "url": row[4],
                })
            logger.info(f"Загружено {len(REQUIRED_SUBSCRIPTIONS)} обязательных подписок из БД")


async def save_subscriptions_to_db() -> None:
    """Сохраняет текущий список подписок (REQUIRED_SUBSCRIPTIONS) в БД."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM required_subscriptions")

        for sub in REQUIRED_SUBSCRIPTIONS:
            await db.execute("""
                INSERT INTO required_subscriptions(sub_type, sub_id, username, name, url, added_time)
                VALUES(?,?,?,?,?,?)
            """, (
                sub["type"],
                str(sub["id"]),
                sub["username"],
                sub["name"],
                sub["url"],
                str(datetime.now()),
            ))

        await db.commit()
        logger.info(f"Сохранено {len(REQUIRED_SUBSCRIPTIONS)} подписок в БД")


# ================== ПОЛЬЗОВАТЕЛИ И ПОДПИСКА ==================
async def update_user_subscription_status(user_id: int, is_subscribed: bool) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET is_subscribed=? WHERE user_id=?",
            (1 if is_subscribed else 0, user_id),
        )
        await db.commit()


async def get_user_subscription_status(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT is_subscribed FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return bool(row and row[0] == 1)


async def register_user(user) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (user.id,))
        if not await cur.fetchone():
            await db.execute(
                "INSERT INTO users(user_id, username, reg_date, is_subscribed) VALUES(?,?,?,?)",
                (user.id, user.username, datetime.now(TIMEZONE).date().isoformat(), 0),
            )
            await db.commit()
            logger.info(f"Зарегистрирован новый пользователь: {user.id}")
        elif user.username:
            # Юзернейм мог измениться — держим его актуальным, иначе учёт
            # упоминаний (по @username) начнёт "терять" пользователя.
            await db.execute(
                "UPDATE users SET username=? WHERE user_id=?", (user.username, user.id)
            )
            await db.commit()


async def get_all_users() -> List[int]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
        return [row[0] for row in rows]


async def get_users_count() -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
        return row[0] if row else 0


async def get_username_by_user_id(user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT username FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def increment_comment_count(user_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET comments_count = COALESCE(comments_count,0) + 1 WHERE user_id=?",
            (user_id,),
        )
        await db.commit()


async def increment_mention_count(username: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET mentions_count = COALESCE(mentions_count,0) + 1 WHERE lower(username)=lower(?)",
            (username,),
        )
        await db.commit()


async def get_user_post_stats(user_id: int) -> Dict[str, int]:
    """Количество постов пользователя по статусам."""
    stats = {"moderation": 0, "approved": 0, "published": 0, "rejected": 0}
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT status, COUNT(*) FROM posts WHERE user_id=? GROUP BY status", (user_id,)
        )
        for status, cnt in await cur.fetchall():
            stats[status] = cnt
    return stats


async def get_user_extra_stats(user_id: int) -> Tuple[int, int]:
    """(комментариев, упоминаний) пользователя."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT comments_count, mentions_count FROM users WHERE user_id=?", (user_id,)
        )
        row = await cur.fetchone()
        return (row[0] or 0, row[1] or 0) if row else (0, 0)


async def get_next_scheduled_time(user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT scheduled_time FROM posts WHERE user_id=? AND status='approved' "
            "AND scheduled_time IS NOT NULL ORDER BY scheduled_time ASC LIMIT 1",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row or not row[0]:
            return None
        try:
            return datetime.fromisoformat(row[0]).strftime('%d.%m.%Y %H:%M')
        except ValueError:
            return row[0]


# ================== ЛОГИ ==================
async def log(action: str, data: str = "") -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO logs(action,data,time) VALUES(?,?,?)",
            (action, data, str(datetime.now())),
        )
        await db.commit()
    logger.info(f"Лог: {action} - {data}")


# ================== БАНЫ ==================
async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT 1 FROM bans WHERE user_id=?", (user_id,))
        return await cur.fetchone() is not None


async def get_ban_info(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            cur = await db.execute(
                "SELECT reason, ban_time, admin_username FROM bans WHERE user_id=?",
                (user_id,),
            )
            return await cur.fetchone()
        except Exception as e:
            logger.error(f"Ошибка получения информации о блокировке: {e}")
            return None


async def ban_user(user_id: int, reason: str, admin) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO bans(user_id, reason, ban_time, admin_id, admin_username) VALUES(?,?,?,?,?)",
            (user_id, reason, str(datetime.now()), admin.id, admin.username or str(admin.id)),
        )
        await db.commit()
    await log("ban", f"admin {admin.id} banned user {user_id}: {reason}")


async def unban_user(user_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM bans WHERE user_id=?", (user_id,))
        await db.commit()
    await log("unban", f"user {user_id} unbanned")


async def get_banned_users(page: int = 1, per_page: int = 5) -> Tuple[list, int]:
    offset = (page - 1) * per_page
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("""
            SELECT b.user_id, b.reason, b.ban_time, b.admin_username, u.username
            FROM bans b
            LEFT JOIN users u ON b.user_id = u.user_id
            ORDER BY b.ban_time DESC
            LIMIT ? OFFSET ?
        """, (per_page, offset))
        rows = await cur.fetchall()

        cur_count = await db.execute("SELECT COUNT(*) FROM bans")
        total = (await cur_count.fetchone())[0]

        return rows, total


# ================== ЧЁРНЫЙ СПИСОК ПУБЛИКАЦИЙ (маты/оскорбления/спам) ==================
async def add_to_publication_blacklist(keyword: str, admin_id: int) -> bool:
    keyword_clean = keyword.strip().lower()
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO publication_blacklist(keyword, added_by, added_time) VALUES(?,?,?)",
                (keyword_clean, admin_id, str(datetime.now())),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_from_publication_blacklist(keyword: str) -> bool:
    keyword_clean = keyword.strip().lower()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM publication_blacklist WHERE keyword=?", (keyword_clean,))
        await db.commit()
        return True


async def get_publication_blacklist(page: int = 1, per_page: int = 5) -> Tuple[list, int]:
    offset = (page - 1) * per_page
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT keyword, added_by, added_time FROM publication_blacklist ORDER BY keyword LIMIT ? OFFSET ?",
            (per_page, offset),
        )
        rows = await cur.fetchall()

        cur_count = await db.execute("SELECT COUNT(*) FROM publication_blacklist")
        total = (await cur_count.fetchone())[0]

        return rows, total


async def is_in_publication_blacklist(text: str) -> Tuple[bool, str]:
    text_lower = text.lower()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT keyword FROM publication_blacklist")
        rows = await cur.fetchall()

        for row in rows:
            keyword = row[0]
            if keyword in text_lower:
                return True, keyword
    return False, ""


# ================== ДЕДУПЛИКАЦИЯ ПОСТОВ ==================
async def count_similar_posts(text: str, exclude_post_id: Optional[int] = None) -> int:
    """Проверяет повторы только в последние N часов. После окна повтор разрешён."""
    norm = normalize_text(text)
    if not norm:
        return 0
    cutoff = (datetime.now(TIMEZONE).replace(tzinfo=None) - timedelta(hours=get_setting("DUPLICATE_LOOKBACK_HOURS"))).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id, text_norm FROM posts WHERE time>=? AND status IN ('published','approved','publishing') "
            "AND text_norm IS NOT NULL AND text_norm != '' AND length(text_norm) BETWEEN ? AND ?",
            (cutoff, max(1,int(len(norm)*0.55)), max(len(norm),int(len(norm)*1.8))),
        )
        rows = await cur.fetchall()
    threshold = get_setting("DUPLICATE_SIMILARITY_THRESHOLD")
    def _count():
        return sum(1 for pid, other in rows if not exclude_post_id or pid != exclude_post_id if other == norm or SequenceMatcher(None,norm,other).ratio() >= threshold)
    return await asyncio.to_thread(_count)

async def get_recent_photo_hashes(photo_hash: str) -> List[str]:
    if not photo_hash: return []
    cutoff=(datetime.now(TIMEZONE).replace(tzinfo=None)-timedelta(hours=get_setting("PHOTO_DUPLICATE_LOOKBACK_HOURS"))).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT photo_hash FROM posts WHERE time>=? AND status IN ('published','approved','publishing') AND photo_hash IS NOT NULL",(cutoff,))
        return [r[0] for r in await cur.fetchall()]

async def set_ai_analysis(post_id:int, score:int, confidence:float, decision:str, reason:str, ocr_text:str="") -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE posts SET ai_score=?, ai_confidence=?, ai_decision=?, ai_reason=?, ocr_text=? WHERE id=?",(score,confidence,decision,reason,ocr_text[:3000],post_id))
        await db.commit()

async def get_user_trust_score(user_id:int) -> float:
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT COALESCE(trust_score,0) FROM users WHERE user_id=?",(user_id,)); r=await cur.fetchone()
        return float(r[0]) if r else 0.0

async def adjust_user_trust(user_id:int, delta:float) -> float:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET trust_score=MAX(-100,MIN(100,COALESCE(trust_score,0)+?)) WHERE user_id=?",(delta,user_id))
        await db.commit()
        cur=await db.execute("SELECT COALESCE(trust_score,0) FROM users WHERE user_id=?",(user_id,)); r=await cur.fetchone()
        return float(r[0]) if r else 0.0


async def record_ai_feedback(post_id: int, human_decision: str, trust_delta: float = 0.0) -> None:
    """Сохраняет решение человека как обучающий сигнал и обновляет trust.
    Повторный клик по той же карточке не создаёт второй feedback."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT user_id, ai_decision, ai_score, ai_confidence FROM posts WHERE id=?",
            (post_id,),
        )
        row = await cur.fetchone()
        if not row:
            return
        user_id, ai_decision, ai_score, ai_confidence = row
        cur = await db.execute("SELECT 1 FROM ai_feedback WHERE post_id=? LIMIT 1", (post_id,))
        if await cur.fetchone():
            return
        trust_before = 0.0
        cur = await db.execute("SELECT COALESCE(trust_score,0) FROM users WHERE user_id=?", (user_id,))
        r = await cur.fetchone()
        if r:
            trust_before = float(r[0])
        now = datetime.now(TIMEZONE).replace(tzinfo=None).isoformat()
        await db.execute(
            "INSERT INTO ai_feedback(post_id,user_id,ai_decision,human_decision,ai_score,ai_confidence,trust_before,trust_delta,created_time) VALUES(?,?,?,?,?,?,?,?,?)",
            (post_id,user_id,ai_decision,human_decision,ai_score,ai_confidence,trust_before,trust_delta,now),
        )
        if user_id and trust_delta:
            await db.execute("UPDATE users SET trust_score=MAX(-100,MIN(100,COALESCE(trust_score,0)+?)) WHERE user_id=?", (trust_delta,user_id))
        await db.commit()


async def record_ai_correction(post_id: int, admin_id: int, correction: str = "error") -> bool:
    """Фиксирует нажатие «ИИ ошибся» независимо от доступности локального ИИ.

    Кнопка является человеческой обратной связью, поэтому она не должна
    зависеть от того, удалось ли модели загрузиться в момент обработки поста.
    Повторное нажатие тем же администратором безопасно игнорируется.
    """
    async with aiosqlite.connect(DB_NAME) as db:
        # Убеждаемся, что пост существует. Это защищает от битых старых
        # callback-кнопок, но не требует наличия AI-анализа.
        cur = await db.execute("SELECT user_id FROM posts WHERE id=?", (post_id,))
        row = await cur.fetchone()
        if not row:
            return False

        cur = await db.execute(
            "SELECT 1 FROM ai_corrections WHERE post_id=? AND admin_id=? LIMIT 1",
            (post_id, admin_id),
        )
        if await cur.fetchone():
            return False

        await db.execute(
            "INSERT INTO ai_corrections(post_id,admin_id,correction,created_time) VALUES(?,?,?,?)",
            (post_id, admin_id, correction, datetime.now(TIMEZONE).replace(tzinfo=None).isoformat()),
        )
        if row[0]:
            await db.execute(
                "UPDATE users SET trust_score=MAX(-100,MIN(100,COALESCE(trust_score,0)-3)) WHERE user_id=?",
                (row[0],),
            )
        await db.commit()
        return True


async def get_ai_stats(days: int = 30) -> Dict[str, int]:
    cutoff=(datetime.now(TIMEZONE).replace(tzinfo=None)-timedelta(days=days)).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT COUNT(*) FROM posts WHERE time>=? AND ai_decision='auto'",(cutoff,)); auto=(await cur.fetchone())[0]
        cur=await db.execute("SELECT COUNT(*) FROM posts WHERE time>=? AND ai_decision='manual'",(cutoff,)); manual=(await cur.fetchone())[0]
        cur=await db.execute("SELECT COUNT(*) FROM ai_feedback WHERE created_time>=? AND human_decision='published'",(cutoff,)); approved=(await cur.fetchone())[0]
        cur=await db.execute("SELECT COUNT(*) FROM ai_feedback WHERE created_time>=? AND human_decision='rejected'",(cutoff,)); rejected=(await cur.fetchone())[0]
        cur=await db.execute("SELECT COUNT(*) FROM ai_feedback WHERE created_time>=? AND ai_decision='auto' AND human_decision='rejected'",(cutoff,)); false_positive=(await cur.fetchone())[0]
        cur=await db.execute("SELECT COUNT(*) FROM ai_feedback WHERE created_time>=? AND ai_decision='manual' AND human_decision='published'",(cutoff,)); false_negative=(await cur.fetchone())[0]
    return {"auto":auto,"manual":manual,"approved":approved,"rejected":rejected,"false_positive":false_positive,"false_negative":false_negative}


# ================== ПОСТЫ ==================
async def create_post(user_id: int, text: str, photo: Optional[str], photo_hash: Optional[str] = None) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO posts(user_id, text, photo, time, status, text_norm, photo_hash) VALUES(?,?,?,?,?,?,?)",
            (user_id, text, photo, datetime.now(TIMEZONE).replace(tzinfo=None).isoformat(), "moderation", normalize_text(text), photo_hash),
        )
        post_id = cursor.lastrowid
        await db.commit()
        return post_id


async def posts_today(user_id: int) -> int:
    today = datetime.now(TIMEZONE).date().isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE user_id=? AND date(time)=?",
            (user_id, today),
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def posts_week(user_id: int) -> int:
    week_ago = (datetime.now(TIMEZONE).replace(tzinfo=None) - timedelta(days=7)).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE user_id=? AND time>=?",
            (user_id, week_ago),
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def get_post_status(post_id: int) -> str:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT status FROM posts WHERE id=?", (post_id,))
        row = await cur.fetchone()
        return row[0] if row else ""


async def get_post_moderator_info(post_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT moderator_id, reject_reason FROM posts WHERE id=?",
            (post_id,),
        )
        row = await cur.fetchone()
        if row:
            moderator_id, reject_reason = row
            # ВАЖНО: 0 в Python falsy — раньше здесь стояло "if moderator_id:",
            # и посты, опубликованные/отклонённые автоматически (moderator_id=0),
            # ошибочно считались "без информации о модераторе". Проверяем на
            # None явно, чтобы отличать "модератора нет вообще" от "модератор — система".
            if moderator_id is not None:
                if moderator_id == 0:
                    return moderator_id, "автоматически (по расписанию)", reject_reason
                cur2 = await db.execute("SELECT username FROM users WHERE user_id=?", (moderator_id,))
                mod_row = await cur2.fetchone()
                mod_username = mod_row[0] if mod_row else None
                return moderator_id, mod_username, reject_reason
        return None, None, None


async def update_post_message_ids(post_id: int, moderators_message_id: Optional[int] = None,
                                   admins_message_id: Optional[int] = None) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        if moderators_message_id:
            await db.execute(
                "UPDATE posts SET message_id_moderators=?, chat_id_moderators=? WHERE id=?",
                (moderators_message_id, get_setting("MODERATORS_CHAT_ID"), post_id),
            )
        if admins_message_id:
            await db.execute(
                "UPDATE posts SET message_id_admins=?, chat_id_admins=? WHERE id=?",
                (admins_message_id, get_setting("ADMINS_CHAT_ID"), post_id),
            )
        await db.commit()


async def get_pending_posts(page: int = 1, per_page: int = 5) -> Tuple[list, int]:
    """Посты в очереди: обычно это статус 'approved' (автоматически одобрены,
    ждут своего времени публикации); 'moderation' встречается только как
    короткий переходный статус или после сбоя (см. recover_pending_posts)."""
    offset = (page - 1) * per_page
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id, user_id, text, time, photo, status, scheduled_time FROM posts "
            "WHERE status IN ('moderation','approved') "
            "ORDER BY COALESCE(scheduled_time, time) ASC LIMIT ? OFFSET ?",
            (per_page, offset),
        )
        rows = await cur.fetchall()

        cur_count = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE status IN ('moderation','approved')"
        )
        total = (await cur_count.fetchone())[0]

        return rows, total


async def get_post_by_id(post_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id, user_id, text, photo, time, status FROM posts WHERE id=?",
            (post_id,),
        )
        return await cur.fetchone()


async def get_orphaned_moderation_posts() -> List[int]:
    """Посты, которые вообще не были направлены ни по одному сценарию
    (ни автопубликация, ни ручная модерация фото) — признак того, что бот
    упал сразу после создания записи, ещё до выбора маршрута. Отличаем от
    постов с фото, которые СОЗНАТЕЛЬНО остаются в статусе 'moderation' на
    неопределённый срок в ожидании решения человека — у них auto_status
    уже выставлен в 'pending_manual_review' (см. get_stuck_manual_review_posts)."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id FROM posts WHERE status='moderation' AND scheduled_time IS NULL "
            "AND auto_status IS NULL"
        )
        return [r[0] for r in await cur.fetchall()]


async def get_stuck_manual_review_posts() -> List[int]:
    """Посты с фото, которые уже помечены как требующие ручной модерации,
    но карточка модераторам так и не была отправлена (бот упал между
    пометкой и отправкой сообщения) — нужно дослать карточку."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id FROM posts WHERE status='moderation' AND auto_status='pending_manual_review' "
            "AND message_id_moderators IS NULL"
        )
        return [r[0] for r in await cur.fetchall()]


async def set_post_auto_status(post_id: int, auto_status: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE posts SET auto_status=? WHERE id=?", (auto_status, post_id))
        await db.commit()


async def get_post_channel_message_id(post_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT channel_message_id FROM posts WHERE id=?", (post_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def claim_intro_comment(post_id: int) -> bool:
    """Захватывает право на отправку комментария без ложного 'sent'."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "UPDATE posts SET comment_posted=2, comment_claimed_at=? WHERE id=? AND (comment_posted IS NULL OR comment_posted=0)",
            (datetime.now(TIMEZONE).replace(tzinfo=None).isoformat(), post_id),
        )
        await db.commit()
        return cur.rowcount == 1


async def mark_intro_comment_posted(post_id: int) -> bool:
    """Сохраняет совместимый API: claim + final success."""
    return await claim_intro_comment(post_id)


async def finish_intro_comment(post_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE posts SET comment_posted=1, comment_claimed_at=NULL WHERE id=? AND comment_posted=2", (post_id,))
        await db.commit()


async def reset_intro_comment_claim(post_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE posts SET comment_posted=0, comment_claimed_at=NULL WHERE id=? AND comment_posted=2", (post_id,))
        await db.commit()


async def get_stuck_intro_comments() -> List[int]:
    cutoff = (datetime.now(TIMEZONE).replace(tzinfo=None) - timedelta(minutes=5)).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id FROM posts WHERE comment_posted=2 AND (comment_claimed_at IS NULL OR comment_claimed_at<=?)",
            (cutoff,),
        )
        return [r[0] for r in await cur.fetchall()]


async def approve_and_schedule(post_id: int, scheduled_time: datetime) -> None:
    """Переводит пост в статус 'approved' и назначает время публикации.
    Время сохраняется как наивная (без смещения) строка в часовом поясе
    TIMEZONE — так проще и надёжнее сравнивать даты в SQLite."""
    local_dt = scheduled_time.astimezone(TIMEZONE).replace(tzinfo=None) if scheduled_time.tzinfo else scheduled_time
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE posts SET status='approved', auto_status='auto_approved', scheduled_time=? WHERE id=?",
            (local_dt.isoformat(), post_id),
        )
        await db.commit()


async def get_due_posts() -> List[int]:
    """ID постов из очереди, чьё время публикации уже наступило."""
    now_local = datetime.now(TIMEZONE).replace(tzinfo=None).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id FROM posts WHERE status='approved' AND scheduled_time IS NOT NULL "
            "AND scheduled_time<=? AND (publish_next_retry_at IS NULL OR publish_next_retry_at<=?) ORDER BY scheduled_time",
            (now_local, now_local),
        )
        return [r[0] for r in await cur.fetchall()]


async def get_queue_count_for_date(date_str: str) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE status='approved' AND date(scheduled_time)=?",
            (date_str,),
        )
        return (await cur.fetchone())[0]


async def get_published_count_for_date(date_str: str) -> int:
    """Сколько постов, запланированных на эту дату, уже опубликовано
    (используем scheduled_time, а не moderation_time, чтобы не зависеть
    от часового пояса сервера)."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE status='published' AND date(scheduled_time)=?",
            (date_str,),
        )
        return (await cur.fetchone())[0]


async def get_scheduled_times_for_date(date_str: str) -> List[str]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT scheduled_time FROM posts WHERE status='approved' AND date(scheduled_time)=? "
            "ORDER BY scheduled_time",
            (date_str,),
        )
        return [r[0] for r in await cur.fetchall() if r[0]]


async def claim_post_for_publishing(post_id: int, publisher_id: int = 0) -> bool:
    """Атомарно захватывает пост перед вызовом Telegram API."""
    now = datetime.now(TIMEZONE).replace(tzinfo=None).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "UPDATE posts SET status='publishing', moderator_id=?, publishing_started_at=?, "
            "publish_attempts=COALESCE(publish_attempts,0)+1, last_publish_error=NULL, publish_next_retry_at=NULL "
            "WHERE id=? AND status IN ('moderation','approved')",
            (publisher_id, now, post_id),
        )
        await db.commit()
        return cur.rowcount == 1


async def finish_publishing(post_id: int, channel_message_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "UPDATE posts SET status='published', channel_message_id=?, "
            "moderation_time=?, published_at=?, publishing_started_at=NULL, last_publish_error=NULL "
            "WHERE id=? AND status='publishing'",
            (channel_message_id, datetime.now(TIMEZONE).replace(tzinfo=None).isoformat(), datetime.now(TIMEZONE).replace(tzinfo=None).isoformat(), post_id),
        )
        await db.commit()
        return cur.rowcount == 1


async def fail_publishing(post_id: int, error: str, retry: bool = True) -> None:
    """Возвращает публикацию в очередь с экспоненциальной паузой."""
    status = "approved" if retry else "moderation"
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COALESCE(publish_attempts,0) FROM posts WHERE id=?", (post_id,))
        row = await cur.fetchone(); attempts = int(row[0]) if row else 1
        delay = min(3600, max(30, 30 * (2 ** max(0, attempts-1)))) if retry else 0
        next_retry = (datetime.now(TIMEZONE).replace(tzinfo=None) + timedelta(seconds=delay)).isoformat() if retry else None
        await db.execute(
            "UPDATE posts SET status=?, last_publish_error=?, publishing_started_at=NULL, publish_next_retry_at=? WHERE id=? AND status='publishing'",
            (status, (error or "")[:1000], next_retry, post_id),
        )
        await db.commit()


async def get_stuck_publishing_posts() -> List[int]:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id FROM posts WHERE status='publishing' "
            "AND (publishing_started_at IS NULL OR publishing_started_at <= ?)",
            ((datetime.now(TIMEZONE).replace(tzinfo=None) - timedelta(minutes=5)).isoformat(),),
        )
        return [r[0] for r in await cur.fetchall()]


async def try_finalize_post(post_id: int, moderator_id: int, status: str,
                             reason: Optional[str] = None,
                             from_statuses: Tuple[str, ...] = ("moderation", "approved")) -> bool:
    """
    Атомарно переводит пост из статуса 'moderation'/'approved' в 'published'/'rejected'.

    Возвращает True только если именно этот вызов совершил переход
    (WHERE status IN (...) гарантирует, что UPDATE применится ровно один раз).
    Это защита от гонки: если модератор нажмёт кнопку в тот момент, когда пост
    уже опубликован автопланировщиком (или наоборот) — решение "кто первый"
    принимает сама БД, а не порядок проверок в Python.
    """
    placeholders = ",".join("?" for _ in from_statuses)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            f"UPDATE posts SET status=?, moderator_id=?, moderation_time=?, reject_reason=? "
            f"WHERE id=? AND status IN ({placeholders})",
            (status, moderator_id, datetime.now(TIMEZONE).replace(tzinfo=None).isoformat(), reason, post_id, *from_statuses),
        )
        await db.commit()
        return cur.rowcount == 1


async def try_finalize_post_revert(post_id: int) -> None:
    """Откатывает пост назад в 'moderation', если финализация не удалась
    (например, Telegram API отказал при отправке в канал уже после того,
    как мы атомарно пометили пост как published/rejected)."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE posts SET status='moderation', moderator_id=NULL, "
            "moderation_time=NULL, reject_reason=NULL WHERE id=?",
            (post_id,),
        )
        await db.commit()


# ================== ID ПОСТА В КАНАЛЕ (для удаления / поиска автора) ==================
async def set_channel_message_id(post_id: int, channel_message_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE posts SET channel_message_id=? WHERE id=?",
            (channel_message_id, post_id),
        )
        await db.commit()


async def get_post_by_channel_message_id(channel_message_id: int):
    """Ищет опубликованный пост по ID его сообщения в канале
    (используется для распознавания ссылки/пересланного поста)."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id, user_id, text, photo, time, status FROM posts "
            "WHERE channel_message_id=? AND status='published'",
            (channel_message_id,),
        )
        return await cur.fetchone()


async def mark_post_deleted(post_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE posts SET status='deleted', moderation_time=? WHERE id=?",
            (datetime.now(TIMEZONE).replace(tzinfo=None).isoformat(), post_id),
        )
        await db.commit()


# ================== ЗАЯВКИ НА УДАЛЕНИЕ ПОСТА ==================
async def create_deletion_request(post_id: int, requester_id: int, reason: str,
                                   timeout_hours: int) -> int:
    now = datetime.now(TIMEZONE).replace(tzinfo=None)
    expire = now + timedelta(hours=timeout_hours)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "INSERT INTO deletion_requests(post_id, requester_id, reason, status, "
            "created_time, expire_time) VALUES(?,?,?,?,?,?)",
            (post_id, requester_id, reason, "pending_admin", str(now), str(expire)),
        )
        request_id = cur.lastrowid
        await db.commit()
        return request_id


async def set_deletion_request_admin_refs(request_id: int, admin_refs: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE deletion_requests SET admin_refs=? WHERE id=?",
            (admin_refs, request_id),
        )
        await db.commit()


async def get_deletion_request(request_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id, post_id, requester_id, reason, status, created_time, "
            "expire_time, decided_by, decided_time, admin_refs FROM deletion_requests WHERE id=?",
            (request_id,),
        )
        return await cur.fetchone()


async def claim_deletion_request(request_id: int, decided_by: int, status: str) -> bool:
    """Атомарно переводит заявку из 'pending_admin' в 'approved'/'rejected'/'expired'.
    Та же защита от гонки, что и у try_finalize_post: если два админа кликнут
    почти одновременно, решение засчитается только одному из них."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "UPDATE deletion_requests SET status=?, decided_by=?, decided_time=? "
            "WHERE id=? AND status='pending_admin'",
            (status, decided_by, datetime.now(TIMEZONE).replace(tzinfo=None).isoformat(), request_id),
        )
        await db.commit()
        return cur.rowcount == 1


async def reopen_deletion_request(request_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE deletion_requests SET status='pending_admin', decided_by=NULL, decided_time=NULL WHERE id=? AND status='approved'",
            (request_id,),
        )
        await db.commit()


async def get_expired_deletion_requests() -> List[int]:
    now = datetime.now(TIMEZONE).replace(tzinfo=None).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id FROM deletion_requests WHERE status='pending_admin' AND expire_time<=?",
            (now,),
        )
        return [r[0] for r in await cur.fetchall()]


# ================== ПОКУПКИ ЗА TELEGRAM STARS (расшифровка автора, ускорение) ==================
async def get_star_purchase(user_id: int, post_id: int, kind: str):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id FROM star_purchases WHERE user_id=? AND post_id=? AND kind=?",
            (user_id, post_id, kind),
        )
        return await cur.fetchone()


async def record_star_purchase(user_id: int, kind: str, post_id: Optional[int], amount_stars: int,
                                telegram_charge_id: str) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO star_purchases(user_id, kind, post_id, amount_stars, telegram_charge_id, time) "
                "VALUES(?,?,?,?,?,?)",
                (user_id, kind, post_id, amount_stars, telegram_charge_id, datetime.now(TIMEZONE).replace(tzinfo=None).isoformat()),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # идемпотентный повтор successful_payment


# ================== РЕКЛАМНЫЕ ПОСТЫ ==================
async def create_advertising_post(admin_id: int, source_chat_id: int, source_message_id: int,
                                  duration_hours: int, ad_type: str = "post") -> int:
    now = datetime.now(TIMEZONE)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            """INSERT INTO advertising_posts(
                admin_id, source_chat_id, source_message_id, duration_hours,
                created_at, status, ad_type
            ) VALUES(?,?,?,?,?,?,?)""",
            (admin_id, source_chat_id, source_message_id, duration_hours,
             now.isoformat(), "draft", ad_type),
        )
        await db.commit()
        return cur.lastrowid


async def set_advertising_preview(ad_id: int, chat_id: int, message_id: int,
                                  control_message_id: Optional[int] = None) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """UPDATE advertising_posts
               SET preview_chat_id=?, preview_message_id=?, control_chat_id=?, control_message_id=?
               WHERE id=?""",
            (chat_id, message_id, chat_id, control_message_id, ad_id),
        )
        await db.commit()


async def set_advertising_control(ad_id: int, chat_id: int, message_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE advertising_posts SET control_chat_id=?, control_message_id=? WHERE id=?",
            (chat_id, message_id, ad_id),
        )
        await db.commit()


async def get_advertising_post(ad_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT * FROM advertising_posts WHERE id=?", (ad_id,))
        return await cur.fetchone()


async def update_advertising_source(ad_id: int, source_chat_id: int, source_message_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE advertising_posts SET source_chat_id=?, source_message_id=?, preview_chat_id=NULL, preview_message_id=NULL WHERE id=? AND status='draft'",
            (source_chat_id, source_message_id, ad_id),
        )
        await db.commit()


async def set_advertising_type(ad_id: int, ad_type: str) -> None:
    if ad_type not in ("post", "combo"):
        ad_type = "post"
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE advertising_posts SET ad_type=? WHERE id=? AND status='draft'", (ad_type, ad_id))
        await db.commit()


async def update_advertising_pin(ad_id: int, pin_duration_hours: int) -> None:
    pin_duration_hours = max(0, int(pin_duration_hours))
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE advertising_posts SET pin_duration_hours=? WHERE id=? AND status='draft'",
            (pin_duration_hours, ad_id),
        )
        await db.commit()


async def update_advertising_duration(ad_id: int, duration_hours: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE advertising_posts SET duration_hours=? WHERE id=? AND status='draft'",
            (duration_hours, ad_id),
        )
        await db.commit()


async def publish_advertising_post(ad_id: int, channel_message_id: int,
                                   pin_duration_hours: int = 0) -> bool:
    now = datetime.now(TIMEZONE)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            """UPDATE advertising_posts
               SET channel_message_id=?, pin_duration_hours=?, published_at=?,
                   expires_at=?, pin_expires_at=?, status='published', error=NULL
               WHERE id=? AND status='draft'""",
            (
                channel_message_id, pin_duration_hours, now.isoformat(),
                (now + timedelta(hours=(await _ad_duration(db, ad_id)))).isoformat(),
                (now + timedelta(hours=pin_duration_hours)).isoformat() if pin_duration_hours else None,
                ad_id,
            ),
        )
        await db.commit()
        return bool(cur.rowcount)


async def _ad_duration(db: aiosqlite.Connection, ad_id: int) -> int:
    cur = await db.execute("SELECT duration_hours FROM advertising_posts WHERE id=?", (ad_id,))
    row = await cur.fetchone()
    return int(row[0]) if row else 0


async def set_advertising_error(ad_id: int, error: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE advertising_posts SET error=?, status='error' WHERE id=?", (error[:1000], ad_id))
        await db.commit()


async def cancel_advertising_post(ad_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE advertising_posts SET status='cancelled' WHERE id=? AND status='draft'", (ad_id,))
        await db.execute("DELETE FROM advertising_broadcasts WHERE ad_id=?", (ad_id,))
        await db.execute("DELETE FROM advertising_subscriptions WHERE ad_id=?", (ad_id,))
        await db.commit()


async def reopen_advertising_post(ad_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE advertising_posts SET status='draft', error=NULL WHERE id=? AND status='error'",
            (ad_id,),
        )
        await db.commit()


async def get_due_advertising_posts(now: Optional[datetime] = None):
    now = now or datetime.now(TIMEZONE)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            """SELECT id, channel_message_id, expires_at, pin_expires_at
               FROM advertising_posts
               WHERE status='published' AND (
                   (expires_at IS NOT NULL AND expires_at<=?) OR
                   (pin_expires_at IS NOT NULL AND pin_expires_at<=?)
               )""",
            (now.isoformat(), now.isoformat()),
        )
        return await cur.fetchall()


async def finish_advertising_expiry(ad_id: int, deleted: bool = False) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        if deleted:
            await db.execute("UPDATE advertising_posts SET status='expired' WHERE id=?", (ad_id,))
        else:
            await db.execute(
                "UPDATE advertising_posts SET pin_duration_hours=0, pin_expires_at=NULL WHERE id=?",
                (ad_id,),
            )
        await db.commit()


async def get_active_advertising_posts():
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id, channel_message_id, expires_at, pin_expires_at FROM advertising_posts WHERE status='published'"
        )
        return await cur.fetchall()


# ================== РЕКЛАМНЫЕ ПОДПИСКИ И РАССЫЛКИ ==================
async def set_advertising_subscription_target(ad_id: int, sub: Optional[dict], hours: int = 0) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        if sub:
            await db.execute(
                """UPDATE advertising_posts SET subscription_type=?, subscription_id=?, subscription_username=?,
                   subscription_name=?, subscription_url=?, subscription_hours=? WHERE id=? AND status='draft'""",
                (sub.get('type'), str(sub.get('id')), sub.get('username',''), sub.get('name',''), sub.get('url',''), int(hours), ad_id),
            )
        else:
            await db.execute("UPDATE advertising_posts SET subscription_type=NULL, subscription_id=NULL, subscription_username=NULL, subscription_name=NULL, subscription_url=NULL, subscription_hours=0 WHERE id=? AND status='draft'", (ad_id,))
        await db.commit()

async def get_advertising_subscription_target(ad_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT subscription_type, subscription_id, subscription_username, subscription_name, subscription_url, subscription_hours FROM advertising_posts WHERE id=?", (ad_id,))
        return await cur.fetchone()

async def activate_advertising_subscription(ad_id: int, starts_at: datetime) -> bool:
    target = await get_advertising_subscription_target(ad_id)
    if not target or not target[0] or not target[1] or not target[5]:
        return False
    expires = starts_at + timedelta(hours=int(target[5]))
    return await add_advertising_subscription(ad_id, {"type": target[0], "id": target[1], "username": target[2] or "", "name": target[3] or "", "url": target[4] or ""}, starts_at, expires)

async def clear_advertising_scheduled_extras(ad_id: int) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM advertising_broadcasts WHERE ad_id=?", (ad_id,))
        await db.execute("DELETE FROM advertising_subscriptions WHERE ad_id=?", (ad_id,))
        await db.commit()


async def add_advertising_subscription(ad_id: int, sub: dict, starts_at: datetime, expires_at: datetime) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                """INSERT INTO advertising_subscriptions
                (ad_id, sub_type, sub_id, username, name, url, starts_at, expires_at, status)
                VALUES(?,?,?,?,?,?,?,?, 'active')""",
                (ad_id, sub["type"], str(sub["id"]), sub.get("username", ""), sub["name"], sub["url"], starts_at.isoformat(), expires_at.isoformat()),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def get_active_advertising_subscriptions(now: Optional[datetime] = None) -> List[dict]:
    now = now or datetime.now(TIMEZONE)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            """SELECT id, ad_id, sub_type, sub_id, username, name, url, starts_at, expires_at
               FROM advertising_subscriptions
               WHERE status='active' AND starts_at<=? AND expires_at>? ORDER BY id""",
            (now.isoformat(), now.isoformat()),
        )
        rows = await cur.fetchall()
    return [{"row_id": r[0], "ad_id": r[1], "type": r[2], "id": r[3], "username": r[4], "name": r[5], "url": r[6], "starts_at": r[7], "expires_at": r[8]} for r in rows]

async def expire_advertising_subscriptions(now: Optional[datetime] = None) -> int:
    now = now or datetime.now(TIMEZONE)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("UPDATE advertising_subscriptions SET status='expired' WHERE status='active' AND expires_at<=?", (now.isoformat(),))
        await db.commit()
        return cur.rowcount or 0

async def add_advertising_broadcast(ad_id: int, sequence_no: int, scheduled_at: datetime) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("INSERT INTO advertising_broadcasts(ad_id, sequence_no, scheduled_at, status) VALUES(?,?,?,'scheduled')", (ad_id, sequence_no, scheduled_at.isoformat()))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def get_due_advertising_broadcasts(now: Optional[datetime] = None):
    now = now or datetime.now(TIMEZONE)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            """SELECT b.id, b.ad_id, b.sequence_no, b.scheduled_at, a.channel_message_id
               FROM advertising_broadcasts b JOIN advertising_posts a ON a.id=b.ad_id
               WHERE b.status='scheduled' AND b.scheduled_at<=? AND a.status='published'
                 AND a.channel_message_id IS NOT NULL
                 AND (a.expires_at IS NULL OR b.scheduled_at < a.expires_at)
               ORDER BY b.scheduled_at, b.id""", (now.isoformat(),)
        )
        return await cur.fetchall()


async def claim_advertising_delivery(broadcast_id: int, user_id: int) -> bool:
    """Атомарно резервирует доставку одной рассылки одному пользователю."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR IGNORE INTO advertising_broadcast_deliveries(broadcast_id,user_id,status) VALUES(?,?, 'pending')",
            (broadcast_id, user_id),
        )
        cur = await db.execute(
            """UPDATE advertising_broadcast_deliveries SET status='sending', attempts=COALESCE(attempts,0)+1
               WHERE broadcast_id=? AND user_id=? AND status='pending'""",
            (broadcast_id, user_id),
        )
        await db.commit()
        return cur.rowcount == 1


async def retry_advertising_delivery(broadcast_id: int, user_id: int, error: Optional[str] = None) -> None:
    """Возвращает доставку в pending после временной ошибки Telegram."""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE advertising_broadcast_deliveries SET status='pending', last_error=? WHERE broadcast_id=? AND user_id=?",
            ((error or '')[:1000], broadcast_id, user_id),
        )
        await db.commit()


async def finish_advertising_delivery(broadcast_id: int, user_id: int, success: bool, error: Optional[str] = None) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        if success:
            await db.execute(
                "UPDATE advertising_broadcast_deliveries SET status='sent', sent_at=?, last_error=NULL WHERE broadcast_id=? AND user_id=?",
                (datetime.now(TIMEZONE).isoformat(), broadcast_id, user_id),
            )
        else:
            await db.execute(
                "UPDATE advertising_broadcast_deliveries SET status='failed', last_error=? WHERE broadcast_id=? AND user_id=?",
                ((error or 'unknown error')[:1000], broadcast_id, user_id),
            )
        await db.commit()


async def get_advertising_delivery_stats(broadcast_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT status, COUNT(*) FROM advertising_broadcast_deliveries WHERE broadcast_id=? GROUP BY status",
            (broadcast_id,),
        )
        return dict(await cur.fetchall())

async def claim_advertising_broadcast(broadcast_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("UPDATE advertising_broadcasts SET status='sending', attempts=COALESCE(attempts,0)+1 WHERE id=? AND status='scheduled'", (broadcast_id,))
        await db.commit()
        return cur.rowcount == 1

async def finish_advertising_broadcast(broadcast_id: int, error: Optional[str] = None) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        if error:
            await db.execute("UPDATE advertising_broadcasts SET status='scheduled', last_error=? WHERE id=?", (error[:1000], broadcast_id))
        else:
            await db.execute("UPDATE advertising_broadcasts SET status='sent', sent_at=? WHERE id=?", (datetime.now(TIMEZONE).isoformat(), broadcast_id))
        await db.commit()

async def get_advertising_broadcasts(ad_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT id, sequence_no, scheduled_at, sent_at, status FROM advertising_broadcasts WHERE ad_id=? ORDER BY sequence_no", (ad_id,))
        return await cur.fetchall()

# ================== ОЧЕРЕДЬ: ПОЗИЦИЯ, ПЕРЕНОС ПОСТОВ, ПРОСРОЧЕННАЯ МОДЕРАЦИЯ ==================
async def get_total_pending_count() -> int:
    """Сколько постов сейчас в очереди (ждут публикации или решения модератора)."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE status IN ('moderation','approved')")
        return (await cur.fetchone())[0]


async def set_post_review_deadline(post_id: int, deadline: datetime) -> None:
    """Дедлайн хранится как наивная (без смещения) строка в TIMEZONE —
    так же, как scheduled_time (см. approve_and_schedule)."""
    local_dt = deadline.astimezone(TIMEZONE).replace(tzinfo=None) if deadline.tzinfo else deadline
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE posts SET review_deadline=? WHERE id=?", (local_dt.isoformat(), post_id))
        await db.commit()


async def get_expired_manual_review_posts() -> List[int]:
    """Посты на ручной модерации, чей 24-часовой (по умолчанию) срок истёк
    без решения человека — подлежат автоотклонению."""
    now_local = datetime.now(TIMEZONE).replace(tzinfo=None).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id FROM posts WHERE status='moderation' AND auto_status='pending_manual_review' "
            "AND review_deadline IS NOT NULL AND review_deadline<=?",
            (now_local,),
        )
        return [r[0] for r in await cur.fetchall()]


async def get_future_scheduled_posts(after_date_str: str, limit: int) -> List[int]:
    """Одобренные посты, запланированные позже указанной даты — кандидаты на
    перенос на сегодня, если сегодняшняя дневная квота недобрана (см.
    scheduler.rebalance_daily_queue)."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id FROM posts WHERE status='approved' AND date(scheduled_time) > ? "
            "ORDER BY scheduled_time ASC LIMIT ?",
            (after_date_str, limit),
        )
        return [r[0] for r in await cur.fetchall()]


# ================== ЭКСПОРТ ПОЛЬЗОВАТЕЛЕЙ (для Excel) ==================
async def get_user_export_stats() -> List[tuple]:
    """(user_id, username, reg_date, posts_count, stars_spent), отсортировано
    по потраченным звёздам по убыванию — для выгрузки в Excel."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("""
            SELECT u.user_id, u.username, u.reg_date,
                   COALESCE(p.post_count, 0) AS post_count,
                   COALESCE(s.stars_spent, 0) AS stars_spent
            FROM users u
            LEFT JOIN (SELECT user_id, COUNT(*) as post_count FROM posts GROUP BY user_id) p
                ON p.user_id = u.user_id
            LEFT JOIN (SELECT user_id, SUM(amount_stars) as stars_spent FROM star_purchases GROUP BY user_id) s
                ON s.user_id = u.user_id
            ORDER BY stars_spent DESC, post_count DESC
        """)
        return await cur.fetchall()

