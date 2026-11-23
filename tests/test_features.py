"""Предпросмотр, уведомление автору, закреп, готовые причины, антиспам,
сохранение диалогов, /cancel и подсказки."""
import aiosqlite
import pytest
from aiogram.types import Chat

from app.config import DB_NAME
from app.loader import bot
from tests import helpers as H
from tests.conftest import ADMIN_ID

pytestmark = pytest.mark.asyncio(loop_scope="session")
MOD_CHAT = Chat(id=-1001000000002, type="supergroup")
CHANNEL = -1001000000001


async def feed(dp, upd):
    await dp.feed_update(bot, upd)


async def _count_posts(uid):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM posts WHERE user_id=?", (uid,))
        return (await cur.fetchone())[0]


# ---------- Предпросмотр ----------
async def test_preview_edit_then_send_creates_single_post(app_ready, fake):
    uid = 6001
    await feed(app_ready, H.message_update(uid, "/start"))
    await feed(app_ready, H.callback_update(uid, "offer"))
    await feed(app_ready, H.callback_update(uid, "no_photo"))
    await feed(app_ready, H.message_update(uid, "🧑 расскажите про парня в синей кофте"))
    preview = fake.of("SendMessage")[-1]
    assert preview.reply_markup.inline_keyboard[0][0].callback_data == "post_send"
    assert await _count_posts(uid) == 0  # до подтверждения пост не создаётся

    await feed(app_ready, H.callback_update(uid, "post_edit"))
    await feed(app_ready, H.message_update(uid, "🧑 расскажите про парня в красной кофте"))
    await feed(app_ready, H.callback_update(uid, "post_send"))
    await feed(app_ready, H.callback_update(uid, "post_send"))  # двойной клик
    assert await _count_posts(uid) == 1
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT text FROM posts WHERE user_id=?", (uid,))
        assert "красной" in (await cur.fetchone())[0]


async def test_preview_new_text_replaces_draft(app_ready, fake):
    uid = 6002
    await feed(app_ready, H.message_update(uid, "/start"))
    await feed(app_ready, H.callback_update(uid, "no_photo"))
    await feed(app_ready, H.message_update(uid, "🧑 расскажите про парня в кепке"))
    await feed(app_ready, H.message_update(uid, "🧑 расскажите про парня в шапке"))
    await feed(app_ready, H.callback_update(uid, "post_send"))
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT text FROM posts WHERE user_id=?", (uid,))
        rows = await cur.fetchall()
    assert len(rows) == 1 and "шапке" in rows[0][0]


async def test_preview_cancel(app_ready, fake):
    uid = 6003
    await feed(app_ready, H.message_update(uid, "/start"))
    await feed(app_ready, H.callback_update(uid, "no_photo"))
    await feed(app_ready, H.message_update(uid, "🧑 расскажите про этого парня"))
    await feed(app_ready, H.callback_update(uid, "post_cancel"))
    await feed(app_ready, H.callback_update(uid, "post_send"))
    assert await _count_posts(uid) == 0


# ---------- Уведомление автору + закреп ----------
async def _published_post(uid):
    from app.database import create_post
    from app.services import publish_post
    pid = await create_post(uid, "🧑 кто знает этого парня", None)
    ok, _ = await publish_post(pid, moderator_id=ADMIN_ID)
    assert ok
    return pid


async def test_author_notified_on_publish_with_link(app_ready, fake):
    uid = 6004
    pid = await _published_post(uid)
    note = [c for c in fake.of("SendMessage") if int(c.chat_id) == uid][-1]
    assert f"#{pid} опубликован" in note.text
    buttons = [b for row in note.reply_markup.inline_keyboard for b in row]
    assert any(b.url and "t.me/c/1000000001/" in b.url for b in buttons)
    assert any(b.callback_data == f"pin_offer_{pid}" for b in buttons)


async def test_author_notification_can_be_disabled(app_ready, fake):
    from app.runtime_settings import SETTINGS
    SETTINGS["NOTIFY_AUTHOR_ON_PUBLISH"] = 0
    try:
        uid = 6005
        await _published_post(uid)
        assert not [c for c in fake.of("SendMessage") if int(c.chat_id) == uid]
    finally:
        SETTINGS["NOTIFY_AUTHOR_ON_PUBLISH"] = 1


async def test_pin_purchase_flow_and_auto_unpin(app_ready, fake):
    from app.handlers.post_pin import unpin_expired
    uid = 6006
    pid = await _published_post(uid)
    await feed(app_ready, H.message_update(uid, "/start"))
    await feed(app_ready, H.callback_update(uid, f"pin_offer_{pid}"))
    await feed(app_ready, H.callback_update(uid, f"pin_buy_{pid}_3"))
    invoice = fake.of("SendInvoice")[-1]
    assert invoice.payload == f"pin|{pid}|3" and invoice.prices[0].amount == 45

    await feed(app_ready, H.pre_checkout_update(uid, f"pin|{pid}|3", 45))
    assert fake.of("AnswerPreCheckoutQuery")[-1].ok is True
    # Чужой пост закрепить нельзя
    await feed(app_ready, H.pre_checkout_update(uid + 1, f"pin|{pid}|3", 45))
    assert fake.of("AnswerPreCheckoutQuery")[-1].ok is False

    await feed(app_ready, H.payment_update(uid, f"pin|{pid}|3", 45))
    assert fake.of("PinChatMessage")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE post_pins SET expires_at='2000-01-01T00:00:00' WHERE post_id=?", (pid,))
        await db.commit()
    assert await unpin_expired() == 1
    assert fake.of("UnpinChatMessage")


