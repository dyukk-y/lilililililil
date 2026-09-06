"""
Поиск автора поста (платная расшифровка через Telegram Stars).

Флоу: пользователь присылает ссылку на пост/пересылает его -> бот находит
автора в базе и присылает замаскированные ID/юзернейм с кнопкой
"Расшифровать" -> оплата AUTHOR_LOOKUP_PRICE_STARS звёзд (нативные Telegram
Stars, currency=XTR, отдельный провайдер не нужен) -> после успешной оплаты
данные раскрываются прямо в том же сообщении.

Если пользователь уже покупал расшифровку этого же поста раньше — повторно
платить не нужно (см. database.get_star_purchase).

Обработка pre_checkout_query/successful_payment централизована в
handlers/payments.py (Telegram не даёт нескольким роутерам делить между
собой один и тот же платёж — обработчик должен быть один на весь проект),
поэтому здесь только формирование инвойса и функция complete_author_reveal,
которую payments.py вызывает после успешной оплаты.
"""
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, LabeledPrice
from aiogram.fsm.context import FSMContext

from app.runtime_settings import get as get_setting
from app.database import (
    is_banned, get_post_by_channel_message_id, get_post_by_id,
    get_username_by_user_id, get_star_purchase, record_star_purchase,
)
from app.keyboards import cancel_to_menu_keyboard, menu_btn, author_reveal_keyboard, author_revealed_keyboard
from app.states import AuthorLookupState
from app.services import resolve_channel_post_id, mask_user_id, mask_username

router = Router()


def _lookup_text(author_id: int, author_username, revealed: bool) -> str:
    if revealed:
        title = "🔓 Данные автора расшифрованы"
        id_str = str(author_id)
        username_str = f"@{author_username}" if author_username else "не установлен"
    else:
        title = "🔒 Я нашёл автора поста"
        id_str = mask_user_id(author_id)
        username_str = mask_username(author_username)

    return (
        f"{title}\n\n"
        f"🆔 ID: <code>{id_str}</code>\n"
        f"📛 Юзернейм: {username_str}"
    )


async def present_author_lookup(msg: Message, post_id: int) -> None:
    """Показывает информацию об авторе поста (замаскированную, либо сразу
    открытую — если это свой пост или уже куплено). Используется и в
    обычном флоу (после ввода ссылки на пост), и из deep-link кнопки
    "🔎 Узнать автора" под первым комментарием в группе обсуждений —
    поведение полностью идентично."""
    post = await get_post_by_id(post_id)
    if not post:
        return await msg.answer("❌ Пост не найден.", reply_markup=menu_btn())

    author_id = post[1]

    if author_id == msg.from_user.id:
        return await msg.answer(
            "🙂 Это ваш собственный пост — вы его автор.",
            reply_markup=menu_btn()
        )

    author_username = await get_username_by_user_id(author_id)

    already_purchased = await get_star_purchase(msg.from_user.id, post_id, "author_reveal")
    if already_purchased:
        return await msg.answer(
            _lookup_text(author_id, author_username, revealed=True),
            parse_mode='HTML',
            reply_markup=author_revealed_keyboard()
        )

    price = get_setting("AUTHOR_LOOKUP_PRICE_STARS")
    await msg.answer(
        _lookup_text(author_id, author_username, revealed=False)
        + f"\n\n💰 Расшифровка стоит {price} ⭐",
        parse_mode='HTML',
        reply_markup=author_reveal_keyboard(post_id)
    )


@router.callback_query(F.data == "author_lookup_start")
async def author_lookup_start(cb: CallbackQuery, state: FSMContext):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)

    await state.set_state(AuthorLookupState.wait_post_link)
    await cb.message.edit_text(
        "🔎 Узнать автора поста\n\n"
        "Пришлите ссылку на пост (вида <code>https://t.me/канал/123</code>) "
        "или перешлите этот пост сюда из канала.",
        parse_mode='HTML',
        reply_markup=cancel_to_menu_keyboard()
    )


@router.message(AuthorLookupState.wait_post_link)
async def author_lookup_receive_link(msg: Message, state: FSMContext):
    if await is_banned(msg.from_user.id):
        return await msg.answer("🚫 Вы заблокированы.")

    channel_message_id = await resolve_channel_post_id(msg)
    if not channel_message_id:
        return await msg.answer(
            "❌ Не удалось распознать пост. Пришлите ссылку вида "
            "<code>https://t.me/канал/123</code> или перешлите сообщение прямо из канала.",
            parse_mode='HTML',
            reply_markup=cancel_to_menu_keyboard()
        )

    post = await get_post_by_channel_message_id(channel_message_id)
    if not post:
        return await msg.answer(
            "❌ Пост не найден среди опубликованных.",
            reply_markup=cancel_to_menu_keyboard()
        )

    await state.clear()
    await present_author_lookup(msg, post[0])


@router.callback_query(F.data.startswith("reveal_author_"))
async def reveal_author(cb: CallbackQuery):
    if await is_banned(cb.from_user.id):
        return await cb.answer("🚫 Вы заблокированы.", show_alert=True)

    post_id = int(cb.data.split("_")[2])
    post = await get_post_by_id(post_id)
    if not post:
        return await cb.answer("❌ Пост не найден.", show_alert=True)

    author_id = post[1]

    if author_id == cb.from_user.id:
        author_username = await get_username_by_user_id(author_id)
        await cb.message.edit_text(
            _lookup_text(author_id, author_username, revealed=True),
            parse_mode='HTML',
            reply_markup=author_revealed_keyboard()
        )
        return await cb.answer()

    already_purchased = await get_star_purchase(cb.from_user.id, post_id, "author_reveal")
    if already_purchased:
        author_username = await get_username_by_user_id(author_id)
        await cb.message.edit_text(
            _lookup_text(author_id, author_username, revealed=True),
            parse_mode='HTML',
            reply_markup=author_revealed_keyboard()
        )
        return await cb.answer()

    await cb.answer()
    price = get_setting("AUTHOR_LOOKUP_PRICE_STARS")
    payload = f"reveal|{post_id}|{cb.message.chat.id}|{cb.message.message_id}"
    await cb.message.bot.send_invoice(
        chat_id=cb.message.chat.id,
        title="Информация об авторе поста",
        description=f"Расшифровка ID и юзернейма автора поста #{post_id}",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Расшифровка автора", amount=price)],
    )


async def complete_author_reveal(msg: Message, post_id: int, chat_id: int, message_id: int) -> None:
    """Вызывается из handlers/payments.py после успешной оплаты."""
    payment = msg.successful_payment
    await record_star_purchase(
        msg.from_user.id, "author_reveal", post_id, payment.total_amount, payment.telegram_payment_charge_id
    )

    post = await get_post_by_id(post_id)
    if not post:
        await msg.answer("⚠️ Оплата прошла, но пост не найден — свяжитесь с администрацией.")
        return

    author_id = post[1]
    author_username = await get_username_by_user_id(author_id)

    try:
        await msg.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=_lookup_text(author_id, author_username, revealed=True),
            parse_mode='HTML',
            reply_markup=author_revealed_keyboard()
        )
    except Exception:
        # если исходное сообщение отредактировать не удалось (например, слишком
        # старое) — присылаем результат новым сообщением, оплата не пропадёт
        await msg.answer(
            _lookup_text(author_id, author_username, revealed=True),
            parse_mode='HTML',
            reply_markup=author_revealed_keyboard()
        )

    await msg.answer("✅ Оплата получена, данные расшифрованы выше.", reply_markup=menu_btn())
