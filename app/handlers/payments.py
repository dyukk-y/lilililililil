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
from app.handlers.donation import complete_donation
from app.handlers.unlock import SCOPE_LABELS, get_ban_status
from app.database import get_post_by_id, get_star_purchase

logger = logging.getLogger(__name__)

router = Router()

async def _validate_payload(payload: str, payer_id: int) -> str | None:
    """Проверяет заказ ДО списания звёзд. Возвращает текст ошибки или None.

    Раньше pre_checkout подтверждался для любого известного префикса, поэтому
    можно было заплатить за ускорение уже опубликованного/отклонённого поста,
    повторно оплатить уже купленную расшифровку автора (вторая покупка не
    записывалась из-за уникального индекса — звёзды просто пропадали) или
    оплатить разблокировку, не будучи заблокированным.
    """
    parts = payload.split("|")
    kind = parts[0]

    if kind == "reveal" and len(parts) == 4:
        post_id = int(parts[1])
        post = await get_post_by_id(post_id)
        if not post:
            return "Пост не найден."
        if post[1] == payer_id:
            return "Это ваш собственный пост — платить не нужно."
        if await get_star_purchase(payer_id, post_id, "author_reveal"):
            return "Вы уже купили расшифровку этого поста — повторно платить не нужно."
        return None

    if kind == "priority" and len(parts) == 3:
        post_id = int(parts[1])
        post = await get_post_by_id(post_id)
        if not post:
            return "Пост не найден."
        if post[1] != payer_id:
            return "Это не ваш пост."
        if post[5] not in ("moderation", "approved"):
            return "Пост уже обработан — ускорять больше нечего."
        return None

    if kind == "unlock" and len(parts) == 2:
        scope = parts[1]
        if scope not in SCOPE_LABELS:
            return "Неизвестный тип разблокировки."
        status = await get_ban_status(payer_id)
        if not status.get(scope):
            return "Вы не заблокированы — оплата не требуется."
        return None

    if kind == "donation" and len(parts) == 3:
        if int(parts[1]) != payer_id:
            return "Счёт выставлен другому пользователю."
        if int(parts[2]) < 1:
            return "Некорректная сумма."
        return None

    return "Неизвестный товар."


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    payload = pre_checkout_query.invoice_payload or ""
    try:
        error = await _validate_payload(payload, pre_checkout_query.from_user.id)
    except Exception:
        logger.exception("Ошибка проверки pre_checkout (payload=%r)", payload)
        error = "Не удалось проверить заказ, попробуйте ещё раз."

    if error is None:
        await pre_checkout_query.answer(ok=True)
    else:
        logger.warning("pre_checkout отклонён (payload=%r): %s", payload, error)
        await pre_checkout_query.answer(ok=False, error_message=error)


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
            _, post_id_str, _payer_id_str = parts
            await complete_priority_boost(msg, int(post_id_str))
            return

        if parts[0] == "unlock" and len(parts) == 2:
            _, scope = parts
            await complete_unlock(msg, scope)
            return

        if parts[0] == "donation" and len(parts) == 3:
            _, payer_id_str, amount_str = parts
            if int(payer_id_str) != msg.from_user.id:
                raise ValueError("Плательщик в payload не совпадает с пользователем")
            await complete_donation(msg, int(amount_str))
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
