import logging
from aiogram import F, Router
from aiogram.types import *
from datetime import datetime

import aiosqlite

from app.auth import is_admin as _is_admin
from app.config import (
    ADMINS, SUPER_ADMINS, DB_NAME, REQUIRED_SUBSCRIPTIONS, TIMEZONE,
)
from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *
from app.scheduler import make_backup
from app.runtime_settings import get as get_setting
from app.tg_ui import edit_rich, expandable, rich_table, tg_time
from app.utils import h, shorten

logger = logging.getLogger(__name__)

router = Router()

# ================== АДМИНСКАЯ СТАТИСТИКА ==================
@router.callback_query(F.data == "admin_stats")
async def admin_stats(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    users_count = await get_users_count()
    _, banned_count = await get_banned_users(page=1, per_page=1)
    _, blacklist_count = await get_publication_blacklist(page=1, per_page=1)
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
        
        # posts.time и users.reg_date хранятся в часовом поясе TIMEZONE, а не
        # во времени сервера — раньше «сегодня» считалось по серверу (UTC) и
        # в первые часы суток по Новосибирску статистика была неверной.
        today = datetime.now(TIMEZONE).date().isoformat()
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
        f"📅 <b>Публикация сегодня:</b>\n"
        f"• Опубликовано: {published_today_nsk}\n"
        f"• Запланировано: {queued_today}\n"
        f"• Занято слотов: {published_today_nsk + queued_today} из {get_setting('DAILY_PUBLISH_LIMIT')}\n\n"
        f"🕐 Обновлено: {tg_time(datetime.now(TIMEZONE))}"
    )
    rich = (
        "<h3>📊 Статистика бота</h3>"
        + rich_table([
            ("👥 Пользователей", str(users_count)),
            ("🆕 Новых сегодня", str(today_users)),
            ("🚫 Заблокировано", str(banned_count)),
            ("📝 Стоп-слов", str(blacklist_count)),
            ("📢 Обязательных подписок", str(subscription_count)),
        ], header=("Пользователи", ""))
        + rich_table([
            ("Всего", str(total_posts)),
            ("✅ Опубликовано", str(published_posts)),
            ("⏳ В очереди", str(pending_posts)),
            ("❌ Отклонено", str(rejected_posts)),
            ("🆕 Создано сегодня", str(today_posts)),
        ], header=("Посты", ""))
        + rich_table([
            ("✅ Опубликовано", str(published_today_nsk)),
            ("🗓 Запланировано", str(queued_today)),
            ("📦 Занято слотов", f"{published_today_nsk + queued_today} / {get_setting('DAILY_PUBLISH_LIMIT')}"),
        ], header=("Сегодня", ""))
        + f"<footer>Обновлено: {tg_time(datetime.now(TIMEZONE))}</footer>"
    )
    await edit_rich(cb.message, rich, text, reply_markup=admin_back_keyboard([
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_stats")]]))

# ================== ЛОГИ ==================
@router.callback_query(F.data == "admin_logs")
async def show_admin_logs(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
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
            except (TypeError, ValueError):
                formatted_time = time
            
            # В логах бывает пользовательский текст (причины, слова) —
            # без экранирования весь экран логов падал с ошибкой.
            line = f"🕐 {h(formatted_time)} | {h(action)} | {h(shorten(data, 300))}"
            if sum(len(x) + 1 for x in text_lines) + len(line) > 4000:
                text_lines.append("…")
                break
            text_lines.append(line)
        
        text = "\n".join(text_lines)
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=admin_back_keyboard([
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_logs")]]))

# ================== РЕЗЕРВНОЕ КОПИРОВАНИЕ ==================
@router.callback_query(F.data == "admin_backup_now")
async def admin_backup_now(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
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
        await cb.message.answer(f"❌ Не удалось создать резервную копию: {h(e)}")


# ================== СТАТИСТИКА ИИ ==================
# Кнопка «🤖 ИИ» раньше была в админ-панели, но обработчика у неё не было.
def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else "—"


@router.callback_query(F.data == "admin_ai_stats")
async def admin_ai_stats(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)

    from app import ai_learning
    stats = await get_ai_stats(30)
    model = ai_learning.get_model()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM logs WHERE action='autopilot' AND time >= datetime('now', '-30 days')")
        autopilot_count = (await cur.fetchone())[0]
    autopilot_line = (
        f"🤖 Автопилот: текст {'вкл' if get_setting('AUTOPILOT_TEXT') else 'выкл'}, "
        f"фото доверенных {'вкл' if get_setting('AUTOPILOT_TRUSTED_PHOTOS') else 'выкл'} · "
        f"решений за 30 дней: <b>{autopilot_count}</b>"
    )

    routed = stats["auto"] + stats["manual"]
    reviewed = stats["approved"] + stats["rejected"]
    if model is None:
        model_line = "⏳ ещё не обучалась (обучение идёт в фоне каждые 30 мин)"
    elif model.ready:
        model_line = (
            f"✅ активна · примеров: <b>{model.samples}</b> "
            f"(одобрено {model.docs['ok']}, отклонено {model.docs['bad']})"
        )
    else:
        need = ai_learning.MIN_SAMPLES_PER_CLASS
        model_line = (
            f"📚 набирает данные: одобрено {model.docs['ok']}/{need}, "
            f"отклонено {model.docs['bad']}/{need}"
        )

    text = (
        "🤖 <b>ИИ-модерация · 30 дней</b>\n\n"
        "<blockquote>"
        f"🚦 Маршрутизация: авто <b>{stats['auto']}</b> ({_pct(stats['auto'], routed)}) · "
        f"вручную <b>{stats['manual']}</b>\n"
        f"👮 Решения людей: одобрено <b>{stats['approved']}</b> · отклонено <b>{stats['rejected']}</b>\n"
        f"⚠️ Авто, но отклонено людьми: <b>{stats['false_positive']}</b>\n"
        f"🙈 На ручной, но одобрено: <b>{stats['false_negative']}</b>"
        "</blockquote>\n\n"
        f"🧠 <b>Самообучаемая модель</b>\n{model_line}\n\n"
        f"{autopilot_line}\n\n"
        + expandable(
            "Модель учится на решениях модераторов: чем больше постов вы "
            "публикуете и отклоняете вручную, тем точнее она подсказывает. "
            "Сама она пост не публикует — только отправляет сомнительные на "
            "ручную проверку."
        )
    )
    rich = (
        "<h3>🤖 ИИ-модерация · 30 дней</h3>"
        + rich_table([
            ("🚦 Автопубликация", f"{stats['auto']} ({_pct(stats['auto'], routed)})"),
            ("✋ Ручная проверка", str(stats['manual'])),
            ("👮 Одобрено людьми", str(stats['approved'])),
            ("👮 Отклонено людьми", str(stats['rejected'])),
            ("⚠️ Авто, но отклонено", str(stats['false_positive'])),
            ("🙈 Вручную, но одобрено", str(stats['false_negative'])),
        ])
        + f"<h4>🧠 Самообучаемая модель</h4><p>{model_line}</p><p>{autopilot_line}</p>"
        + "<footer>Модель учится на решениях модераторов и сама посты не публикует.</footer>"
    )
    await edit_rich(cb.message, rich, text, reply_markup=admin_back_keyboard([
        [InlineKeyboardButton(text="🧠 Переобучить сейчас", callback_data="admin_ai_retrain")]]))


@router.callback_query(F.data == "admin_ai_retrain")
async def admin_ai_retrain(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    from app import ai_learning
    model = await ai_learning.retrain()
    await cb.answer(f"🧠 Модель переобучена: {model.samples} примеров", show_alert=True)
    await admin_ai_stats(cb)
