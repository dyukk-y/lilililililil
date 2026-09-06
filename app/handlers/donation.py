"""Пожертвования через Telegram Stars."""
import logging
import os

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, LabeledPrice

from app.config import ADMINS_CHAT_ID
from app.database import record_star_purchase
from app.runtime_settings import get as get_setting
from app.states import DonationState

router = Router()
logger = logging.getLogger(__name__)

DONATION_TOPIC_ID = int(os.getenv("DONATION_TOPIC_ID", "81476"))


@router.callback_query(F.data == "donate")
async def donation_start(cb: CallbackQuery, state: FSMContext):
    if cb.message.chat.type != "private":
        return await cb.answer("⚠️ Пожертвование доступно только в личных сообщениях.", show_alert=True)

    await state.set_state(DonationState.wait_amount)
    await cb.answer()
    await cb.message.edit_text(
        "💛 Пожертвование\n\n"
        "Введите любое количество Stars, которое хотите пожертвовать.\n"
        "Например: 10, 50 или 100."
    )


@router.message(DonationState.wait_amount, F.text)
async def donation_amount(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip().replace(" ", "")
    try:
        amount = int(raw)
    except ValueError:
        return await msg.answer("⚠️ Введите целое число Stars, например: 50.")

    if amount < 1 or amount > 1_000_000:
        return await msg.answer("⚠️ Сумма должна быть от 1 до 1 000 000 Stars.")

    await state.clear()

    # Сообщение пользователя удаляем, чтобы в диалоге осталась только понятная
    # форма оплаты. Сам инвойс Telegram создаётся отдельным сообщением.
    try:
        await msg.delete()
    except Exception:
        pass

    await msg.answer(
        f"💛 Пожертвование: {amount} Stars\n\n"
        "Нажмите кнопку оплаты ниже, чтобы завершить пожертвование."
    )

    await msg.answer_invoice(
        title="Пожертвование",
        description=f"Добровольное пожертвование боту: {amount} Stars",
        payload=f"donation|{msg.from_user.id}|{amount}",
        currency="XTR",
        prices=[LabeledPrice(label="Пожертвование", amount=amount)],
    )


async def complete_donation(msg: Message, amount: int):
    payment = msg.successful_payment
    username = f"@{msg.from_user.username}" if msg.from_user.username else "без username"
    name = (msg.from_user.full_name or "Без имени").replace("\n", " ")

    await record_star_purchase(
        user_id=msg.from_user.id,
        kind="donation",
        post_id=None,
        amount_stars=amount,
        telegram_charge_id=payment.telegram_payment_charge_id,
    )

    admin_chat_id = get_setting("ADMINS_CHAT_ID") or ADMINS_CHAT_ID
    if not admin_chat_id:
        logger.error("ADMINS_CHAT_ID не задан — невозможно отправить уведомление о пожертвовании")
    else:
        await msg.bot.send_message(
            chat_id=int(admin_chat_id),
            message_thread_id=DONATION_TOPIC_ID,
            text=(
                "💛 Новое пожертвование\n\n"
                f"👤 Пользователь: {username}\n"
                f"📝 Имя: {name}\n"
                f"🆔 ID: {msg.from_user.id}\n"
                f"⭐️ Сумма: {amount} Stars\n"
                f"🧾 ID платежа: {payment.telegram_payment_charge_id}"
            ),
        )

    await msg.answer(
        f"💛 Спасибо за пожертвование — {amount} Stars!\n"
        "Ваш вклад получил администратора. Спасибо ❤️"
    )
