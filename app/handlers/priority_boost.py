"""
Платное ускорение проверки поста ("⚡ Ускорить проверку" под сообщением
"Ваш пост отправлен на модерацию").

Оплата PRIORITY_BOOST_PRICE_STARS звёзд -> бот переставляет пост (если он
уже в очереди на автопубликацию) на ближайший возможный момент и шлёт всем
администраторам пометку "приоритетная публикация". Обработка
pre_checkout_query/successful_payment централизована в handlers/payments.py.
"""
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, LabeledPrice

from app.runtime_settings import get as get_setting
from app.database import is_banned, get_post_by_id, record_star_purchase
from app.services import apply_priority_boost

router = Router()


@router.callback_query(F.data.startswith("priority_boost_"))
async def priority_boost_start(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)

    post_id = int(cb.data.split("_")[2])
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
