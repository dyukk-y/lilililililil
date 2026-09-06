import asyncio
from aiogram import F, Router
from aiogram.types import *
from aiogram.fsm.context import FSMContext


from app.loader import bot, logger
from app.config import (
    ADMINS, SUPER_ADMINS,
)
from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *

router = Router()

# ================== BROADCAST ==================
def message_to_html(text: str, entities: list = None) -> str:
    if not entities:
        return text
    
    sorted_entities = sorted(entities, key=lambda e: e.length, reverse=True)
    html_text = text
    offset_shift = 0
    
    for entity in sorted_entities:
        start = entity.offset
        end = entity.offset + entity.length
        
        start += offset_shift
        end += offset_shift
        
        original = html_text[start:end]
        
        if entity.type == "bold":
            replacement = f"{original}"
        elif entity.type == "italic":
            replacement = f"{original}"
        elif entity.type == "underline":
            replacement = f"<u>{original}</u>"
        elif entity.type == "strikethrough":
            replacement = f"<s>{original}</s>"
        elif entity.type == "code":
            replacement = f"<code>{original}</code>"
        elif entity.type == "pre":
            replacement = f"<pre>{original}</pre>"
        elif entity.type == "text_link":
            url = entity.url
            replacement = f'<a href="{url}">{original}</a>'
        elif entity.type == "text_mention":
            user = entity.user
            replacement = f'<a href="tg://user?id={user.id}">{original}</a>'
        elif entity.type == "spoiler":
            replacement = f"<span class='tg-spoiler'>{original}</span>"
        else:
            continue
        
        html_text = html_text[:start] + replacement + html_text[end:]
        offset_shift += len(replacement) - len(original)
    
    return html_text

@router.callback_query(F.data == "broadcast")
async def broadcast_menu_handler(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    users_count = await get_users_count()
    
    text = (
        f"📢 Рассылка сообщений\n\n"
        f"👥 Всего пользователей: {users_count}\n\n"
        f"Выберите тип рассылки:"
    )
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=broadcast_menu())

