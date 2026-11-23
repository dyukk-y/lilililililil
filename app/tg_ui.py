"""Новые возможности Telegram Bot API (10.x) с безопасным откатом.

Всё здесь устроено так, чтобы при любой проблеме (старый клиент, функция
недоступна, Telegram вернул ошибку) бот вёл себя как раньше, а не падал:

- цветные кнопки (style: success/danger/primary) — старые клиенты просто
  покажут обычную кнопку;
- <tg-time> — дата/время в часовом поясе читателя; внутри всегда есть
  запасной текст по Новосибирску;
- <blockquote expandable> — сворачиваемые цитаты;
- кнопка «Скопировать» (copy_text);
- реакции бота на сообщения пользователя;
- эффекты сообщений (🎉) — при ошибке сообщение уходит без эффекта;
- «богатые» сообщения (таблицы, заголовки) — при ошибке обычный HTML.
"""
import logging
from datetime import datetime
from typing import Optional

from aiogram.types import (
    CopyTextButton, InlineKeyboardButton, InputRichMessage, ReactionTypeEmoji,
)

from app.config import TIMEZONE
from app.utils import h

logger = logging.getLogger(__name__)

# Идентификаторы стандартных эффектов сообщений Telegram.
EFFECT_CONFETTI = "5046509860389126442"  # 🎉
EFFECT_FIRE = "5104841245755180586"      # 🔥

SUCCESS, DANGER, PRIMARY = "success", "danger", "primary"


def btn(text: str, callback_data: str | None = None, *, url: str | None = None,
        style: str | None = None, copy: str | None = None) -> InlineKeyboardButton:
    """Кнопка с необязательным цветом и действием «скопировать»."""
    kwargs = {"text": text, "style": style}
    if copy is not None:
        kwargs["copy_text"] = CopyTextButton(text=str(copy)[:256])
    elif url is not None:
        kwargs["url"] = url
    else:
        kwargs["callback_data"] = callback_data
    return InlineKeyboardButton(**kwargs)


def tg_time(dt: datetime, fmt: str = "%d.%m.%Y %H:%M", suffix: str = "") -> str:
    """Дата/время, которое Telegram покажет в часовом поясе читателя.
    Наивные datetime считаются временем TIMEZONE (так они хранятся в БД)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TIMEZONE)
    fallback = dt.astimezone(TIMEZONE).strftime(fmt) + suffix
    return f'<tg-time unix="{int(dt.timestamp())}">{h(fallback)}</tg-time>'


def tg_time_iso(value: Optional[str], fmt: str = "%d.%m.%Y %H:%M") -> str:
    if not value:
        return "—"
    try:
        return tg_time(datetime.fromisoformat(value), fmt)
    except (TypeError, ValueError):
        return h(value)


def expandable(inner_html: str) -> str:
    """Сворачиваемая цитата: длинный текст не занимает весь экран."""
    return f"<blockquote expandable>{inner_html}</blockquote>"


async def react(bot, chat_id: int, message_id: int, emoji: str) -> None:
    """Реакция бота на сообщение. Ошибки игнорируются — это украшение."""
    try:
        await bot.set_message_reaction(
            chat_id=chat_id, message_id=message_id, reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception as e:
        logger.debug("Реакция %s не поставлена: %s", emoji, e)


async def send_with_effect(bot, chat_id: int, text: str, effect_id: str = EFFECT_CONFETTI, **kwargs):
    """Сообщение с анимацией. Эффекты работают только в личных чатах — при
    ошибке отправляем то же сообщение без эффекта."""
    try:
        return await bot.send_message(chat_id, text, message_effect_id=effect_id, **kwargs)
    except Exception as e:
        logger.debug("Эффект не применён: %s", e)
        return await bot.send_message(chat_id, text, **kwargs)


async def edit_rich(message, rich_html: str, fallback_html: str, reply_markup=None) -> None:
    """Показывает «богатое» сообщение (заголовки, таблицы). Если Telegram
    его не принял — показывает обычный HTML-вариант."""
    from app.runtime_settings import get as get_setting
    if get_setting("RICH_MESSAGES"):
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id, message_id=message.message_id,
                rich_message=InputRichMessage(html=rich_html), parse_mode=None,
                reply_markup=reply_markup,
            )
            return
        except Exception as e:
            if "message is not modified" in str(e):
                return
            logger.info("Rich message не принят, показываю обычный текст: %s", e)
    await message.edit_text(fallback_html, parse_mode="HTML", reply_markup=reply_markup)


def rich_table(rows: list[tuple[str, str]], header: tuple[str, str] | None = None) -> str:
    """HTML-таблица для «богатого» сообщения из пар (подпись, значение)."""
    parts = ["<table bordered striped>"]
    if header:
        parts.append(f"<tr><th>{h(header[0])}</th><th>{h(header[1])}</th></tr>")
    for label, value in rows:
        parts.append(f"<tr><td>{h(label)}</td><td align=\"right\">{h(value)}</td></tr>")
    parts.append("</table>")
    return "".join(parts)


async def show_screen(message, text: str, reply_markup=None, parse_mode: str = "HTML") -> None:
    """Перерисовывает экран меню. Если сообщение нельзя отредактировать
    (например, оно было «богатым» или с фото) — удаляет его и присылает
    экран новым сообщением, чтобы навигация никогда не «ломалась»."""
    from aiogram.exceptions import TelegramBadRequest
    try:
        await message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
