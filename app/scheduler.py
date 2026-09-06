"""
Планировщик автопубликации, ребалансировки дневной квоты и резервного
копирования БД.

Ничего не хранится в памяти процесса: расписание публикаций живёт в колонке
posts.scheduled_time, поэтому при падении и перезапуске бота ничего не
"забывается" — фоновый цикл просто продолжает проверять БД по таймеру.

Все "тонкие" параметры (окно публикации, дневной лимит, зазор между
слотами, интервал бэкапа) читаются через app.runtime_settings.get() КАЖДЫЙ
раз заново — это позволяет супер-администраторам менять их прямо в
админ-панели без перезапуска бота (см. handlers/settings_admin.py).
"""
import asyncio
import logging
import os
import random
import shutil
from datetime import datetime, timedelta
from datetime import time as dt_time
from typing import Awaitable, Callable, Optional

import aiosqlite

from app.config import DB_NAME, TIMEZONE, BACKUP_DIR
from app.runtime_settings import get as get_setting
from app.database import (
    get_queue_count_for_date, get_scheduled_times_for_date,
    get_published_count_for_date, get_due_posts, get_future_scheduled_posts,
    approve_and_schedule,
)

logger = logging.getLogger(__name__)


def _window_bounds(target_date, now: Optional[datetime] = None):
    """Возвращает (нижняя граница слота, конец окна) для указанной даты,
    с учётом того, что для СЕГОДНЯ нижняя граница не может быть раньше
    текущего момента (+2 минуты про запас)."""
    window_start = datetime.combine(
        target_date, dt_time(get_setting("PUBLISH_WINDOW_START_HOUR"), 0), tzinfo=TIMEZONE
    )
    window_end = datetime.combine(
        target_date, dt_time(get_setting("PUBLISH_WINDOW_END_HOUR"), get_setting("PUBLISH_WINDOW_END_MINUTE")),
        tzinfo=TIMEZONE,
    )
    if window_end <= window_start:
        window_end += timedelta(days=1)
    lower_bound = window_start
    if now is not None and target_date == now.date():
        lower_bound = max(window_start, now + timedelta(minutes=2))
    return lower_bound, window_end


async def _pick_slot_within_date(target_date, now: datetime) -> Optional[datetime]:
    """Подбирает случайный свободный слот СТРОГО в пределах указанной даты
    (без проверки дневного лимита — это ответственность вызывающего кода).
    None, если в этот день уже физически не осталось места/времени."""
    lower_bound, window_end = _window_bounds(target_date, now)
    if lower_bound >= window_end:
        return None

    date_str = target_date.isoformat()
    min_gap_seconds = get_setting("MIN_SLOT_GAP_MINUTES") * 60
    existing = [
        datetime.fromisoformat(t).replace(tzinfo=TIMEZONE)
        for t in await get_scheduled_times_for_date(date_str)
    ]

    span_seconds = int((window_end - lower_bound).total_seconds())
    for _ in range(80):
        candidate = lower_bound + timedelta(seconds=random.randint(0, span_seconds))
        if all(abs((candidate - e).total_seconds()) >= min_gap_seconds for e in existing):
            return candidate

    return None


async def pick_schedule_slot() -> datetime:
    """Подбирает случайное время публикации в пределах окна (по умолчанию
    8:00-23:59 по Новосибирску), учитывая дневной лимит публикаций и
    минимальный зазор между уже занятыми слотами. Если день заполнен —
    переносит на ближайший свободный день."""
    now = datetime.now(TIMEZONE)
    target_date = now.date()

    _, window_end_today = _window_bounds(target_date, now=None)
    if now >= window_end_today:
        target_date += timedelta(days=1)

    for _ in range(366):  # защита от неожиданного бесконечного цикла
        date_str = target_date.isoformat()
        queued = await get_queue_count_for_date(date_str)
        published = await get_published_count_for_date(date_str)

        if queued + published >= get_setting("DAILY_PUBLISH_LIMIT"):
            target_date += timedelta(days=1)
            continue

        slot = await _pick_slot_within_date(target_date, now)
        if slot is not None:
            return slot

        target_date += timedelta(days=1)

    return datetime.now(TIMEZONE) + timedelta(hours=1)


