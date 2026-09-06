"""
Единая точка обработки платежей Telegram Stars.

Telegram доставляет pre_checkout_query и successful_payment один раз —
их не может "поделить" несколько независимых обработчиков в разных
роутерах (сработает только первый зарегистрированный, и все остальные
платежи, включая другого типа, до них не дойдут). Поэтому вся логика
диспетчеризации по payload собрана здесь, а сами хендлеры
(handlers/author_lookup.py, handlers/priority_boost.py) только
выставляют инвойс и предоставляют функцию-обработчик успешной оплаты.
"""
import logging

from aiogram import F, Router
from aiogram.types import Message, PreCheckoutQuery

from app.handlers.author_lookup import complete_author_reveal
from app.handlers.priority_boost import complete_priority_boost
from app.handlers.unlock import complete_unlock

logger = logging.getLogger(__name__)

router = Router()

_KNOWN_PREFIXES = ("reveal|", "priority|", "unlock|")


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    if pre_checkout_query.invoice_payload.startswith(_KNOWN_PREFIXES):
        await pre_checkout_query.bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
    else:
        logger.warning(f"Неизвестный payload в pre_checkout_query: {pre_checkout_query.invoice_payload}")
        await pre_checkout_query.bot.answer_pre_checkout_query(
            pre_checkout_query.id, ok=False, error_message="Неизвестный товар."
        )


@router.message(F.successful_payment)
async def process_successful_payment(msg: Message):
    payload = msg.successful_payment.invoice_payload
    parts = payload.split("|")

    try:
        if parts[0] == "reveal" and len(parts) == 4:
            _, post_id_str, chat_id_str, message_id_str = parts
            await complete_author_reveal(msg, int(post_id_str), int(chat_id_str), int(message_id_str))
            return

        if parts[0] == "priority" and len(parts) == 3:
            _, post_id_str, payer_id_str = parts
            post_id = int(post_id_str)
            payer_id = int(payer_id_str)
            if payer_id != msg.from_user.id:
                raise ValueError("payer mismatch")
            from app.runtime_settings import get as get_setting
            if msg.successful_payment.total_amount != get_setting("PRIORITY_BOOST_PRICE_STARS"):
                raise ValueError("priority price mismatch")
            await complete_priority_boost(msg, post_id)
            return

        if parts[0] == "unlock" and len(parts) == 2:
            _, scope = parts
            await complete_unlock(msg, scope)
            return

        logger.error(f"Не удалось обработать successful_payment с payload={payload!r}")
        await msg.answer(
            "⚠️ Оплата прошла, но бот не смог определить, за что именно — "
            "свяжитесь с администрацией, укажите время платежа."
        )
    except Exception:
        logger.exception(f"Ошибка обработки successful_payment (payload={payload!r})")
        await msg.answer(
            "⚠️ Оплата прошла, но при обработке произошла ошибка — "
            "свяжитесь с администрацией, мы всё поправим."
        )
