"""Мониторинг, автобаны, «Мои посты», предложенные посты."""
import asyncio
from datetime import datetime

import aiosqlite
import pytest
from aiogram.types import Chat, Message, SuggestedPostInfo, SuggestedPostPrice, Update

from app.config import DB_NAME
from app.loader import bot
from tests import helpers as H
from tests.conftest import ADMIN_ID

pytestmark = pytest.mark.asyncio(loop_scope="session")
ADMINS_CHAT = -1001000000003
DM_CHAT = -1009990000001


async def feed(dp, upd):
    await dp.feed_update(bot, upd)


# ---------------- Мониторинг ----------------
async def test_supervisor_restarts_crashed_task_and_alerts(app_ready, fake, monkeypatch):
    from app import alerts
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    runs = []

    async def flaky():
        runs.append(1)
        if len(runs) < 3:
            raise RuntimeError("boom")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await alerts.supervise("тест-задача", flaky)
    assert len(runs) == 3  # упала дважды — дважды перезапущена
    notes = [c for c in fake.of("SendMessage") if int(c.chat_id) == ADMINS_CHAT]
    assert len(notes) == 1 and "тест-задача" in notes[0].text  # одно оповещение, без спама


_real_sleep = asyncio.sleep


async def _fast_sleep(_delay, *a, **k):
    await _real_sleep(0)


async def test_notify_admins_cooldown(app_ready, fake):
    from app.alerts import notify_admins
    assert await notify_admins("A", key="k-test")
    assert not await notify_admins("B", key="k-test")
    assert len([c for c in fake.of("SendMessage") if int(c.chat_id) == ADMINS_CHAT]) == 1


async def test_error_burst_triggers_alert(app_ready, fake):
    import logging
    from app import alerts
    alerts.install()
    for i in range(alerts.ERRORS_PER_5MIN):
        logging.getLogger("app.test").error("Сбой %s", i % 2)
    await alerts.HealthMonitor().check_errors()
    note = [c for c in fake.of("SendMessage") if int(c.chat_id) == ADMINS_CHAT][-1]
    assert "Много ошибок" in note.text


async def test_memory_pressure_self_heals_before_alert(app_ready, fake, monkeypatch):
    from app import alerts, moderation_photo
    released = []
    monkeypatch.setattr(alerts, "memory_limit_mb", lambda: 1000.0)
    readings = iter([900.0, 300.0])  # до и после очистки
    monkeypatch.setattr(alerts, "memory_rss_mb", lambda: next(readings))
    monkeypatch.setattr(moderation_photo, "release_if_idle", lambda s: released.append(s) or True)
    await alerts.HealthMonitor().check_memory()
    assert released == [0]
    assert not [c for c in fake.of("SendMessage") if "Мало памяти" in (c.text or "")]


async def test_telegram_outage_and_recovery_message(app_ready, fake):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import GetMe
    from app.alerts import HealthMonitor
    mon = HealthMonitor()
    fake.fail_methods["GetMe"] = TelegramNetworkError(method=GetMe(), message="down")
    for _ in range(3):
        await mon.check_telegram()
    assert mon.outage_started is not None
    fake.fail_methods.pop("GetMe")
    await mon.check_telegram()
    assert "восстановлена" in [c for c in fake.of("SendMessage") if int(c.chat_id) == ADMINS_CHAT][-1].text


# ---------------- Автобаны ----------------
async def test_repeat_violations_trigger_temp_ban_and_release(app_ready, fake):
    from app.autoban import register_violation, release_expired_bans
    from app.database import is_banned
    uid = 8001
    assert not await register_violation(bot, uid, "blacklist")
    assert not await register_violation(bot, uid, "duplicate")
    assert await register_violation(bot, uid, "blacklist")
    assert await is_banned(uid)
    note = [c for c in fake.of("SendMessage") if int(c.chat_id) == uid][-1]
    assert "24 часа" in note.text
    # Срок истёк — бан снимается сам, пользователь получает уведомление.
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE bans SET until='2000-01-01T00:00:00' WHERE user_id=?", (uid,))
        await db.commit()
    assert await release_expired_bans(bot) == 1
    assert not await is_banned(uid)
    assert "снята" in [c for c in fake.of("SendMessage") if int(c.chat_id) == uid][-1].text
    # Повторный автобан в течение 30 дней — уже на неделю.
    for kind in ("gibberish", "gibberish", "gibberish"):
        await register_violation(bot, uid, kind)
    assert "7 дней" in [c for c in fake.of("SendMessage") if int(c.chat_id) == uid][-1].text


