"""Обработка сообщений в чате комментариев (COMMENTS_CHAT_ID):

1. Учёт активности пользователей — счётчик комментариев в профиле.
2. Обнаружение автоматической пересылки нового поста из канала (Telegram
   создаёт её сама, когда к каналу подключена группа обсуждений) — под ней
   бот сажает первый комментарий "Будьте вежливы..." с кнопками
   (см. services.handle_channel_auto_forward).

ID чата комментариев — динамическая настройка (может меняться из
админ-панели без перезапуска), поэтому используем асинхронный
callable-фильтр вместо `F.chat.id == COMMENTS_CHAT_ID`: значения
магических фильтров в aiogram вычисляются один раз при регистрации
хендлера и не отслеживают изменения "на лету", а callable-фильтр
вызывается заново на каждое сообщение.
"""
from aiogram import Router
from aiogram.types import Message

from app.runtime_settings import get as get_setting
from app.database import increment_comment_count, register_user
from app.services import handle_channel_auto_forward

router = Router()


async def _is_comments_chat(message: Message) -> bool:
    comments_chat_id = get_setting("COMMENTS_CHAT_ID")
    return bool(comments_chat_id) and message.chat.id == comments_chat_id


@router.message(_is_comments_chat)
async def on_comments_chat_message(msg: Message) -> None:
    # Автоматическая пересылка поста из канала — сажаем первый комментарий.
    # У таких сообщений либо нет msg.from_user, либо это служебный
    # отправитель — их точно не нужно засчитывать как "комментарий
    # пользователя" ниже, поэтому проверяем это первым и выходим.
    if getattr(msg, "is_automatic_forward", False):
        await handle_channel_auto_forward(msg)
        return

    if not msg.from_user or msg.from_user.is_bot:
        return
    # register_user на случай, если человек пишет в комментариях, ни разу
    # не открыв бота напрямую — иначе счётчик увеличится для "несуществующего"
    # в users пользователя и не будет виден нигде.
    await register_user(msg.from_user)
    await increment_comment_count(msg.from_user.id)
