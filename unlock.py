from aiogram import F, Router
from aiogram.types import *
from aiogram.fsm.context import FSMContext

import aiosqlite

from app.runtime_settings import get as get_setting
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
        "📜 <b>Правила Смотра</b>\n\n"
        "<blockquote>Перед отправкой поста быстро проверьте эти пункты — так публикация пройдёт без лишних задержек.</blockquote>\n\n"
        "<b>🚫 Нельзя</b>\n"
        "• оскорбления и запрещённый контент\n"
        "• интимные материалы\n"
        "• контент с людьми младше 14 лет\n"
        "• упоминания возраста младше 14 лет\n"
        "• вредоносные вещества\n"
        "• публикации о питбайкерах\n"
        "• мат, спам и повторяющийся текст\n\n"
        "<blockquote>⚠️ Администрация оставляет за собой право удалить публикацию или отказать в размещении.</blockquote>\n\n"
        "Перед использованием бота также ознакомьтесь с юридическим уведомлением.",
        parse_mode='HTML',
        reply_markup=rules_keyboard()
    )

# ================== MENU ==================
@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "🏠 <b>Главное меню</b>\n\n"
        "<blockquote>✨ Всё необходимое — в одном месте.\nПредложить пост • профиль • помощь • реклама</blockquote>\n"
        "Что хотите сделать? 👇",
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
            "SELECT reg_date, trust_score FROM users WHERE user_id=?",
            (user_id,)
        )
        row = await cur.fetchone()
        reg = row[0] if row else "Неизвестно"
        trust = float(row[1] or 0) if row else 0.0

    published = post_stats.get("published", 0)
    rejected = post_stats.get("rejected", 0)
    in_queue = post_stats.get("approved", 0) + post_stats.get("moderation", 0)
    total_activity = published + comments_count + mentions_count

    if total_activity >= 50:
        activity = "🔥 Очень высокая"
    elif total_activity >= 20:
        activity = "✨ Высокая"
    elif total_activity >= 5:
        activity = "🌿 Обычная"
    else:
        activity = "🌱 Начинающая"

    display_name = cb.from_user.full_name or "Пользователь"
    username = f"@{cb.from_user.username}" if cb.from_user.username else "не установлен"

    text = (
        "👤 <b>Ваш профиль</b>\n"
        "<i>Личная статистика и активность</i>\n\n"
        "<blockquote>"
        f"<b>{escape(display_name)}</b>\n"
        f"📛 {escape(username)}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"📅 С нами с: <b>{escape(str(reg))}</b>"
        "</blockquote>\n\n"
        "📊 <b>Публикации</b>\n"
        "<blockquote>"
        f"📨 Сегодня  <b>{today}/{get_setting('USER_DAILY_POST_LIMIT')}</b>\n"
        f"🗓 За 7 дней  <b>{week}</b>\n"
        f"✅ Опубликовано  <b>{published}</b>\n"
        f"⏳ В очереди  <b>{in_queue}</b>\n"
        f"❌ Отклонено  <b>{rejected}</b>"
        "</blockquote>\n\n"
        "💬 <b>Активность</b>\n"
        "<blockquote>"
        f"💭 Комментариев  <b>{comments_count}</b>\n"
        f"🔔 Упоминаний  <b>{mentions_count}</b>\n"
        f"⚡ Уровень активности  <b>{activity}</b>"
        "</blockquote>\n\n"
        "🛡 <b>Надёжность</b>\n"
        "<blockquote>"
        f"Рейтинг доверия: <b>{trust:+.1f}</b>\n"
        "Чем аккуратнее публикации, тем выше доверие системы."
        "</blockquote>\n\n"
        "<i>Спасибо, что помогаете делать канал живее 💛</i>"
    )

    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=profile_keyboard())
    await cb.answer()

# ================== FAQ / ADS ==================
@router.callback_query(F.data == "faq")
async def faq(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)

    limit = get_setting('USER_DAILY_POST_LIMIT')
    await cb.message.edit_text(
        "❓ <b>Помощь</b>\n\n"
        "<blockquote>Здесь собрали самое важное. Если вопроса нет — напишите администрации.</blockquote>\n\n"
        f"<b>📝 Сколько постов можно отправить?</b>\nДо {limit} публикаций в сутки.\n\n"
        "<b>⏱ Как быстро публикуется пост?</b>\nПост проходит проверку. Обычный срок рассмотрения — до 24 часов.\n\n"
        "<b>🤖 Почему пост мог не пройти?</b>\nАвтоматическая проверка отсеивает запрещённый контент, спам, слишком короткие и повторяющиеся публикации. Остальные решения принимает модерация.\n\n"
        "<b>🗑 Можно удалить свою запись?</b>\nДа — воспользуйтесь кнопкой «Удалить запись».\n\n"
        "<b>🔎 Можно узнать автора?</b>\nДа, для этой функции используется платная расшифровка через Telegram Stars.\n\n"
        "<b>👥 Нужна помощь администрации?</b>\nОткройте раздел «Администрация» ниже.",
        parse_mode='HTML',
        reply_markup=faq_keyboard()
    )

@router.callback_query(F.data == "ads")
async def ads(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)

    kb = ads_keyboard()
    await cb.message.edit_text(
        "📢 <b>Реклама в канале</b>\n\n"
        "<blockquote>Разместите рекламную публикацию и выберите удобный срок. Актуальный прайс и оформление — по кнопкам ниже.</blockquote>\n\n"
        "<b>Размещение</b>\n"
        "• 24 часа — <b>199 ₽</b>\n"
        "• 48 часов — <b>289 ₽</b>\n"
        "• 72 часа — <b>379 ₽</b>\n"
        "• навсегда — <b>419 ₽</b>\n\n"
        "<b>📌 Закрепление</b>\n"
        "• 24 часа — +199 ₽\n"
        "• 48 часов — +299 ₽\n"
        "• 72 часа — +399 ₽\n\n"
        "<i>Другие рекламные возможности доступны в прайс-листе.</i>",
        parse_mode='HTML',
        reply_markup=kb
    )
