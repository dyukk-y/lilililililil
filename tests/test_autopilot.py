"""Автопилот модерации."""
from datetime import datetime, timedelta

import aiosqlite
import pytest

from app.config import DB_NAME, TIMEZONE
from tests import helpers as H
from tests.conftest import ADMIN_ID

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _status(pid):
    from app.database import get_post_status
    return await get_post_status(pid)


async def _new(uid, text="🧑 подскажите, видели этого человека вчера", photo=None):
    from app.database import create_post, register_user
    await register_user(H.user(uid))
    return await create_post(uid, text, photo)


@pytest.fixture()
def model(monkeypatch):
    """Подменяет вероятность одобрения обученной модели."""
    from app import ai_learning
    holder = {"p": None}
    monkeypatch.setattr(ai_learning, "predict", lambda text: holder["p"])
    return holder


async def test_confident_text_is_published_automatically(app_ready, fake, model):
    from app.services import route_new_post
    model["p"] = 0.95
    pid = await _new(7001)
    await route_new_post(pid)
    assert await _status(pid) == "approved"  # в очереди публикации


async def test_clearly_bad_text_is_rejected_automatically(app_ready, fake, model):
    from app.services import route_new_post
    model["p"] = 0.03
    pid = await _new(7002)
    await route_new_post(pid)
    assert await _status(pid) == "rejected"
    note = [c for c in fake.of("SendMessage") if int(c.chat_id) == 7002][-1]
    assert "автоматическую проверку" in note.text


async def test_uncertain_text_goes_to_moderator_with_short_deadline(app_ready, fake, model):
    from app.services import route_new_post
    model["p"] = 0.5
    pid = await _new(7003)
    await route_new_post(pid)
    assert await _status(pid) == "moderation"
    card = [c for c in fake.of("SendMessage") if int(c.chat_id) == -1001000000002][-1]
    assert "решит автопилот" in card.text
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT review_deadline FROM posts WHERE id=?", (pid,))
        deadline = datetime.fromisoformat((await cur.fetchone())[0])
    assert deadline - datetime.now(TIMEZONE).replace(tzinfo=None) < timedelta(hours=2, minutes=1)


async def _expire(pid):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE posts SET review_deadline='2000-01-01T00:00:00' WHERE id=?", (pid,))
        await db.commit()


async def test_timeout_autopilot_publishes_or_rejects(app_ready, fake, model):
    from app.services import route_new_post, _handle_expired_review
    model["p"] = 0.5
    pid_ok = await _new(7004)
    await route_new_post(pid_ok)
    model["p"] = 0.7   # к моменту таймаута «скорее да»
    await _expire(pid_ok)
    await _handle_expired_review(pid_ok)
    assert await _status(pid_ok) == "approved"

    model["p"] = 0.5
    pid_bad = await _new(7005)
    await route_new_post(pid_bad)
    model["p"] = 0.3   # «скорее нет»
    await _expire(pid_bad)
    await _handle_expired_review(pid_bad)
    assert await _status(pid_bad) == "rejected"


async def test_moderator_decision_wins_over_autopilot(app_ready, fake, model):
    """Если модератор отклонил пост, автопилот его не «оживит»."""
    from app.services import route_new_post, reject_post, _handle_expired_review
    model["p"] = 0.5
    pid = await _new(7006)
    await route_new_post(pid)
    await reject_post(pid, moderator_id=ADMIN_ID, reason="нет")
    model["p"] = 0.99
    await _expire(pid)
    await _handle_expired_review(pid)
    assert await _status(pid) == "rejected"


async def test_autopilot_off_keeps_manual_flow(app_ready, fake, model):
    from app.runtime_settings import SETTINGS
    from app.services import route_new_post
    SETTINGS["AUTOPILOT_TEXT"] = 0
    try:
        model["p"] = 0.5
        pid = await _new(7007)
        await route_new_post(pid)
        assert await _status(pid) == "moderation"
        card = [c for c in fake.of("SendMessage") if int(c.chat_id) == -1001000000002][-1]
        assert "Автоотклонение через 24" in card.text
    finally:
        SETTINGS["AUTOPILOT_TEXT"] = 1


async def _make_trusted(uid, n=5):
    async with aiosqlite.connect(DB_NAME) as db:
        for i in range(n):
            await db.execute(
                "INSERT INTO posts(user_id, text, time, status, moderator_id) VALUES(?,?,?,?,?)",
                (uid, f"🧑 старый пост {i}", datetime.now().isoformat(), "published", ADMIN_ID))
        await db.commit()


async def test_trusted_author_photo_is_queued_untrusted_waits(app_ready, fake, model, monkeypatch):
    from app import services
    from app.services import route_new_post

    async def clean_media(_bytes):
        return "0" * 64, "", True, ""

    class Buf:
        def read(self):
            return b"img"

    async def fake_download(*a, **k):
        return Buf()

    monkeypatch.setattr(services, "analyze_media", clean_media)
    monkeypatch.setattr(services.bot, "download", fake_download)
    monkeypatch.setattr(services, "photo_detector_available", lambda: True)
    model["p"] = 0.6

    trusted, newbie = 7010, 7011
    await _make_trusted(trusted)
    pid_t = await _new(trusted, photo="file_trusted")
    await route_new_post(pid_t, photo_checked=True)
    assert await _status(pid_t) == "approved"
    # Модераторы всё равно получают карточку и могут отклонить до выхода.
    assert [c for c in fake.of("SendPhoto") if int(c.chat_id) == -1001000000002]

    pid_n = await _new(newbie, photo="file_newbie")
    await route_new_post(pid_n, photo_checked=True)
    assert await _status(pid_n) == "moderation"


async def test_trusted_status_lost_after_recent_rejection(app_ready):
    from app.autopilot import is_trusted_author
    uid = 7012
    await _make_trusted(uid)
    assert await is_trusted_author(uid)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO posts(user_id, text, time, status, moderator_id, moderation_time) VALUES(?,?,?,?,?,?)",
            (uid, "🧑 плохой", datetime.now().isoformat(), "rejected", ADMIN_ID,
             datetime.now(TIMEZONE).replace(tzinfo=None).isoformat()))
        await db.commit()
    assert not await is_trusted_author(uid)
