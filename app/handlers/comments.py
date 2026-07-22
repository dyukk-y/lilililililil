"""Учёт комментариев пользователей в отдельном чате комментариев (COMMENTS_CHAT_ID).

Не модерирует и не отвечает на сообщения — только считает активность для
раздела "Профиль" (сколько комментариев пользователь оставил под постами).

ID чата комментариев — динамическая настройка (может меняться из
админ-панели без перезапуска), поэтому используем асинхронный
callable-фильтр вместо `F.chat.id == COMMENTS_CHAT_ID`: значения
магических фильтров в aiogram вычисляются один раз при регистрации
хендлера и не отслеживают изменения "на лету", а callable-фильтр
вызывается заново на каждое сообщение.
"""
from aiogram import Router
from aiogram.types import Message

from app.loader import logger
from app.runtime_settings import get as get_setting
from app.database import increment_comment_count, register_user, log
from app.keyboards import first_comment_keyboard

router = Router()


async def _is_comments_chat(message: Message) -> bool:
    comments_chat_id = get_setting("COMMENTS_CHAT_ID")
    return bool(comments_chat_id) and message.chat.id == comments_chat_id


async def _is_channel_auto_forward(message: Message) -> bool:
    """True, если это автоматическая копия поста основного канала,
    которую Telegram сам разместил в связанном чате комментариев (именно
    так Telegram связывает канал с группой обсуждений). Под таким
    сообщением бот и оставляет самый первый комментарий."""
    if not message.is_automatic_forward:
        return False
    if not await _is_comments_chat(message):
        return False

    main_channel_id = get_setting("MAIN_CHANNEL_ID")
    origin = getattr(message, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None) if origin else None
    if origin_chat is not None and main_channel_id and origin_chat.id != main_channel_id:
        return False
    return True


@router.message(_is_channel_auto_forward)
async def post_first_comment(msg: Message) -> None:
    """Оставляет первым комментарием под свежим постом канала напоминание
    о вежливости + кнопки (см. FIRST_COMMENT_* в app/runtime_settings.py,
    редактируются супер-админами через админ-панель)."""
    try:
        await msg.reply(
            get_setting("FIRST_COMMENT_TEXT"),
            parse_mode="HTML",
            reply_markup=first_comment_keyboard(),
            disable_web_page_preview=True,
        )
        await log("first_comment", f"greeting comment posted under message {msg.message_id}")
    except Exception as e:
        logger.warning(f"Не удалось оставить первый комментарий под постом: {e}")


@router.message(_is_comments_chat)
async def track_comment(msg: Message) -> None:
    if not msg.from_user or msg.from_user.is_bot:
        return
    # register_user на случай, если человек пишет в комментариях, ни разу
    # не открыв бота напрямую — иначе счётчик увеличится для "несуществующего"
    # в users пользователя и не будет виден нигде.
    await register_user(msg.from_user)
    await increment_comment_count(msg.from_user.id)
