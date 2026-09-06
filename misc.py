"""
Платное ускорение проверки поста ("⚡ Ускорить проверку" под сообщением
"Ваш пост отправлен на модерацию").

Оплата PRIORITY_BOOST_PRICE_STARS звёзд -> бот переставляет пост (если он
уже в очереди на автопубликацию) на ближайший возможный момент и шлёт всем
администраторам пометку "приоритетная публикация". Обработка
pre_checkout_query/successful_payment централизована в handlers/payments.py.
"""
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton
from app.config import ADMINS
from app.services import publish_priority_post

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


@router.callback_query(F.data.startswith("priority_pub_"))
async def priority_publish_now(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    try:
        post_id = int(cb.data.split("_")[2])
    except (ValueError, IndexError):
        return await cb.answer("❌ Некорректный ID.", show_alert=True)

    ok, reason = await publish_priority_post(post_id, cb.from_user.id)
    if not ok:
        msg = {
            "already_processed": "⚠️ Пост уже обработан.",
            "not_found": "❌ Пост не найден.",
            "forbidden": "🚫 Нет доступа.",
        }.get(reason, "❌ Не удалось опубликовать пост.")
        return await cb.answer(msg, show_alert=True)

    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Опубликовано", callback_data="disabled")]
        ]))
    except Exception:
        pass
    await cb.answer("🚀 Пост опубликован!", show_alert=True)


@router.callback_query(F.data.startswith("priority_rej_"))
async def priority_reject_now(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:
        return await cb.answer("🚫 У вас нет доступа.", show_alert=True)
    try:
        post_id = int(cb.data.split("_")[2])
    except (ValueError, IndexError):
        return await cb.answer("❌ Некорректный ID.", show_alert=True)
    from app.services import reject_post
    ok = await reject_post(post_id, cb.from_user.id, "Отклонено администратором из приоритетной заявки.")
    if not ok:
        return await cb.answer("⚠️ Пост уже обработан.", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отклонено", callback_data="disabled")]
        ]))
    except Exception:
        pass
    await cb.answer("❌ Пост отклонён.", show_alert=True)


async def complete_priority_boost(msg: Message, post_id: int) -> None:
    """Вызывается из handlers/payments.py после успешной оплаты."""
    payment = msg.successful_payment
    recorded = await record_star_purchase(
        msg.from_user.id, "priority_boost", post_id, payment.total_amount, payment.telegram_payment_charge_id
    )
    if not recorded:
        await msg.answer("ℹ️ Эта оплата уже была обработана. Если приоритет не применился, обратитесь в администрацию.")
        return

    await apply_priority_boost(post_id, msg.from_user.id)

    await msg.answer(
        "🚀 Оплата получена! Ваш пост поставлен в приоритет, "
        "администраторы уведомлены.",
    )
