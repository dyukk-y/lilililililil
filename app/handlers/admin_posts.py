from aiogram import F, Router
from aiogram.types import *
from aiogram.fsm.context import FSMContext
from datetime import datetime

from app.loader import logger
from app.config import (
    ADMINS, SUPER_ADMINS,
)
from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *

router = Router()

_STATUS_LABELS = {
    "published": "опубликован",
    "rejected": "отклонён",
    "moderation": "обрабатывается",
    "approved": "в очереди на публикацию",
    "deleted": "удалён по заявке",
}


# ================== ОЧЕРЕДЬ ПУБЛИКАЦИЙ ==================
@router.callback_query(F.data == "pending_posts")
async def show_pending_posts(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await show_pending_posts_page(cb, page=1)

async def show_pending_posts_page(cb: CallbackQuery, page: int):
    posts, total = await get_pending_posts(page=page, per_page=5)
    total_pages = (total + 4) // 5 or 1
    
    if not posts:
        text = "📭 <b>Очередь публикаций пуста</b>\n\nВсе посты либо уже опубликованы, либо ещё не отправлены."
        await cb.message.edit_text(text, parse_mode='HTML', reply_markup=admin_menu(cb.from_user.id in SUPER_ADMINS))
        return
    
    text_lines = [f"📅 <b>Очередь публикаций (стр. {page}/{total_pages}):</b>\n"]
    
    start_idx = (page - 1) * 5 + 1
    for post_id, user_id, post_text, time, photo, status, scheduled_time in posts:
        preview = post_text[:50] + "..." if len(post_text) > 50 else post_text

        if scheduled_time:
            try:
                sched_str = datetime.fromisoformat(scheduled_time).strftime('%d.%m.%Y %H:%M')
            except ValueError:
                sched_str = scheduled_time
            time_line = f"   🕐 Публикация: {sched_str} (Новосибирск)"
        else:
            time_line = f"   🕐 Ждёт решения модератора (до {get_setting('MODERATION_TIMEOUT_HOURS')} ч.)"

        text_lines.append(f"<b>{start_idx}. 📌 Пост #{post_id}</b>")
        text_lines.append(f"   👤 Автор: <code>{user_id}</code>")
        text_lines.append(time_line)
        text_lines.append(f"   📄 {preview}")
        text_lines.append(f"   {'📷 С фото' if photo else '📝 Без фото'}")
        text_lines.append("")
        start_idx += 1
    
    text = "\n".join(text_lines)
    
    await cb.message.edit_text(
        text, 
        parse_mode='HTML', 
        reply_markup=pending_posts_keyboard(page, total_pages)
    )

@router.callback_query(F.data.startswith("pending_page_"))
async def pending_page_handler(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    try:
        page = int(cb.data.split("_")[2])
        await show_pending_posts_page(cb, page)
    except (ValueError, IndexError):
        await cb.answer("❌ Ошибка при загрузке страницы", show_alert=True)

# ================== АДМИНСКАЯ ПУБЛИКАЦИЯ ПОСТА (форс-паблиш вне очереди) ==================
@router.callback_query(F.data == "admin_publish_post")
async def admin_publish_post(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(AdminPostState.wait_post_id_for_publish)
    await cb.message.edit_text(
        "⏩ <b>Публикация поста прямо сейчас</b>\n\n"
        "Введите номер поста, который хотите опубликовать немедленно "
        "(минуя очередь и запланированное время):",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="pending_posts")]
        ])
    )

@router.message(AdminPostState.wait_post_id_for_publish)
async def process_admin_publish_post_id(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMINS:
        return
    
    try:
        post_id = int(msg.text.strip())
    except ValueError:
        return await msg.answer("❌ Пожалуйста, введите число.")
    
    post = await get_post_by_id(post_id)
    
    if not post:
        return await msg.answer(
            f"❌ Пост #{post_id} не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К постам", callback_data="pending_posts")]
            ])
        )
    
    if post[5] not in ("moderation", "approved"):
        status_text = _STATUS_LABELS.get(post[5], post[5])
        return await msg.answer(
            f"❌ Пост #{post_id} уже {status_text}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К постам", callback_data="pending_posts")]
            ])
        )
    
    await state.update_data(post_id=post_id, post_text=post[2], post_photo=post[3])
    
    preview_text = (
        f"📨 <b>Пост #{post_id}</b>\n\n"
        f"{post[2]}\n\n"
        f"Опубликовать этот пост прямо сейчас?"
    )
    
    if post[3]:
        await msg.answer_photo(
            photo=post[3],
            caption=preview_text,
            parse_mode='HTML',
            reply_markup=admin_post_confirm_keyboard(post_id, "publish")
        )
    else:
        await msg.answer(
            preview_text,
            parse_mode='HTML',
            reply_markup=admin_post_confirm_keyboard(post_id, "publish")
        )

@router.callback_query(F.data.startswith("admin_publish_confirm_"))
async def admin_publish_confirm(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    try:
        post_id = int(cb.data.split("_")[3])
    except (ValueError, IndexError):
        return await cb.answer("❌ Неверный ID поста", show_alert=True)

    success, reason = await publish_post(post_id, moderator_id=cb.from_user.id)

    await state.clear()

    if not success:
        text = "❌ Пост уже был обработан ранее." if reason == "already_processed" else f"❌ Ошибка публикации: {reason}"
        return await cb.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К постам", callback_data="pending_posts")]
            ])
        )

    await log("admin_publish", f"admin {cb.from_user.id} published post #{post_id}")

    await cb.message.edit_text(
        f"✅ Пост #{post_id} успешно опубликован!",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 К очереди", callback_data="pending_posts")],
            [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_panel")]
        ])
    )

