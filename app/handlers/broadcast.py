import asyncio
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
from aiogram.types import *
from aiogram.fsm.context import FSMContext


from app.loader import bot, logger
from app.auth import is_admin as _is_admin
from app.config import (
    ADMINS, SUPER_ADMINS,
)
from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *
from app.utils import h, shorten

router = Router()

# ================== BROADCAST ==================
@router.callback_query(F.data == "broadcast")
async def broadcast_menu_handler(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    users_count = await get_users_count()
    
    text = (
        f"📢 <b>Рассылка сообщений</b>\n\n"
        f"👥 Всего пользователей: <b>{users_count}</b>\n\n"
        f"<i>Выберите тип рассылки:</i>"
    )
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=broadcast_menu())

@router.callback_query(F.data == "broadcast_text")
async def broadcast_text_handler(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(BroadcastState.wait_broadcast_text)
    await cb.message.edit_text(
        "📝 <b>Текстовая рассылка</b>\n\n"
        "Отправьте сообщение для рассылки пользователям.\n\n"
        "✅ <b>Бот автоматически определит:</b>\n"
        "• 🔗 Гиперссылки\n"
        "• ⭐ Премиум эмодзи\n"
        "• <b>Жирный текст</b>\n"
        "• <i>Курсив</i>\n"
        "• <u>Подчеркнутый</u>\n"
        "• <s>Зачеркнутый</s>\n"
        "• <code>Моноширинный</code>\n\n"
        "📤 <i>Отправьте сообщение в том виде, в котором оно должно быть отправлено пользователям:</i>",
        parse_mode='HTML',
        reply_markup=broadcast_cancel_menu()
    )

@router.message(BroadcastState.wait_broadcast_text)
async def process_broadcast_text(msg: Message, state: FSMContext):
    if not _is_admin(msg.from_user.id):
        return
    
    if not msg.text and not msg.caption:
        return await msg.answer("❌ Сообщение не содержит текста.")
    
    text = msg.text or msg.caption
    entities = msg.entities or msg.caption_entities
    # html_text из aiogram корректно учитывает UTF-16 смещения сущностей
    # (эмодзи) и экранирует текст; самописный message_to_html ломал разметку
    # при эмодзи перед форматированием и при символах "<"/"&".
    html_text = msg.html_text
    
    await state.update_data(
        broadcast_text=text,
        broadcast_html=html_text,
        broadcast_entities=entities,
        broadcast_type="text"
    )
    
    await show_broadcast_preview(msg, state)

@router.callback_query(F.data == "broadcast_photo")
async def broadcast_photo_handler(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(BroadcastState.wait_broadcast_photo)
    await cb.message.edit_text(
        "📷 <b>Рассылка с фото</b>\n\n"
        "Отправьте фото для рассылки.\n\n"
        "✅ <b>После загрузки фото:</b>\n"
        "1. Бот примет фото\n"
        "2. Вы отправите текст с форматированием\n"
        "3. Бот автоматически определит все гиперссылки и премиум эмодзи\n\n"
        "📤 <i>Отправьте фото:</i>",
        parse_mode='HTML',
        reply_markup=broadcast_cancel_menu()
    )

@router.message(BroadcastState.wait_broadcast_photo)
async def process_broadcast_photo(msg: Message, state: FSMContext):
    if not _is_admin(msg.from_user.id):
        return
    
    if not msg.photo:
        return await msg.answer("❌ Пожалуйста, отправьте фото.")
    
    await state.update_data(
        broadcast_photo=msg.photo[-1].file_id,
        broadcast_type="photo"
    )
    
    await state.set_state(BroadcastState.wait_broadcast_text_with_photo)
    await msg.answer(
        "📝 <b>Добавьте текст к фото</b>\n\n"
        "Отправьте текст сообщения в том виде, в котором он должен быть:\n"
        "• С гиперссылками\n"
        "• С премиум эмодзи\n"
        "• С форматированием\n\n"
        "📤 <i>Отправьте текст:</i>",
        parse_mode='HTML',
        reply_markup=broadcast_cancel_menu()
    )

@router.message(BroadcastState.wait_broadcast_text_with_photo)
async def process_broadcast_text_with_photo(msg: Message, state: FSMContext):
    if not _is_admin(msg.from_user.id):
        return
    
    if not msg.text and not msg.caption:
        return await msg.answer("❌ Сообщение не содержит текста.")
    
    text = msg.text or msg.caption
    entities = msg.entities or msg.caption_entities
    # html_text из aiogram корректно учитывает UTF-16 смещения сущностей
    # (эмодзи) и экранирует текст; самописный message_to_html ломал разметку
    # при эмодзи перед форматированием и при символах "<"/"&".
    html_text = msg.html_text
    
    await state.update_data(
        broadcast_text=text,
        broadcast_html=html_text,
        broadcast_entities=entities
    )
    
    await show_broadcast_preview(msg, state)

async def show_broadcast_preview(msg: Message, state: FSMContext):
    data = await state.get_data()
    broadcast_type = data.get("broadcast_type")
    broadcast_text = data.get("broadcast_text")
    broadcast_html = data.get("broadcast_html")
    broadcast_entities = data.get("broadcast_entities")
    broadcast_photo = data.get("broadcast_photo")
    
    users_count = await get_users_count()
    
    preview_header = (
        f"📢 <b>Предпросмотр рассылки</b>\n\n"
        f"👥 Будет отправлено <b>{users_count}</b> пользователям\n\n"
        f"📝 <b>Сообщение будет выглядеть так:</b>\n\n"
    )
    
    try:
        if broadcast_type == "photo" and broadcast_photo:
            await msg.answer_photo(
                photo=broadcast_photo,
                caption=preview_header + "\n" + broadcast_html,
                parse_mode='HTML'
            )
        else:
            await msg.answer(preview_header, parse_mode='HTML')
            await msg.answer(broadcast_text, entities=broadcast_entities)
    except Exception as e:
        logger.error(f"Ошибка при предпросмотре: {e}")
        try:
            await msg.answer(preview_header + "\n" + broadcast_html, parse_mode='HTML')
        except Exception:
            await msg.answer(f"{preview_header}\n{h(broadcast_text)}", parse_mode='HTML')
    
    await msg.answer(
        "👇 <b>Подтвердите рассылку:</b>",
        parse_mode='HTML',
        reply_markup=broadcast_confirm_menu()
    )
    
    await state.set_state(BroadcastState.wait_broadcast_confirm)

@router.callback_query(F.data == "broadcast_start")
async def start_broadcast(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    data = await state.get_data()
    broadcast_type = data.get("broadcast_type")
    broadcast_text = data.get("broadcast_text")
    broadcast_html = data.get("broadcast_html")
    broadcast_entities = data.get("broadcast_entities")
    broadcast_photo = data.get("broadcast_photo")
    
    if not broadcast_text:
        await state.clear()
        return await cb.message.edit_text("❌ Ошибка: текст рассылки не найден.")
    
    users = await get_all_users()
    total_users = len(users)
    
    if total_users == 0:
        await state.clear()
        return await cb.message.edit_text("❌ Нет пользователей для рассылки.")
    
    await cb.message.delete()
    
    status_msg = await cb.message.answer(
        f"📢 <b>Рассылка началась!</b>\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"✅ Отправлено: 0/{total_users}\n"
        f"❌ Ошибок: 0\n"
        f"⏳ Прогресс: 0%",
        parse_mode='HTML'
    )
    
    await cb.answer()
    
    success_count = 0
    error_count = 0
    blocked_count = 0

    async def _send_once(user_id: int, mode: str) -> None:
        """mode: entities — оригинальная разметка Telegram (премиум-эмодзи,
        ссылки), html — запасной вариант, plain — без разметки."""
        if mode == "entities":
            text_kwargs = {"entities": broadcast_entities}
            caption_kwargs = {"caption_entities": broadcast_entities}
            body = broadcast_text
        elif mode == "html":
            text_kwargs = caption_kwargs = {"parse_mode": "HTML"}
            body = broadcast_html
        else:
            text_kwargs = caption_kwargs = {"parse_mode": None}
            body = re.sub(r"<[^>]+>", "", broadcast_html or broadcast_text)
        if broadcast_type == "photo" and broadcast_photo:
            await bot.send_photo(chat_id=user_id, photo=broadcast_photo, caption=body, **caption_kwargs)
        else:
            await bot.send_message(chat_id=user_id, text=body, **text_kwargs)

    async def _deliver(user_id: int) -> str:
        """Возвращает 'ok' | 'blocked' | 'error'."""
        for mode in ("entities", "html", "plain"):
            for _attempt in range(3):
                try:
                    await _send_once(user_id, mode)
                    return "ok"
                except TelegramRetryAfter as e:
                    # Флуд-лимит Telegram: ждём и повторяем ТОТ ЖЕ вариант.
                    # Раньше бот сразу слал ещё два «запасных» варианта и только
                    # усиливал блокировку, теряя сообщения.
                    await asyncio.sleep(e.retry_after + 0.5)
                except (TelegramForbiddenError, TelegramNotFound):
                    # Пользователь заблокировал бота/удалил аккаунт — повторять
                    # бессмысленно.
                    return "blocked"
                except TelegramBadRequest as e:
                    if "chat not found" in str(e).lower() or "user is deactivated" in str(e).lower():
                        return "blocked"
                    logger.warning("Рассылка: %s не принял вариант %s: %s", user_id, mode, e)
                    break  # пробуем следующий вариант разметки
                except Exception as e:
                    logger.warning("Рассылка: ошибка отправки пользователю %s: %s", user_id, e)
                    await asyncio.sleep(1)
        return "error"

    for i, user_id in enumerate(users, 1):
        result = await _deliver(user_id)
        if result == "ok":
            success_count += 1
        elif result == "blocked":
            blocked_count += 1
        else:
            error_count += 1

        if i % 25 == 0 or i == total_users:
            progress = int((i / total_users) * 100)
            try:
                await status_msg.edit_text(
                    f"📢 <b>Рассылка в процессе...</b>\n\n"
                    f"👥 Всего пользователей: {total_users}\n"
                    f"✅ Отправлено: {success_count}/{total_users}\n"
                    f"🚫 Заблокировали бота: {blocked_count}\n"
                    f"❌ Ошибок: {error_count}\n"
                    f"⏳ Прогресс: {progress}%",
                    parse_mode='HTML'
                )
            except Exception:
                pass
        
        await asyncio.sleep(0.05)
    
    preview = h(shorten(broadcast_text, 100))
    try:
        await status_msg.edit_text(
            f"📢 <b>Рассылка завершена!</b>\n\n"
            f"👥 Всего пользователей: {total_users}\n"
            f"✅ Успешно отправлено: {success_count}\n"
            f"🚫 Заблокировали бота: {blocked_count}\n"
            f"❌ Ошибок: {error_count}\n"
            f"📊 Процент успеха: {int((success_count/total_users)*100)}%\n\n"
            f"📝 Текст рассылки:\n{preview}",
            parse_mode='HTML',
            reply_markup=admin_menu(cb.from_user.id in SUPER_ADMINS)
        )
    except Exception:
        logger.exception("Не удалось обновить итог рассылки")
    
    await log("broadcast", f"admin {cb.from_user.id}: {success_count}/{total_users} успешно, заблокировали: {blocked_count}")
    await state.clear()

@router.callback_query(F.data == "broadcast_cancel")
async def broadcast_cancel(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.clear()
    await broadcast_menu_handler(cb)

