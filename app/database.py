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
            mentions_count INTEGER DEFAULT 0
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
            comment_posted INTEGER DEFAULT 0
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
        CREATE TABLE IF NOT EXISTS auto_approve_phrases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phrase TEXT UNIQUE,
            added_by INTEGER,
            added_time TEXT
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
    ):
        if col not in posts_cols:
            await db.execute(f"ALTER TABLE posts ADD COLUMN {col} {coltype}")
            logger.info(f"Миграция: добавлена колонка posts.{col}")

    users_cols = await _columns("users")
    for col, coltype in (
        ("comments_count", "INTEGER DEFAULT 0"),
        ("mentions_count", "INTEGER DEFAULT 0"),
        ("trust_score", "REAL DEFAULT 0.0"),
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
    "что за", "расскажите про", "расскажите о", "срочно дайте юз",
    "расскажите кто", "понравилась", "понравился", "кто это", "кто она",
    "кто он", "кто знает", "подскажите кто", "как зовут", "юз в лс",
    "юзернейм в лс", "контакты в лс", "разыскивается", "ищу эту",
    "ищу этого", "видели её", "видели его", "было приятно познакомиться",
    "хочу познакомиться",
]


async def seed_default_auto_approve_phrases() -> None:
    """Загружает стандартный список ключевых фраз для автопубликации, только
    если список ещё пуст — не затирает правки администратора."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM auto_approve_phrases")
        count = (await cur.fetchone())[0]
        if count > 0:
            return

        now = str(datetime.now())
        added = 0
        for phrase in DEFAULT_AUTO_APPROVE_PHRASES:
            try:
                await db.execute(
                    "INSERT INTO auto_approve_phrases(phrase, added_by, added_time) VALUES(?,?,?)",
                    (phrase, None, now),
                )
                added += 1
            except aiosqlite.IntegrityError:
                pass
        await db.commit()
        if added:
            logger.info(f"Загружен стандартный список из {added} ключевых фраз для автопубликации")


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
            await db.execute(
                "INSERT INTO auto_approve_phrases(phrase, added_by, added_time) VALUES(?,?,?)",
                (phrase_clean, admin_id, str(datetime.now())),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_auto_approve_phrase(phrase: str) -> None:
    phrase_clean = phrase.strip().lower()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM auto_approve_phrases WHERE phrase=?", (phrase_clean,))
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
                (user.id, user.username, str(datetime.now().date()), 0),
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
    """Считает, сколько раз уже встречался похожий текст (нечёткое сравнение,
    без учёта регистра/пробелов/пунктуации/эмодзи). Сравнение (CPU-затратная
    часть) выполняется в отдельном потоке, чтобы не блокировать event loop."""
    norm = normalize_text(text)
    if not norm:
        return 0

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT id, text_norm FROM posts WHERE text_norm IS NOT NULL AND text_norm != ''")
        rows = await cur.fetchall()

    def _count() -> int:
        matches = 0
        for pid, other_norm in rows:
            if exclude_post_id and pid == exclude_post_id:
                continue
            if not other_norm:
                continue
            if other_norm == norm:
                matches += 1
                continue
            if SequenceMatcher(None, norm, other_norm).ratio() >= get_setting("DUPLICATE_SIMILARITY_THRESHOLD"):
                matches += 1
        return matches

    return await asyncio.to_thread(_count)


# ================== ПОСТЫ ==================
async def create_post(user_id: int, text: str, photo: Optional[str]) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO posts(user_id, text, photo, time, status, text_norm) VALUES(?,?,?,?,?,?)",
            (user_id, text, photo, str(datetime.now()), "moderation", normalize_text(text)),
        )
        post_id = cursor.lastrowid
        await db.commit()
        return post_id


async def posts_today(user_id: int) -> int:
    today = str(datetime.now().date())
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE user_id=? AND date(time)=?",
            (user_id, today),
        )
        row = await cur.fetchone()
        return row[0] if row else 0


async def posts_week(user_id: int) -> int:
    week_ago = str(datetime.now() - timedelta(days=7))
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


async def mark_intro_comment_posted(post_id: int) -> bool:
    """Атомарно помечает, что вводный комментарий ('Будьте вежливы...' с
    кнопками) под постом уже отправлен. Возвращает True только для того
    вызова, который реально выставил флаг — защита от отправки комментария
    дважды, если по каким-то причинам обработчик автопересылки сработает
    больше одного раза для одного и того же поста."""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "UPDATE posts SET comment_posted=1 WHERE id=? AND (comment_posted IS NULL OR comment_posted=0)",
            (post_id,),
        )
        await db.commit()
        return cur.rowcount == 1


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
            "AND scheduled_time<=? ORDER BY scheduled_time",
            (now_local,),
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
            (status, moderator_id, str(datetime.now()), reason, post_id, *from_statuses),
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
            (str(datetime.now()), post_id),
        )
        await db.commit()


# ================== ЗАЯВКИ НА УДАЛЕНИЕ ПОСТА ==================
async def create_deletion_request(post_id: int, requester_id: int, reason: str,
                                   timeout_hours: int) -> int:
    now = datetime.now()
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
            (status, decided_by, str(datetime.now()), request_id),
        )
        await db.commit()
        return cur.rowcount == 1


async def get_expired_deletion_requests() -> List[int]:
    now = str(datetime.now())
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
                                telegram_charge_id: str) -> None:
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO star_purchases(user_id, kind, post_id, amount_stars, telegram_charge_id, time) "
                "VALUES(?,?,?,?,?,?)",
                (user_id, kind, post_id, amount_stars, telegram_charge_id, str(datetime.now())),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            pass  # уже записано (повторный successful_payment на один и тот же лот — игнорируем)


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

