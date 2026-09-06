from aiogram import F, Router
from aiogram.types import *


from app.config import (
    ADMINS, SUPER_ADMINS, REQUIRED_SUBSCRIPTIONS,
)
from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *

router = Router()

# ================== ADMIN PANEL ==================
async def _admin_panel_text() -> str:
    users_count = await get_users_count()
    banned_users, _ = await get_banned_users(page=1, per_page=1)
    banned_count = len(banned_users) if banned_users else 0
    blacklist, _ = await get_publication_blacklist(page=1, per_page=100)
    blacklist_count = len(blacklist) if blacklist else 0
    subscription_count = len(REQUIRED_SUBSCRIPTIONS)
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE status IN ('moderation','approved')")
        queue_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE ai_decision='auto' AND time >= datetime('now','-30 days')")
        ai_auto_30d = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE ai_decision IS NOT NULL AND time >= datetime('now','-30 days')")
        ai_total_30d = (await cur.fetchone())[0]
    ai_coverage = (ai_auto_30d / ai_total_30d * 100) if ai_total_30d else 0

    return (
        f"🛠 Центр управления\n\n"
        f"Панель администратора: публикации, модерация, пользователи и настройки.\n\n"
        f"📊 Состояние:\n"
        f"👥 Пользователей: {users_count}\n"
        f"🚫 Заблокировано: {banned_count}\n"
        f"📝 Стоп-слов (маты/оскорбления/спам): {blacklist_count}\n"
        f"📢 Обязательных подписок: {subscription_count}\n"
        f"⏳ В очереди сейчас: {queue_count}\n\n"
        f"🤖 ИИ за 30 дней · автопроверка {ai_coverage:.0f}% из анализированных постов.\n\n"
        f"🤖 Посты с ключевыми фразами (см. «Фразы для автопубликации») "
        f"публикуются автоматически по расписанию; всё остальное — фото и "
        f"текст без таких фраз — ждёт решения модератора (до 24 часов).\n\n"
        f"Выберите действие:"
    )


@router.message(F.text == "/admin")
async def admin_panel_command(msg: Message):
    if msg.from_user.id not in ADMINS:
        return await msg.answer("🚫 У вас нет доступа к этой команде.")
    
    await msg.answer(
        await _admin_panel_text(), parse_mode='HTML',
        reply_markup=admin_menu(msg.from_user.id in SUPER_ADMINS)
    )

@router.callback_query(F.data == "admin_panel")
async def admin_panel_callback(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await cb.message.edit_text(
        await _admin_panel_text(), parse_mode='HTML',
        reply_markup=admin_menu(cb.from_user.id in SUPER_ADMINS)
    )

@router.callback_query(F.data == "blacklist")
async def blacklist_panel(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    banned_users, _ = await get_banned_users(page=1, per_page=1)
    banned_count = len(banned_users) if banned_users else 0
    blacklist, _ = await get_publication_blacklist(page=1, per_page=1)
    blacklist_count = len(blacklist) if blacklist else 0
    
    text = (
        f"🚫 Заблокированные пользователи и стоп-слова\n\n"
        f"📊 Статистика:\n"
        f"👤 Заблокированных пользователей: {banned_count}\n"
        f"📝 Стоп-слов (маты/оскорбления/спам): {blacklist_count}\n\n"
        f"Стоп-слова автоматически отклоняют пост ещё до отправки в очередь "
        f"на публикацию. Список можно свободно редактировать ниже.\n\n"
        f"Выберите действие:"
    )
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=blacklist_menu())
