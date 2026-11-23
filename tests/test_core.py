"""БД, планировщик, настройки, публикация."""
import sqlite3

import aiosqlite
import pytest

from app.config import DB_NAME
from tests import helpers as H

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_backup_is_consistent_sqlite_file(app_ready):
    from app.scheduler import make_backup
    path = await make_backup()
    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT COUNT(*) FROM settings").fetchone()[0] > 0
    finally:
        con.close()


async def test_settings_reject_empty_admins(app_ready):
    from app import runtime_settings as rs
    ok, _ = await rs.set_value("ADMINS", "")
    assert not ok
    assert rs.get("ADMINS")
    ok, _ = await rs.set_value("DAILY_PUBLISH_LIMIT", "0")
    assert not ok


async def test_posts_today_counts_only_today(app_ready):
    from app.database import create_post, posts_today
    uid = 4001
    before = await posts_today(uid)
    pid = await create_post(uid, "🧑 расскажите про этого парня", None)
    assert await posts_today(uid) == before + 1
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE posts SET time='2000-01-01T10:00:00' WHERE id=?", (pid,))
        await db.commit()
    assert await posts_today(uid) == before


async def test_publish_success_not_requeued_when_side_effect_fails(app_ready, fake, monkeypatch):
    """Если пост уже ушёл в канал, сбой уведомления админов не должен
    возвращать его в очередь (иначе — повторная публикация)."""
    from app import services
    from app.database import create_post, get_post_status
    pid = await create_post(4002, "🧑 кто знает этого парня", None)

    async def boom(*a, **k):
        raise RuntimeError("admins chat unavailable")

    monkeypatch.setattr(services, "notify_admins_outcome", boom)
    ok, reason = await services.publish_post(pid)
    assert ok and reason == "published"
    assert await get_post_status(pid) == "published"
    assert len([c for c in fake.of("SendMessage") if int(c.chat_id) == -1001000000001]) == 1


async def test_publish_failure_requeues_with_backoff(app_ready, fake):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import SendMessage
    from app.services import publish_post
    from app.database import create_post, get_post_status
    pid = await create_post(4003, "🧑 кто знает этого парня", None)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE posts SET status='approved' WHERE id=?", (pid,))
        await db.commit()
    fake.fail_methods["SendMessage"] = TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="down")
    ok, reason = await publish_post(pid)
    assert not ok and reason == "telegram_publish_failed"
    assert await get_post_status(pid) == "approved"
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT publish_next_retry_at FROM posts WHERE id=?", (pid,))
        assert (await cur.fetchone())[0] is not None


async def test_photo_review_card_is_sent_even_with_long_ocr(app_ready, fake):
    """Раньше подпись > 1024 символов -> Telegram отклонял карточку, и
    модераторы не видели пост."""
    from app.services import send_post_for_review
    from app.database import create_post, set_ai_analysis
    pid = await create_post(4004, "🧑 расскажите про девушку на фото", "photo_file_id")
    await set_ai_analysis(pid, 10, 0.3, "manual", "причина " * 150, "текст на фото " * 300)
    await send_post_for_review(pid, None, "фото")
    photo = [c for c in fake.of("SendPhoto") if int(c.chat_id) == -1001000000002][-1]
    from html import unescape
    from app.services import _TAG_RE
    assert len(unescape(_TAG_RE.sub("", photo.caption))) <= 1024


async def test_schedule_slot_respects_window(app_ready):
    from app.scheduler import pick_schedule_slot
    from app.runtime_settings import get
    slot = await pick_schedule_slot()
    start = get("PUBLISH_WINDOW_START_HOUR")
    assert slot.hour >= start or slot.hour <= get("PUBLISH_WINDOW_END_HOUR")


async def test_deletion_request_failed_delete_is_reported(app_ready, fake):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import DeleteMessage
    from app.database import create_post, register_user, set_channel_message_id
    from app.services import submit_deletion_request
    from tests.conftest import ADMIN_ID
    author, requester = 4005, 4006
    await register_user(H.user(author))
    await register_user(H.user(requester))
    pid = await create_post(author, "🧑 кто знает этого парня", None)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE posts SET status='published' WHERE id=?", (pid,))
        await db.commit()
    await set_channel_message_id(pid, 777)
    result, req_id = await submit_deletion_request(pid, requester, "не нравится <тег>")
    assert result == "pending"
    fake.fail_methods["DeleteMessage"] = TelegramBadRequest(method=DeleteMessage(chat_id=1, message_id=1), message="can't delete")
    await app_ready.feed_update(__import__("app.loader", fromlist=["bot"]).bot,
                                H.callback_update(1002, f"delreq_approve_{req_id}"))
    answer = fake.of("AnswerCallbackQuery")[-1]
    assert "не дал удалить" in answer.text


async def test_load_settings_keeps_admins_and_reviewers(app_ready):
    """Регрессия: load_settings опустошал in-place списки ADMINS и
    DELETION_REVIEWERS (clear() на том же объекте перед extend())."""
    from app import config
    from app.runtime_settings import load_settings
    await load_settings()
    assert config.ADMINS == [1001]
    assert config.DELETION_REVIEWERS == [1002]


async def test_priority_payment_notifies_admins(app_ready, fake):
    from app.database import create_post, register_user
    from tests.conftest import ADMIN_ID
    uid = 4010
    await register_user(H.user(uid))
    pid = await create_post(uid, "🧑 кто знает этого парня", None)
    from app.loader import bot
    await app_ready.feed_update(bot, H.payment_update(uid, f"priority|{pid}|{uid}"))
    to_admin = [c for c in fake.of("SendMessage") if int(c.chat_id) == ADMIN_ID]
    assert to_admin and "Приоритетная публикация" in to_admin[-1].text


async def test_auto_publish_disables_moderator_card(app_ready, fake):
    from app.services import publish_post, send_post_for_review
    from app.database import create_post
    pid = await create_post(4020, "🧑 кто знает этого парня", None)
    await send_post_for_review(pid, None, "проверка")
    ok, _ = await publish_post(pid)  # moderator_id=0 — планировщик
    assert ok
    edits = fake.of("EditMessageReplyMarkup")
    assert edits and edits[-1].reply_markup.inline_keyboard[0][0].text == "✅ Опубликовано"
