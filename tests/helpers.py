"""Конструкторы апдейтов Telegram для сквозных тестов."""
import itertools
from datetime import datetime

from aiogram.types import (
    CallbackQuery, Chat, Message, PreCheckoutQuery, SuccessfulPayment, Update, User,
)

_ids = itertools.count(1)


def user(uid: int, username: str | None = None) -> User:
    return User(id=uid, is_bot=False, first_name=f"User{uid}", username=username)


def private_chat(uid: int) -> Chat:
    return Chat(id=uid, type="private")


def message_update(uid: int, text: str | None = None, chat: Chat | None = None, **extra) -> Update:
    msg = Message(
        message_id=next(_ids), date=datetime.now(), chat=chat or private_chat(uid),
        from_user=user(uid), text=text, **extra,
    )
    return Update(update_id=next(_ids), message=msg)


def callback_update(uid: int, data: str, chat: Chat | None = None, message_text: str = "x") -> Update:
    msg = Message(message_id=next(_ids), date=datetime.now(), chat=chat or private_chat(uid), text=message_text)
    cb = CallbackQuery(id=str(next(_ids)), from_user=user(uid), chat_instance="ci", data=data, message=msg)
    return Update(update_id=next(_ids), callback_query=cb)


def payment_update(uid: int, payload: str, amount: int = 50) -> Update:
    sp = SuccessfulPayment(
        currency="XTR", total_amount=amount, invoice_payload=payload,
        telegram_payment_charge_id=f"charge_{next(_ids)}", provider_payment_charge_id="",
    )
    return message_update(uid, successful_payment=sp)


def pre_checkout_update(uid: int, payload: str, amount: int = 50) -> Update:
    q = PreCheckoutQuery(id=str(next(_ids)), from_user=user(uid), currency="XTR",
                         total_amount=amount, invoice_payload=payload)
    return Update(update_id=next(_ids), pre_checkout_query=q)