@router.callback_query(F.data == "admin_publish_cancel")
async def admin_publish_cancel(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.clear()
    await show_pending_posts_page(cb, page=1)

# ================== АДМИНСКОЕ ОТКЛОНЕНИЕ ПОСТА ==================
@router.callback_query(F.data == "admin_reject_post")
async def admin_reject_post(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(AdminPostState.wait_post_id_for_reject)
    await cb.message.edit_text(
        "📝 <b>Отклонение поста</b>\n\n"
        "Введите номер поста, который хотите отклонить:",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="pending_posts")]
        ])
    )

@router.message(AdminPostState.wait_post_id_for_reject)
async def process_admin_reject_post_id(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMINS:
        return
    
    try:
        post_id = int(msg.text.strip())
    except ValueError:
        return await msg.answer("❌ Пожалуйста, введите число.")
    
    post = await get_post_by_id(post_id)
    
    if not post:
        return await msg.answer(
            f"❌ Пост #{post_id} не найден.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К постам", callback_data="pending_posts")]
            ])
        )
    
    if post[5] not in ("moderation", "approved"):
        status_text = _STATUS_LABELS.get(post[5], post[5])
        return await msg.answer(
            f"❌ Пост #{post_id} уже {status_text}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К постам", callback_data="pending_posts")]
            ])
        )
    
    await state.update_data(post_id=post_id, post_text=post[2], post_photo=post[3], user_id=post[1])
    
    preview_text = (
        f"📨 <b>Пост #{post_id}</b>\n\n"
        f"{post[2]}\n\n"
        f"Отклонить этот пост?"
    )
    
    if post[3]:
        await msg.answer_photo(
            photo=post[3],
            caption=preview_text,
            parse_mode='HTML',
            reply_markup=admin_post_confirm_keyboard(post_id, "reject")
        )
    else:
        await msg.answer(
            preview_text,
            parse_mode='HTML',
            reply_markup=admin_post_confirm_keyboard(post_id, "reject")
        )

@router.callback_query(F.data.startswith("admin_reject_confirm_"))
async def admin_reject_confirm(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    try:
        post_id = int(cb.data.split("_")[3])
    except (ValueError, IndexError):
        return await cb.answer("❌ Неверный ID поста", show_alert=True)
    
    post = await get_post_by_id(post_id)
    
    if not post or post[5] not in ("moderation", "approved"):
        await state.clear()
        return await cb.message.edit_text(
            "❌ Пост уже был обработан ранее.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К постам", callback_data="pending_posts")]
            ])
        )
    
    await state.set_state(AdminPostState.wait_reject_reason)
    await state.update_data(post_id=post_id)
    
    await cb.message.edit_text(
        f"📝 <b>Причина отклонения поста #{post_id}</b>\n\n"
        "Напишите причину, которая будет отправлена автору:",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_reject_cancel")]
        ])
    )

@router.message(AdminPostState.wait_reject_reason)
async def process_reject_reason(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMINS:
        return
    
    data = await state.get_data()
    post_id = data.get("post_id")
    
    if not post_id:
        return await msg.answer("❌ Ошибка: не найден ID поста.")
    
    post = await get_post_by_id(post_id)
    
    if not post or post[5] not in ("moderation", "approved"):
        await state.clear()
        return await msg.answer(
            "❌ Пост уже был обработан ранее.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К постам", callback_data="pending_posts")]
            ])
        )
    
    reason = msg.text.strip()
    await state.update_data(reject_reason=reason)
    
    preview_text = (
        f"📨 <b>Пост #{post_id}</b>\n\n"
        f"{post[2]}\n\n"
        f"📝 <b>Причина отклонения:</b>\n{reason}\n\n"
        f"Отправить это пользователю?"
    )
    
    if post[3]:
        await msg.answer_photo(
            photo=post[3],
            caption=preview_text,
            parse_mode='HTML',
            reply_markup=admin_reject_reason_confirm_keyboard(post_id)
        )
    else:
        await msg.answer(
            preview_text,
            parse_mode='HTML',
            reply_markup=admin_reject_reason_confirm_keyboard(post_id)
        )
    
    await state.set_state(AdminPostState.wait_reject_confirm)

@router.callback_query(F.data.startswith("admin_reject_send_"))
async def admin_reject_send(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    try:
        post_id = int(cb.data.split("_")[3])
    except (ValueError, IndexError):
        return await cb.answer("❌ Неверный ID поста", show_alert=True)
    
    data = await state.get_data()
    reason = data.get("reject_reason") or "Не указана"

    success = await reject_post(post_id, moderator_id=cb.from_user.id, reason=reason)

    await state.clear()

    if not success:
        return await cb.message.edit_text(
            "❌ Пост уже был обработан ранее.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ К постам", callback_data="pending_posts")]
            ])
        )

    await log("admin_reject", f"admin {cb.from_user.id} rejected post #{post_id}: {reason}")
    
    await cb.message.edit_text(
        f"✅ Пост #{post_id} отклонен. Причина отправлена пользователю.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 К очереди", callback_data="pending_posts")],
            [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_panel")]
        ])
    )

@router.callback_query(F.data == "admin_reject_cancel")
async def admin_reject_cancel(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.clear()
    await show_pending_posts_page(cb, page=1)
