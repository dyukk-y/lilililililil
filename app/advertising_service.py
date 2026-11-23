"""Публикация рекламного поста — единая логика для «опубликовать сейчас»
и для публикации по расписанию (advertising_expiry_loop).

Раньше вся публикация жила внутри обработчика кнопки и зависела от
данных диалога (FSM) — запланировать публикацию на потом было нельзя.
Теперь всё нужное хранится в БД (advertising_posts / advertising_broadcasts),
и опубликовать рекламу может как админ, так и фоновая задача.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.config import TIMEZONE
from app.database import (
    activate_advertising_subscription, get_ad, log, publish_advertising_post, set_advertising_error,
)
from app.loader import bot
from app.runtime_settings import get as get_setting

logger = logging.getLogger(__name__)

COMBOS = {
    "24": {"label": "24 часа", "duration": 24, "pin": 24, "subscription": 0, "broadcasts": 0},
    "48": {"label": "48 часов", "duration": 48, "pin": 48, "subscription": 0, "broadcasts": 0},
    "72": {"label": "72 часа", "duration": 72, "pin": 72, "subscription": 0, "broadcasts": 1},
    "week": {"label": "Неделя", "duration": 168, "pin": 72, "subscription": 24, "broadcasts": 0},
    "week_plus": {"label": "Неделя+", "duration": 168, "pin": 72, "subscription": 72, "broadcasts": 3},
}


def hours_label(hours: int) -> str:
    if hours and hours % 24 == 0 and hours >= 48:
        days = hours // 24
        return "7 дней" if days == 7 else f"{days} дн."
    return f"{hours} ч."


def pin_label(hours: int) -> str:
    return hours_label(hours) if hours else "без закрепа"


@dataclass
class PublishResult:
    ok: bool
    channel_message_id: int | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


async def publish_ad(ad_id: int) -> PublishResult:
    """Публикует черновик/запланированную рекламу: копия предпросмотра в
    канал, закреп, временная обязательная подписка. Идемпотентно: повторный
    вызов для уже опубликованной рекламы ничего не делает."""
    ad = await get_ad(ad_id)
    if not ad or ad["status"] not in ("draft", "scheduled"):
        return PublishResult(False, error="already_processed")

    channel_id = get_setting("MAIN_CHANNEL_ID")
    source_chat = ad["preview_chat_id"] or ad["source_chat_id"]
    source_msg = ad["preview_message_id"] or ad["source_message_id"]
    try:
        copied = await bot.copy_message(chat_id=channel_id, from_chat_id=source_chat, message_id=source_msg)
    except Exception as e:
        logger.exception("Реклама #%s: не удалось скопировать пост в канал", ad_id)
        await set_advertising_error(ad_id, str(e))
        return PublishResult(False, error=str(e))

    pin_hours = int(ad["pin_duration_hours"] or 0)
    if not await publish_advertising_post(ad_id, copied.message_id, pin_hours):
        # Кто-то опубликовал параллельно — убираем дубль из канала.
        try:
            await bot.delete_message(channel_id, copied.message_id)
        except Exception:
            pass
        return PublishResult(False, error="already_processed")

    result = PublishResult(True, channel_message_id=copied.message_id)
    if pin_hours:
        try:
            await bot.pin_chat_message(channel_id, copied.message_id, disable_notification=True)
        except Exception as e:
            result.warnings.append("не удалось закрепить")
            logger.warning("Реклама #%s: не удалось закрепить: %s", ad_id, e)

    combo = COMBOS.get(ad["combo_key"] or "")
    if combo and combo["subscription"]:
        try:
            if not await activate_advertising_subscription(ad_id, datetime.now(TIMEZONE)):
                result.warnings.append("обязательная подписка не активирована")
        except Exception:
            result.warnings.append("обязательная подписка не активирована")
            logger.exception("Реклама #%s: ошибка активации подписки", ad_id)

    await log("advertising_publish", f"ad #{ad_id}: {ad['ad_type']}, {ad['duration_hours']}h, pin={pin_hours}h")
    return result
