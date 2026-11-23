"""Самообучаемая модель, админ-панель, обслуживание."""
import aiosqlite
import pytest

from app.config import DB_NAME
from app.loader import bot
from tests import helpers as H
from tests.conftest import ADMIN_ID

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.asyncio(loop_scope="session")
async def test_naive_bayes_learns_from_examples():
    from app.ai_learning import NaiveBayesModel, MIN_SAMPLES_PER_CLASS
    ok = [f"🧑 расскажите про парня в синей куртке {i}" for i in range(MIN_SAMPLES_PER_CLASS)]
    bad = [f"👩 продам айфон недорого торг {i}" for i in range(MIN_SAMPLES_PER_CLASS)]
    model = NaiveBayesModel().fit([(t, "ok") for t in ok] + [(t, "bad") for t in bad])
    assert model.ready
    assert model.prob_ok("🧑 расскажите кто этот парень") > 0.8
    assert model.prob_ok("продам телефон недорого") < 0.2


async def test_model_not_ready_with_little_data():
    from app.ai_learning import NaiveBayesModel
    model = NaiveBayesModel().fit([("🧑 кто это", "ok"), ("продам", "bad")])
    assert not model.ready


async def test_retrain_uses_only_human_decisions(app_ready):
    from app import ai_learning
    from app.database import create_post
    updates = []
    for i in range(30):
        for text, status, mod in (
            (f"🧑 кто знает этого парня номер {i}", "published", ADMIN_ID),
            (f"👩 куплю шкаф самовывоз {i}", "rejected", ADMIN_ID),
            (f"👩 автоотказ по таймауту {i}", "rejected", 0),  # не мнение человека
        ):
            pid = await create_post(5000 + i, text, None)
            updates.append((status, mod, pid))
    async with aiosqlite.connect(DB_NAME) as db:
        await db.executemany("UPDATE posts SET status=?, moderator_id=? WHERE id=?", updates)
        await db.commit()
    model = await ai_learning.retrain()
    assert model.ready
    assert not any("w:автоотказ" == k for k in model.counts["bad"])
    assert ai_learning.predict("👩 куплю шкаф") < 0.5
    assert ai_learning.predict("🧑 кто знает этого парня") > 0.5


async def test_learned_model_sends_suspicious_post_to_manual(app_ready):
    from app.decision_engine import evaluate
    # «расскажите про» — триггер автопубликации, но модель видела, что
    # объявления о покупке модераторы отклоняют.
    d = await evaluate("👩 куплю шкаф самовывоз срочно", has_trigger=True)
    assert d.decision == "manual"
    assert d.learned is not None and d.learned < 0.5


async def test_ai_stats_button_works(app_ready, fake):
    await app_ready.feed_update(bot, H.callback_update(ADMIN_ID, "admin_ai_stats"))
    edit = fake.of("EditMessageText")[-1]
    assert edit.rich_message is not None and "Самообучаемая модель" in edit.rich_message.html


async def test_rich_message_falls_back_to_plain_html(app_ready, fake):
    """Если Telegram не принял rich-сообщение — показывается обычный текст."""
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText
    real = fake.make_request

    async def picky(bot_, method, timeout=None):
        if type(method).__name__ == "EditMessageText" and method.rich_message is not None:
            fake.calls.append(method)
            raise TelegramBadRequest(method=method, message="Bad Request: RICH_MESSAGE_INVALID")
        return await real(bot_, method, timeout)

    fake.make_request = picky
    await app_ready.feed_update(bot, H.callback_update(ADMIN_ID, "admin_stats"))
    edits = fake.of("EditMessageText")
    assert edits[-1].rich_message is None and "Статистика бота" in edits[-1].text
    # Возврат в панель после rich-экрана: если редактирование не удалось —
    # панель приходит новым сообщением, без «ошибки».
    fake.fail_methods["EditMessageText"] = TelegramBadRequest(
        method=EditMessageText(text="x"), message="Bad Request: message can't be edited")
    await app_ready.feed_update(bot, H.callback_update(ADMIN_ID, "admin_panel"))
    assert "Админ-панель" in fake.of("SendMessage")[-1].text


async def test_admin_panel_is_compact(app_ready, fake):
    await app_ready.feed_update(bot, H.message_update(ADMIN_ID, "/admin"))
    markup = fake.of("SendMessage")[-1].reply_markup
    assert len(markup.inline_keyboard) <= 9
    callbacks = {b.callback_data for row in markup.inline_keyboard for b in row}
    assert {"pending_posts", "admin_ai_stats", "settings_list", "broadcast"} <= callbacks


async def test_settings_categories_navigation_and_edit(app_ready, fake):
    from app import runtime_settings as rs
    await app_ready.feed_update(bot, H.callback_update(ADMIN_ID, "settings_list"))
    cats = fake.of("EditMessageText")[-1].reply_markup
    assert any(b.callback_data == "settings_cat_3" for row in cats.inline_keyboard for b in row)
    await app_ready.feed_update(bot, H.callback_update(ADMIN_ID, "settings_cat_3"))
    await app_ready.feed_update(bot, H.callback_update(ADMIN_ID, "settings_edit_UNLOCK_PRICE_STARS"))
    await app_ready.feed_update(bot, H.message_update(ADMIN_ID, "abc"))
    assert "Неверный формат" in fake.of("SendMessage")[-1].text
    # После ошибки можно сразу прислать исправленное значение.
    await app_ready.feed_update(bot, H.message_update(ADMIN_ID, "77"))
    assert rs.get("UNLOCK_PRICE_STARS") == 77


async def test_prune_logs(app_ready):
    from app.database import prune_logs
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO logs(action,data,time) VALUES('old','x','2000-01-01 00:00:00')")
        await db.commit()
    assert await prune_logs(90) >= 1


async def test_nudenet_release_if_idle(monkeypatch):
    from app import moderation_photo as mp
    monkeypatch.setattr(mp, "_detector", object())
    monkeypatch.setattr(mp, "_last_used", 0.0)
    assert mp.release_if_idle(1) is True
    assert mp._detector is None
    assert mp.release_if_idle(1) is False


async def test_main_menu_greets_by_name(app_ready, fake):
    uid = 5101
    await app_ready.feed_update(bot, H.message_update(uid, "/start"))
    text = fake.of("SendMessage")[-1].text
    assert "Привет, <b>User5101</b>" in text


async def test_profile_shows_progress(app_ready, fake):
    uid = 5102
    await app_ready.feed_update(bot, H.message_update(uid, "/start"))
    await app_ready.feed_update(bot, H.callback_update(uid, "profile"))
    assert "▱" in fake.of("EditMessageText")[-1].text
