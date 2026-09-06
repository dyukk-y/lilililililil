import asyncio
from aiogram import F, Router
from aiogram.types import *
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from datetime import datetime

import aiosqlite

from app.loader import bot, logger
from app.config import DB_NAME
from app.runtime_settings import get as get_setting
from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *

router = Router()

# ================== КТО ОПУБЛИКОВАЛ/ОТКЛОНИЛ ==================
@router.callback_query(F.data.startswith("who_pub_"))
async def who_published(cb: CallbackQuery):
    try:
        post_id = int(cb.data.split("_")[2])
    except (ValueError, IndexError):
        return await cb.answer("❌ Неверный ID поста", show_alert=True)
    
    moderator_id, mod_username, reject_reason = await get_post_moderator_info(post_id)
    
    if moderator_id is None:
        return await cb.answer("❌ Информация о модераторе не найдена", show_alert=True)

    if moderator_id == 0:
        text = "🤖 Опубликовано автоматически ботом (по расписанию)."
    elif mod_username:
        text = f"👤 Опубликовал: @{mod_username}\nID: {moderator_id}"
    else:
        text = f"👤 Опубликовал: ID {moderator_id} (юзернейм неизвестен)"

    # Всплывающее окно Telegram не поддерживает HTML и ограничено ~200
    # символами — поэтому текст здесь простой и короткий, без разметки.
    await cb.answer(text, show_alert=True)

@router.callback_query(F.data.startswith("who_rej_"))
async def who_rejected(cb: CallbackQuery):
    try:
        post_id = int(cb.data.split("_")[2])
    except (ValueError, IndexError):
        return await cb.answer("❌ Неверный ID поста", show_alert=True)
    
    moderator_id, mod_username, reject_reason = await get_post_moderator_info(post_id)
    
    if moderator_id is None or not reject_reason:
        return await cb.answer("❌ Информация об отклонении не найдена", show_alert=True)

    if moderator_id == 0:
        base = "🤖 Отклонено автоматически ботом."
    elif mod_username:
        base = f"👤 Отклонил: @{mod_username}"
    else:
        base = f"👤 Отклонил: ID {moderator_id}"

    text = f"{base}\nПричина: {reject_reason}"
    # Всплывающее окно Telegram ограничено ~200 символами и не поддерживает
    # HTML — обрезаем длинные причины, чтобы гарантированно уместиться.
    if len(text) > 200:
        text = text[:197] + "..."

    await cb.answer(text, show_alert=True)


@router.callback_query(F.data.startswith("why_ai_"))
async def why_ai(cb: CallbackQuery):
    if not await validate_chat_for_moderation(cb):
        return await cb.answer("⚠️ Это действие доступно только в теме модерации", show_alert=True)
    try: post_id=int(cb.data.split("_")[-1])
    except ValueError: return await cb.answer("Неверный ID", show_alert=True)
    async with aiosqlite.connect(DB_NAME) as db:
        cur=await db.execute("SELECT ai_score, ai_confidence, ai_decision, ai_reason, trust_score FROM posts p LEFT JOIN users u ON p.user_id=u.user_id WHERE p.id=?",(post_id,))
        row=await cur.fetchone()
    if not row or row[0] is None:
        return await cb.answer("ИИ-анализ для этого поста ещё не сохранён.", show_alert=True)
    score, conf, dec, reason, trust=row
    text=f"🤖 AI: {score}/100\nУверенность: {float(conf or 0):.0%}\nTrust: {float(trust or 0):.1f}\nРешение: {'авто' if dec=='auto' else 'модерация'}\n{reason or ''}"
    if len(text)>200: text=text[:197]+"..."
    await cb.answer(text, show_alert=True)