async def publisher_loop(publish_func: Callable[[int], Awaitable[None]]) -> None:
    """Фоновая задача: раз в 30 секунд публикует посты, время которых подошло."""
    while True:
        try:
            due_ids = await get_due_posts()
            for post_id in due_ids:
                try:
                    await publish_func(post_id)
                except Exception:
                    logger.exception(f"Ошибка автопубликации поста #{post_id}")
        except Exception:
            logger.exception("Ошибка в цикле автопубликации")
        await asyncio.sleep(30)


async def rebalance_daily_queue() -> int:
    """Если сегодня опубликовано+запланировано меньше дневного лимита
    (например, из-за удалённых постов или переноса на другие дни при
    перегрузке очереди) — подтягивает на сегодня ближайшие по времени
    посты, изначально запланированные на будущие дни. Возвращает, сколько
    постов было перенесено."""
    now = datetime.now(TIMEZONE)
    today = now.date()

    _, window_end_today = _window_bounds(today, now=None)
    if now >= window_end_today:
        return 0  # окно публикации на сегодня уже закрыто, переносить некуда

    today_str = today.isoformat()
    daily_limit = get_setting("DAILY_PUBLISH_LIMIT")
    moved = 0

    while True:
        published_today = await get_published_count_for_date(today_str)
        queued_today = await get_queue_count_for_date(today_str)
        shortfall = daily_limit - (published_today + queued_today)
        if shortfall <= 0:
            break

        candidates = await get_future_scheduled_posts(today_str, limit=shortfall)
        if not candidates:
            break

        progressed = False
        for post_id in candidates:
            slot = await _pick_slot_within_date(today, now)
            if slot is None:
                break
            await approve_and_schedule(post_id, slot)
            moved += 1
            progressed = True
            logger.info(f"Ребалансировка очереди: пост #{post_id} перенесён на сегодня ({slot.isoformat()})")

        if not progressed:
            break

    return moved


async def rebalance_loop() -> None:
    """Фоновая задача: периодически проверяет, не образовалась ли нехватка
    публикаций за сегодня (удаление поста, недобор постов днём и т.п.), и
    подтягивает посты с будущих дней, чтобы дневная квота выполнялась."""
    while True:
        try:
            moved = await rebalance_daily_queue()
            if moved:
                logger.info(f"Ребалансировка очереди: перенесено на сегодня {moved} пост(ов)")
        except Exception:
            logger.exception("Ошибка в цикле ребалансировки дневной квоты")
        await asyncio.sleep(900)  # каждые 15 минут


