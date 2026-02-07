import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict, Any
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.types import *
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
import aiosqlite

# Import configuration
from config import (
    BOT_TOKEN,
    MAIN_CHANNEL_ID,
    COMMENTS_CHAT_ID,
    MODERATORS_CHAT_ID,
    MODERATORS_TOPIC_ID,
    ADMINS_CHAT_ID,
    ADMINS_TOPIC_ID,
    ADMINS,
    MODERATORS,
    DB_NAME,
    REQUIRED_SUBSCRIPTIONS
)

# ================== INIT ==================
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ================== ВАЛИДАЦИЯ ЧАТА И ТЕМЫ ==================
def is_valid_moderators_chat(message: Message) -> bool:
    """Проверяет, что сообщение из правильной темы группы модераторов"""
    if message.chat.id != MODERATORS_CHAT_ID:
        return False
    
    # Проверяем тему, если это группа с темами
    if message.message_thread_id is not None and message.message_thread_id != MODERATORS_TOPIC_ID:
        return False
    
    return True

def is_valid_admins_chat(message: Message) -> bool:
    """Проверяет, что сообщение из правильной темы группы администраторов"""
    if message.chat.id != ADMINS_CHAT_ID:
        return False
    
    # Проверяем тему, если это группа с темами
    if message.message_thread_id is not None and message.message_thread_id != ADMINS_TOPIC_ID:
        return False
    
    return True

async def validate_chat_for_moderation(callback: CallbackQuery) -> bool:
    """Проверяет, что колбэк из правильной темы для модерации"""
    if callback.message.chat.id != MODERATORS_CHAT_ID:
        return False
    
    # Для групп с темами проверяем ID темы
    if callback.message.message_thread_id is not None:
        return callback.message.message_thread_id == MODERATORS_TOPIC_ID
    
    return True

async def validate_chat_for_admin_actions(callback: CallbackQuery) -> bool:
    """Проверяет, что колбэк из правильной темы для админ-действий"""
    if callback.message.chat.id != ADMINS_CHAT_ID:
        return False
    
    # Для групп с темами проверяем ID темы
    if callback.message.message_thread_id is not None:
        return callback.message.message_thread_id == ADMINS_TOPIC_ID
    
    return True

# ================== DB ==================
async def init_db():
    """Инициализация базы данных"""
    async with aiosqlite.connect(DB_NAME) as db:
        # Таблица users
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            reg_date TEXT,
            is_subscribed INTEGER DEFAULT 0
        )""")
        
        # Таблица posts
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
            chat_id_admins INTEGER
        )""")
        
        # Таблица bans
        await db.execute("""
        CREATE TABLE IF NOT EXISTS bans(
            user_id INTEGER PRIMARY KEY,
            reason TEXT,
            ban_time TEXT,
            admin_id INTEGER,
            admin_username TEXT
        )""")
        
        # Таблица publication_blacklist
        await db.execute("""
        CREATE TABLE IF NOT EXISTS publication_blacklist(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT UNIQUE,
            added_by INTEGER,
            added_time TEXT
        )""")
        
        # Таблица logs
        await db.execute("""
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            data TEXT,
            time TEXT
        )""")
        
        # Таблица required_subscriptions
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
        
        await db.commit()
        logger.info("База данных инициализирована")
        
        # Загружаем подписки из базы данных
        await load_subscriptions_from_db()

async def load_subscriptions_from_db():
    """Загружаем список обязательных подписок из базы данных"""
    global REQUIRED_SUBSCRIPTIONS
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("""
            SELECT sub_type, sub_id, username, name, url 
            FROM required_subscriptions 
            ORDER BY id
        """)
        rows = await cur.fetchall()
        
        if rows:
            REQUIRED_SUBSCRIPTIONS = []
            for row in rows:
                REQUIRED_SUBSCRIPTIONS.append({
                    "type": row[0],
                    "id": str(row[1]),  # Преобразуем в строку для единообразия
                    "username": row[2],
                    "name": row[3],
                    "url": row[4]
                })
            logger.info(f"Загружено {len(REQUIRED_SUBSCRIPTIONS)} обязательных подписок из БД")
        else:
            await save_subscriptions_to_db()

async def save_subscriptions_to_db():
    """Сохраняем текущий список подписок в базу данных"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM required_subscriptions")
        
        for sub in REQUIRED_SUBSCRIPTIONS:
            await db.execute("""
                INSERT INTO required_subscriptions(sub_type, sub_id, username, name, url, added_time)
                VALUES(?,?,?,?,?,?)
            """, (
                sub["type"],
                sub["id"],
                sub["username"],
                sub["name"],
                sub["url"],
                str(datetime.now())
            ))
        
        await db.commit()
        logger.info(f"Сохранено {len(REQUIRED_SUBSCRIPTIONS)} подписок в БД")

# ================== STATES ==================
class PostState(StatesGroup):
    wait_photo = State()
    wait_text_after_photo = State()
    wait_text_only = State()

class RejectState(StatesGroup):
    wait_reason = State()

class BroadcastState(StatesGroup):
    wait_broadcast_text = State()
    wait_broadcast_photo = State()
    wait_broadcast_text_with_photo = State()
    wait_broadcast_confirm = State()

class BlacklistState(StatesGroup):
    wait_keyword = State()
    wait_remove_keyword = State()

class SubscriptionState(StatesGroup):
    wait_subscription_add = State()

# ================== UTILS ==================
async def check_subscription(user_id: int) -> Tuple[bool, List[Dict[str, Any]]]:
    """Проверяет подписку пользователя на обязательные каналы и ботов."""
    if not REQUIRED_SUBSCRIPTIONS:
        return True, []
    
    unsubscribed = []
    
    for sub in REQUIRED_SUBSCRIPTIONS:
        if sub["type"] == "channel":
            try:
                chat_id = int(sub["id"])
                chat_member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                if chat_member.status not in ["member", "administrator", "creator"]:
                    unsubscribed.append(sub)
            except Exception as e:
                logger.error(f"Ошибка при проверке подписки на канал {sub['id']}: {e}")
                unsubscribed.append(sub)
        elif sub["type"] == "bot":
            # Для ботов проверяем, что пользователь начал с ним диалог
            # На самом деле мы не можем проверить, начал ли пользователь диалог с ботом
            # Поэтому просто добавляем бота в список для отображения
            unsubscribed.append(sub)
    
    return len(unsubscribed) == 0, unsubscribed

def get_subscription_keyboard(unsubscribed: List[Dict[str, Any]] = None) -> InlineKeyboardMarkup:
    """Создает клавиатуру для подписки на каналы/бота."""
    if unsubscribed is None:
        subscriptions_to_show = REQUIRED_SUBSCRIPTIONS
    else:
        subscriptions_to_show = unsubscribed
    
    keyboard = []
    
    for sub in subscriptions_to_show:
        emoji = "📢" if sub["type"] == "channel" else "🤖"
        keyboard.append([
            InlineKeyboardButton(
                text=f"{emoji} {sub['name']}",
                url=sub["url"]
            )
        ])
    
    # Проверяем, есть ли каналы для подписки
    has_channels = any(sub["type"] == "channel" for sub in subscriptions_to_show)
    has_bots = any(sub["type"] == "bot" for sub in subscriptions_to_show)
    
    if has_channels:
        keyboard.append([
            InlineKeyboardButton(text="✅ Продолжить", callback_data="check_subscription")
        ])
    elif has_bots:
        keyboard.append([
            InlineKeyboardButton(text="✅ Продолжить", callback_data="check_subscription")
        ])
    else:
        keyboard.append([
            InlineKeyboardButton(text="✅ Продолжить", callback_data="check_subscription")
        ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def update_user_subscription_status(user_id: int, is_subscribed: bool):
    """Обновляет статус подписки пользователя в базе данных"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "UPDATE users SET is_subscribed=? WHERE user_id=?",
            (1 if is_subscribed else 0, user_id)
        )
        await db.commit()

async def get_user_subscription_status(user_id: int) -> bool:
    """Получает статус подписки пользователя из базы данных"""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT is_subscribed FROM users WHERE user_id=?",
            (user_id,)
        )
        row = await cur.fetchone()
        if row:
            return bool(row[0] == 1)
        return False

async def log(action: str, data: str = ""):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO logs(action,data,time) VALUES(?,?,?)",
            (action, data, str(datetime.now()))
        )
        await db.commit()
    logger.info(f"Лог: {action} - {data}")

async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT 1 FROM bans WHERE user_id=?", (user_id,))
        return await cur.fetchone() is not None

async def get_ban_info(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            cur = await db.execute("PRAGMA table_info(bans)")
            columns = await cur.fetchall()
            column_names = [col[1] for col in columns]
            
            if 'admin_id' in column_names and 'admin_username' in column_names:
                cur = await db.execute(
                    "SELECT reason, ban_time, admin_id, admin_username FROM bans WHERE user_id=?",
                    (user_id,)
                )
            else:
                cur = await db.execute(
                    "SELECT reason, ban_time FROM bans WHERE user_id=?",
                    (user_id,)
                )
                row = await cur.fetchone()
                if row:
                    return (row[0], row[1], None, None)
                return None
                
            return await cur.fetchone()
        except Exception as e:
            logger.error(f"Ошибка получения информации о блокировке: {e}")
            return None

async def ban_user(user_id: int, reason: str, admin: User):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            cur = await db.execute("PRAGMA table_info(bans)")
            columns = await cur.fetchall()
            column_names = [col[1] for col in columns]
            
            if 'admin_id' in column_names and 'admin_username' in column_names:
                await db.execute(
                    "INSERT OR REPLACE INTO bans(user_id, reason, ban_time, admin_id, admin_username) VALUES(?,?,?,?,?)",
                    (user_id, reason, str(datetime.now()), admin.id, admin.username)
                )
            else:
                await db.execute("ALTER TABLE bans ADD COLUMN admin_id INTEGER")
                await db.execute("ALTER TABLE bans ADD COLUMN admin_username TEXT")
                await db.execute(
                    "INSERT OR REPLACE INTO bans(user_id, reason, ban_time, admin_id, admin_username) VALUES(?,?,?,?,?)",
                    (user_id, reason, str(datetime.now()), admin.id, admin.username)
                )
        except Exception as e:
            logger.error(f"Ошибка при блокировке пользователя: {e}")
            await db.execute(
                "INSERT OR REPLACE INTO bans(user_id, reason, ban_time) VALUES(?,?,?)",
                (user_id, reason, str(datetime.now()))
            )
        await db.commit()
    await log("ban", f"admin {admin.id} banned user {user_id}: {reason}")

async def unban_user(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM bans WHERE user_id=?", (user_id,))
        await db.commit()
    await log("unban", f"user {user_id} unbanned")

async def get_banned_users():
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            cur = await db.execute("""
                SELECT b.user_id, b.reason, b.ban_time, b.admin_username, u.username 
                FROM bans b 
                LEFT JOIN users u ON b.user_id = u.user_id 
                ORDER BY b.ban_time DESC
            """)
            rows = await cur.fetchall()
            return rows
        except Exception as e:
            logger.error(f"Ошибка получения списка заблокированных: {e}")
            cur = await db.execute(
                "SELECT user_id, reason, ban_time FROM bans ORDER BY ban_time DESC"
            )
            rows = await cur.fetchall()
            result = []
            for user_id, reason, ban_time in rows:
                cur2 = await db.execute("SELECT username FROM users WHERE user_id=?", (user_id,))
                user_row = await cur2.fetchone()
                username = user_row[0] if user_row else None
                result.append((user_id, reason, ban_time, None, username))
            return result

async def add_to_publication_blacklist(keyword: str, admin_id: int):
    """Добавить ключевое слово в черный список для публикаций"""
    keyword_clean = keyword.strip().lower()
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                "INSERT INTO publication_blacklist(keyword, added_by, added_time) VALUES(?,?,?)",
                (keyword_clean, admin_id, str(datetime.now()))
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def remove_from_publication_blacklist(keyword: str):
    """Удалить ключевое слово из черного списка"""
    keyword_clean = keyword.strip().lower()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "DELETE FROM publication_blacklist WHERE keyword=?",
            (keyword_clean,)
        )
        await db.commit()
        return True

async def get_publication_blacklist():
    """Получить весь черный список"""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT keyword, added_by, added_time FROM publication_blacklist ORDER BY keyword"
        )
        return await cur.fetchall()