async def test_pin_failure_refunds_stars(app_ready, fake):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import PinChatMessage
    uid = 6007
    pid = await _published_post(uid)
    fake.fail_methods["PinChatMessage"] = TelegramBadRequest(method=PinChatMessage(chat_id=1, message_id=1), message="not enough rights")
    await feed(app_ready, H.payment_update(uid, f"pin|{pid}|1", 15))
    assert fake.of("RefundStarPayment")
    assert "возвращены" in fake.of("SendMessage")[-1].text


# ---------- Готовые причины отказа ----------
async def test_preset_reject_reason_from_moderation_card(app_ready, fake):
    from app.database import create_post, get_post_status
    uid = 6008
    pid = await create_post(uid, "🧑 кто знает этого парня", None)
    await feed(app_ready, H.callback_update(ADMIN_ID, f"rej_{pid}", chat=MOD_CHAT))
    prompt = fake.of("SendMessage")[-1]
    assert any(b.callback_data == f"rejr_{pid}_1" for row in prompt.reply_markup.inline_keyboard for b in row)
    await feed(app_ready, H.callback_update(ADMIN_ID, f"rejr_{pid}_1", chat=MOD_CHAT))
    assert await get_post_status(pid) == "rejected"
    note = [c for c in fake.of("SendMessage") if int(c.chat_id) == uid][-1]
    assert "Реклама или продажа" in note.text


async def test_preset_reject_reason_in_admin_panel(app_ready, fake):
    from app.database import create_post, get_post_status
    uid = 6009
    pid = await create_post(uid, "🧑 кто знает этого парня", None)
    await feed(app_ready, H.callback_update(ADMIN_ID, f"admin_reject_confirm_{pid}"))
    await feed(app_ready, H.callback_update(ADMIN_ID, f"arejr_{pid}_0"))
    await feed(app_ready, H.callback_update(ADMIN_ID, f"admin_reject_send_{pid}"))
    assert await get_post_status(pid) == "rejected"


# ---------- Антиспам ----------
async def test_throttling_blocks_flood(app_ready, fake):
    uid = 6010
    for _ in range(25):
        await feed(app_ready, H.callback_update(uid, "rules"))
    warnings = [a for a in fake.of("AnswerCallbackQuery") if a.text and "Слишком много" in a.text]
    assert len(warnings) == 1
    assert len(fake.of("EditMessageText")) <= 15


async def test_throttling_ignores_admins(app_ready, fake):
    for _ in range(25):
        await feed(app_ready, H.callback_update(ADMIN_ID, "rules"))
    assert not [a for a in fake.of("AnswerCallbackQuery") if a.text and "Слишком много" in a.text]


# ---------- Сохранение диалогов ----------
async def test_fsm_state_survives_restart(app_ready):
    from datetime import datetime
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.types import MessageEntity
    from app.fsm_storage import SQLiteStorage
    key = StorageKey(bot_id=1, chat_id=7, user_id=7)
    s1 = SQLiteStorage(DB_NAME)
    now = datetime(2026, 1, 2, 3, 4, 5)
    await s1.set_state(key, "PostState:wait_text_only")
    await s1.set_data(key, {"photo": "x", "ts": now,
                            "ents": [MessageEntity(type="bold", offset=0, length=3)]})
    await s1.close()

    s2 = SQLiteStorage(DB_NAME)  # «перезапуск»
    assert await s2.get_state(key) == "PostState:wait_text_only"
    data = await s2.get_data(key)
    assert data["ts"] == now and isinstance(data["ents"][0], MessageEntity)
    await s2.set_state(key, None)
    await s2.set_data(key, {})
    await s2.close()
    s3 = SQLiteStorage(DB_NAME)
    assert await s3.get_state(key) is None
    await s3.close()


# ---------- /cancel, /help, подсказки ----------
async def test_cancel_command_clears_state(app_ready, fake):
    from aiogram.fsm.storage.base import StorageKey
    uid = 6011
    await feed(app_ready, H.message_update(uid, "/start"))
    await feed(app_ready, H.callback_update(uid, "no_photo"))
    await feed(app_ready, H.message_update(uid, "/cancel"))
    assert "Действие отменено" in fake.of("SendMessage")[-1].text
    key = StorageKey(bot_id=bot.id, chat_id=uid, user_id=uid)
    assert await app_ready.storage.get_state(key) is None


async def test_unknown_text_gets_hint(app_ready, fake):
    uid = 6012
    await feed(app_ready, H.message_update(uid, "/start"))
    await feed(app_ready, H.message_update(uid, "привет"))
    assert "Не совсем понял" in fake.of("SendMessage")[-1].text


async def test_stale_button_is_answered(app_ready, fake):
    uid = 6013
    await feed(app_ready, H.message_update(uid, "/start"))
    await feed(app_ready, H.callback_update(uid, "some_old_button_v1"))
    assert "устарела" in fake.of("AnswerCallbackQuery")[-1].text


async def test_cancel_in_advertising_master_uses_its_own_handler(app_ready, fake):
    await feed(app_ready, H.message_update(ADMIN_ID, "/post"))
    await feed(app_ready, H.message_update(ADMIN_ID, "/cancel"))
    assert "Создание рекламной публикации отменено" in fake.of("SendMessage")[-1].text