async def make_backup() -> str:
    """Делает checkpoint WAL-журнала и копирует файл БД в BACKUP_DIR,
    ротируя старые копии (оставляя последние BACKUP_KEEP_LAST)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        await db.commit()

    timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"smotrbot_{timestamp}.db")
    shutil.copy2(DB_NAME, backup_path)
    logger.info(f"Резервная копия БД сохранена: {backup_path}")

    backups = sorted(
        f for f in os.listdir(BACKUP_DIR) if f.startswith("smotrbot_") and f.endswith(".db")
    )
    keep_last = get_setting("BACKUP_KEEP_LAST")
    while len(backups) > keep_last:
        oldest = backups.pop(0)
        try:
            os.remove(os.path.join(BACKUP_DIR, oldest))
        except OSError:
            pass

    return backup_path


async def backup_loop() -> None:
    """Фоновая задача: периодически создаёт резервную копию БД."""
    while True:
        try:
            await make_backup()
        except Exception:
            logger.exception("Ошибка при резервном копировании БД")
        await asyncio.sleep(get_setting("BACKUP_INTERVAL_MINUTES") * 60)

async def advertising_expiry_loop() -> None:
    """Единый фоновой обработчик рекламных постов, временных подписок и рассылок.

    Все даты хранятся в TIMEZONE (Asia/Novosibirsk по умолчанию). Состояние
    идемпотентно хранится в SQLite, поэтому перезапуск бота не теряет таймеры.
    """
    from app.loader import bot
    from app.database import (
        get_due_advertising_posts, finish_advertising_expiry,
        expire_advertising_subscriptions, get_due_advertising_broadcasts,
        claim_advertising_broadcast, finish_advertising_broadcast,
        claim_advertising_delivery, finish_advertising_delivery,
        retry_advertising_delivery, get_advertising_delivery_stats,
        get_all_users,
    )
    from app.runtime_settings import get as get_setting

    while True:
        try:
            now = datetime.now(TIMEZONE)
            await expire_advertising_subscriptions(now)

            # Сначала выполняем запланированные рассылки. Рассылка делается
            # копированием исходного сообщения, поэтому Telegram-разметка и медиа
            # не пересобираются вручную.
            due_broadcasts = await get_due_advertising_broadcasts(now)
            for bid, ad_id, seq, scheduled_at, channel_message_id in due_broadcasts:
                if not await claim_advertising_broadcast(bid):
                    continue
                transient_error = None
                try:
                    user_ids = await get_all_users()
                    for user_id in user_ids:
                        if not await claim_advertising_delivery(bid, user_id):
                            continue
                        try:
                            # Рассылка идёт из уже опубликованного сообщения в канале.
                            # Поэтому удаление/очистка исходного сообщения администратора
                            # больше не ломает запланированную рекламу.
                            await bot.copy_message(
                                chat_id=user_id,
                                from_chat_id=get_setting("MAIN_CHANNEL_ID"),
                                message_id=channel_message_id,
                            )
                            await finish_advertising_delivery(bid, user_id, True)
                        except Exception as e:
                            retry_after = getattr(e, "retry_after", None)
                            if retry_after:
                                transient_error = str(e)
                                await retry_advertising_delivery(bid, user_id, transient_error)
                                try:
                                    await asyncio.sleep(float(retry_after) + 0.5)
                                except Exception:
                                    pass
                            else:
                                # Запрещённые/удалившие чат пользователи не должны
                                # заставлять всю рассылку бесконечно повторяться.
                                name = e.__class__.__name__
                                if name in {"TelegramForbiddenError", "TelegramBadRequest", "TelegramNotFound"}:
                                    await finish_advertising_delivery(bid, user_id, False, str(e))
                                else:
                                    transient_error = str(e)
                                    await retry_advertising_delivery(bid, user_id, transient_error)
                            logger.warning("Рекламная рассылка #%s: пользователь %s: %s", bid, user_id, e)
                        await asyncio.sleep(0.05)

                    stats = await get_advertising_delivery_stats(bid)
                    pending = stats.get("pending", 0) + stats.get("sending", 0)
                    if pending:
                        await finish_advertising_broadcast(bid, transient_error or "Остались пользователи для повторной доставки")
                    else:
                        await finish_advertising_broadcast(bid)
                    logger.info(
                        "Рекламная рассылка #%s (ad #%s, #%s): sent=%s failed=%s pending=%s",
                        bid, ad_id, seq, stats.get("sent", 0), stats.get("failed", 0), pending,
                    )
                except Exception as e:
                    await finish_advertising_broadcast(bid, str(e))
                    logger.exception("Ошибка рекламной рассылки #%s", bid)

            due = await get_due_advertising_posts(now)
            for ad_id, channel_message_id, expires_at, pin_expires_at in due:
                if not channel_message_id:
                    continue
                try:
                    expires = datetime.fromisoformat(expires_at) if expires_at else None
                    pin_expires = datetime.fromisoformat(pin_expires_at) if pin_expires_at else None

                    if expires and now >= expires:
                        try:
                            await bot.unpin_chat_message(get_setting("MAIN_CHANNEL_ID"), channel_message_id)
                        except Exception:
                            pass
                        try:
                            await bot.delete_message(get_setting("MAIN_CHANNEL_ID"), channel_message_id)
                        except Exception as e:
                            logger.warning("Не удалось удалить рекламный пост #%s: %s", ad_id, e)
                            continue
                        await finish_advertising_expiry(ad_id, deleted=True)
                        logger.info("Рекламный пост #%s удалён по истечении срока", ad_id)
                    elif pin_expires and now >= pin_expires:
                        try:
                            await bot.unpin_chat_message(get_setting("MAIN_CHANNEL_ID"), channel_message_id)
                        except Exception:
                            pass
                        await finish_advertising_expiry(ad_id, deleted=False)
                        logger.info("С рекламного поста #%s снят закреп по истечении срока", ad_id)
                except Exception:
                    logger.exception("Ошибка обработки срока рекламного поста #%s", ad_id)
        except Exception:
            logger.exception("Ошибка в цикле рекламных публикаций")
        await asyncio.sleep(20)