async def test_autoban_never_touches_manual_ban_or_admins(app_ready, fake):
    from app.autoban import register_violation
    from app.database import ban_user, get_ban_info
    uid = 8002
    await ban_user(uid, "ручной", H.user(ADMIN_ID))
    for _ in range(5):
        await register_violation(bot, uid, "flood")
    assert (await get_ban_info(uid))[3] is None  # остался бессрочным
    for _ in range(5):
        assert not await register_violation(bot, ADMIN_ID, "flood")


async def test_blacklisted_posts_count_as_violations(app_ready, fake):
    from app.database import is_banned
    uid = 8003
    await feed(app_ready, H.message_update(uid, "/start"))
    for _ in range(3):
        await feed(app_ready, H.callback_update(uid, "no_photo"))
        await feed(app_ready, H.message_update(uid, "🧑 ты лох и дурак вообще"))
    assert await is_banned(uid)


# ---------------- Мои посты ----------------
async def test_my_posts_list_and_cancel(app_ready, fake):
    from app.database import create_post, get_post_status
    from app.services import send_post_for_review
    uid = 8004
    await feed(app_ready, H.message_update(uid, "/start"))
    pid = await create_post(uid, "🧑 расскажите про этого парня", None)
    await send_post_for_review(pid, None, "проверка")
    await feed(app_ready, H.callback_update(uid, "my_posts"))
    screen = fake.of("EditMessageText")[-1]
    assert f"#{pid}" in screen.text and "В очереди" in screen.text
    buttons = [b.callback_data for row in screen.reply_markup.inline_keyboard for b in row]
    assert f"mypost_cancel_{pid}" in buttons

    await feed(app_ready, H.callback_update(uid, f"mypost_cancel_{pid}"))
    await feed(app_ready, H.callback_update(uid, f"mypost_yes_{pid}"))
    assert await get_post_status(pid) == "cancelled"
    card = fake.of("EditMessageReplyMarkup")[-1]
    assert "Отменён автором" in card.reply_markup.inline_keyboard[0][0].text
    # Чужой пост отменить нельзя
    other = await create_post(uid + 1, "🧑 чужой пост для теста", None)
    await feed(app_ready, H.callback_update(uid, f"mypost_yes_{other}"))
    assert await get_post_status(other) == "moderation"


# ---------------- Предложенные посты ----------------
def _suggested(uid: int, text: str, msg_id: int, price: int | None = None) -> Update:
    info = SuggestedPostInfo(state="pending", price=SuggestedPostPrice(currency="XTR", amount=price) if price else None)
    msg = Message(message_id=msg_id, date=datetime.now(),
                  chat=Chat(id=DM_CHAT, type="supergroup", is_direct_messages=True),
                  from_user=H.user(uid), text=text, suggested_post_info=info)
    return Update(update_id=msg_id + 900000, message=msg)


@pytest.fixture()
def our_dm():
    from app.handlers import suggested_posts
    suggested_posts._dm_parent_cache[DM_CHAT] = True
    yield
    suggested_posts._dm_parent_cache.pop(DM_CHAT, None)


