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
        "📜 Правила Смотра\n\n"
        "Перед отправкой поста быстро проверьте эти пункты — так публикация пройдёт без лишних задержек.\n\n"
        "🚫 Нельзя\n"
        "• оскорбления и запрещённый контент\n"
        "• интимные материалы\n"
        "• контент с людьми младше 14 лет\n"
        "• упоминания возраста младше 14 лет\n"
        "• вредоносные вещества\n"
        "• публикации о питбайкерах\n"
        "• мат, спам и повторяющийся текст\n\n"
        "⚠️ Администрация оставляет за собой право удалить публикацию или отказать в размещении.\n\n"
        "Перед использованием бота также ознакомьтесь с юридическим уведомлением.",
        parse_mode='HTML',
        reply_markup=rules_keyboard()
    )

# ================== MENU ==================
@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text(
        "🏠 Главное меню\n\n"
        "✨ Всё необходимое — в одном месте.\nПредложить пост • профиль • помощь • реклама\n"
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
        "👤 Ваш профиль\n"
        "Личная статистика и активность\n\n"
        ""
        f"{escape(display_name)}\n"
        f"📛 {escape(username)}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"📅 С нами с: {escape(str(reg))}"
        "\n\n"
        "📊 Публикации\n"
        ""
        f"📨 Сегодня  {today}/{get_setting('USER_DAILY_POST_LIMIT')}\n"
        f"🗓 За 7 дней  {week}\n"
        f"✅ Опубликовано  {published}\n"
        f"⏳ В очереди  {in_queue}\n"
        f"❌ Отклонено  {rejected}"
        "\n\n"
        "💬 Активность\n"
        ""
        f"💭 Комментариев  {comments_count}\n"
        f"🔔 Упоминаний  {mentions_count}\n"
        f"⚡ Уровень активности  {activity}"
        "\n\n"
        "🛡 Надёжность\n"
        ""
        f"Рейтинг доверия: {trust:+.1f}\n"
        "Чем аккуратнее публикации, тем выше доверие системы."
        "\n\n"
        "Спасибо, что помогаете делать канал живее 💛"
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
        "❓ Помощь\n\n"
        "Здесь собрали самое важное. Если вопроса нет — напишите администрации.\n\n"
        f"📝 Сколько постов можно отправить?\nДо {limit} публикаций в сутки.\n\n"
        "⏱ Как быстро публикуется пост?\nПост проходит проверку. Обычный срок рассмотрения — до 24 часов.\n\n"
        "🤖 Почему пост мог не пройти?\nАвтоматическая проверка отсеивает запрещённый контент, спам, слишком короткие и повторяющиеся публикации. Остальные решения принимает модерация.\n\n"
        "🗑 Можно удалить свою запись?\nДа — воспользуйтесь кнопкой «Удалить запись».\n\n"
        "🔎 Можно узнать автора?\nДа, для этой функции используется платная расшифровка через Telegram Stars.\n\n"
        "👥 Нужна помощь администрации?\nОткройте раздел «Администрация» ниже.",
        parse_mode='HTML',
        reply_markup=faq_keyboard()
    )

@router.callback_query(F.data == "ads")
async def ads(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)

    kb = ads_keyboard()
    await cb.message.edit_text(
        "📢 Реклама в канале\n\n"
        "Разместите рекламную публикацию и выберите удобный срок. Актуальный прайс и оформление — по кнопкам ниже.\n\n"
        "Размещение\n"
        "• 24 часа — 199 ₽\n"
        "• 48 часов — 289 ₽\n"
        "• 72 часа — 379 ₽\n"
        "• навсегда — 419 ₽\n\n"
        "📌 Закрепление\n"
        "• 24 часа — +199 ₽\n"
        "• 48 часов — +299 ₽\n"
        "• 72 часа — +399 ₽\n\n"
        "Другие рекламные возможности доступны в прайс-листе.",
        parse_mode='HTML',
        reply_markup=kb
    )
