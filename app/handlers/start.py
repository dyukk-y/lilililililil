from aiogram import F, Router
from aiogram.types import *


from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *

router = Router()

# ================== START ==================
@router.message(F.text == "/start")
async def start(msg: Message):
    if msg.chat.type not in ['private']:
        return await msg.answer("⚠️ Бот работает только в личных сообщениях")
    
    if await is_banned(msg.from_user.id):
        ban_info = await get_ban_info(msg.from_user.id)
        if ban_info:
            reason, ban_time, admin_username = ban_info
            return await msg.answer(
                f"🚫 Вы заблокированы.\n\n"
                f"📝 Причина: {reason}\n"
                f"🕐 Время блокировки: {ban_time}\n"
                f"👮 Вас заблокировал администратор: @{admin_username or 'неизвестно'}"
            )
        return await msg.answer("🚫 Вы заблокированы.")
    
    await register_user(msg.from_user)
    
    # Проверяем подписку при старте
    is_subscribed, unsubscribed = await check_subscription(msg.from_user.id)
    unsubscribed_required = [sub for sub in unsubscribed if sub["type"] in ["channel", "group"]]
    
    if unsubscribed_required:
        await msg.answer(
            f"<b>Для начала вам нужно подписаться</b>\n"
            f"После этого нажмите на кнопку «Я подписался».\n",
            parse_mode='HTML',
            reply_markup=get_subscription_keyboard(unsubscribed_required)
        )
        return
    
    await update_user_subscription_status(msg.from_user.id, True)
    
    await msg.answer(
        "Привет! 👋\n"
        "Предложи запись для размещения в канале. \n\n"
        "⚠️ <b>Важное правило:</b>\n"
        "Каждый пост должен содержать эмодзи 🧑 или 👩\n\n"
        "Выбери действие:",
        parse_mode='HTML',
        reply_markup=main_menu()
    )

@router.callback_query(F.data == "check_subscription")
async def check_subscription_callback(cb: CallbackQuery):
    if cb.message.chat.type not in ['private']:
        return await cb.answer("⚠️ Действие доступно только в личных сообщениях", show_alert=True)
    
    await cb.answer("⏳ Проверяем подписку...")
    
    is_subscribed, unsubscribed = await check_subscription(cb.from_user.id)
    
    unsubscribed_required = [sub for sub in unsubscribed if sub["type"] in ["channel", "group"]]
    
    if unsubscribed_required:
        await cb.message.edit_text(
            f"<b>Вы еще не подписались 😡</b>\n"
            f"После подписки нажмите кнопку «Я подписался» еще раз",
            parse_mode='HTML',
            reply_markup=get_subscription_keyboard(unsubscribed_required)
        )
        return
    
    await update_user_subscription_status(cb.from_user.id, True)
    
    await cb.message.edit_text(
        "✅ <b>Отлично! Вы подписались на необходимые ресурсы</b>\n\n"
        "Привет! 👋\n"
        "Предложи запись для размещения в канале. \n\n"
        "⚠️ <b>Важное правило:</b>\n"
        "Каждый пост должен содержать эмодзи 🧑 или 👩!\n\n"
        "Выбери действие:",
        parse_mode='HTML',
        reply_markup=main_menu()
    )