async def is_in_publication_blacklist(text: str) -> tuple[bool, str]:
    """Проверить, содержит ли текст слова из черного списка"""
    text_lower = text.lower()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT keyword FROM publication_blacklist")
        rows = await cur.fetchall()
        
        for row in rows:
            keyword = row[0]
            if keyword in text_lower:
                return True, keyword
    return False, ""

async def register_user(user: User):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (user.id,))
        if not await cur.fetchone():
            # Сначала создаем запись с is_subscribed = 0
            # Потом пользователь должен будет проверить подписку
            await db.execute(
                "INSERT INTO users(user_id, username, reg_date, is_subscribed) VALUES(?,?,?,?)",
                (user.id, user.username, str(datetime.now().date()), 0)
            )
            await db.commit()
            logger.info(f"Зарегистрирован новый пользователь: {user.id}")

async def posts_today(user_id: int) -> int:
    today = str(datetime.now().date())
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE user_id=? AND date(time)=?",
            (user_id, today)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

async def posts_week(user_id: int) -> int:
    week_ago = str(datetime.now() - timedelta(days=7))
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE user_id=? AND time>=?",
            (user_id, week_ago)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

async def get_all_users():
    """Получить всех пользователей бота"""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
        return [row[0] for row in rows]

async def get_users_count():
    """Получить количество пользователей"""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
        return row[0] if row else 0

async def get_post_status(post_id: int) -> str:
    """Получить статус поста"""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT status FROM posts WHERE id=?",
            (post_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else ""

async def get_post_moderator_info(post_id: int):
    """Получить информацию о модераторе поста"""
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT moderator_id, reject_reason FROM posts WHERE id=?",
            (post_id,)
        )
        row = await cur.fetchone()
        if row:
            moderator_id, reject_reason = row
            if moderator_id:
                cur2 = await db.execute(
                    "SELECT username FROM users WHERE user_id=?",
                    (moderator_id,)
                )
                mod_row = await cur2.fetchone()
                mod_username = mod_row[0] if mod_row else None
                return moderator_id, mod_username, reject_reason
        return None, None, None

async def update_post_message_ids(post_id: int, moderators_message_id: int = None, 
                                 admins_message_id: int = None):
    """Обновить ID сообщений поста в группах"""
    async with aiosqlite.connect(DB_NAME) as db:
        if moderators_message_id:
            await db.execute(
                "UPDATE posts SET message_id_moderators=?, chat_id_moderators=? WHERE id=?",
                (moderators_message_id, MODERATORS_CHAT_ID, post_id)
            )
        if admins_message_id:
            await db.execute(
                "UPDATE posts SET message_id_admins=?, chat_id_admins=? WHERE id=?",
                (admins_message_id, ADMINS_CHAT_ID, post_id)
            )
        await db.commit()

async def update_admin_message_status(post_id: int, status: str, reason: str = None):
    """Обновить сообщение в группе администраторов при изменении статуса поста"""
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute("""
                SELECT p.text, p.photo, p.message_id_admins, p.chat_id_admins, 
                       p.user_id, u.username, p.moderator_id
                FROM posts p 
                LEFT JOIN users u ON p.user_id = u.user_id 
                WHERE p.id=?
            """, (post_id,))
            row = await cur.fetchone()
            
            if not row or not row[2] or not row[3]:
                return
            
            text, photo, message_id, chat_id, user_id, username, moderator_id = row
            
            mod_username = None
            if moderator_id:
                cur2 = await db.execute(
                    "SELECT username FROM users WHERE user_id=?",
                    (moderator_id,)
                )
                mod_row = await cur2.fetchone()
                mod_username = mod_row[0] if mod_row else None
            
            if status == "published":
                header = f"📨 Пост #{post_id} опубликован"
                action_text = "👤 <b>Опубликовал:</b>"
                button_text = "👤 Кто опубликовал"
                callback_data = f"who_pub_{post_id}"
            elif status == "rejected":
                header = f"📨 Пост #{post_id} отклонён"
                action_text = "👤 <b>Отклонил:</b>"
                button_text = "👤 Кто отклонил"
                callback_data = f"who_rej_{post_id}"
            else:
                return
            
            admin_text = (
                f"{header}\n\n"
                f"📄 <b>Текст:</b>\n{text}\n\n"
                f"👤 <b>Автор:</b> @{username or 'без username'}\n"
                f"🆔 <b>ID автора:</b> <code>{user_id}</code>\n"
                f"{action_text} @{mod_username or 'неизвестно'}"
            )
            
            if status == "rejected" and reason:
                admin_text += f"\n📝 <b>Причина:</b> {reason}"
            
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=button_text, 
                        callback_data=callback_data
                    )
                ]
            ])
            
            try:
                if photo:
                    await bot.edit_message_caption(
                        chat_id=chat_id,
                        message_id=message_id,
                        caption=admin_text,
                        parse_mode='HTML',
                        reply_markup=kb
                    )
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=admin_text,
                        parse_mode='HTML',
                        reply_markup=kb
                    )
                
                logger.info(f"✅ Обновлено сообщение администраторов для поста #{post_id}")
            except Exception as e:
                logger.error(f"Ошибка при редактировании сообщения администраторов: {e}")
            
    except Exception as e:
        logger.error(f"Ошибка обновления сообщения администраторов для поста #{post_id}: {e}")

# ================== VALIDATION ==================
def validate_post_text(text: str) -> tuple[bool, str]:
    """Проверяет текст поста на соответствие требованиям."""
    if not text or text.strip() == "":
        return False, "❌ Текст поста не может быть пустым."
    
    if len(text.strip()) < 5:
        return False, "❌ Текст поста слишком короткий"
    
    if len(text) > 100:
        return False, "❌ Текст поста слишком длинный (максимум 100 символов)."
    
    if "🧑" not in text and "👩" not in text:
        return False, (
            "❌ <b>Обязательно добавьте один из этих эмодзи:</b>\n"
            "• 🧑 (мужчина)\n"
            "• 👩 (женщина)\n\n"
            "Примеры:\n"
            "• 🧑 Понравилась ...\n"
            "• 👩 Расскажите о ..."
        )
    
    return True, "✅ Текст прошел проверку."

# ================== KEYBOARDS ==================
def main_menu():
    """Красивое главное меню с новым расположением кнопок"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📩 Предложить пост", callback_data="offer")],
        [
            InlineKeyboardButton(text="❓ Частые вопросы", callback_data="faq"),
            InlineKeyboardButton(text="📜 Правила", callback_data="rules")
        ],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [
            InlineKeyboardButton(text="📢 Реклама", callback_data="ads"),
            InlineKeyboardButton(text="🛒 Магазин звёзд", url="https://t.me/smotrmaslyaninostars_bot")
        ]
    ])

def menu_btn():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ])

def rules_keyboard():
    """Клавиатура для правил с кнопкой Юр.увед."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚖️ Юридическое уведомление", url="https://teletype.in/@smotrmaslyanino/responsibility")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
    ])

def back_to_post_type():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ К выбору типа поста", callback_data="offer")]
    ])

def faq_keyboard():
    """Клавиатура для FAQ с кнопкой удаления записи и админами"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Удалить запись", url="https://t.me/nekon4il")],
        [InlineKeyboardButton(text="👥 Администрация", callback_data="admins")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
    ])

def admins_keyboard():
    """Клавиатура для страницы админов"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="faq")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu")]
    ])

def admin_menu():
    """Клавиатура админ-панели (только для администраторов)"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📨 Посты на модерации", callback_data="pending_posts")],
        [InlineKeyboardButton(text="🚫 Черный список", callback_data="blacklist")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="broadcast")],
        [InlineKeyboardButton(text="📋 Логи", callback_data="admin_logs")],
        [InlineKeyboardButton(text="💳 Управление подписками", callback_data="manage_subscriptions")]
    ])

def blacklist_menu():
    """Меню черного списка"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Заблокированные пользователи", callback_data="banned_users")],
        [InlineKeyboardButton(text="📝 Черный список публикаций", callback_data="pub_blacklist")],
        [InlineKeyboardButton(text="➕ Добавить слово в ЧС", callback_data="add_blacklist_keyword")],
        [InlineKeyboardButton(text="🗑️ Удалить слово из ЧС", callback_data="remove_blacklist_keyword")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])

def blacklist_cancel_menu():
    """Клавиатура отмены для черного списка"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="blacklist")]
    ])

def broadcast_menu():
    """Клавиатура для выбора типа рассылки"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Текстовая рассылка", callback_data="broadcast_text")],
        [InlineKeyboardButton(text="📷 Рассылка с фото", callback_data="broadcast_photo")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])

def broadcast_confirm_menu():
    """Клавиатура подтверждения рассылки"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Начать рассылку", callback_data="broadcast_start"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel")
        ]
    ])

def broadcast_cancel_menu():
    """Клавиатура отмены рассылки"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="broadcast_cancel")]
    ])

def subscriptions_menu():
    """Меню управления обязательными подписками"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список подписок", callback_data="list_subscriptions")],
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="add_channel_subscription")],
        [InlineKeyboardButton(text="🤖 Добавить бота", callback_data="add_bot_subscription")],
        [InlineKeyboardButton(text="🗑️ Удалить подписку", callback_data="remove_subscription")],
        [InlineKeyboardButton(text="🔄 Обновить подписки", callback_data="refresh_subscriptions")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])

def subscription_cancel_menu():
    """Клавиатура отмены для управления подписками"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="manage_subscriptions")]
    ])

