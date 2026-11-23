"""Платное закрепление своего поста в канале на N часов (Telegram Stars).

Флоу: «📌 Закрепить в канале» (в уведомлении о публикации) -> выбор срока
-> счёт -> оплата -> бот закрепляет пост, а maintenance_loop снимает
закреп по истечении срока. Повторная покупка продлевает действующий
закреп. Если Telegram не дал закрепить — звёзды возвращаются
автоматически (refund_star_payment).
"""
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message

from app.config import PIN_HOURS_OPTIONS
from app.database import (
    add_post_pin, get_active_pin, get_post_by_id, get_post_channel_message_id, log, record_star_purchase,
)
from app.keyboards import menu_btn, pin_hours_keyboard
from app.loader import bot
from app.runtime_settings import get as get_setting

logger = logging.getLogger(__name__)
router = Router()


async def validate_pin_order(payer_id: int, post_id: int, hours: int) -> str | None:
    """Текст ошибки или None. Используется и перед счётом, и в pre_checkout."""
    if hours not in PIN_HOURS_OPTIONS:
        return "Недоступный срок закрепа."
    post = await get_post_by_id(post_id)
    if not post:
        return "Пост не найден."
    if post[1] != payer_id:
        return "Закрепить можно только свой пост."
    if post[5] != "published" or not await get_post_channel_message_id(post_id):
        return "Пост не опубликован в канале."
    return None


@router.callback_query(F.data.startswith("pin_offer_"))
async def pin_offer(cb: CallbackQuery):
    try:
        post_id = int(cb.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return await cb.answer("❌ Неверный запрос.", show_alert=True)
    error = await validate_pin_order(cb.from_user.id, post_id, PIN_HOURS_OPTIONS[0])
    if error:
        return await cb.answer(f"❌ {error}", show_alert=True)

    price = get_setting("PIN_PRICE_STARS_PER_HOUR")
    active = await get_active_pin(post_id)
    note = "\n\n📌 Пост уже закреплён — покупка продлит закреп." if active else ""
    await cb.answer()
    await cb.message.answer(
        f"📌 <b>Закрепить пост #{post_id}</b>\n\n"
        f"Пост будет висеть вверху канала выбранное время, потом закреп снимется "
        f"автоматически.\nЦена: <b>{price} ⭐ за час</b>.{note}",
        parse_mode="HTML",
        reply_markup=pin_hours_keyboard(post_id, PIN_HOURS_OPTIONS, price),
    )


@router.callback_query(F.data == "pin_cancel")
async def pin_cancel(cb: CallbackQuery):
    await cb.answer("Отменено")
    try:
        await cb.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("pin_buy_"))
async def pin_buy(cb: CallbackQuery):
    try:
        _, _, post_id_s, hours_s = cb.data.split("_")
        post_id, hours = int(post_id_s), int(hours_s)
    except ValueError:
        return await cb.answer("❌ Неверный запрос.", show_alert=True)
    error = await validate_pin_order(cb.from_user.id, post_id, hours)
    if error:
        return await cb.answer(f"❌ {error}", show_alert=True)

    price = get_setting("PIN_PRICE_STARS_PER_HOUR") * hours
    await cb.answer()
    await cb.message.answer_invoice(
        title=f"Закреп поста на {hours} ч",
        description=f"Закрепление поста #{post_id} вверху канала на {hours} ч.",
        payload=f"pin|{post_id}|{hours}",
        currency="XTR",
        prices=[LabeledPrice(label=f"Закреп {hours} ч", amount=price)],
    )


async def complete_pin(msg: Message, post_id: int, hours: int) -> None:
    """Вызывается из handlers/payments.py после успешной оплаты."""
    payment = msg.successful_payment
    user_id = msg.from_user.id
    charge_id = payment.telegram_payment_charge_id
    if not await record_star_purchase(user_id, "pin", post_id, payment.total_amount, charge_id):
        return  # повторная доставка того же платежа

    channel_message_id = await get_post_channel_message_id(post_id)
    try:
        await bot.pin_chat_message(get_setting("MAIN_CHANNEL_ID"), channel_message_id, disable_notification=True)
    except Exception:
        logger.exception("Не удалось закрепить пост #%s после оплаты — возвращаю звёзды", post_id)
        try:
            await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id)
            await msg.answer("⚠️ Не удалось закрепить пост, звёзды возвращены на ваш счёт.", reply_markup=menu_btn())
        except Exception:
            logger.exception("Не удалось вернуть звёзды за закреп поста #%s", post_id)
            await msg.answer("⚠️ Не удалось закрепить пост. Напишите администрации — мы всё вернём.")
        await log("pin_failed", f"user {user_id} post #{post_id}: pin failed")
        return

    expires = await add_post_pin(post_id, user_id, channel_message_id, hours, charge_id)
    await log("pin_purchase", f"user {user_id} pinned post #{post_id} for {hours}h")
    await msg.answer(
        f"📌 <b>Пост #{post_id} закреплён!</b>\n\n"
        f"Закреп снимется автоматически {expires.strftime('%d.%m в %H:%M')} (Новосибирск).",
        parse_mode="HTML",
        reply_markup=menu_btn(),
    )


async def unpin_expired() -> int:
    """Снимает закрепы с истёкшим сроком. Вызывается из maintenance_loop."""
    from app.database import finish_post_pin, get_expired_pins
    done = 0
    for pin_id, post_id, _user_id, channel_message_id in await get_expired_pins():
        try:
            await bot.unpin_chat_message(get_setting("MAIN_CHANNEL_ID"), message_id=channel_message_id)
        except Exception as e:
            # Пост удалён/уже откреплён вручную — закреп всё равно закрываем.
            logger.info("Снятие закрепа поста #%s: %s", post_id, e)
        await finish_post_pin(pin_id=pin_id)
        done += 1
    return done
