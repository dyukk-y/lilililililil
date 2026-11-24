"""Нативные «предложенные посты» Telegram (личные сообщения канала).

Подписчик предлагает пост прямо в личных сообщениях канала — в том числе
платно, со звёздами. Такой пост проходит ТОТ ЖЕ конвейер, что и посты
из бота: стоп-слова, правила, ИИ, автопилот, карточка модераторам,
готовые причины отказа, сроки. Отличается только финал:
- одобрение → approve_suggested_post: пост публикует сам Telegram
  (и списывает звёзды за платное предложение);
- отказ → decline_suggested_post с комментарием автору.

Что нужно включить один раз:
1. В канале — личные сообщения (Настройки канала → Личные сообщения).
2. Бот — администратор канала с правами «Публикация сообщений» и
   «Управление личными сообщениями».
"""
import logging

from aiogram import F, Router
from aiogram.types import Message

from app.autoban import register_violation
from app.database import (
    count_similar_posts, create_suggested_post, get_post_by_dm_message, is_banned,
    is_in_publication_blacklist, log, mark_suggested_failed, posts_today, register_user,
    try_finalize_post,
)
from app.runtime_settings import get as get_setting
from app.text_quality import detect_gibberish
from app.validators import validate_post_text
from app.emoji_rules import has_person_emoji

logger = logging.getLogger(__name__)
router = Router()

_dm_parent_cache: dict[int, bool] = {}


async def is_our_channel_dm(chat) -> bool:
    """Личный чат именно нашего канала (а не чужого, куда добавили бота)."""
    if not getattr(chat, "is_direct_messages", False):
        return False
    if chat.id in _dm_parent_cache:
        return _dm_parent_cache[chat.id]
    try:
        from app.loader import bot
        full = await bot.get_chat(chat.id)
        parent = getattr(full, "parent_chat", None)
        ok = bool(parent) and parent.id == get_setting("MAIN_CHANNEL_ID")
    except Exception:
        logger.warning("Не удалось определить канал для личного чата %s", chat.id, exc_info=True)
        return False
    _dm_parent_cache[chat.id] = ok
    return ok


async def decline(msg: Message, comment: str) -> None:
    try:
        await msg.bot.decline_suggested_post(chat_id=msg.chat.id, message_id=msg.message_id, comment=comment[:128])
    except Exception:
        logger.warning("Не удалось отклонить предложенный пост %s", msg.message_id, exc_info=True)


async def _precheck(msg: Message, text: str, has_photo: bool) -> str | None:
    """Те же жёсткие правила, что и для постов из бота. Текст ошибки — автору."""
    user_id = msg.from_user.id
    if await is_banned(user_id):
        return "Вы заблокированы в боте канала."
    if await posts_today(user_id) >= get_setting("USER_DAILY_POST_LIMIT"):
        return f"Лимит: {get_setting('USER_DAILY_POST_LIMIT')} постов в день."
    blacklisted, _ = await is_in_publication_blacklist(text)
    if blacklisted:
        await register_violation(msg.bot, user_id, "blacklist")
        return "Текст содержит запрещённые слова."
    ok, error = validate_post_text(text, has_photo=has_photo)
    if not ok and not has_person_emoji(text):
        return "Добавьте в пост эмодзи 🧑 (парень) или 👩 (девушка) — другие не подходят."
    if not ok:
        import re
        return re.sub(r"<[^>]+>", "", error).replace("❌", "").strip().split("\n")[0]
    if detect_gibberish(text)[0]:
        await register_violation(msg.bot, user_id, "gibberish")
        return "Текст не похож на осмысленное сообщение."
    if await count_similar_posts(text) >= get_setting("DUPLICATE_REPEAT_LIMIT"):
        await register_violation(msg.bot, user_id, "duplicate")
        return "Такой текст уже присылали слишком много раз."
    return None


@router.message(F.chat.is_direct_messages, F.suggested_post_info)
async def on_suggested_post(msg: Message):
    info = msg.suggested_post_info
    if info.state != "pending" or not msg.from_user or msg.from_user.is_bot:
        return
    if not await is_our_channel_dm(msg.chat):
        return
    if msg.video or msg.animation or msg.document or msg.audio or msg.voice or msg.sticker:
        return await decline(msg, "Канал принимает только текст или одно фото с подписью.")

    text = msg.text or msg.caption or ""
    photo = msg.photo[-1].file_id if msg.photo else None
    await register_user(msg.from_user)

    error = await _precheck(msg, text, has_photo=bool(photo))
    if error:
        await log("suggested_declined", f"user {msg.from_user.id}: {error}")
        return await decline(msg, error)

    price = info.price.amount if info.price else None
    post_id = await create_suggested_post(msg.from_user.id, text, photo, msg.chat.id, msg.message_id, price)
    if post_id is None:
        return  # повторная доставка того же сообщения
    await log("suggested_post", f"post #{post_id} from user {msg.from_user.id} (price={price})")

    from app.services import route_new_post
    try:
        await route_new_post(post_id)
    except Exception:
        # Пост в БД — recover_pending_posts доведёт его при следующем запуске.
        logger.exception("Не удалось маршрутизировать предложенный пост #%s", post_id)


async def _post_of(service_obj) -> int | None:
    original = getattr(service_obj, "suggested_post_message", None)
    if original is None:
        return None
    return await get_post_by_dm_message(original.chat.id, original.message_id)


@router.message(F.chat.is_direct_messages, F.suggested_post_approval_failed)
async def on_approval_failed(msg: Message):
    """Telegram не смог опубликовать одобренный пост (например, у автора не
    хватило звёзд для платного предложения)."""
    post_id = await _post_of(msg.suggested_post_approval_failed)
    if post_id:
        await mark_suggested_failed(post_id, "Telegram не смог опубликовать (оплата не прошла)")
        await log("suggested_failed", f"post #{post_id}: approval failed")


@router.message(F.chat.is_direct_messages, F.suggested_post_approved)
async def on_approved_elsewhere(msg: Message):
    """Админ одобрил предложение вручную в приложении Telegram — синхронизируем."""
    post_id = await _post_of(msg.suggested_post_approved)
    if post_id and await try_finalize_post(post_id, 0, "published", None,
                                           from_statuses=("moderation", "approved")):
        from app.services import _disable_moderator_card
        await _disable_moderator_card(post_id, "published")
        await log("suggested_synced", f"post #{post_id}: approved in Telegram app")


@router.message(F.chat.is_direct_messages, F.suggested_post_declined)
async def on_declined_elsewhere(msg: Message):
    post_id = await _post_of(msg.suggested_post_declined)
    if post_id and await try_finalize_post(post_id, 0, "rejected", "Отклонено в приложении Telegram",
                                           from_statuses=("moderation", "approved")):
        from app.services import _disable_moderator_card
        await _disable_moderator_card(post_id, "rejected")
        await log("suggested_synced", f"post #{post_id}: declined in Telegram app")


@router.message(F.chat.is_direct_messages)
async def on_other_dm_message(msg: Message):
    """Обычные сообщения в личке канала (не предложения) бот не трогает."""
    return