def ads_keyboard():
    """Клавиатура для платного поста с кнопкой Прайс-лист"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Прайс-лист", url="https://t.me/smotrmaslyanino_price")],
        [InlineKeyboardButton(text="🛒 Купить", url="https://t.me/theaugustine")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")]
    ])

def moderation_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Создает клавиатуру для модерации поста"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"pub_{post_id}"),
            InlineKeyboardButton(text="❌ Отказать", callback_data=f"rej_{post_id}")
        ]
    ])

def disabled_moderation_keyboard(post_id: int, action: str = "published") -> InlineKeyboardMarkup:
    """Создает клавиатуру с отключенными кнопками после модерации"""
    if action == "published":
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Опубликовано", callback_data="disabled")
            ]
        ])
    else:  # rejected
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ Отклонено", callback_data="disabled")
            ]
        ])

def back_to_previous():
    """Клавиатура с кнопкой Назад для возврата к предыдущему шагу"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_previous_step")]
    ])

# ================== MIDDLEWARE ДЛЯ ПРОВЕРКИ ЧАТА И ТЕМЫ ==================
class ChatValidationMiddleware:
    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            # Проверяем, что сообщение не из групп модераторов/администраторов
            # (пользовательские команды должны приходить только из личных сообщений)
            if event.chat.type in ['group', 'supergroup']:
                # Если это группа модераторов или администраторов
                if event.chat.id in [MODERATORS_CHAT_ID, ADMINS_CHAT_ID]:
                    # Проверяем, что это правильная тема
                    if event.chat.id == MODERATORS_CHAT_ID and not is_valid_moderators_chat(event):
                        logger.warning(f"Сообщение из неправильной темы группы модераторов: {event.message_thread_id}")
                        return
                    elif event.chat.id == ADMINS_CHAT_ID and not is_valid_admins_chat(event):
                        logger.warning(f"Сообщение из неправильной темы группы администраторов: {event.message_thread_id}")
                        return
                else:
                    # Игнорируем сообщения из других групп
                    return
        
        elif isinstance(event, CallbackQuery):
            # Для колбэков из групп проверяем тему
            if event.message.chat.type in ['group', 'supergroup']:
                # Если это группа модераторов
                if event.message.chat.id == MODERATORS_CHAT_ID:
                    if not await validate_chat_for_moderation(event):
                        await event.answer("⚠️ Это действие доступно только в теме модерации", show_alert=True)
                        return
                # Если это группа администраторов
                elif event.message.chat.id == ADMINS_CHAT_ID:
                    if not await validate_chat_for_admin_actions(event):
                        await event.answer("⚠️ Это действие доступно только в теме администраторов", show_alert=True)
                        return
                else:
                    # Игнорируем колбэки из других групп
                    await event.answer("⚠️ Это действие недоступно в этой группе", show_alert=True)
                    return
        
        return await handler(event, data)

# ================== START с проверкой подписки ==================
@dp.message(F.text == "/start")
async def start(msg: Message):
    # Проверяем, что это личное сообщение
    if msg.chat.type not in ['private']:
        return await msg.answer("⚠️ Бот работает только в личных сообщениях")
    
    if await is_banned(msg.from_user.id):
        ban_info = await get_ban_info(msg.from_user.id)
        if ban_info:
            reason, ban_time, admin_id, admin_username = ban_info
            return await msg.answer(
                f"🚫 Вы заблокированы.\n\n"
                f"📝 Причина: {reason}\n"
                f"🕐 Время блокировки: {ban_time}\n"
                f"👮 Вас заблокировал администратор: @{admin_username or 'неизвестно'}"
            )
        return await msg.answer("🚫 Вы заблокированы.")
    
    await register_user(msg.from_user)
    
    is_subscribed, unsubscribed = await check_subscription(msg.from_user.id)
    unsubscribed_channels = [sub for sub in unsubscribed if sub["type"] == "channel"]
    
    if unsubscribed_channels:
        channels_text = "\n".join([f"• {sub['name']} ({sub['username']})" for sub in unsubscribed_channels])
        
        await msg.answer(
            f"<b>Для начала вам нужно подписаться на канал/ы</b>\n"
            f"После этого нажмите на кнопку «Я подписался».\n",
            parse_mode='HTML',
            reply_markup=get_subscription_keyboard(unsubscribed)
        )
        return
    
    await update_user_subscription_status(msg.from_user.id, True)
    
    await msg.answer(
        "Привет! 👋\n"
        "Предложи запись для размещения в канале. \n\n"
        "⚠️ <b>Важное правило:</b>\n"
        "Каждый пост должен содержать эмодзи 🧑 или 👩\n\n"
        "Выбери действие:",
        parse_mode='HTML',
        reply_markup=main_menu()
    )

# ================== ПРОВЕРКА ПОДПИСКИ ПО КНОПКЕ ==================
@dp.callback_query(F.data == "check_subscription")
async def check_subscription_callback(cb: CallbackQuery):
    # Проверяем, что это личное сообщение
    if cb.message.chat.type not in ['private']:
        return await cb.answer("⚠️ Действие доступно только в личных сообщениях", show_alert=True)
    
    await cb.answer("⏳ Проверяем подписку...")
    
    is_subscribed, unsubscribed = await check_subscription(cb.from_user.id)
    
    # Проверяем только каналы, ботов не проверяем
    unsubscribed_channels = [sub for sub in unsubscribed if sub["type"] == "channel"]
    
    if unsubscribed_channels:
        channels_text = "\n".join([f"• {sub['name']} ({sub['username']})" for sub in unsubscribed_channels])
        
        await cb.message.edit_text(
            f"<b>Вы еще не подписались на канал/ы 😡</b>\n"
            f"После подписки нажмите кнопку «Я подписался» еще раз",
            parse_mode='HTML',
            reply_markup=get_subscription_keyboard(unsubscribed_channels)
        )
        return
    
    await update_user_subscription_status(cb.from_user.id, True)
    
    await cb.message.edit_text(
        "✅ <b>Отлично! Вы подписались на необходимый/е канал/ы</b>\n\n"
        "Привет! 👋\n"
        "Предложи запись для размещения в канале. \n\n"
        "⚠️ <b>Важное правило:</b>\n"
        "Каждый пост должен содержать эмодзи 🧑 или 👩!\n\n"
        "Выбери действие:",
        parse_mode='HTML',
        reply_markup=main_menu()
    )

# ================== MIDDLEWARE ДЛЯ ПРОВЕРКИ ПОДПИСКИ ==================
class SubscriptionMiddleware:
    async def __call__(self, handler, event, data):
        # Пропускаем все сообщения и колбэки от админов
        if hasattr(event, 'from_user') and event.from_user.id in ADMINS:
            return await handler(event, data)
            
        if isinstance(event, Message):
            # Пропускаем сообщения из групп (для них своя проверка в ChatValidationMiddleware)
            if event.chat.type in ['group', 'supergroup']:
                return await handler(event, data)
                
            user_id = event.from_user.id
            
            # Пропускаем команду /start
            if event.text and event.text == "/start":
                return await handler(event, data)
            
            if await is_banned(user_id):
                return
            
            # Проверяем статус подписки
            is_subscribed = await get_user_subscription_status(user_id)
            
            if not is_subscribed:
                is_subscribed_now, unsubscribed = await check_subscription(user_id)
                
                # Проверяем только каналы
                unsubscribed_channels = [sub for sub in unsubscribed if sub["type"] == "channel"]
                
                if unsubscribed_channels:
                    text = (
                        f"📢 <b>Для использования бота необходимо подписаться на каналы:</b>\n\n"
                        f"👇 <i>Нажмите на кнопки ниже, чтобы перейти в каналы и подписаться, затем нажмите «Я подписался»:</i>"
                    )
                    
                    await event.answer(text, parse_mode='HTML', reply_markup=get_subscription_keyboard(unsubscribed_channels))
                    return
                else:
                    await update_user_subscription_status(user_id, True)
                    return await handler(event, data)
        
        elif isinstance(event, CallbackQuery):
            # Пропускаем колбэки из групп (для них своя проверка в ChatValidationMiddleware)
            if event.message.chat.type in ['group', 'supergroup']:
                return await handler(event, data)
                
            user_id = event.from_user.id
            
            # Пропускаем кнопку проверки подписки
            if event.data == "check_subscription":
                return await handler(event, data)
            
            if await is_banned(user_id):
                await event.answer("🚫 Вы заблокированы.", show_alert=True)
                return
            
            # Проверяем статус подписки
            is_subscribed = await get_user_subscription_status(user_id)
            
            if not is_subscribed:
                await event.answer("⚠️ Для использования бота необходимо подписаться на каналы.", show_alert=True)
                
                is_subscribed_now, unsubscribed = await check_subscription(user_id)
                
                # Проверяем только каналы
                unsubscribed_channels = [sub for sub in unsubscribed if sub["type"] == "channel"]
                
                if unsubscribed_channels:
                    text = (
                        f"📢 <b>Для использования бота необходимо подписаться на каналы:</b>\n\n"
                        f"👇 <i>Нажмите на кнопки ниже, чтобы перейти в каналы и подписаться, затем нажмите «Я подписался»:</i>"
                    )
                    
                    await event.message.edit_text(text, parse_mode='HTML', reply_markup=get_subscription_keyboard(unsubscribed_channels))
                    return
                else:
                    await update_user_subscription_status(user_id, True)
                    return await handler(event, data)
        
        return await handler(event, data)

# ================== РЕГИСТРАЦИЯ MIDDLEWARE ==================
chat_validation_middleware = ChatValidationMiddleware()
subscription_middleware = SubscriptionMiddleware()

dp.message.middleware(chat_validation_middleware)
dp.message.middleware(subscription_middleware)
dp.callback_query.middleware(chat_validation_middleware)
dp.callback_query.middleware(subscription_middleware)

