"""Небольшие общие помощники для работы с Telegram-разметкой.

Бот по умолчанию отправляет все сообщения с parse_mode=HTML (см.
app/loader.py), поэтому любой текст, пришедший от пользователя (текст
поста, причина отказа/бана, имя, ключевое слово и т.д.), обязательно нужно
экранировать — иначе символы ``<``, ``>`` или ``&`` ломают отправку
сообщения целиком ("can't parse entities").
"""
from html import escape
from typing import Any, Optional

from aiogram.exceptions import TelegramBadRequest

# Лимиты Telegram Bot API (в символах после разбора разметки).
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024


def h(value: Any) -> str:
    """HTML-экранирование, безопасное для None и не-строк."""
    if value is None:
        return ""
    return escape(str(value), quote=False)


def shorten(value: Optional[str], limit: int, suffix: str = "…") -> str:
    """Обрезает СЫРОЙ (ещё не экранированный) текст до limit символов."""
    value = value or ""
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - len(suffix))] + suffix


async def edit_text_or_caption(message, text: str, **kwargs) -> None:
    """Редактирует сообщение независимо от того, текстовое оно или с медиа.

    ``Message.edit_text`` падает на сообщениях с фото ("there is no text in
    the message to edit"), а ``edit_caption`` — на текстовых. Если сообщение
    вообще нельзя отредактировать (слишком старое/удалено), отправляем
    новое сообщение в тот же чат, чтобы пользователь не остался без ответа.
    """
    try:
        if message.photo or message.video or message.document or message.animation:
            await message.edit_caption(caption=text, **kwargs)
        else:
            await message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        await message.answer(text, **kwargs)


def progress_bar(value: int, total: int, width: int = 10) -> str:
    """Текстовый индикатор: ▰▰▰▱▱▱▱▱▱▱"""
    if total <= 0:
        return "▱" * width
    filled = max(0, min(width, round(width * value / total)))
    return "▰" * filled + "▱" * (width - filled)


def main_menu_text(first_name: str | None = None) -> str:
    name = h(first_name) if first_name else "друг"
    return (
        f"👋 Привет, <b>{name}</b>!\n\n"
        "Здесь можно предложить пост для публикации в канале.\n\n"
        "<blockquote>⚠️ В посте нужен эмодзи, о ком он: 🧑 парень или 👩 девушка</blockquote>\n\n"
        "Выберите действие 👇"
    )