@router.callback_query(F.data.startswith("ai_error_"))
async def ai_error(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 Нет доступа", show_alert=True)
    try:
        pid=int(cb.data.split("_")[-1])
    except ValueError:
        return await cb.answer("Неверный ID", show_alert=True)
    ok=await mark_ai_error(pid, cb.from_user.id)
    await cb.answer("✅ Ошибка ИИ записана" if ok else "ℹ️ Уже записано", show_alert=True)

# ================== ПУБЛИКАЦИЯ (одним кликом, без лишнего второго подтверждения) ==================
@router.callback_query(F.data.startswith("pub_"))
async def confirm_pub(cb: CallbackQuery):
    # Действие разрешено только из чата модераторов — это единственное
    # место, где показываются кнопки решения. Чат администраторов теперь
    # получает только готовый итог (см. services.notify_admins_outcome),
    # без кнопок вообще.
    if not await validate_chat_for_moderation(cb):
        return await cb.answer("⚠️ Это действие доступно только в теме модерации", show_alert=True)
    
    try:
        pid = int(cb.data.split("_")[1])
    except ValueError:
        return await cb.answer("Неверный ID поста", show_alert=True)

    # Используем общую services.publish_post — она же используется и для
    # автопубликации по расписанию, и для форс-паблиша из админ-панели.
    ok, reason = await publish_post(pid, moderator_id=cb.from_user.id)

    if not ok:
        messages = {
            "already_processed": "❌ Этот пост уже обработан (опубликован/отклонён кем-то ещё)!",
            "not_found": "❌ Пост не найден.",
        }
        return await cb.answer(messages.get(reason, f"❌ Ошибка публикации: {reason}"), show_alert=True)

    await cb.answer("✅ Пост опубликован!", show_alert=True)

    # Отключаем кнопки на самой карточке, чтобы по ней нельзя было кликнуть повторно.
    try:
        await bot.edit_message_reply_markup(
            chat_id=cb.message.chat.id,
            message_id=cb.message.message_id,
            reply_markup=disabled_moderation_keyboard(pid, "published"),
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logger.warning(f"Не удалось отключить кнопки карточки поста #{pid}: {e}")
    except Exception as e:
        logger.warning(f"Не удалось отключить кнопки карточки поста #{pid}: {e}")

# ================== ОТКЛОНЕНИЕ ==================
async def reset_reject_state(post_id: int, message_id: int, chat_id: int, text: str, photo: str = None):
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
        logger.info(f"✅ Состояние отказа для поста #{post_id} сброшено")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logger.error(f"Ошибка при сбросе состояния отказа: {e}")
    except Exception as e:
        logger.error(f"Ошибка при сбросе состояния отказа: {e}")

async def reject_timeout_handler(state: FSMContext, post_id: int, message_id: int, 
                                chat_id: int, original_text: str, photo: str = None):
    await asyncio.sleep(60)
    
    data = await state.get_data()
    current_post_id = data.get("post_id")
    
    if current_post_id == post_id:
        current_state = await state.get_state()
        if current_state == RejectState.wait_reason.state:
            await state.clear()
            await reset_reject_state(post_id, message_id, chat_id, original_text, photo)

@router.callback_query(F.data.startswith("rej_"))
async def reject(cb: CallbackQuery, state: FSMContext):
    if not await validate_chat_for_moderation(cb):
        return await cb.answer("⚠️ Это действие доступно только в теме модерации", show_alert=True)
    
    try:
        pid = int(cb.data.split("_")[1])
    except ValueError:
        return await cb.answer("Неверный ID поста", show_alert=True)
    
    current_status = await get_post_status(pid)
    if current_status == "published":
        await cb.answer("❌ Этот пост уже опубликован!", show_alert=True)
        return
    elif current_status == "rejected":
        await cb.answer("❌ Этот пост уже отклонен!", show_alert=True)
        return
    
    message_id = cb.message.message_id
    chat_id = cb.message.chat.id
    topic_id = cb.message.message_thread_id
    
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT text, photo FROM posts WHERE id=?",
            (pid,)
        )
        row = await cur.fetchone()
        if not row:
            return await cb.answer("Пост не найден", show_alert=True)
        
        post_text, photo = row
        original_text = cb.message.caption if photo else cb.message.text
        if not original_text:
            original_text = f"Пост #{pid}\n\n{post_text}"

    await state.set_state(RejectState.wait_reason)
    await state.update_data(
        post_id=pid,
        message_id=message_id,
        chat_id=chat_id,
        topic_id=topic_id,
        original_text=original_text,
        photo=photo,
        timestamp=datetime.now()
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_rej_{pid}")]
    ])

    # Отвечаем в ТОТ ЖЕ чат/тему, откуда пришёл клик (модераторы или
    # администраторы) — раньше это было жёстко зашито на чат модераторов,
    # из-за чего для постов, доступных только админам, ответ "опишите
    # причину" улетал бы не туда.
    await bot.send_message(
        chat_id=chat_id,
        message_thread_id=topic_id,
        text="Опишите причину отказа (у вас 1 минута):",
        reply_markup=kb
    )
    await cb.answer()
    
    asyncio.create_task(reject_timeout_handler(state, pid, message_id, chat_id, original_text, photo))

@router.callback_query(F.data.startswith("cancel_rej_"))
async def cancel_rej(cb: CallbackQuery, state: FSMContext):
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
    await reset_reject_state(pid, message_id, chat_id, original_text, photo)
    await cb.answer("❌ Отмена отклонения")

@router.message(RejectState.wait_reason)
async def reject_reason(msg: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("post_id")
    if not pid:
        return await msg.answer("Ошибка: не найден ID поста.")
    
    message_id = data.get("message_id")
    chat_id = data.get("chat_id")
    topic_id = data.get("topic_id")
    original_text = data.get("original_text")
    photo = data.get("photo")
    timestamp = data.get("timestamp")
    
    if timestamp and (datetime.now() - timestamp).total_seconds() > 70:
        await state.clear()
        await reset_reject_state(pid, message_id, chat_id, original_text, photo)
        return await bot.send_message(
            chat_id=chat_id,
            message_thread_id=topic_id,
            text="⚠️ Время на указание причины истекло. Действие отменено."
        )
    
    await state.clear()

    # Общая services.reject_post: атомарный переход статуса, обновление
    # карточки у администраторов и уведомление автора — всё в одном месте,
    # без дублирования логики (как было раньше).
    ok = await reject_post(pid, moderator_id=msg.from_user.id, reason=msg.text)
    if not ok:
        return await msg.answer("⚠️ Этот пост уже был обработан другим модератором.")

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
        if "message is not modified" not in str(e):
            logger.error(f"Ошибка при обновлении сообщения: {e}")
    except Exception as e:
        logger.error(f"Ошибка при обновлении сообщения: {e}")

    await bot.send_message(
        chat_id=chat_id,
        message_thread_id=topic_id,
        text="✅ Причина отправлена пользователю."
    )

@router.callback_query(F.data == "disabled")
async def disabled_button_handler(cb: CallbackQuery):
    await cb.answer("❌ Это действие недоступно - пост уже был обработан модератором", show_alert=True)
