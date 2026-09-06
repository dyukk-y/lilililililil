"""
Платная разблокировка за Telegram Stars — сразу в трёх возможных местах:
- бот (наш собственный DB-бан, см. app.database.bans);
- основной канал (пользователь был исключён/забанен в MAIN_CHANNEL_ID);
- группа комментариев (исключён/забанен в COMMENTS_CHAT_ID).

Кнопка показывается только для тех мест, где пользователь ДЕЙСТВИТЕЛЬНО
заблокирован (проверяется динамически через get_ban_status) — если он
не заблокирован ни там, ни там, кнопок не будет вообще.

Обработка pre_checkout_query/successful_payment централизована в
handlers/payments.py — здесь только формирование инвойса и функция
complete_unlock, которую payments.py вызывает после успешной оплаты.
"""
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton

from app.runtime_settings import get as get_setting
from app.loader import bot
from app.database import is_banned, unban_user, log, record_star_purchase

logger = logging.getLogger(__name__)

router = Router()

SCOPE_LABELS = {
    "bot": "в боте",
    "channel": "в канале",
    "comments": "в комментариях",
}


async def get_ban_status(user_id: int) -> dict:
    """Проверяет блокировку пользователя во всех трёх местах. Для канала и
    комментариев блокировка определяется через статус участника чата
    ("kicked" — это именно бан/исключение в Telegram, а не просто выход
    из чата по своей воле)."""
    status = {"bot": False, "channel": False, "comments": False}

    status["bot"] = await is_banned(user_id)

    main_channel_id = get_setting("MAIN_CHANNEL_ID")
    if main_channel_id:
        try:
            member = await bot.get_chat_member(main_channel_id, user_id)
            status["channel"] = member.status == "kicked"
        except Exception:
            pass

    comments_chat_id = get_setting("COMMENTS_CHAT_ID")
    if comments_chat_id:
        try:
            member = await bot.get_chat_member(comments_chat_id, user_id)
            status["comments"] = member.status == "kicked"
        except Exception:
            pass

    return status


def unlock_keyboard(status: dict) -> InlineKeyboardMarkup | None:
    """Кнопка появляется только для мест, где пользователь реально
    заблокирован — если status пуст, возвращает None (нечего показывать)."""
    price = get_setting("UNLOCK_PRICE_STARS")
    rows = []
    if status.get("bot"):
        rows.append([InlineKeyboardButton(text=f"🔓 Разблокировать бота за {price} ⭐", callback_data="unlock_bot")])
    if status.get("channel"):
        rows.append([InlineKeyboardButton(text=f"🔓 Разблокировать канал за {price} ⭐", callback_data="unlock_channel")])
    if status.get("comments"):
        rows.append([InlineKeyboardButton(text=f"🔓 Разблокировать комментарии за {price} ⭐", callback_data="unlock_comments")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def ban_status_text(status: dict) -> str:
    """Человекочитаемое описание того, где именно заблокирован пользователь."""
    places = [SCOPE_LABELS[k] for k, v in status.items() if v]
    if not places:
        return ""
    return "🚫 Вы заблокированы: " + ", ".join(places) + "."


@router.callback_query(F.data.startswith("unlock_"))
async def unlock_start(cb: CallbackQuery):
    scope = cb.data[len("unlock_"):]
    if scope not in SCOPE_LABELS:
        return await cb.answer("❌ Неизвестный запрос", show_alert=True)

    await cb.answer()
    price = get_setting("UNLOCK_PRICE_STARS")
    payload = f"unlock|{scope}"
    await cb.message.answer_invoice(
        title=f"Разблокировка {SCOPE_LABELS[scope]}",
        description=f"Снятие блокировки {SCOPE_LABELS[scope]}",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Разблокировка", amount=price)],
    )


async def complete_unlock(msg: Message, scope: str) -> None:
    """Вызывается из handlers/payments.py после успешной оплаты."""
    user_id = msg.from_user.id
    payment = msg.successful_payment

    await record_star_purchase(user_id, f"unlock_{scope}", None, payment.total_amount, payment.telegram_payment_charge_id)
    await log("unlock_purchase", f"user {user_id} paid to unlock scope={scope}")

    if scope == "bot":
        await unban_user(user_id)
        await msg.answer("✅ Вы разблокированы в боте! Можете пользоваться им снова.")
        return

    if scope == "channel":
        main_channel_id = get_setting("MAIN_CHANNEL_ID")
        try:
            await bot.unban_chat_member(main_channel_id, user_id, only_if_banned=True)
            await msg.answer("✅ Вы разблокированы в канале!")
        except Exception:
            logger.exception(f"Не удалось снять блокировку канала для пользователя {user_id}")
            await msg.answer(
                "⚠️ Оплата прошла, но снять блокировку в канале автоматически не удалось "
                "— напишите администрации, мы поправим вручную."
            )
        return

    if scope == "comments":
        comments_chat_id = get_setting("COMMENTS_CHAT_ID")
        try:
            await bot.unban_chat_member(comments_chat_id, user_id, only_if_banned=True)
            await msg.answer("✅ Вы разблокированы в комментариях!")
        except Exception:
            logger.exception(f"Не удалось снять блокировку комментариев для пользователя {user_id}")
            await msg.answer(
                "⚠️ Оплата прошла, но снять блокировку в комментариях автоматически не удалось "
                "— напишите администрации, мы поправим вручную."
            )
        return