# ================== КОМАНДА ДЛЯ АДМИНОВ ДЛЯ УПРАВЛЕНИЯ ПОДПИСКАМИ ==================
@dp.callback_query(F.data == "manage_subscriptions")
async def manage_subscriptions(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await cb.message.edit_text(
        "📢 <b>Управление обязательными подписками</b>\n\n"
        f"📊 <b>Текущее количество подписок:</b> {len(REQUIRED_SUBSCRIPTIONS)}\n\n"
        "<i>Выберите действие:</i>",
        parse_mode='HTML',
        reply_markup=subscriptions_menu()
    )

@dp.callback_query(F.data == "list_subscriptions")
async def list_subscriptions(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    if not REQUIRED_SUBSCRIPTIONS:
        text = "📋 <b>Список обязательных подписок пуст</b>"
    else:
        text_lines = ["📋 <b>Обязательные подписки:</b>\n\n"]
        
        for i, sub in enumerate(REQUIRED_SUBSCRIPTIONS, 1):
            emoji = "📢" if sub["type"] == "channel" else "🤖"
            text_lines.append(f"{i}. {emoji} <b>{sub['name']}</b>")
            text_lines.append(f"   Тип: {sub['type']}")
            text_lines.append(f"   ID/Username: <code>{sub['id']}</code>")
            text_lines.append(f"   Юзернейм: {sub['username']}")
            text_lines.append(f"   Ссылка: {sub['url']}\n")
        
        text = "\n".join(text_lines)
        
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (список слишком длинный)"
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=subscriptions_menu())

@dp.callback_query(F.data == "add_channel_subscription")
async def add_channel_subscription(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(SubscriptionState.wait_subscription_add)
    await state.update_data(sub_type="channel")
    
    await cb.message.edit_text(
        "➕ <b>Добавление обязательного канала</b>\n\n"
        "Отправьте данные канала в формате:\n"
        "<code>ID_канала @юзернейм Название_канала</code>\n\n"
        "<i>Пример:</i>\n"
        "<code>-1001234567890 @example_channel Основной канал</code>\n\n"
        "<i>Примечания:</i>\n"
        "1. ID канала должен быть числом (начинаться с -100)\n"
        "2. Юзернейм должен начинаться с @\n"
        "3. Название может содержать пробелы",
        parse_mode='HTML',
        reply_markup=subscription_cancel_menu()
    )

@dp.callback_query(F.data == "add_bot_subscription")
async def add_bot_subscription(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(SubscriptionState.wait_subscription_add)
    await state.update_data(sub_type="bot")
    
    await cb.message.edit_text(
        "🤖 <b>Добавление обязательного бота</b>\n\n"
        "Отправьте данные бота в формате:\n"
        "<code>юзернейм_бота Название_бота</code>\n\n"
        "<i>Пример:</i>\n"
        "<code>@smotrmaslyaninostars_bot Магазин звёзд</code>\n\n"
        "<i>Примечания:</i>\n"
        "1. Юзернейм должен начинаться с @\n"
        "2. Название может содержать пробелы\n"
        "3. Для бота будет создана ссылка вида: https://t.me/юзернейм_без_@",
        parse_mode='HTML',
        reply_markup=subscription_cancel_menu()
    )

@dp.message(SubscriptionState.wait_subscription_add)
async def process_subscription_add(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMINS:
        return
    
    data = await state.get_data()
    sub_type = data.get("sub_type")
    
    if sub_type == "channel":
        parts = msg.text.split(maxsplit=2)
        if len(parts) < 3:
            return await msg.answer("❌ Неверный формат. Нужно: ID_канала @юзернейм Название_канала")
        
        channel_id_str, username, name = parts
        
        try:
            channel_id = int(channel_id_str)
        except ValueError:
            return await msg.answer("❌ ID канала должен быть числом.")
        
        if not username.startswith("@"):
            return await msg.answer("❌ Юзернейм должен начинаться с @.")
        
        url = f"https://t.me/{username.lstrip('@')}"
        
        for sub in REQUIRED_SUBSCRIPTIONS:
            if sub["type"] == "channel" and (str(sub["id"]) == str(channel_id) or sub["username"] == username):
                return await msg.answer(f"❌ Канал уже есть в списке.")
        
        new_sub = {
            "type": "channel",
            "id": str(channel_id),
            "username": username,
            "name": name,
            "url": url
        }
        
        REQUIRED_SUBSCRIPTIONS.append(new_sub)
        await save_subscriptions_to_db()
        
        await msg.answer(
            f"✅ Канал добавлен:\n"
            f"<b>Тип:</b> Канал\n"
            f"<b>Название:</b> {name}\n"
            f"<b>ID:</b> <code>{channel_id}</code>\n"
            f"<b>Юзернейм:</b> {username}\n"
            f"<b>Ссылка:</b> {url}",
            parse_mode='HTML',
            reply_markup=subscriptions_menu()
        )
        
        await log("subscription_add", f"admin {msg.from_user.id} added channel {channel_id} ({name})")
    
    elif sub_type == "bot":
        parts = msg.text.split(maxsplit=1)
        if len(parts) < 2:
            return await msg.answer("❌ Неверный формат. Нужно: @юзернейм_бота Название_бота")
        
        username, name = parts
        
        if not username.startswith("@"):
            return await msg.answer("❌ Юзернейм должен начинаться с @.")
        
        bot_id = username.lstrip("@")
        url = f"https://t.me/{bot_id}"
        
        for sub in REQUIRED_SUBSCRIPTIONS:
            if sub["type"] == "bot" and sub["username"] == username:
                return await msg.answer(f"❌ Бот уже есть в списке.")
        
        new_sub = {
            "type": "bot",
            "id": bot_id,
            "username": username,
            "name": name,
            "url": url
        }
        
        REQUIRED_SUBSCRIPTIONS.append(new_sub)
        await save_subscriptions_to_db()
        
        await msg.answer(
            f"✅ Бот добавлен:\n"
            f"<b>Тип:</b> Бот\n"
            f"<b>Название:</b> {name}\n"
            f"<b>Username:</b> {username}\n"
            f"<b>ID:</b> <code>{bot_id}</code>\n"
            f"<b>Ссылка:</b> {url}",
            parse_mode='HTML',
            reply_markup=subscriptions_menu()
        )
        
        await log("subscription_add", f"admin {msg.from_user.id} added bot {username} ({name})")
    
    await state.clear()

@dp.callback_query(F.data == "remove_subscription")
async def remove_subscription(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    if not REQUIRED_SUBSCRIPTIONS:
        return await cb.answer("📋 Список подписок пуст.", show_alert=True)
    
    keyboard = []
    
    for i, sub in enumerate(REQUIRED_SUBSCRIPTIONS, 1):
        emoji = "📢" if sub["type"] == "channel" else "🤖"
        keyboard.append([
            InlineKeyboardButton(
                text=f"{i}. {emoji} {sub['name']}",
                callback_data=f"remove_sub_{i-1}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="manage_subscriptions")])
    
    await cb.message.edit_text(
        "🗑️ <b>Удаление подписки</b>\n\n"
        "Выберите подписку для удаления:",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )

@dp.callback_query(F.data.startswith("remove_sub_"))
async def process_remove_subscription(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    try:
        index = int(cb.data.split("_")[2])
        if 0 <= index < len(REQUIRED_SUBSCRIPTIONS):
            removed_sub = REQUIRED_SUBSCRIPTIONS.pop(index)
            
            await save_subscriptions_to_db()
            
            emoji = "📢" if removed_sub["type"] == "channel" else "🤖"
            await cb.message.edit_text(
                f"✅ Подписка удалена:\n\n"
                f"{emoji} <b>{removed_sub['name']}</b>\n"
                f"Тип: {removed_sub['type']}\n"
                f"Юзернейм: {removed_sub['username']}\n"
                f"Ссылка: {removed_sub['url']}",
                parse_mode='HTML',
                reply_markup=subscriptions_menu()
            )
            
            await log("subscription_remove", f"admin {cb.from_user.id} removed {removed_sub['type']} {removed_sub['username']}")
        else:
            await cb.answer("❌ Неверный индекс подписки.", show_alert=True)
    except (ValueError, IndexError):
        await cb.answer("❌ Ошибка при удалении подписки.", show_alert=True)

@dp.callback_query(F.data == "refresh_subscriptions")
async def refresh_subscriptions(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await load_subscriptions_from_db()
    await cb.answer("✅ Список подписок обновлен из базы данных.", show_alert=True)

# ================== ADMINS PAGE ==================
@dp.callback_query(F.data == "admins")
async def admins_page(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    
    text = (
        "👥 <b>Администраторы проекта</b>\n\n"
        "📱 <b>Контакты для связи:</b>\n"
        "• @theaugustine\n"
        "• @nekon4il\n\n"
        "⏰ <b>Время работы:</b>\n"
        "• Пн-Пт: 10:00 - 23:00\n"
        "• Сб-Вс: 12:00 - 00:00\n\n"
        "📞 <b>По вопросам:</b>\n"
        "• Публикации постов\n"
        "• Модерации\n"
        "• Рекламы\n"
        "• Технических проблем\n\n"
        "✉️ <b>Пишите нам, мы всегда на связи!</b>"
    )
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=admins_keyboard())

# ================== OFFER ==================
@dp.callback_query(F.data == "offer")
async def offer(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    if await posts_today(cb.from_user.id) >= 5:
        return await cb.answer("🔒 Лимит: 5 постов в день.", show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 С фото", callback_data="with_photo")],
        [InlineKeyboardButton(text="📝 Без фото", callback_data="no_photo")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="menu")]
    ])
    await cb.message.edit_text(
        "Выберите тип поста:\n\n"
        "⚠️ <b>Важно:</b>\n"
        "Помните о правилах публикации",
        parse_mode='HTML',
        reply_markup=kb
    )

# ================== WITH PHOTO ==================
@dp.callback_query(F.data == "with_photo")
async def with_photo(cb: CallbackQuery, state: FSMContext):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    
    await state.set_state(PostState.wait_photo)
    await cb.message.edit_text(
        "Пришлите фото для публикации. 📷",
        reply_markup=back_to_previous()
    )

@dp.message(PostState.wait_photo)
async def get_photo(msg: Message, state: FSMContext):
    if await is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы и не можете отправлять посты.")
    
    if not msg.photo:
        return await msg.answer("❗ Нужно отправить именно фото.", reply_markup=back_to_previous())
    
    await state.update_data(photo=msg.photo[-1].file_id)
    await state.set_state(PostState.wait_text_after_photo)
    
    await msg.answer(
        "✅ Фото принято.\n\n"
        "📝 <b>Теперь пришли текст к фото:</b>\n\n"
        "⚠️ Не забудьте добавить 🧑 или 👩 в текст!",
        parse_mode='HTML',
        reply_markup=back_to_post_type()
    )

@dp.message(PostState.wait_text_after_photo)
async def get_text_after_photo(msg: Message, state: FSMContext):
    if await is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы и не можете отправлять посты.")
    
    data = await state.get_data()
    
    is_blacklisted, keyword = await is_in_publication_blacklist(msg.text)
    if is_blacklisted:
        await msg.answer(
            f"❌ <b>Публикация отклонена</b>\n\n"
            f"Текст содержит пользователя из списка запрещённых на публикацию: <code>{keyword}</code>\n",
            parse_mode='HTML',
            reply_markup=menu_btn()
        )
        await log("blacklist_reject", f"user {msg.from_user.id}: keyword '{keyword}'")
        await state.clear()
        return
    
    is_valid, error_message = validate_post_text(msg.text)
    if not is_valid:
        await msg.answer(
            error_message,
            parse_mode='HTML',
            reply_markup=back_to_post_type()
        )
        return
    
    await state.clear()

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO posts(user_id, text, photo, time, status) VALUES(?,?,?,?,?)",
            (msg.from_user.id, msg.text, data["photo"], str(datetime.now()), "moderation")
        )
        post_id = cursor.lastrowid
        await db.commit()

    logger.info(f"Создан пост #{post_id} с фото от пользователя {msg.from_user.id}")
    
    await msg.answer(
        "✅ Ваш пост отправлен на модерацию.\n"
        "Мы сообщим вам о итогах, как только наши модераторы рассмотрят пост",
        reply_markup=menu_btn()
    )
    
    # Отправляем на модерацию
    await send_to_moderation(post_id)
    await log("new_post", f"photo post #{post_id} from user {msg.from_user.id}")

# ================== NO PHOTO ==================
@dp.callback_query(F.data == "no_photo")
async def no_photo(cb: CallbackQuery, state: FSMContext):
    if await is_banned(cb.from_user.id):
        return await cb.answer("Вы заблокированы.", show_alert=True)
    
    await state.set_state(PostState.wait_text_only)
    await cb.message.edit_text(
        "Пришлите текст для публикации.\n\n"
        "⚠️ Не забудь добавить 🧑 или 👩 в текст!",
        reply_markup=back_to_previous()
    )

@dp.message(PostState.wait_text_only)
async def get_text_only(msg: Message, state: FSMContext):
    if await is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы и не можете отправлять посты.")
    
    is_blacklisted, keyword = await is_in_publication_blacklist(msg.text)
    if is_blacklisted:
        await msg.answer(
            f"❌ <b>Публикация отклонена</b>\n\n"
            f"Текст содержит пользователя из списка запрещённых на публикацию: <code>{keyword}</code>",
            parse_mode='HTML',
            reply_markup=menu_btn()
        )
        await log("blacklist_reject", f"user {msg.from_user.id}: keyword '{keyword}'")
        await state.clear()
        return
    
    is_valid, error_message = validate_post_text(msg.text)
    if not is_valid:
        await msg.answer(
            error_message,
            parse_mode='HTML',
            reply_markup=back_to_post_type()
        )
        return
    
    await state.clear()

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO posts(user_id, text, photo, time, status) VALUES(?,?,?,?,?)",
            (msg.from_user.id, msg.text, None, str(datetime.now()), "moderation")
        )
        post_id = cursor.lastrowid
        await db.commit()

    logger.info(f"Создан текстовый пост #{post_id} от пользователя {msg.from_user.id}")
    
    await msg.answer(
        "✅ Ваш пост отправлен на модерацию.\n"
        "Мы сообщим вам о итогах, как только наши модераторы рассмотрят пост",
        reply_markup=menu_btn()
    )
    
    # Отправляем на модерацию
    await send_to_moderation(post_id)
    await log("new_post", f"text post #{post_id} from user {msg.from_user.id}")

# ================== ОБРАБОТЧИК КНОПКИ "НАЗАД" ==================
@dp.callback_query(F.data == "back_to_previous_step")
async def back_to_previous_step(cb: CallbackQuery, state: FSMContext):
    """Обработчик кнопки Назад в состояниях ввода"""
    await state.clear()
    
    # Возвращаем пользователя к выбору типа поста
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    if await posts_today(cb.from_user.id) >= 5:
        return await cb.answer("🔒 Лимит: 5 постов в день.", show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 С фото", callback_data="with_photo")],
        [InlineKeyboardButton(text="📝 Без фото", callback_data="no_photo")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="menu")]
    ])
    
    await cb.message.edit_text(
        "Выберите тип поста:\n\n"
        "⚠️ <b>Важно:</b>\n"
        "Помните о правилах публикации",
        parse_mode='HTML',
        reply_markup=kb
    )
    await cb.answer()

# ================== ОТПРАВКА НА МОДЕРАЦИЮ ==================
async def send_to_moderation(post_id: int):
    """Отправляет пост на модерацию в группу модераторов"""
    try:
        logger.info(f"Отправляю пост #{post_id} на модерацию...")
        
        # Получаем данные поста
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute(
                "SELECT text, photo FROM posts WHERE id=?",
                (post_id,)
            )
            row = await cur.fetchone()

        if not row:
            logger.error(f"Пост #{post_id} не найден в базе данных")
            return

        text, photo = row
        
        # Форматируем текст для модерации
        moderation_text = f"📨 <b>Новый пост #{post_id} на модерации</b>\n\n{text}"
        
        # Отправляем в группу модераторов с указанием темы
        try:
            if photo:
                logger.info(f"Отправляю фото-пост #{post_id} модераторам в тему {MODERATORS_TOPIC_ID}")
                sent_msg = await bot.send_photo(
                    chat_id=MODERATORS_CHAT_ID,
                    message_thread_id=MODERATORS_TOPIC_ID,
                    photo=photo,
                    caption=moderation_text,
                    parse_mode='HTML',
                    reply_markup=moderation_keyboard(post_id)
                )
            else:
                logger.info(f"Отправляю текстовый пост #{post_id} модераторам в тему {MODERATORS_TOPIC_ID}")
                sent_msg = await bot.send_message(
                    chat_id=MODERATORS_CHAT_ID,
                    message_thread_id=MODERATORS_TOPIC_ID,
                    text=moderation_text,
                    parse_mode='HTML',
                    reply_markup=moderation_keyboard(post_id)
                )
            
            logger.info(f"✅ Пост #{post_id} отправлен модераторам в чат {MODERATORS_CHAT_ID}, тема {MODERATORS_TOPIC_ID}")
            
            # Сохраняем ID сообщения для модераторов
            await update_post_message_ids(post_id, moderators_message_id=sent_msg.message_id)
                
        except Exception as e:
            logger.error(f"Ошибка отправки поста #{post_id} модераторам: {e}")
            # Пробуем отправить без указания темы
            try:
                if photo:
                    sent_msg = await bot.send_photo(
                        chat_id=MODERATORS_CHAT_ID,
                        photo=photo,
                        caption=moderation_text,
                        parse_mode='HTML',
                        reply_markup=moderation_keyboard(post_id)
                    )
                else:
                    sent_msg = await bot.send_message(
                        chat_id=MODERATORS_CHAT_ID,
                        text=moderation_text,
                        parse_mode='HTML',
                        reply_markup=moderation_keyboard(post_id)
                    )
                await update_post_message_ids(post_id, moderators_message_id=sent_msg.message_id)
                logger.info(f"✅ Пост #{post_id} отправлен модераторам без указания темы")
            except Exception as e2:
                logger.error(f"Критическая ошибка отправки поста #{post_id} модераторам: {e2}")
        
        # Отправляем уведомление администраторам
        await send_to_admins(post_id)
            
    except Exception as e:
        logger.error(f"Общая ошибка при отправке поста #{post_id} на модерацию: {e}")

async def send_to_admins(post_id: int):
    """Отправляет пост администраторам с информацией об авторе"""
    try:
        logger.info(f"Отправляю пост #{post_id} администраторам...")
        
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute("""
                SELECT p.text, p.photo, p.time, p.user_id, u.username 
                FROM posts p 
                LEFT JOIN users u ON p.user_id = u.user_id 
                WHERE p.id=?
            """, (post_id,))
            row = await cur.fetchone()

        if not row:
            logger.error(f"Пост #{post_id} не найден для администраторов")
            return

        text, photo, time, user_id, username = row
        
        try:
            post_time = datetime.fromisoformat(time)
            formatted_time = post_time.strftime('%d.%m.%Y %H:%M:%S')
        except:
            formatted_time = time

        # Формируем текст для администраторов
        admin_text = (
            f"📨 <b>Новый пост #{post_id} на модерации</b>\n\n"
            f"📄 <b>Текст:</b>\n{text}\n\n"
            f"👤 <b>Автор:</b> @{username or 'без username'}\n"
            f"🆔 <b>ID автора:</b> <code>{user_id}</code>\n"
            f"📅 <b>Время отправки:</b> {formatted_time}"
        )
        
        # Отправляем администраторам с указанием темы
        try:
            if photo:
                sent_msg = await bot.send_photo(
                    chat_id=ADMINS_CHAT_ID,
                    message_thread_id=ADMINS_TOPIC_ID,
                    photo=photo,
                    caption=admin_text,
                    parse_mode='HTML'
                )
            else:
                sent_msg = await bot.send_message(
                    chat_id=ADMINS_CHAT_ID,
                    message_thread_id=ADMINS_TOPIC_ID,
                    text=admin_text,
                    parse_mode='HTML'
                )
            
            # Сохраняем ID сообщения для администраторов
            await update_post_message_ids(post_id, admins_message_id=sent_msg.message_id)
            
            logger.info(f"✅ Пост #{post_id} отправлен администраторам в чат {ADMINS_CHAT_ID}, тема {ADMINS_TOPIC_ID}")
            
        except Exception as e:
            logger.error(f"Ошибка отправки поста #{post_id} администраторам: {e}")
            # Пробуем отправить без указания темы
            try:
                if photo:
                    sent_msg = await bot.send_photo(
                        chat_id=ADMINS_CHAT_ID,
                        photo=photo,
                        caption=admin_text,
                        parse_mode='HTML'
                    )
                else:
                    sent_msg = await bot.send_message(
                        chat_id=ADMINS_CHAT_ID,
                        text=admin_text,
                        parse_mode='HTML'
                    )
                await update_post_message_ids(post_id, admins_message_id=sent_msg.message_id)
                logger.info(f"✅ Пост #{post_id} отправлен администраторам без указания темы")
            except Exception as e2:
                logger.error(f"Критическая ошибка отправки поста #{post_id} администраторам: {e2}")
        
    except Exception as e:
        logger.error(f"Общая ошибка при отправке поста #{post_id} администраторам: {e}")

# ================== КТО ОПУБЛИКОВАЛ/ОТКЛОНИЛ ==================
@dp.callback_query(F.data.startswith("who_pub_"))
async def who_published(cb: CallbackQuery):
    """Показывает информацию о том, кто опубликовал пост"""
    try:
        post_id = int(cb.data.split("_")[2])
    except (ValueError, IndexError):
        return await cb.answer("❌ Неверный ID поста", show_alert=True)
    
    moderator_id, mod_username, reject_reason = await get_post_moderator_info(post_id)
    
    if not moderator_id:
        return await cb.answer("❌ Информация о модераторе не найдена", show_alert=True)
    
    text = (
        f"👤 <b>Информация о публикации поста #{post_id}</b>\n\n"
        f"🆔 <b>ID модератора:</b> <code>{moderator_id}</code>\n"
        f"📛 <b>Юзернейм:</b> @{mod_username or 'неизвестно'}\n"
        f"🔗 <b>Ссылка:</b> tg://user?id={moderator_id}"
    )
    
    await cb.answer()
    await bot.send_message(
        chat_id=cb.message.chat.id,
        text=text,
        parse_mode='HTML'
    )

@dp.callback_query(F.data.startswith("who_rej_"))
async def who_rejected(cb: CallbackQuery):
    """Показывает информацию о том, кто отклонил пост и причину"""
    try:
        post_id = int(cb.data.split("_")[2])
    except (ValueError, IndexError):
        return await cb.answer("❌ Неверный ID поста", show_alert=True)
    
    moderator_id, mod_username, reject_reason = await get_post_moderator_info(post_id)
    
    if not moderator_id or not reject_reason:
        return await cb.answer("❌ Информация об отклонении не найдена", show_alert=True)
    
    text = (
        f"👤 <b>Информация об отклонении поста #{post_id}</b>\n\n"
        f"🆔 <b>ID модератора:</b> <code>{moderator_id}</code>\n"
        f"📛 <b>Юзернейм:</b> @{mod_username or 'неизвестно'}\n"
        f"🔗 <b>Ссылка:</b> tg://user?id={moderator_id}\n\n"
        f"📝 <b>Причина отклонения:</b>\n{reject_reason}"
    )
    
    await cb.answer()
    await bot.send_message(
        chat_id=cb.message.chat.id,
        text=text,
        parse_mode='HTML'
    )

# ================== ПУБЛИКАЦИЯ ==================
@dp.callback_query(F.data.startswith("pub_"))
async def confirm_pub(cb: CallbackQuery):
    """Проверяем статус поста перед публикацией"""
    # Проверяем, что это правильный чат для модерации
    if not await validate_chat_for_moderation(cb):
        return await cb.answer("⚠️ Это действие доступно только в теме модерации", show_alert=True)
    
    try:
        pid = int(cb.data.split("_")[1])
    except ValueError:
        return await cb.answer("Неверный ID поста", show_alert=True)
    
    # Проверяем текущий статус поста
    current_status = await get_post_status(pid)
    if current_status == "published":
        await cb.answer("❌ Этот пост уже опубликован!", show_alert=True)
        return
    elif current_status == "rejected":
        await cb.answer("❌ Этот пост уже отклонен!", show_alert=True)
        return
    
    # Создаем клавиатуру подтверждения
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, опубликовать", callback_data=f"yes_{pid}")],
        [InlineKeyboardButton(text="❌ Нет, отменить", callback_data=f"no_{pid}")]
    ])
    
    # Отправляем сообщение в ТЕМУ группы модераторов
    await bot.send_message(
        chat_id=MODERATORS_CHAT_ID,
        message_thread_id=MODERATORS_TOPIC_ID,  # Указываем тему
        text="Подтвердить публикацию?",
        reply_markup=kb
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("yes_"))
async def publish(cb: CallbackQuery):
    """Публикация поста"""
    # Проверяем, что это правильный чат для модерации
    if not await validate_chat_for_moderation(cb):
        return await cb.answer("⚠️ Это действие доступно только в теме модерации", show_alert=True)
    
    try:
        pid = int(cb.data.split("_")[1])
    except ValueError:
        return await cb.answer("Неверный ID поста", show_alert=True)

    # Проверяем статус поста перед публикацией
    current_status = await get_post_status(pid)
    if current_status in ["published", "rejected"]:
        await cb.answer(f"❌ Этот пост уже {'опубликован' if current_status == 'published' else 'отклонен'}!", show_alert=True)
        return

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT text, photo, user_id FROM posts WHERE id=?",
            (pid,)
        )
        row = await cur.fetchone()

        if not row:
            return await cb.answer("Пост не найден", show_alert=True)

        text, photo, user_id = row
        
        # Публикуем в основном канале
        try:
            if photo:
                await bot.send_photo(MAIN_CHANNEL_ID, photo, caption=text)
            else:
                await bot.send_message(MAIN_CHANNEL_ID, text)
        except Exception as e:
            logger.error(f"Ошибка публикации поста #{pid} в канал: {e}")
            return await cb.answer(f"Ошибка публикации: {e}", show_alert=True)

        # Обновляем статус поста
        await db.execute(
            "UPDATE posts SET status='published', moderator_id=?, moderation_time=? WHERE id=?",
            (cb.from_user.id, str(datetime.now()), pid)
        )
        await db.commit()

        # Обновляем сообщение в группе администраторов
        await update_admin_message_status(pid, "published")

        # Уведомляем автора поста
        try:
            await bot.send_message(
                user_id,
                "🎉 Ваш пост был опубликован в канале!"
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {user_id}: {e}")

    await log("publish", str(pid))
    
    # Отправляем подтверждение в ТЕМУ группы модераторов
    await bot.send_message(
        chat_id=MODERATORS_CHAT_ID,
        message_thread_id=MODERATORS_TOPIC_ID,  # Указываем тему
        text="✅ Пост опубликован"
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("no_"))
async def cancel_pub(cb: CallbackQuery):
    """Отмена публикации"""
    # Проверяем, что это правильный чат для модерации
    if not await validate_chat_for_moderation(cb):
        return await cb.answer("⚠️ Это действие доступно только в теме модерации", show_alert=True)
    
    # Отправляем сообщение в ТЕМУ группы модераторов
    await bot.send_message(
        chat_id=MODERATORS_CHAT_ID,
        message_thread_id=MODERATORS_TOPIC_ID,  # Указываем тему
        text="❌ Действие отменено."
    )
    await cb.answer()

# ================== ОТКЛОНЕНИЕ С ТАЙМАУТОМ ==================
async def reset_reject_state(post_id: int, message_id: int, chat_id: int, text: str, photo: str = None):
    """Сбрасывает состояние отказа и возвращает к исходному сообщению"""
    try:
        if photo:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=text,
                parse_mode='HTML',
                reply_markup=moderation_keyboard(post_id)
            )
        else:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode='HTML',
                reply_markup=moderation_keyboard(post_id)
            )
        logger.info(f"✅ Состояние отказа для поста #{post_id} сброшено (таймаут)")
    except TelegramBadRequest as e:
        # Если сообщение не изменилось - это нормально, просто пропускаем
        if "message is not modified" in str(e):
            logger.debug(f"Сообщение #{post_id} уже имеет нужное состояние")
        else:
            logger.error(f"Ошибка при сбросе состояния отказа для поста #{post_id}: {e}")
    except Exception as e:
        logger.error(f"Ошибка при сбросе состояния отказа для поста #{post_id}: {e}")

async def reject_timeout_handler(state: FSMContext, post_id: int, message_id: int, 
                                chat_id: int, original_text: str, photo: str = None):
    """Обработчик таймаута для состояния отказа"""
    await asyncio.sleep(60)  # Ждем 1 минуту
    
    data = await state.get_data()
    current_post_id = data.get("post_id")
    
    # Проверяем, что это все еще тот же пост и состояние не изменилось
    if current_post_id == post_id:
        current_state = await state.get_state()
        if current_state == RejectState.wait_reason.state:
            # Сбрасываем состояние
            await state.clear()
            
            # Возвращаем исходное сообщение
            await reset_reject_state(post_id, message_id, chat_id, original_text, photo)

@dp.callback_query(F.data.startswith("rej_"))
async def reject(cb: CallbackQuery, state: FSMContext):
    """Отклонение поста"""
    # Проверяем, что это правильный чат для модерации
    if not await validate_chat_for_moderation(cb):
        return await cb.answer("⚠️ Это действие доступно только в теме модерации", show_alert=True)
    
    try:
        pid = int(cb.data.split("_")[1])
    except ValueError:
        return await cb.answer("Неверный ID поста", show_alert=True)
    
    # Проверяем текущий статус поста
    current_status = await get_post_status(pid)
    if current_status == "published":
        await cb.answer("❌ Этот пост уже опубликован!", show_alert=True)
        return
    elif current_status == "rejected":
        await cb.answer("❌ Этот пост уже отклонен!", show_alert=True)
        return
    
    # Сохраняем информацию о сообщении для возможного восстановления
    message_id = cb.message.message_id
    chat_id = cb.message.chat.id
    
    # Получаем текст и фото поста
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT text, photo FROM posts WHERE id=?",
            (pid,)
        )
        row = await cur.fetchone()
        if not row:
            return await cb.answer("Пост не найден", show_alert=True)
        
        post_text, photo = row
        original_text = f"📨 <b>Новый пост #{pid} на модерации</b>\n\n{post_text}"
    
    await state.set_state(RejectState.wait_reason)
    await state.update_data(
        post_id=pid,
        message_id=message_id,
        chat_id=chat_id,
        original_text=original_text,
        photo=photo,
        timestamp=datetime.now()
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_rej_{pid}")]
    ])

    # Отправляем сообщение в ТЕМУ группы модераторов
    await bot.send_message(
        chat_id=MODERATORS_CHAT_ID,
        message_thread_id=MODERATORS_TOPIC_ID,  # Указываем тему
        text="Опишите причину отказа (у вас 1 минута):",
        reply_markup=kb
    )
    await cb.answer()
    
    # Запускаем таймер для сброса состояния через 1 минуту
    asyncio.create_task(reject_timeout_handler(state, pid, message_id, chat_id, original_text, photo))