async def test_suggested_post_full_cycle(app_ready, fake, our_dm):
    from app.database import get_post_by_dm_message, get_post_status
    from app.services import publish_post
    await feed(app_ready, _suggested(8010, "🧑 кто знает парня с рыжей собакой в парке", 501, price=100))
    pid = await get_post_by_dm_message(DM_CHAT, 501)
    assert pid is not None
    card = [c for c in fake.of("SendMessage") if int(c.chat_id) == -1001000000002][-1]
    assert "личных сообщениях канала" in card.text and "100 ⭐" in card.text
    # Повторная доставка того же сообщения не создаёт дубль
    await feed(app_ready, _suggested(8010, "🧑 кто знает парня с рыжей собакой в парке", 501, price=100))
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE dm_message_id=501")
        assert (await cur.fetchone())[0] == 1

    ok, _ = await publish_post(pid, moderator_id=ADMIN_ID)
    assert ok and await get_post_status(pid) == "published"
    approve = fake.of("ApproveSuggestedPost")[-1]
    assert approve.chat_id == DM_CHAT and approve.message_id == 501
    assert not [c for c in fake.of("SendMessage") if int(c.chat_id) == -1001000000001]  # бот сам не постит


async def test_suggested_post_rule_violation_declined_with_reason(app_ready, fake, our_dm):
    await feed(app_ready, _suggested(8011, "кто это вообще такой парень", 502))  # нет 🧑/👩
    decline = fake.of("DeclineSuggestedPost")[-1]
    assert decline.message_id == 502 and "эмодзи" in decline.comment.lower()


async def test_suggested_post_reject_by_moderator_declines_in_telegram(app_ready, fake, our_dm):
    from app.database import get_post_by_dm_message
    from app.services import reject_post
    await feed(app_ready, _suggested(8012, "🧑 подскажите кто этот парень на остановке", 503))
    pid = await get_post_by_dm_message(DM_CHAT, 503)
    assert await reject_post(pid, moderator_id=ADMIN_ID, reason="Реклама или продажа")
    decline = fake.of("DeclineSuggestedPost")[-1]
    assert decline.message_id == 503 and decline.comment == "Реклама или продажа"
    assert not [c for c in fake.of("SendMessage") if int(c.chat_id) == 8012]


async def test_suggested_from_foreign_channel_ignored(app_ready, fake):
    from app.handlers import suggested_posts
    suggested_posts._dm_parent_cache[DM_CHAT] = False
    try:
        await feed(app_ready, _suggested(8013, "🧑 кто знает этого парня с гитарой", 504))
        from app.database import get_post_by_dm_message
        assert await get_post_by_dm_message(DM_CHAT, 504) is None
    finally:
        suggested_posts._dm_parent_cache.pop(DM_CHAT, None)


async def test_suggested_missing_rights_alerts_admins(app_ready, fake, our_dm):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import ApproveSuggestedPost
    from app.database import get_post_by_dm_message, get_post_status
    from app.services import publish_post
    await feed(app_ready, _suggested(8014, "🧑 кто знает эту девушку в красном пальто", 505))
    pid = await get_post_by_dm_message(DM_CHAT, 505)
    fake.fail_methods["ApproveSuggestedPost"] = TelegramBadRequest(
        method=ApproveSuggestedPost(chat_id=1, message_id=1), message="Bad Request: not enough rights")
    ok, _ = await publish_post(pid, moderator_id=ADMIN_ID)
    assert not ok and await get_post_status(pid) == "rejected"
    assert "Управление личными сообщениями" in [c for c in fake.of("SendMessage") if int(c.chat_id) == ADMINS_CHAT][-1].text


# ---------------- Связь предложенного поста с каналом ----------------
def _channel_post(msg_id: int, text: str) -> Update:
    msg = Message(message_id=msg_id, date=datetime.now(),
                  chat=Chat(id=-1001000000001, type="channel"), text=text)
    return Update(update_id=msg_id + 700000, channel_post=msg)


