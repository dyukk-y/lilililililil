from aiogram import F, Router
from aiogram.types import *
from aiogram.fsm.context import FSMContext


from app.loader import bot, logger
from app.config import (
    ADMINS, REQUIRED_SUBSCRIPTIONS,
)
from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *

router = Router()

# ================== УПРАВЛЕНИЕ ПОДПИСКАМИ ==================
@router.callback_query(F.data == "manage_subscriptions")
async def manage_subscriptions(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await cb.message.edit_text(
        "📢 <b>Управление обязательными подписками</b>\n\n"
        f"📊 <b>Текущее количество подписок:</b> {len(REQUIRED_SUBSCRIPTIONS)}\n\n"
        "<i>Выберите действие:</i>",
        parse_mode='HTML',
        reply_markup=subscriptions_menu()
    )

@router.callback_query(F.data == "list_subscriptions")
async def list_subscriptions(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    if not REQUIRED_SUBSCRIPTIONS:
        text = "📋 <b>Список обязательных подписок пуст</b>"
    else:
        text_lines = ["📋 <b>Обязательные подписки:</b>\n\n"]
        
        for i, sub in enumerate(REQUIRED_SUBSCRIPTIONS, 1):
            if sub["type"] == "channel":
                emoji = "📢"
            elif sub["type"] == "group":
                emoji = "👥"
            else:
                emoji = "🤖"
                
            text_lines.append(f"{i}. {emoji} <b>{sub['name']}</b>")
            text_lines.append(f"   Тип: {sub['type']}")
            text_lines.append(f"   ID/Username: <code>{sub['id']}</code>")
            text_lines.append(f"   Юзернейм: {sub['username']}")
            text_lines.append(f"   Ссылка: {sub['url']}\n")
        
        text = "\n".join(text_lines)
        
        if len(text) > 4000:
            text = text[:4000] + "\n\n... (список слишком длинный)"
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=subscriptions_menu())

@router.callback_query(F.data == "add_channel_subscription")
async def add_channel_subscription(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(SubscriptionState.wait_subscription_add)
    await state.update_data(sub_type="channel")
    
    await cb.message.edit_text(
        "➕ <b>Добавление обязательного канала</b>\n\n"
        "Отправьте данные канала в формате:\n"
        "<code>ID_канала @юзернейм_или_ссылка Название_канала</code>\n\n"
        "<i>Примеры:</i>\n"
        "<code>-1001234567890 @example_channel Основной канал</code>\n"
        "<code>-1001234567890 https://t.me/+AbCdEfGhIjK Закрытый канал</code>\n\n"
        "<i>Примечания:</i>\n"
        "1. ID канала должен быть числом (начинаться с -100)\n"
        "2. Для открытого канала — юзернейм с @; для закрытого (без "
        "публичного юзернейма) — пригласительная ссылка (https://t.me/+...)\n"
        "3. Бот должен быть участником/администратором канала, чтобы "
        "проверять подписку — для закрытого канала это обязательно\n"
        "4. Название может содержать пробелы",
        parse_mode='HTML',
        reply_markup=subscription_cancel_menu()
    )

@router.callback_query(F.data == "add_group_subscription")
async def add_group_subscription(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await state.set_state(SubscriptionState.wait_subscription_add)
    await state.update_data(sub_type="group")
    
    await cb.message.edit_text(
        "👥 <b>Добавление обязательной группы</b>\n\n"
        "Отправьте данные группы в формате:\n"
        "<code>ID_группы @юзернейм_или_ссылка Название_группы</code>\n\n"
        "<i>Примеры:</i>\n"
        "<code>-1001234567890 @example_group Наша группа</code>\n"
        "<code>-1001234567890 https://t.me/+AbCdEfGhIjK Закрытая группа</code>\n\n"
        "<i>Примечания:</i>\n"
        "1. ID группы должен быть числом (начинаться с -100)\n"
        "2. Для открытой группы — юзернейм с @; для закрытой (без "
        "публичного юзернейма) — пригласительная ссылка (https://t.me/+...)\n"
        "3. Пользователь должен быть участником группы\n"
        "4. Бот должен быть администратором группы — для закрытой группы "
        "это обязательно, иначе проверка подписки не сработает\n"
        "5. Название может содержать пробелы",
        parse_mode='HTML',
        reply_markup=subscription_cancel_menu()
    )


def _parse_username_or_link(token: str):
    """Принимает либо @юзернейм (открытый канал/группа), либо пригласительную
    ссылку (закрытый канал/группа без публичного юзернейма).
    Возвращает (username, url) или (None, None), если формат не распознан."""
    if token.startswith("@") and len(token) > 1:
        return token, f"https://t.me/{token.lstrip('@')}"
    if token.startswith("http://") or token.startswith("https://"):
        return "", token
    return None, None


@router.message(SubscriptionState.wait_subscription_add)
async def process_subscription_add(msg: Message, state: FSMContext):
    if msg.from_user.id not in ADMINS:
        return
    
    data = await state.get_data()
    sub_type = data.get("sub_type")
    
    if sub_type == "channel":
        parts = msg.text.split(maxsplit=2)
        if len(parts) < 3:
            return await msg.answer("❌ Неверный формат. Нужно: ID_канала @юзернейм_или_ссылка Название_канала")
        
        channel_id_str, username_token, name = parts
        
        try:
            channel_id = int(channel_id_str)
        except ValueError:
            return await msg.answer("❌ ID канала должен быть числом.")
        
        username, url = _parse_username_or_link(username_token)
        if url is None:
            return await msg.answer(
                "❌ Второй параметр должен быть либо @юзернейм, либо ссылкой "
                "(https://t.me/... или https://t.me/+...)."
            )
        
        for sub in REQUIRED_SUBSCRIPTIONS:
            if sub["type"] == "channel" and (
                str(sub["id"]) == str(channel_id)
                or (username and sub["username"] == username)
                or sub["url"] == url
            ):
                return await msg.answer(f"❌ Канал уже есть в списке.")
        
        new_sub = {
            "type": "channel",
            "id": str(channel_id),
            "username": username,
            "name": name,
            "url": url
        }
        
        REQUIRED_SUBSCRIPTIONS.append(new_sub)
        await save_subscriptions_to_db()
        
        await msg.answer(
            f"✅ Канал добавлен:\n"
            f"<b>Тип:</b> Канал\n"
            f"<b>Название:</b> {name}\n"
            f"<b>ID:</b> <code>{channel_id}</code>\n"
            f"<b>Юзернейм:</b> {username or '—  (закрытый, по ссылке)'}\n"
            f"<b>Ссылка:</b> {url}",
            parse_mode='HTML',
            reply_markup=subscriptions_menu()
        )
        
        await log("subscription_add", f"admin {msg.from_user.id} added channel {channel_id} ({name})")
    
    elif sub_type == "group":
        parts = msg.text.split(maxsplit=2)
        if len(parts) < 3:
            return await msg.answer("❌ Неверный формат. Нужно: ID_группы @юзернейм_или_ссылка Название_группы")
        
        group_id_str, username_token, name = parts
        
        try:
            group_id = int(group_id_str)
        except ValueError:
            return await msg.answer("❌ ID группы должен быть числом.")
        
        username, url = _parse_username_or_link(username_token)
        if url is None:
            return await msg.answer(
                "❌ Второй параметр должен быть либо @юзернейм, либо ссылкой "
                "(https://t.me/... или https://t.me/+...)."
            )
        
        for sub in REQUIRED_SUBSCRIPTIONS:
            if sub["type"] == "group" and (
                str(sub["id"]) == str(group_id)
                or (username and sub["username"] == username)
                or sub["url"] == url
            ):
                return await msg.answer(f"❌ Группа уже есть в списке.")
        
        try:
            bot_member = await bot.get_chat_member(chat_id=group_id, user_id=bot.id)
            if bot_member.status not in ["administrator", "creator"]:
                await msg.answer(
                    f"⚠️ <b>Предупреждение!</b>\n\n"
                    f"Бот не является администратором в группе '{name}'.\n"
                    f"Для корректной проверки подписок бот должен быть администратором "
                    f"(особенно если группа закрытая).\n\n"
                    f"Группа все равно будет добавлена, но проверка может не работать.",
                    parse_mode='HTML'
                )
        except Exception as e:
            logger.error(f"Ошибка проверки прав бота в группе: {e}")
            await msg.answer(
                f"⚠️ <b>Предупреждение!</b>\n\n"
                f"Не удалось проверить права бота в группе '{name}'.\n"
                f"Убедитесь, что бот добавлен в группу и является администратором.\n\n"
                f"Группа все равно будет добавлена.",
                parse_mode='HTML'
            )
        
        new_sub = {
            "type": "group",
            "id": str(group_id),
            "username": username,
            "name": name,
            "url": url
        }
        
        REQUIRED_SUBSCRIPTIONS.append(new_sub)
        await save_subscriptions_to_db()
        
        await msg.answer(
            f"✅ Группа добавлена:\n"
            f"<b>Тип:</b> Группа\n"
            f"<b>Название:</b> {name}\n"
            f"<b>ID:</b> <code>{group_id}</code>\n"
            f"<b>Юзернейм:</b> {username or '—  (закрытая, по ссылке)'}\n"
            f"<b>Ссылка:</b> {url}",
            parse_mode='HTML',
            reply_markup=subscriptions_menu()
        )
        
        await log("subscription_add", f"admin {msg.from_user.id} added group {group_id} ({name})")
    
    await state.clear()

@router.callback_query(F.data == "remove_subscription")
async def remove_subscription(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    if not REQUIRED_SUBSCRIPTIONS:
        return await cb.answer("📋 Список подписок пуст.", show_alert=True)
    
    keyboard = []
    
    for i, sub in enumerate(REQUIRED_SUBSCRIPTIONS, 1):
        if sub["type"] == "channel":
            emoji = "📢"
        elif sub["type"] == "group":
            emoji = "👥"
        else:
            emoji = "🤖"
            
        keyboard.append([
            InlineKeyboardButton(
                text=f"{i}. {emoji} {sub['name']}",
                callback_data=f"remove_sub_{i-1}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="manage_subscriptions")])
    
    await cb.message.edit_text(
        "🗑️ <b>Удаление подписки</b>\n\n"
        "Выберите подписку для удаления:",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )

@router.callback_query(F.data.startswith("remove_sub_"))
async def process_remove_subscription(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    try:
        index = int(cb.data.split("_")[2])
        if 0 <= index < len(REQUIRED_SUBSCRIPTIONS):
            removed_sub = REQUIRED_SUBSCRIPTIONS.pop(index)
            
            await save_subscriptions_to_db()
            
            if removed_sub["type"] == "channel":
                emoji = "📢"
            elif removed_sub["type"] == "group":
                emoji = "👥"
            else:
                emoji = "🤖"
                
            await cb.message.edit_text(
                f"✅ Подписка удалена:\n\n"
                f"{emoji} <b>{removed_sub['name']}</b>\n"
                f"Тип: {removed_sub['type']}\n"
                f"Юзернейм: {removed_sub['username']}\n"
                f"Ссылка: {removed_sub['url']}",
                parse_mode='HTML',
                reply_markup=subscriptions_menu()
            )
            
            await log("subscription_remove", f"admin {cb.from_user.id} removed {removed_sub['type']} {removed_sub['username']}")
        else:
            await cb.answer("❌ Неверный индекс подписки.", show_alert=True)
    except (ValueError, IndexError):
        await cb.answer("❌ Ошибка при удалении подписки.", show_alert=True)

@router.callback_query(F.data == "refresh_subscriptions")
async def refresh_subscriptions(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    
    await load_subscriptions_from_db()
    await cb.answer("✅ Список подписок обновлен из базы данных.", show_alert=True)

# ================== ADMINS PAGE ==================
@router.callback_query(F.data == "admins")
async def admins_page(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)
    
    text = (
        "👥 <b>Администраторы проекта</b>\n\n"
        "📱 <b>Контакты для связи:</b>\n"
        "• @theaugustine\n"
        "• @nekon4il\n\n"
        "⏰ <b>Время работы:</b>\n"
        "• Пн-Пт: 10:00 - 23:00\n"
        "• Сб-Вс: 12:00 - 00:00\n\n"
        "📞 <b>По вопросам:</b>\n"
        "• Публикации постов\n"
        "• Модерации\n"
        "• Рекламы\n"
        "• Технических проблем\n\n"
        "✉️ <b>Пишите нам, мы всегда на связи!</b>"
    )
    
    await cb.message.edit_text(text, parse_mode='HTML', reply_markup=admins_keyboard())