@dp.callback_query(F.data.startswith("cancel_rej_"))
async def cancel_rej(cb: CallbackQuery, state: FSMContext):
    """Отмена отказа"""
    try:
        pid = int(cb.data.split("_")[2])
    except ValueError:
        return await cb.answer("❌ Ошибка", show_alert=True)
    
    data = await state.get_data()
    current_post_id = data.get("post_id")
    
    if current_post_id != pid:
        return await cb.answer("❌ Несоответствие ID поста", show_alert=True)
    
    message_id = data.get("message_id")
    chat_id = data.get("chat_id")
    original_text = data.get("original_text")
    photo = data.get("photo")
    
    await state.clear()
    
    # Возвращаем исходное сообщение
    await reset_reject_state(pid, message_id, chat_id, original_text, photo)
    
    await cb.answer("❌ Отмена отклонения")

@dp.message(RejectState.wait_reason)
async def reject_reason(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("post_id")
    if not pid:
        return await msg.answer("Ошибка: не найден ID поста.")
    
    message_id = data.get("message_id")
    chat_id = data.get("chat_id")
    original_text = data.get("original_text")
    photo = data.get("photo")
    timestamp = data.get("timestamp")
    
    # Проверяем, не прошло ли больше 1 минуты
    if timestamp and (datetime.now() - timestamp).total_seconds() > 70:  # Даем небольшую фору
        await state.clear()
        await reset_reject_state(pid, message_id, chat_id, original_text, photo)
        # Отправляем сообщение в ТЕМУ группы модераторов
        return await bot.send_message(
            chat_id=MODERATORS_CHAT_ID,
            message_thread_id=MODERATORS_TOPIC_ID,
            text="⚠️ Время на указание причины истекло. Действие отменено."
        )
    
    await state.clear()

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT user_id FROM posts WHERE id=?",
            (pid,)
        )
        row = await cur.fetchone()

        if not row:
            return await msg.answer("Пост не найден.")

        user_id = row[0]
        await db.execute(
            "UPDATE posts SET status='rejected', moderator_id=?, moderation_time=?, reject_reason=? WHERE id=?",
            (msg.from_user.id, str(datetime.now()), msg.text, pid)
        )
        await db.commit()

        # Обновляем сообщение в группе администраторов
        await update_admin_message_status(pid, "rejected", msg.text)

        # Обновляем исходное сообщение (отключаем кнопки)
        try:
            if photo:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=original_text,
                    parse_mode='HTML',
                    reply_markup=disabled_moderation_keyboard(pid, "rejected")
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=original_text,
                    parse_mode='HTML',
                    reply_markup=disabled_moderation_keyboard(pid, "rejected")
                )
        except TelegramBadRequest as e:
            # Если сообщение не изменилось - это нормально, просто пропускаем
            if "message is not modified" not in str(e):
                logger.error(f"Ошибка при обновлении сообщения: {e}")
        except Exception as e:
            logger.error(f"Ошибка при обновлении сообщения: {e}")

    # Уведомляем автора
    try:
        await bot.send_message(
            user_id,
            f"❌ Ваш пост отклонён.\n\n"
            f"📝 <b>Причина:</b> {msg.text}\n\n"
            f"👮 <b>Модератор:</b> @{msg.from_user.username or 'без username'}",
            parse_mode='HTML'
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {user_id}: {e}")

    await log("reject", str(pid))
    
    # Отправляем подтверждение в ТЕМУ группы модераторов
    await bot.send_message(
        chat_id=MODERATORS_CHAT_ID,
        message_thread_id=MODERATORS_TOPIC_ID,
        text="✅ Причина отправлена пользователю."
    )
# ================== ОБРАБОТЧИК ДЛЯ ОТКЛЮЧЕННЫХ КНОПОК ==================
@dp.callback_query(F.data == "disabled")
async def disabled_button_handler(cb: CallbackQuery):
    """Обработчик для отключенных кнопок"""
    await cb.answer("❌ Это действие недоступно - пост уже был обработан модератором", show_alert=True)

# ================== RULES с кнопкой Юр.увед. ==================
@dp.callback_query(F.data == "rules")
async def rules(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    
    await cb.message.edit_text(
        "📜 <b>Правила смотра:</b>\n\n"
        "1. Не публикуются посты на которых присутствуют: оскорбления, фото интимного характера\n"
        "2. Не публикуются посты с фотографией, на которой человеку меньше 14 лет (будем определять на вид)\n"
        "3. Не публикуются посты с упоминанием возраста младше 14 лет\n"
        "4. Администрация оставляет за собой право удалять любой контент\n"
        "5. Не публикуются посты в которых упоминается о вредоносных веществах\n"
        "6. Не публикуются посты с упоминанием питбайкеров\n\n"
        "⚠️ Перед пользованием нашим ботом ознакомьтесь также с юридическим уведомление:",
        parse_mode='HTML',
        reply_markup=rules_keyboard()
    )

# ================== MENU ==================
@dp.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "🏠 Вы в главном меню \n\n"
        "Бот от @maslyanino, ты сегодня прекрасно выглядишь 😘\n\n"
        "Выбери действие: 👇",
        parse_mode='HTML',
        reply_markup=main_menu()
    )

# ================== PROFILE ==================
@dp.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    
    today = await posts_today(cb.from_user.id)
    week = await posts_week(cb.from_user.id)

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT reg_date, is_subscribed FROM users WHERE user_id=?",
            (cb.from_user.id,)
        )
        row = await cur.fetchone()
        reg = row[0] if row else "Неизвестно"
        is_subscribed = row[1] if row and row[1] == 1 else 0

    subscription_status = "✅ Подписан" if is_subscribed else "❌ Не подписан"

    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 <b>ID:</b> <code>{cb.from_user.id}</code>\n"
        f"📛 <b>Юзернейм:</b> @{cb.from_user.username or 'не установлен'}\n"
        f"📊 <b>Статистика:</b>\n"
        f"• Постов за день: {today}/5\n"
        f"• Постов за неделю: {week}\n\n"
        f"📅 <b>Дата регистрации:</b> {reg}\n"
        f"🕵 <b>Разработчик: @theaugustine</b>"
    )
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=menu_btn())

