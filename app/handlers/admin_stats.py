import logging
from aiogram import F, Router
from aiogram.types import *
from datetime import datetime

import aiosqlite

from app.config import (
    ADMINS, SUPER_ADMINS, DB_NAME, REQUIRED_SUBSCRIPTIONS, TIMEZONE,
)
from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *
from app.scheduler import make_backup

logger = logging.getLogger(__name__)

router = Router()

# ================== АДМИНСКАЯ СТАТИСТИКА ==================
@router.callback_query(F.data == "admin_stats")
async def admin_stats(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    users_count = await get_users_count()
    banned_users, _ = await get_banned_users(page=1, per_page=1)
    banned_count = len(banned_users) if banned_users else 0
    blacklist, _ = await get_publication_blacklist(page=1, per_page=100)
    blacklist_count = len(blacklist) if blacklist else 0
    subscription_count = len(REQUIRED_SUBSCRIPTIONS)
    
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM posts")
        total_posts = (await cur.fetchone())[0]
        
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE status='published'")
        published_posts = (await cur.fetchone())[0]
        
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE status IN ('moderation','approved')")
        pending_posts = (await cur.fetchone())[0]
        
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE status='rejected'")
        rejected_posts = (await cur.fetchone())[0]
        
        today = str(datetime.now().date())
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE date(time)=?", (today,))
        today_posts = (await cur.fetchone())[0]
        
        cur = await db.execute("SELECT COUNT(*) FROM users WHERE date(reg_date)=?", (today,))
        today_users = (await cur.fetchone())[0]

    today_nsk = datetime.now(TIMEZONE).date().isoformat()
    queued_today = await get_queue_count_for_date(today_nsk)
    published_today_nsk = await get_published_count_for_date(today_nsk)

    text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 <b>Пользователи:</b>\n"
        f"• Всего: {users_count}\n"
        f"• Новые сегодня: {today_users}\n"
        f"• Заблокировано: {banned_count}\n"
        f"• Стоп-слов: {blacklist_count}\n"
        f"• Обязательных подписок: {subscription_count}\n\n"
        f"📨 <b>Посты:</b>\n"
        f"• Всего: {total_posts}\n"
        f"• Опубликовано: {published_posts}\n"
        f"• В очереди/обрабатывается: {pending_posts}\n"
        f"• Отклонено: {rejected_posts}\n"
        f"• Создано сегодня: {today_posts}\n\n"
        f"📅 <b>Публикация сегодня (Новосибирск):</b>\n"
        f"• Опубликовано: {published_today_nsk}\n"
        f"• Запланировано: {queued_today}\n"
        f"• Занято слотов: {published_today_nsk + queued_today}\n\n"
        f"🕐 <b>Время сервера:</b>\n"
        f"{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n"
        f"🕐 <b>Время Новосибирска:</b>\n"
        f"{datetime.now(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')}"
    )
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=admin_menu(cb.from_user.id in SUPER_ADMINS))

# ================== AI СТАТИСТИКА ==================
@router.callback_query(F.data == "admin_ai_stats")
async def admin_ai_stats(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    stats = await get_ai_stats(30)
    reviewed = stats["approved"] + stats["rejected"]
    fp_rate = (stats["false_positive"] / max(1, stats["auto"])) * 100
    fn_rate = (stats["false_negative"] / max(1, stats["manual"])) * 100
    text = (
        "🤖 <b>ИИ — последние 30 дней</b>\n\n"
        f"🚀 Авто-кандидатов: <b>{stats['auto']}</b>\n"
        f"👤 Ручная модерация: <b>{stats['manual']}</b>\n"
        f"✅ Решений ‘опубликовать’: <b>{stats['approved']}</b>\n"
        f"❌ Решений ‘отклонить’: <b>{stats['rejected']}</b>\n\n"
        f"⚠️ Auto → отказ: <b>{stats['false_positive']}</b> ({fp_rate:.1f}%)\n"
        f"📥 Manual → публикация: <b>{stats['false_negative']}</b> ({fn_rate:.1f}%)\n\n"
        f"📚 Разобрано человеком: <b>{reviewed}</b>\n"
        "Решения модераторов используются как feedback для Trust Score и будущей калибровки."
    )
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=admin_menu(cb.from_user.id in SUPER_ADMINS))

# ================== ЛОГИ ==================
@router.callback_query(F.data == "admin_logs")
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
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=admin_menu(cb.from_user.id in SUPER_ADMINS))

# ================== РЕЗЕРВНОЕ КОПИРОВАНИЕ ==================
@router.callback_query(F.data == "admin_backup_now")
async def admin_backup_now(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await cb.answer("⏳ Создаю резервную копию...")
    
    try:
        path = await make_backup()
        await cb.message.answer_document(
            FSInputFile(path),
            caption=(
                f"🗄 <b>Резервная копия БД</b>\n"
                f"🕐 {datetime.now(TIMEZONE).strftime('%d.%m.%Y %H:%M:%S')} (Новосибирск)"
            ),
            parse_mode='HTML',
        )
        await log("admin_backup", f"admin {cb.from_user.id} создал резервную копию вручную")
    except Exception as e:
        logger.error(f"Ошибка ручного резервного копирования: {e}")
        await cb.message.answer(f"❌ Не удалось создать резервную копию: {e}")