@router.callback_query(F.data == "broadcast_text")
async def broadcast_text_handler(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(BroadcastState.wait_broadcast_text)
    await cb.message.edit_text(
        "📝 Текстовая рассылка\n\n"
        "Отправьте сообщение для рассылки пользователям.\n\n"
        "✅ Бот автоматически определит:\n"
        "• 🔗 Гиперссылки\n"
        "• ⭐ Премиум эмодзи\n"
        "• Жирный текст\n"
        "• Курсив\n"
        "• <u>Подчеркнутый</u>\n"
        "• <s>Зачеркнутый</s>\n"
        "• <code>Моноширинный</code>\n\n"
        "📤 Отправьте сообщение в том виде, в котором оно должно быть отправлено пользователям:",
        parse_mode='HTML',
        reply_markup=broadcast_cancel_menu()
    )

@router.message(BroadcastState.wait_broadcast_text)
async def process_broadcast_text(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMINS:
        return
    
    if not msg.text and not msg.caption:
        return await msg.answer("❌ Сообщение не содержит текста.")
    
    text = msg.text or msg.caption
    entities = msg.entities or msg.caption_entities
    html_text = message_to_html(text, entities)
    
    await state.update_data(
        broadcast_text=text,
        broadcast_html=html_text,
        broadcast_entities=entities,
        broadcast_type="text"
    )
    
    await show_broadcast_preview(msg, state)

@router.callback_query(F.data == "broadcast_photo")
async def broadcast_photo_handler(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(BroadcastState.wait_broadcast_photo)
    await cb.message.edit_text(
        "📷 Рассылка с фото\n\n"
        "Отправьте фото для рассылки.\n\n"
        "✅ После загрузки фото:\n"
        "1. Бот примет фото\n"
        "2. Вы отправите текст с форматированием\n"
        "3. Бот автоматически определит все гиперссылки и премиум эмодзи\n\n"
        "📤 Отправьте фото:",
        parse_mode='HTML',
        reply_markup=broadcast_cancel_menu()
    )

@router.message(BroadcastState.wait_broadcast_photo)
async def process_broadcast_photo(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMINS:
        return
    
    if not msg.photo:
        return await msg.answer("❌ Пожалуйста, отправьте фото.")
    
    await state.update_data(
        broadcast_photo=msg.photo[-1].file_id,
        broadcast_type="photo"
    )
    
    await state.set_state(BroadcastState.wait_broadcast_text_with_photo)
    await msg.answer(
        "📝 Добавьте текст к фото\n\n"
        "Отправьте текст сообщения в том виде, в котором он должен быть:\n"
        "• С гиперссылками\n"
        "• С премиум эмодзи\n"
        "• С форматированием\n\n"
        "📤 Отправьте текст:",
        parse_mode='HTML',
        reply_markup=broadcast_cancel_menu()
    )

@router.message(BroadcastState.wait_broadcast_text_with_photo)
async def process_broadcast_text_with_photo(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMINS:
        return
    
    if not msg.text and not msg.caption:
        return await msg.answer("❌ Сообщение не содержит текста.")
    
    text = msg.text or msg.caption
    entities = msg.entities or msg.caption_entities
    html_text = message_to_html(text, entities)
    
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
        f"📢 Предпросмотр рассылки\n\n"
        f"👥 Будет отправлено {users_count} пользователям\n\n"
        f"📝 Сообщение будет выглядеть так:\n\n"
    )
    
    try:
        if broadcast_type == "photo" and broadcast_photo:
            await msg.answer_photo(
                photo=broadcast_photo,
                caption=preview_header + "\n" + broadcast_text,
                parse_mode='HTML'
            )
        else:
            await msg.answer(preview_header, parse_mode='HTML')
            await msg.answer(broadcast_text, entities=broadcast_entities)
    except Exception as e:
        logger.error(f"Ошибка при предпросмотре: {e}")
        try:
            await msg.answer(preview_header + "\n" + broadcast_html, parse_mode='HTML')
        except:
            await msg.answer(f"{preview_header}\n{broadcast_text}")
    
    await msg.answer(
        "👇 Подтвердите рассылку:",
        parse_mode='HTML',
        reply_markup=broadcast_confirm_menu()
    )
    
    await state.set_state(BroadcastState.wait_broadcast_confirm)

@router.callback_query(F.data == "broadcast_start")
async def start_broadcast(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
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
        f"📢 Рассылка началась!\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"✅ Отправлено: 0/{total_users}\n"
        f"❌ Ошибок: 0\n"
        f"⏳ Прогресс: 0%",
        parse_mode='HTML'
    )
    
    await cb.answer()
    
    success_count = 0
    error_count = 0
    
    for i, user_id in enumerate(users, 1):
        try:
            if broadcast_type == "photo" and broadcast_photo:
                await bot.send_photo(
                    chat_id=user_id,
                    photo=broadcast_photo,
                    caption=broadcast_text,
                    caption_entities=broadcast_entities
                )
            else:
                await bot.send_message(
                    chat_id=user_id,
                    text=broadcast_text,
                    entities=broadcast_entities
                )
            success_count += 1
        except Exception as e:
            error_count += 1
            logger.error(f"Ошибка отправки рассылки пользователю {user_id}: {e}")
            
            try:
                if broadcast_type == "photo" and broadcast_photo:
                    await bot.send_photo(
                        chat_id=user_id,
                        photo=broadcast_photo,
                        caption=broadcast_html,
                        parse_mode='HTML'
                    )
                else:
                    await bot.send_message(
                        chat_id=user_id,
                        text=broadcast_html,
                        parse_mode='HTML'
                    )
                success_count += 1
                error_count -= 1
            except:
                try:
                    import re
                    clean_text = re.sub(r'<[^>]+>', '', broadcast_html)
                    
                    if broadcast_type == "photo" and broadcast_photo:
                        await bot.send_photo(
                            chat_id=user_id,
                            photo=broadcast_photo,
                            caption=clean_text
                        )
                    else:
                        await bot.send_message(
                            chat_id=user_id,
                            text=clean_text
                        )
                    success_count += 1
                    error_count -= 1
                except:
                    pass
        
        if i % 10 == 0 or i == total_users:
            progress = int((i / total_users) * 100)
            try:
                await status_msg.edit_text(
                    f"📢 Рассылка в процессе...\n\n"
                    f"👥 Всего пользователей: {total_users}\n"
                    f"✅ Отправлено: {success_count}/{total_users}\n"
                    f"❌ Ошибок: {error_count}\n"
                    f"⏳ Прогресс: {progress}%",
                    parse_mode='HTML'
                )
            except:
                pass
        
        await asyncio.sleep(0.05)
    
    await status_msg.edit_text(
        f"📢 Рассылка завершена!\n\n"
        f"👥 Всего пользователей: {total_users}\n"
        f"✅ Успешно отправлено: {success_count}\n"
        f"❌ Ошибок: {error_count}\n"
        f"📊 Процент успеха: {int((success_count/total_users)*100)}%\n\n"
        f"📝 Текст рассылки:\n{broadcast_text[:100]}{'...' if len(broadcast_text) > 100 else ''}",
        parse_mode='HTML',
        reply_markup=admin_menu(cb.from_user.id in SUPER_ADMINS)
    )
    
    await log("broadcast", f"admin {cb.from_user.id}: {success_count}/{total_users} успешно")
    await state.clear()

@router.callback_query(F.data == "broadcast_cancel")
async def broadcast_cancel(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.clear()
    await broadcast_menu_handler(cb)

