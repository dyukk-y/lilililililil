"""Реклама: мастер, публикация сейчас/по расписанию, менеджер."""
from datetime import datetime, timedelta

import aiosqlite
import pytest

from app.config import DB_NAME, TIMEZONE
from app.loader import bot
from tests import helpers as H
from tests.conftest import ADMIN_ID

pytestmark = pytest.mark.asyncio(loop_scope="session")
CHANNEL = -1001000000001


async def feed(dp, upd):
    await dp.feed_update(bot, upd)


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def _last_markup(fake):
    for call in reversed(fake.calls):
        if getattr(call, "reply_markup", None) is not None:
            return call.reply_markup
    raise AssertionError("нет клавиатуры")


async def _start_post_draft(dp, fake):
    await feed(dp, H.message_update(ADMIN_ID, "/post"))
    await feed(dp, H.callback_update(ADMIN_ID, "ad_type:post"))
    await feed(dp, H.message_update(ADMIN_ID, "Реклама кофейни ☕"))
    ad_id = int(_last_markup(fake).inline_keyboard[0][0].callback_data.split(":")[1])
    return ad_id


async def test_post_ad_publish_now_and_manage(app_ready, fake):
    from app.database import get_ad
    ad_id = await _start_post_draft(app_ready, fake)
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_duration:{ad_id}:48"))
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_pin:{ad_id}:24"))
    summary = fake.of("EditMessageText")[-1]
    assert "Проверьте рекламу" in summary.text
    assert {"ad_confirm:" + str(ad_id), "ad_schedule:" + str(ad_id)} <= {b.callback_data for b in _buttons(summary.reply_markup)}

    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_confirm:{ad_id}"))
    ad = await get_ad(ad_id)
    assert ad["status"] == "published"
    assert [c for c in fake.of("CopyMessage") if int(c.chat_id) == CHANNEL]
    assert fake.of("PinChatMessage")

    # Менеджер: реклама видна в списке, продление и снятие закрепа работают
    await feed(app_ready, H.callback_update(ADMIN_ID, "admin_ad_post"))
    hub = fake.of("EditMessageText")[-1]
    assert f"#{ad_id}" in hub.text
    before = (await get_ad(ad_id))["expires_at"]
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_extend:{ad_id}:24"))
    after = (await get_ad(ad_id))["expires_at"]
    assert datetime.fromisoformat(after) - datetime.fromisoformat(before) == timedelta(hours=24)
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_unpin_now:{ad_id}"))
    assert (await get_ad(ad_id))["pin_expires_at"] is None
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_delete_now:{ad_id}"))
    assert (await get_ad(ad_id))["status"] == "expired"


async def test_combo_with_relative_broadcasts_scheduled_then_published(app_ready, fake):
    from app.database import get_ad
    from app.handlers.advertising import publish_due_scheduled_ads
    await feed(app_ready, H.message_update(ADMIN_ID, "/post"))
    await feed(app_ready, H.callback_update(ADMIN_ID, "ad_type:combo"))
    await feed(app_ready, H.message_update(ADMIN_ID, "Комбо-реклама"))
    ad_id = int(_buttons(_last_markup(fake))[0].callback_data.split(":")[1])
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_combo:{ad_id}:72"))
    # Быстрая кнопка: рассылка через 24 ч после публикации
    assert any(b.callback_data == f"ad_bt:{ad_id}:24" for b in _buttons(_last_markup(fake)))
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_bt:{ad_id}:24"))
    assert "через 24 ч." in fake.of("EditMessageText")[-1].text

    when = datetime.now(TIMEZONE) + timedelta(hours=5)
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_schedule:{ad_id}"))
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_sched_at:{ad_id}:{int(when.timestamp())}"))
    ad = await get_ad(ad_id)
    assert ad["status"] == "scheduled"
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT scheduled_at FROM advertising_broadcasts WHERE ad_id=?", (ad_id,))
        (sched,) = await cur.fetchone()
    # Рассылка отсчитана от времени публикации, а не от момента настройки.
    assert abs((datetime.fromisoformat(sched) - when).total_seconds() - 24 * 3600) < 5

    # Пока время не пришло — ничего не публикуется
    await publish_due_scheduled_ads()
    assert (await get_ad(ad_id))["status"] == "scheduled"
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE advertising_posts SET publish_at=? WHERE id=?",
                         ((datetime.now(TIMEZONE) - timedelta(minutes=1)).isoformat(), ad_id))
        await db.commit()
    assert await publish_due_scheduled_ads() == 1
    assert (await get_ad(ad_id))["status"] == "published"
    note = [c for c in fake.of("SendMessage") if int(c.chat_id) == ADMIN_ID][-1]
    assert f"#{ad_id} опубликована" in note.text


async def test_schedule_manual_date_and_cancel(app_ready, fake):
    from app.database import get_ad
    ad_id = await _start_post_draft(app_ready, fake)
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_duration:{ad_id}:24"))
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_pin:{ad_id}:0"))
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_schedule:{ad_id}"))
    await feed(app_ready, H.message_update(ADMIN_ID, "01.01.2020 10:00"))
    assert "в будущем" in fake.of("SendMessage")[-1].text
    future = datetime.now(TIMEZONE) + timedelta(days=2)
    await feed(app_ready, H.message_update(ADMIN_ID, future.strftime("%d.%m.%Y %H:%M")))
    assert (await get_ad(ad_id))["status"] == "scheduled"
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_unschedule:{ad_id}"))
    assert (await get_ad(ad_id))["status"] == "cancelled"


async def test_failed_scheduled_publish_can_be_retried(app_ready, fake):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import CopyMessage
    from app.database import get_ad, schedule_advertising_post
    from app.handlers.advertising import publish_due_scheduled_ads
    ad_id = await _start_post_draft(app_ready, fake)
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_duration:{ad_id}:24"))
    await schedule_advertising_post(ad_id, datetime.now(TIMEZONE) - timedelta(minutes=1))
    fake.fail_methods["CopyMessage"] = TelegramBadRequest(method=CopyMessage(chat_id=1, from_chat_id=1, message_id=1), message="chat not found")
    await publish_due_scheduled_ads()
    assert (await get_ad(ad_id))["status"] == "error"
    assert "Не удалось опубликовать" in [c for c in fake.of("SendMessage") if int(c.chat_id) == ADMIN_ID][-1].text

    fake.fail_methods.pop("CopyMessage")
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_card:{ad_id}"))
    assert any(b.callback_data == f"ad_publish_scheduled:{ad_id}" for b in _buttons(_last_markup(fake)))
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_publish_scheduled:{ad_id}"))
    assert (await get_ad(ad_id))["status"] == "published"


async def test_broadcast_outside_paid_period_is_rejected(app_ready, fake):
    await feed(app_ready, H.message_update(ADMIN_ID, "/post"))
    await feed(app_ready, H.callback_update(ADMIN_ID, "ad_type:combo"))
    await feed(app_ready, H.message_update(ADMIN_ID, "Комбо"))
    ad_id = int(_buttons(_last_markup(fake))[0].callback_data.split(":")[1])
    await feed(app_ready, H.callback_update(ADMIN_ID, f"ad_combo:{ad_id}:72"))
    late = datetime.now(TIMEZONE) + timedelta(days=10)
    await feed(app_ready, H.message_update(ADMIN_ID, late.strftime("%d.%m.%Y %H:%M")))
    assert "оплаченный срок" in fake.of("SendMessage")[-1].text
