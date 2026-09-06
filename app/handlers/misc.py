from aiogram import F, Router
from aiogram.types import *
from aiogram.fsm.context import FSMContext

import aiosqlite

from app.config import (
    DB_NAME,
)
from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *

router = Router()

# ================== UNIVERSAL CANCEL ==================
@router.callback_query(F.data == "cancel_action")
async def cancel_action(cb: CallbackQuery, state: FSMContext):
    """Универсальная отмена пользовательских сценариев.

    Кнопка намеренно не требует админских прав: она используется в
    сценариях «Удалить запись» и «Узнать автора» и должна быть доступна
    любому пользователю, которому эти сценарии доступны.
    """
    await state.clear()
    await cb.answer("Отменено")
    await cb.message.edit_text(
        "🏠 Действие отменено.",
        reply_markup=main_menu(),
    )

# ================== RULES ==================
@router.callback_query(F.data == "rules")
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
        "6. Не публикуются посты с упоминанием питбайкеров\n"
        "7. Посты с матами, оскорблениями и повторяющимся текстом отклоняются автоматически\n\n"
        "⚠️ Перед пользованием нашим ботом ознакомьтесь также с юридическим уведомление:",
        parse_mode='HTML',
        reply_markup=rules_keyboard()
    )

# ================== MENU ==================
@router.callback_query(F.data == "menu")
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
@router.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)

    user_id = cb.from_user.id
    today = await posts_today(user_id)
    week = await posts_week(user_id)
    post_stats = await get_user_post_stats(user_id)
    comments_count, mentions_count = await get_user_extra_stats(user_id)

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT reg_date FROM users WHERE user_id=?",
            (user_id,)
        )
        row = await cur.fetchone()
        reg = row[0] if row else "Неизвестно"

    in_queue = post_stats.get("approved", 0) + post_stats.get("moderation", 0)

    # Показываем фактический рейтинг пользователя из БД. Если значения нет,
    # используем нейтральное значение 0.0, а не подставляем пример из макета.
    trust = 0.0
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            cur = await db.execute(
                "SELECT trust_score FROM users WHERE user_id=?",
                (user_id,)
            )
            row = await cur.fetchone()
            if row and row[0] is not None:
                trust = float(row[0])
    except Exception:
        pass

    activity_level = "🌱 Начинающая"
    if comments_count + mentions_count >= 50:
        activity_level = "🔥 Очень активная"
    elif comments_count + mentions_count >= 20:
        activity_level = "🌿 Активная"
    elif comments_count + mentions_count >= 5:
        activity_level = "🌱 Развивающаяся"

    text = (
        "📊 <b>Публикации:</b>\n"
        f"<blockquote>📨 Сегодня  {today}/5\n"
        f"🗓 За 7 дней  {week}\n"
        f"✅ Опубликовано  {post_stats.get('published', 0)}\n"
        f"⏳ В очереди  {in_queue}\n"
        f"❌ Отклонено  {post_stats.get('rejected', 0)}</blockquote>\n\n"
        "👤 <b>Ваш профиль</b>\n\n"
        f"📛 @{cb.from_user.username or 'не установлен'}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"📅 С нами с: {reg}\n\n"
        "💬 <b>Активность:</b>\n"
        f"<blockquote>💭 Комментариев  {comments_count}\n"
        f"🔔 Упоминаний  {mentions_count}\n"
        f"⚡️ Уровень активности  {activity_level}</blockquote>\n\n"
        "🛡 <b>Надёжность:</b>\n"
        f"<blockquote>Рейтинг доверия: {trust:+.1f}\n"
        "Чем аккуратнее публикации, тем выше доверие системы.</blockquote>"
    )
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=profile_keyboard())

# ================== FAQ / ADS ==================
@router.callback_query(F.data == "faq")
async def faq(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    
    await cb.message.edit_text(
        "❓ <b>Частые вопросы:</b>\n\n"
        "<b>- Сколько постов можно отправлять в день?</b>\n"
        "Не более 5 постов в сутки\n\n"
        "<b>- Как быстро публикуется пост?</b>\n"
        "Пост проходит модерацию — срок рассмотрения до 24 часов. После "
        "отправки бот покажет, сколько постов сейчас в очереди\n\n"
        "<b>- Почему мой пост отклонили?</b>\n"
        "Автоматически отклоняются посты с матами/оскорблениями, слишком "
        "короткие (менее 3 слов) и с текстом, который уже публиковался "
        "слишком много раз. В остальных случаях причину укажет модератор\n\n"
        "<b>- Как удалить свою запись?</b>\n"
        "Нажмите кнопку 'Удалить запись' ниже 👇\n\n"
        "<b>- Как узнать, кто автор поста?</b>\n"
        "Нажмите кнопку 'Узнать автора' ниже — это платная услуга (Telegram Stars) 👇\n\n"
        "<b>- Как связаться с администрация?</b>\n"
        "Нажмите кнопку 'Администрация' ниже 👇",
        parse_mode='HTML',
        reply_markup=faq_keyboard()
    )

@router.callback_query(F.data == "ads")
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
