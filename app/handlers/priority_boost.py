"""
Платное ускорение проверки поста ("⚡ Ускорить проверку" под сообщением
"Ваш пост отправлен на модерацию").

Оплата PRIORITY_BOOST_PRICE_STARS звёзд -> бот переставляет пост (если он
уже в очереди на автопубликацию) на ближайший возможный момент и шлёт всем
администраторам пометку "приоритетная публикация". Обработка
pre_checkout_query/successful_payment централизована в handlers/payments.py.
"""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton

from app.auth import is_admin
from app.runtime_settings import get as get_setting
from app.database import is_banned, get_post_by_id, record_star_purchase
from app.services import apply_priority_boost, publish_priority_post
from app.states import AdminPostState

logger = logging.getLogger(__name__)

router = Router()


@router.callback_query(F.data.startswith("priority_boost_"))
async def priority_boost_start(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)

    try:
        post_id = int(cb.data.split("_")[2])
    except (ValueError, IndexError):
        return await cb.answer("❌ Неверный запрос.", show_alert=True)
    post = await get_post_by_id(post_id)
    if not post:
        return await cb.answer("❌ Пост не найден.", show_alert=True)

    if post[1] != cb.from_user.id:
        return await cb.answer("❌ Это не ваш пост.", show_alert=True)

    if post[5] not in ("moderation", "approved"):
        return await cb.answer("❌ Этот пост уже обработан, ускорять больше нечего.", show_alert=True)

    await cb.answer()
    price = get_setting("PRIORITY_BOOST_PRICE_STARS")
    payload = f"priority|{post_id}|{cb.from_user.id}"
    await cb.message.bot.send_invoice(
        chat_id=cb.message.chat.id,
        title="Ускорение проверки поста",
        description=f"Приоритетная проверка и публикация поста #{post_id} — вне общей очереди",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Ускорение проверки", amount=price)],
    )


async def complete_priority_boost(msg: Message, post_id: int) -> None:
    """Вызывается из handlers/payments.py после успешной оплаты."""
    payment = msg.successful_payment
    await record_star_purchase(
        msg.from_user.id, "priority_boost", post_id, payment.total_amount, payment.telegram_payment_charge_id
    )

    await apply_priority_boost(post_id, msg.from_user.id)

    await msg.answer(
        "🚀 Оплата получена! Ваш пост поставлен в приоритет, "
        "администраторы уведомлены.",
    )


# ================== КНОПКИ АДМИНИСТРАТОРА В УВЕДОМЛЕНИИ О ПРИОРИТЕТЕ ==================
# Раньше у кнопок «🚀 Опубликовать сейчас» / «❌ Отклонить» в уведомлении о
# платном ускорении вообще не было обработчиков — нажатие ничего не делало,
# хотя пользователь уже заплатил за приоритет.
def _priority_done_keyboard(text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data="disabled")]])


@router.callback_query(F.data.startswith("priority_pub_"))
async def priority_publish(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    try:
        post_id = int(cb.data.split("_")[2])
    except (ValueError, IndexError):
        return await cb.answer("❌ Неверный ID поста", show_alert=True)

    ok, reason = await publish_priority_post(post_id, cb.from_user.id)
    if not ok:
        messages = {
            "already_processed": "❌ Этот пост уже обработан (опубликован/отклонён).",
            "not_found": "❌ Пост не найден.",
            "forbidden": "🚫 У вас нет доступа.",
        }
        return await cb.answer(messages.get(reason, f"❌ Ошибка публикации: {reason}"), show_alert=True)

    await cb.answer("✅ Пост опубликован!", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=_priority_done_keyboard("✅ Опубликовано"))
    except Exception:
        pass


@router.callback_query(F.data.startswith("priority_rej_"))
async def priority_reject(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    try:
        post_id = int(cb.data.split("_")[2])
    except (ValueError, IndexError):
        return await cb.answer("❌ Неверный ID поста", show_alert=True)

    post = await get_post_by_id(post_id)
    if not post or post[5] not in ("moderation", "approved"):
        return await cb.answer("❌ Этот пост уже обработан.", show_alert=True)

    # Дальше используется тот же сценарий, что и «❌ Отклонить пост» в
    # админ-панели: причина -> предпросмотр -> подтверждение.
    await state.set_state(AdminPostState.wait_reject_reason)
    await state.update_data(post_id=post_id)
    await cb.answer()
    await cb.message.answer(
        f"📝 <b>Причина отклонения поста #{post_id}</b>\n\n"
        "Напишите причину, которая будет отправлена автору:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_reject_cancel")]
        ]),
    )
