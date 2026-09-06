from aiogram import F, Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import *

from app.database import *
from app.keyboards import *
from app.states import *
from app.validators import *
from app.services import *
from app.handlers.author_lookup import present_author_lookup
from app.handlers.post_deletion import present_delete_prompt
from app.handlers.unlock import get_ban_status, unlock_keyboard, ban_status_text

router = Router()


async def _dispatch_deep_link(msg: Message, state: FSMContext, args: str) -> bool:
    """Обрабатывает параметр deep-link'а (payload после /start), которым
    открываются кнопки "🔎 Узнать автора"/"🗑 Удалить пост" под первым
    комментарием бота в группе обсуждений — см. keyboards.intro_comment_keyboard.
    Возвращает True, если запрос был распознан и обработан (тогда обычное
    главное меню показывать не нужно)."""
    if not args:
        return False

    try:
        if args.startswith("author_"):
            post_id = int(args[len("author_"):])
            await present_author_lookup(msg, post_id)
            return True

        if args.startswith("delete_"):
            post_id = int(args[len("delete_"):])
            await present_delete_prompt(msg, state, post_id)
            return True
    except ValueError:
        pass

    return False


# ================== START ==================
@router.message(CommandStart())
async def start(msg: Message, command: CommandObject, state: FSMContext):
    if msg.chat.type not in ['private']:
        return await msg.answer("⚠️ Бот работает только в личных сообщениях")
    
    ban_status = await get_ban_status(msg.from_user.id)

    if ban_status["bot"]:
        # Заблокирован в самом боте — дальше идти нельзя, но показываем
        # кнопку платной разблокировки (для всех мест, где он заблокирован
        # одновременно — не только бота).
        ban_info = await get_ban_info(msg.from_user.id)
        text = ban_status_text(ban_status)
        if ban_info:
            reason, ban_time, admin_username = ban_info
            text += (
                f"\n\n📝 Причина: {reason}\n"
                f"🕐 Время блокировки: {ban_time}\n"
                f"👮 Вас заблокировал администратор: @{admin_username or 'неизвестно'}"
            )
        return await msg.answer(text, reply_markup=unlock_keyboard(ban_status))
    
    await register_user(msg.from_user)
    
    # Проверяем подписку при старте
    is_subscribed, unsubscribed = await check_subscription(msg.from_user.id)
    unsubscribed_required = [sub for sub in unsubscribed if sub["type"] in ["channel", "group"]]
    
    if unsubscribed_required:
        # Если это был переход по deep-link кнопке (узнать автора/удалить
        # пост) — запоминаем, что сделать, как только подписка будет
        # подтверждена (см. check_subscription_callback), а не теряем это.
        if command.args:
            await state.update_data(pending_deep_link=command.args)
        await msg.answer(
            f"<b>Для начала вам нужно подписаться</b>\n"
            f"После этого нажмите на кнопку «Я подписался».\n",
            parse_mode='HTML',
            reply_markup=get_subscription_keyboard(unsubscribed_required)
        )
        return
    
    await update_user_subscription_status(msg.from_user.id, True)

    if await _dispatch_deep_link(msg, state, command.args):
        return

    await msg.answer(
        "✨ <b>Добро пожаловать в Смотр</b>\n\n"
        "<blockquote>Место, где можно предложить пост, проверить его статус и быстро найти нужную информацию.</blockquote>\n\n"
        "📝 <b>Хотите предложить пост?</b>\n"
        "Нажмите «Предложить пост» и следуйте подсказкам.\n\n"
        "⚠️ <b>Важно:</b> каждый пост должен начинаться с 🧑, 👩, 🧑 или 👩 либо 👩 или 🧑.\n\n"
        "Выберите действие ниже 👇",
        parse_mode='HTML',
        reply_markup=main_menu()
    )

    # Не заблокирован в боте, но заблокирован в канале и/или комментариях —
    # это не мешает пользоваться ботом, но стоит предложить разблокировку.
    if ban_status["channel"] or ban_status["comments"]:
        await msg.answer(ban_status_text(ban_status), reply_markup=unlock_keyboard(ban_status))

@router.callback_query(F.data == "check_subscription")
async def check_subscription_callback(cb: CallbackQuery, state: FSMContext):
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

    # Если пользователь пришёл по deep-link кнопке и подписка потребовалась
    # только сейчас — доигрываем отложенный сценарий вместо обычного меню.
    data = await state.get_data()
    pending_deep_link = data.get("pending_deep_link")
    if pending_deep_link:
        await state.update_data(pending_deep_link=None)
        if await _dispatch_deep_link(cb.message, state, pending_deep_link):
            return

    await cb.message.edit_text(
        "✅ <b>Всё готово</b>\n\n"
        "<blockquote>Подписка подтверждена. Теперь вам доступны все основные функции бота.</blockquote>\n\n"
        "📝 Для новой публикации нажмите «Предложить пост».\n"
        "👤 В профиле можно посмотреть свою статистику.\n\n"
        "Выберите действие ниже 👇",
        parse_mode='HTML',
        reply_markup=main_menu()
    )