# ================== FAQ / ADS ==================
@dp.callback_query(F.data == "faq")
async def faq(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    
    await cb.message.edit_text(
        "❓ <b>Частые вопросы:</b>\n\n"
        "<b>- Сколько постов можно отправлять в день?</b>\n"
        "Не более 5 постов в сутки\n\n"
        "<b>- Сколько времени занимает модерация?</b>\n"
        "До 24 часов\n\n"
        "<b>- Почему мой пост отклонили?</b>\n"
        "Модератор должен указать причину отказа\n\n"
        "<b>- Как удалить свою запись?</b>\n"
        "Нажмите кнопку 'Удалить запись' ниже 👇\n\n"
        "<b>- Как связаться с администрация?</b>\n"
        "Нажмите кнопку 'Администрация' ниже 👇",
        parse_mode='HTML',
        reply_markup=faq_keyboard()
    )

@dp.callback_query(F.data == "ads")
async def ads(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    
    kb = ads_keyboard()
    await cb.message.edit_text(
        "📢 <b>Платный пост</b>\n\n"
        "Размещение рекламы в нашем канале:\n"
        "• 24 часа - 199 руб\n"
        "• 48 часа - 289 руб\n"
        "• 72 часа - 379 руб\n"
        "• Навсегда - 419 руб\n\n"
        "Закрепление рекламы:\n"
        "• 24 часа + 199 руб к стоимости\n"
        "• 48 часа + 299 руб к стоимости\n"
        "• 72 часа + 399 руб к стоимости\n\n"
        "Остальные услуги находятся в прайс-листе 📩",
        parse_mode='HTML',
        reply_markup=kb
    )

# ================== COMMANDS FOR ADMINS ==================
@dp.message(F.text.startswith("/ban"))
async def ban_command(msg: Message):
    """Команда для блокировки пользователя: /ban <user_id> <причина>"""
    if msg.from_user.id not in ADMINS:
        return
    
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 2:
        return await msg.answer(
            "❌ <b>Использование:</b> <code>/ban &lt;user_id&gt; [причина]</code>\n\n"
            "<i>Примеры:</i>\n"
            "<code>/ban 123456789 спам</code>\n"
            "<code>/ban 123456789 нарушение правил</code>",
            parse_mode='HTML'
        )
    
    try:
        user_id = int(parts[1])
        reason = parts[2] if len(parts) > 2 else "Нарушение правил"
        
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
            user_exists = await cur.fetchone() is not None
        
        if not user_exists:
            return await msg.answer(f"❌ Пользователь с ID <code>{user_id}</code> не найден в базе.", parse_mode='HTML')
        
        await ban_user(user_id, reason, msg.from_user)
        
        try:
            await bot.send_message(
                user_id,
                f"🚫 <b>Вы были заблокированы!</b>\n\n"
                f"📝 <b>Причина:</b> {reason}\n"
                f"👮 <b>Администратор:</b> @{msg.from_user.username or 'без username'}\n"
                f"🆔 <b>ID администратора:</b> {msg.from_user.id}\n\n"
                f"🔒 <b>Вы больше не можете использовать меню бота</b>\n\n"
                f"📞 <b>Для разблокировки:</b> Свяжитесь с @theaugustine (услуга разблокировки не бесплатна)",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {user_id} о блокировке: {e}")
        
        await msg.answer(
            f"✅ Пользователь <code>{user_id}</code> заблокирован.\n"
            f"📝 <b>Причина:</b> {reason}",
            parse_mode='HTML'
        )
        
    except ValueError:
        await msg.answer("❌ Неверный формат ID пользователя. ID должен быть числом.")

@dp.message(F.text.startswith("/unban"))
async def unban_command(msg: Message):
    """Команда для разблокировки пользователя: /unban <user_id>"""
    if msg.from_user.id not in ADMINS:
        return
    
    parts = msg.text.split()
    if len(parts) < 2:
        return await msg.answer(
            "❌ <b>Использование:</b> <code>/unban &lt;user_id&gt;</code>\n\n"
            "<i>Пример:</i>\n"
            "<code>/unban 123456789</code>",
            parse_mode='HTML'
        )
    
    try:
        user_id = int(parts[1])
        
        if not await is_banned(user_id):
            return await msg.answer(f"❌ Пользователь <code>{user_id}</code> не заблокирован.", parse_mode='HTML')
        
        await unban_user(user_id)
        
        try:
            await bot.send_message(
                user_id,
                "✅ <b>Вы были разблокированы!</b>\n\n"
                "🔓 Теперь вы снова можете использовать бота.\n"
                f"👮 <b>Администратор:</b> @{msg.from_user.username or 'без username'}\n",
                parse_mode='HTML'
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {user_id} о разблокировке: {e}")
        
        await msg.answer(f"✅ Пользователь <code>{user_id}</code> разблокирован.", parse_mode='HTML')
        
    except ValueError:
        await msg.answer("❌ Неверный формат ID пользователя. ID должен быть числом.")

@dp.message(F.text.startswith("/blacklist_add"))
async def blacklist_add_command(msg: Message):
    """Команда для добавления слова в черный список публикаций"""
    if msg.from_user.id not in ADMINS:
        return
    
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer(
            "❌ <b>Использование:</b> <code>/blacklist_add &lt;слово или фраза&gt;</code>\n\n"
            "<i>Примеры:</i>\n"
            "<code>/blacklist_add @spammer</code>\n"
            "<code>/blacklist_add плохое слово</code>\n"
            "<code>/blacklist_add запрещенная реклама</code>",
            parse_mode='HTML'
        )
    
    keyword = parts[1].strip()
    if len(keyword) < 2:
        return await msg.answer("❌ Ключевое слово должно содержать минимум 2 символа.")
    
    success = await add_to_publication_blacklist(keyword, msg.from_user.id)
    
    if success:
        await msg.answer(
            f"✅ Добавлено в черный список публикаций: <code>{keyword}</code>\n\n"
            f"📝 Теперь посты, содержащие это слово/фразу, будут автоматически отклоняться.",
            parse_mode='HTML'
        )
        await log("blacklist_add", f"admin {msg.from_user.id} added '{keyword}'")
    else:
        await msg.answer(f"❌ Ключевое слово <code>{keyword}</code> уже есть в черном списке.", parse_mode='HTML')

@dp.message(F.text.startswith("/blacklist_remove"))
async def blacklist_remove_command(msg: Message):
    """Команда для удаления слова из черного списка публикаций"""
    if msg.from_user.id not in ADMINS:
        return
    
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        return await msg.answer(
            "❌ <b>Использование:</b> <code>/blacklist_remove &lt;слово или фраза&gt;</code>\n\n"
            "<i>Примеры:</i>\n"
            "<code>/blacklist_remove @theaugustine</code>\n"
            "<code>/blacklist_remove плохое слово</code>",
            parse_mode='HTML'
        )
    
    keyword = parts[1].strip()
    
    await remove_from_publication_blacklist(keyword)
    
    await msg.answer(
        f"✅ Удалено из черного списка публикаций: <code>{keyword}</code>\n\n"
        f"📝 Теперь посты могут содержать это слово/фразу.",
        parse_mode='HTML'
    )
    await log("blacklist_remove", f"admin {msg.from_user.id} removed '{keyword}'")

@dp.message(F.text.startswith("/blacklist"))
async def blacklist_show_command(msg: Message):
    """Показать черный список публикаций"""
    if msg.from_user.id not in ADMINS:
        return
    
    blacklist = await get_publication_blacklist()
    
    if not blacklist:
        return await msg.answer("📝 Черный список публикаций пуст.")
    
    text_lines = ["📋 <b>Черный список публикаций:</b>\n\n"]
    
    for i, (keyword, added_by, added_time) in enumerate(blacklist, 1):
        try:
            time_str = datetime.fromisoformat(added_time).strftime('%d.%m.%Y')
        except:
            time_str = added_time
        
        text_lines.append(f"{i}. <code>{keyword}</code>")
        text_lines.append(f"   👤 Добавил: {added_by} | 📅 {time_str}\n")
    
    text = "\n".join(text_lines)
    
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (список слишком длинный)"
    
    await msg.answer(text, parse_mode='HTML')

# ================== ADMIN PANEL ==================
@dp.message(F.text == "/admin")
async def admin_panel_command(msg: Message):
    if msg.from_user.id not in ADMINS:
        return await msg.answer("🚫 У вас нет доступа к этой команде.")
    
    users_count = await get_users_count()
    banned_users = await get_banned_users()
    banned_count = len(banned_users)
    blacklist = await get_publication_blacklist()
    blacklist_count = len(blacklist)
    subscription_count = len(REQUIRED_SUBSCRIPTIONS)
    
    text = (
        f"🛠 <b>Админ-панель</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"👥 Пользователей: <b>{users_count}</b>\n"
        f"🚫 Заблокировано: <b>{banned_count}</b>\n"
        f"📝 Слов в ЧС: <b>{blacklist_count}</b>\n"
        f"📢 Обязательных подписок: <b>{subscription_count}</b>\n\n"
        f"<i>Выберите действие:</i>"
    )
    
    await msg.answer(text, parse_mode='HTML', reply_markup=admin_menu())

@dp.callback_query(F.data == "admin_panel")
async def admin_panel_callback(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    users_count = await get_users_count()
    banned_users = await get_banned_users()
    banned_count = len(banned_users)
    blacklist = await get_publication_blacklist()
    blacklist_count = len(blacklist)
    subscription_count = len(REQUIRED_SUBSCRIPTIONS)
    
    text = (
        f"🛠 <b>Админ-панель</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"👥 Пользователей: <b>{users_count}</b>\n"
        f"🚫 Заблокировано: <b>{banned_count}</b>\n"
        f"📝 Слов в ЧС: <b>{blacklist_count}</b>\n"
        f"📢 Обязательных подписок: <b>{subscription_count}</b>\n\n"
        f"<i>Выберите действие:</i>"
    )
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=admin_menu())

@dp.callback_query(F.data == "blacklist")
async def blacklist_panel(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    banned_users = await get_banned_users()
    banned_count = len(banned_users)
    blacklist = await get_publication_blacklist()
    blacklist_count = len(blacklist)
    
    text = (
        f"🚫 <b>Управление черными списками</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"👤 Заблокированных пользователей: <b>{banned_count}</b>\n"
        f"📝 Слов в черном списке публикаций: <b>{blacklist_count}</b>\n\n"
        f"<i>Выберите действие:</i>"
    )
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=blacklist_menu())

@dp.callback_query(F.data == "banned_users")
async def show_banned_users(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    banned_users = await get_banned_users()
    
    if not banned_users:
        text = "👤 <b>Нет заблокированных пользователей</b>"
    else:
        text_lines = ["🚫 <b>Заблокированные пользователи:</b>\n\n"]
        
        for i, (user_id, reason, ban_time, admin_username, username) in enumerate(banned_users, 1):
            try:
                time_str = datetime.fromisoformat(ban_time).strftime('%d.%m.%Y %H:%M')
            except:
                time_str = ban_time
            
            text_lines.append(f"{i}. <code>{user_id}</code> (@{username or 'без username'})")
            text_lines.append(f"   📝 <b>Причина:</b> {reason}")
            if admin_username:
                text_lines.append(f"   👮 <b>Админ:</b> @{admin_username}")
            text_lines.append(f"   🕐 <b>Заблокирован:</b> {time_str}\n")
        
        text = "\n".join(text_lines)
        
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (список слишком длинный)"
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=blacklist_menu())

@dp.callback_query(F.data == "pub_blacklist")
async def show_pub_blacklist(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    blacklist = await get_publication_blacklist()
    
    if not blacklist:
        text = "📝 <b>Черный список публикаций пуст</b>"
    else:
        text_lines = ["📋 <b>Черный список публикаций:</b>\n\n"]
        
        for i, (keyword, added_by, added_time) in enumerate(blacklist, 1):
            try:
                time_str = datetime.fromisoformat(added_time).strftime('%d.%m.%Y')
            except:
                time_str = added_time
            
            text_lines.append(f"{i}. <code>{keyword}</code>")
            text_lines.append(f"   👤 Добавил: {added_by} | 📅 {time_str}\n")
        
        text = "\n".join(text_lines)
        
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (список слишком длинный)"
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=blacklist_menu())

@dp.callback_query(F.data == "add_blacklist_keyword")
async def add_blacklist_keyword(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(BlacklistState.wait_keyword)
    await cb.message.edit_text(
        "📝 <b>Добавление слова в черный список публикаций</b>\n\n"
        "Отправьте слово или фразу, которую хотите добавить в черный список.\n\n"
        "<i>Примеры:</i>\n"
        "• @theaugustine - для блокировки упоминаний юзернейма\n"
        "• Яков Дибилкин - для блокировки ФИО\n"
        "• запрещенная фраза - для блокировки конкретной фразы\n\n"
        "⚠️ <b>Внимание:</b> Регистр не учитывается.",
        parse_mode='HTML',
        reply_markup=blacklist_cancel_menu()
    )

@dp.message(BlacklistState.wait_keyword)
async def process_blacklist_keyword(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMINS:
        return
    
    keyword = msg.text.strip()
    if len(keyword) < 2:
        await msg.answer("❌ Ключевое слово должно содержать минимум 2 символа.")
        return
    
    success = await add_to_publication_blacklist(keyword, msg.from_user.id)
    
    if success:
        await msg.answer(
            f"✅ Добавлено в черный список публикаций: <code>{keyword}</code>\n\n"
            f"📝 Теперь посты, содержащие это слово/фразу, будут автоматически отклоняться.",
            parse_mode='HTML',
            reply_markup=blacklist_menu()
        )
        await log("blacklist_add", f"admin {msg.from_user.id} added '{keyword}'")
    else:
        await msg.answer(
            f"❌ Ключевое слово <code>{keyword}</code> уже есть в черном списке.",
            parse_mode='HTML',
            reply_markup=blacklist_menu()
        )
    
    await state.clear()

@dp.callback_query(F.data == "remove_blacklist_keyword")
async def remove_blacklist_keyword(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(BlacklistState.wait_remove_keyword)
    
    blacklist = await get_publication_blacklist()
    
    if blacklist:
        preview = "\n".join([f"• <code>{keyword}</code>" for keyword, _, _ in blacklist[:5]])
        if len(blacklist) > 5:
            preview += f"\n... и еще {len(blacklist) - 5} слов"
        
        text = (
            f"🗑️ <b>Удаление слова из черного списка публикаций</b>\n\n"
            f"📋 <b>Текущий список (первые 5):</b>\n{preview}\n\n"
            f"Отправьте слово или фразу, которую хотите удалить из черного списка."
        )
    else:
        text = (
            "🗑️ <b>Удаление слова из черного списка публикаций</b>\n\n"
            "📋 Черный список публикаций пуст.\n\n"
            "Отправьте слово или фразу, которую хотите удалить из черного списка."
        )
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=blacklist_cancel_menu())

@dp.message(BlacklistState.wait_remove_keyword)
async def process_remove_blacklist_keyword(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMINS:
        return
    
    keyword = msg.text.strip()
    
    await remove_from_publication_blacklist(keyword)
    
    await msg.answer(
        f"✅ Удалено из черного списка публикаций: <code>{keyword}</code>\n\n"
        f"📝 Теперь посты могут содержать это слово/фразу.",
        parse_mode='HTML',
        reply_markup=blacklist_menu()
    )
    await log("blacklist_remove", f"admin {msg.from_user.id} removed '{keyword}'")
    
    await state.clear()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    users_count = await get_users_count()
    banned_users = await get_banned_users()
    banned_count = len(banned_users)
    blacklist_count = len(await get_publication_blacklist())
    subscription_count = len(REQUIRED_SUBSCRIPTIONS)
    
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM posts")
        total_posts = (await cur.fetchone())[0]
        
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE status='published'")
        published_posts = (await cur.fetchone())[0]
        
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE status='moderation'")
        pending_posts = (await cur.fetchone())[0]
        
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE status='rejected'")
        rejected_posts = (await cur.fetchone())[0]
        
        today = str(datetime.now().date())
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE date(time)=?", (today,))
        today_posts = (await cur.fetchone())[0]
        
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE date(reg_date)=?", (today,))
        today_users = (await cur.fetchone())[0]
    
    text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 <b>Пользователи:</b>\n"
        f"• Всего: {users_count}\n"
        f"• Новые сегодня: {today_users}\n"
        f"• Заблокировано: {banned_count}\n"
        f"• Слов в ЧС: {blacklist_count}\n"
        f"• Обязательных подписок: {subscription_count}\n\n"
        f"📨 <b>Посты:</b>\n"
        f"• Всего: {total_posts}\n"
        f"• Опубликовано: {published_posts}\n"
        f"• На модерации: {pending_posts}\n"
        f"• Отклонено: {rejected_posts}\n"
        f"• За сегодня: {today_posts}\n\n"
        f"🕐 <b>Время сервера:</b>\n"
        f"{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
    )
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=admin_menu())

@dp.callback_query(F.data == "admin_logs")
async def show_admin_logs(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT action,data,time FROM logs ORDER BY id DESC LIMIT 20"
        )
        rows = await cur.fetchall()

    if not rows:
        text = "📋 <b>Логи пока отсутствуют</b>"
    else:
        text_lines = ["📋 <b>Последние 20 логов:</b>\n"]
        for action, data, time in rows:
            try:
                log_time = datetime.fromisoformat(time)
                formatted_time = log_time.strftime('%H:%M:%S')
            except:
                formatted_time = time
            
            text_lines.append(f"🕐 {formatted_time} | {action} | {data}")
        
        text = "\n".join(text_lines)
        if len(text) > 4000:
            text = text[:4000] + "..."
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=admin_menu())

@dp.callback_query(F.data == "pending_posts")
async def show_pending_posts(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT id, user_id, text, time FROM posts WHERE status='moderation' ORDER BY id DESC LIMIT 10"
        )
        rows = await cur.fetchall()

    if not rows:
        text = "📭 <b>Постов на модерации нет</b>"
    else:
        text_lines = ["📨 <b>Посты на модерации:</b>\n\n"]
        for post_id, user_id, post_text, time in rows:
            preview = post_text[:50] + "..." if len(post_text) > 50 else post_text
            
            try:
                post_time = datetime.fromisoformat(time)
                formatted_time = post_time.strftime('%H:%M')
            except:
                formatted_time = time
            
            text_lines.append(f"📌 <b>Пост #{post_id}</b>")
            text_lines.append(f"👤 Автор: <code>{user_id}</code>")
            text_lines.append(f"🕐 {formatted_time}")
            text_lines.append(f"📄 {preview}")
            text_lines.append("")
        
        text = "\n".join(text_lines)
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=admin_menu())

# ================== BROADCAST FUNCTIONALITY ==================
@dp.callback_query(F.data == "broadcast")
async def broadcast_menu_handler(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    users_count = await get_users_count()
    
    text = (
        f"📢 <b>Рассылка сообщений</b>\n\n"
        f"👥 Всего пользователей: <b>{users_count}</b>\n\n"
        f"<i>Выберите тип рассылки:</i>"
    )
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=broadcast_menu())

# ================== RUN ==================
async def main():
    logger.info("Запуск бота...")
    await init_db()
    logger.info("Бот запущен и готов к работе")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())