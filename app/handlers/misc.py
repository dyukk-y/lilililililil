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

    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"📛 <b>Юзернейм:</b> @{cb.from_user.username or 'не установлен'}\n"
        f"📅 <b>Дата регистрации:</b> {reg}\n\n"
        f"📊 <b>Статистика постов:</b>\n"
        f"• Отправлено сегодня: {today}/5\n"
        f"• Отправлено за неделю: {week}\n"
        f"• ✅ Опубликовано всего: {post_stats.get('published', 0)}\n"
        f"• 🕐 В очереди на модерацию: {in_queue}\n"
        f"• ❌ Отклонено: {post_stats.get('rejected', 0)}\n\n"
        f"💬 <b>Активность в канале:</b>\n"
        f"• Комментариев оставлено: {comments_count}\n"
        f"• Упоминаний в чужих постах: {mentions_count}\n\n"
        f"🕵 <b>Разработчик: @theaugustine</b>"
    )
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=menu_btn())

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