def _auto_forward(msg_id: int, channel_msg_id: int, text: str) -> Update:
    from aiogram.types import MessageOriginChannel
    origin = MessageOriginChannel(date=datetime.now(), chat=Chat(id=-1001000000001, type="channel"),
                                  message_id=channel_msg_id)
    msg = Message(message_id=msg_id, date=datetime.now(),
                  chat=Chat(id=-1001000000004, type="supergroup"), text=text,
                  is_automatic_forward=True, forward_origin=origin,
                  from_user=H.user(777000))
    return Update(update_id=msg_id + 800000, message=msg)


async def _approved_suggested(dp, fake, uid, text, msg_id):
    from app.database import get_post_by_dm_message
    from app.services import publish_post
    await feed(dp, _suggested(uid, text, msg_id))
    pid = await get_post_by_dm_message(DM_CHAT, msg_id)
    ok, _ = await publish_post(pid, moderator_id=ADMIN_ID)
    assert ok
    return pid


async def test_channel_post_links_suggested_post(app_ready, fake, our_dm):
    from app.database import get_post_by_channel_message_id
    text = "🧑 кто знает парня с синим самокатом у школы"
    pid = await _approved_suggested(app_ready, fake, 8020, text, 510)
    await feed(app_ready, _channel_post(4001, text))
    post = await get_post_by_channel_message_id(4001)
    assert post and post[0] == pid
    # Автор получил обычное уведомление со ссылкой и кнопкой закрепа.
    note = [c for c in fake.of("SendMessage") if int(c.chat_id) == 8020][-1]
    assert "опубликован" in note.text
    assert any(b.callback_data == f"pin_offer_{pid}" for r in note.reply_markup.inline_keyboard for b in r)
    # Повторная доставка того же поста — без второго уведомления.
    before = len([c for c in fake.of("SendMessage") if int(c.chat_id) == 8020])
    await feed(app_ready, _channel_post(4001, text))
    assert len([c for c in fake.of("SendMessage") if int(c.chat_id) == 8020]) == before


async def test_auto_forward_links_and_posts_intro_comment_with_buttons(app_ready, fake, our_dm):
    from app.database import get_post_by_channel_message_id
    text = "👩 кто знает девушку с жёлтым зонтом на остановке"
    pid = await _approved_suggested(app_ready, fake, 8021, text, 511)
    await feed(app_ready, _auto_forward(9100, 4002, text))
    assert (await get_post_by_channel_message_id(4002))[0] == pid
    comment = [c for c in fake.of("SendMessage") if int(c.chat_id) == -1001000000004][-1]
    urls = [b.url for r in comment.reply_markup.inline_keyboard for b in r if b.url]
    assert any(f"author_{pid}" in u for u in urls) and any(f"delete_{pid}" in u for u in urls)


async def test_author_lookup_and_delete_work_for_linked_suggested_post(app_ready, fake, our_dm):
    from app.database import get_post_status
    from app.services import submit_deletion_request
    text = "🧑 кто знает парня в красной кепке с собакой"
    pid = await _approved_suggested(app_ready, fake, 8022, text, 512)
    await feed(app_ready, _channel_post(4003, text))
    # «Узнать автора» по ссылке на пост
    buyer = 8023
    await feed(app_ready, H.message_update(buyer, "/start"))
    await feed(app_ready, H.callback_update(buyer, "author_lookup_start"))
    await feed(app_ready, H.message_update(buyer, "https://t.me/c/1000000001/4003"))
    assert "нашёл автора" in fake.of("SendMessage")[-1].text
    # Автор удаляет свой пост — он реально удаляется из канала.
    result, _ = await submit_deletion_request(pid, 8022, "передумал")
    assert result == "auto_deleted" and await get_post_status(pid) == "deleted"
    assert fake.of("DeleteMessage")[-1].message_id == 4003


async def test_unrelated_channel_post_is_not_linked(app_ready, fake, our_dm):
    from app.database import get_post_by_channel_message_id
    await _approved_suggested(app_ready, fake, 8024, "🧑 кто знает парня с гитарой у фонтана", 513)
    await feed(app_ready, _channel_post(4004, "Совсем другой текст от администрации"))
    assert await get_post_by_channel_message_id(4004) is None
