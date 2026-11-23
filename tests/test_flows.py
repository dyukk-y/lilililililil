"""Сквозные сценарии: апдейт Telegram -> middleware -> хендлер -> БД/API."""
import pytest
import aiosqlite

from app.config import DB_NAME
from app.loader import bot
from tests.conftest import ADMIN_ID
from tests import helpers as H

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _feed(dp, update):
    await dp.feed_update(bot, update)


async def _new_user(uid, username=None):
    from app.database import register_user
    await register_user(H.user(uid, username))


async def _make_post(uid, text="🧑 расскажите про этого парня", status="moderation"):
    from app.database import create_post
    pid = await create_post(uid, text, None)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE posts SET status=? WHERE id=?", (status, pid))
        await db.commit()
    return pid


# ---------- Платная разблокировка заблокированного пользователя ----------
async def test_banned_user_can_open_unlock_invoice_and_get_unbanned(app_ready, fake):
    from app.database import ban_user, is_banned
    uid = 3001
    await _new_user(uid)
    await ban_user(uid, "спам <b>", H.user(ADMIN_ID))

    # /start показывает причину (с экранированием) и кнопку разблокировки
    await _feed(app_ready, H.message_update(uid, "/start"))
    sent = fake.of("SendMessage")[-1]
    assert "&lt;b&gt;" in sent.text
    assert sent.reply_markup.inline_keyboard[0][0].callback_data == "unlock_bot"

    # Раньше middleware отвечал «Вы заблокированы» и счёт не выставлялся.
    await _feed(app_ready, H.callback_update(uid, "unlock_bot"))
    assert fake.of("SendInvoice"), fake.names()

    # pre_checkout подтверждается для реально заблокированного
    await _feed(app_ready, H.pre_checkout_update(uid, "unlock|bot"))
    assert fake.of("AnswerPreCheckoutQuery")[-1].ok is True

    # successful_payment больше не отбрасывается middleware
    await _feed(app_ready, H.payment_update(uid, "unlock|bot"))
    assert not await is_banned(uid)


async def test_unlock_pre_checkout_rejected_when_not_banned(app_ready, fake):
    uid = 3002
    await _new_user(uid)
    await _feed(app_ready, H.pre_checkout_update(uid, "unlock|bot"))
    answer = fake.of("AnswerPreCheckoutQuery")[-1]
    assert answer.ok is False


# ---------- Приоритетное ускорение ----------
async def test_priority_pre_checkout_rejects_processed_post(app_ready, fake):
    uid = 3003
    await _new_user(uid)
    pid = await _make_post(uid, status="published")
    await _feed(app_ready, H.pre_checkout_update(uid, f"priority|{pid}|{uid}"))
    assert fake.of("AnswerPreCheckoutQuery")[-1].ok is False

    pid2 = await _make_post(uid, status="approved")
    await _feed(app_ready, H.pre_checkout_update(uid, f"priority|{pid2}|{uid}"))
    assert fake.of("AnswerPreCheckoutQuery")[-1].ok is True

    # Чужой пост ускорить нельзя
    await _feed(app_ready, H.pre_checkout_update(uid + 1, f"priority|{pid2}|{uid + 1}"))
    assert fake.of("AnswerPreCheckoutQuery")[-1].ok is False


async def test_priority_admin_buttons_publish_and_reject(app_ready, fake):
    from app.database import get_post_status
    uid = 3004
    await _new_user(uid)
    pid = await _make_post(uid, status="approved")

    # Раньше у кнопки не было обработчика вообще.
    await _feed(app_ready, H.callback_update(ADMIN_ID, f"priority_pub_{pid}"))
    assert await get_post_status(pid) == "published"
    channel_posts = [c for c in fake.of("SendMessage") if int(c.chat_id) == -1001000000001]
    assert channel_posts, fake.names()

    pid2 = await _make_post(uid, status="approved")
    await _feed(app_ready, H.callback_update(ADMIN_ID, f"priority_rej_{pid2}"))
    await _feed(app_ready, H.message_update(ADMIN_ID, "не подходит <формат>"))
    await _feed(app_ready, H.callback_update(ADMIN_ID, f"admin_reject_send_{pid2}"))
    assert await get_post_status(pid2) == "rejected"
    notice = [c for c in fake.of("SendMessage") if int(c.chat_id) == uid][-1]
    assert "&lt;формат&gt;" in notice.text


async def test_priority_buttons_forbidden_for_non_admin(app_ready, fake):
    from app.database import get_post_status
    uid = 3005
    await _new_user(uid)
    pid = await _make_post(uid, status="approved")
    from app.middlewares import _subscription_ok_until
    await _feed(app_ready, H.callback_update(uid, f"priority_pub_{pid}"))
    assert await get_post_status(pid) == "approved"


# ---------- Расшифровка автора: защита от двойной оплаты ----------
async def test_reveal_pre_checkout_rejects_repeat_purchase(app_ready, fake):
    from app.database import record_star_purchase
    author, buyer = 3006, 3007
    await _new_user(author)
    await _new_user(buyer)
    pid = await _make_post(author, status="published")
    payload = f"reveal|{pid}|{buyer}|1"
    await _feed(app_ready, H.pre_checkout_update(buyer, payload))
    assert fake.of("AnswerPreCheckoutQuery")[-1].ok is True
    await record_star_purchase(buyer, "author_reveal", pid, 100, "ch_reveal_1")
    await _feed(app_ready, H.pre_checkout_update(buyer, payload))
    assert fake.of("AnswerPreCheckoutQuery")[-1].ok is False


# ---------- Пожертвование: имя с HTML не ломает благодарность ----------
async def test_donation_with_html_name(app_ready, fake):
    uid = 3008
    await _new_user(uid)
    upd = H.payment_update(uid, f"donation|{uid}|10", amount=10)
    upd.message.from_user.__dict__["first_name"] = "<Вася & Co>"
    await _feed(app_ready, upd)
    texts = [c.text for c in fake.of("SendMessage")]
    assert any("Спасибо за пожертвование" in (t or "") for t in texts), texts
    admin_note = [c for c in fake.of("SendMessage") if int(c.chat_id) == -1001000000003]
    assert admin_note and "&lt;Вася &amp; Co&gt;" in admin_note[-1].text


# ---------- Кнопки диагностики ИИ ----------
async def test_why_ai_and_ai_error_buttons_answer(app_ready, fake):
    from app.database import set_ai_analysis
    from aiogram.types import Chat
    uid = 3009
    await _new_user(uid)
    pid = await _make_post(uid)
    await set_ai_analysis(pid, 42, 0.7, "manual", "тест")
    mod_chat = Chat(id=-1001000000002, type="supergroup")
    await _feed(app_ready, H.callback_update(ADMIN_ID, f"why_ai_{pid}", chat=mod_chat))
    assert "42/100" in fake.of("AnswerCallbackQuery")[-1].text

    adm_chat = Chat(id=-1001000000003, type="supergroup")
    await _feed(app_ready, H.callback_update(ADMIN_ID, f"ai_error_{pid}", chat=adm_chat))
    assert "учтена" in fake.of("AnswerCallbackQuery")[-1].text


# ---------- Счётчики админ-панели ----------
async def test_admin_panel_counts_all_banned(app_ready, fake):
    from app.database import ban_user
    for uid in (3101, 3102, 3103):
        await _new_user(uid)
        await ban_user(uid, "x", H.user(ADMIN_ID))
    await _feed(app_ready, H.message_update(ADMIN_ID, "/admin"))
    text = fake.of("SendMessage")[-1].text
    import re
    banned = int(re.search(r"банов: <b>(\d+)</b>", text).group(1))
    assert banned >= 3  # раньше всегда было 0 или 1


# ---------- Стоп-слова ----------
async def test_blacklist_trailing_space_word_matches_at_end_and_can_be_removed(app_ready, fake):
    from app.database import is_in_publication_blacklist, get_publication_blacklist
    assert (await is_in_publication_blacklist("🧑 ты лох"))[0]
    rows, _ = await get_publication_blacklist(page=1, per_page=100)
    idx = [r[0] for r in rows].index("лох ")
    await _feed(app_ready, H.callback_update(ADMIN_ID, f"remove_blacklist_idx_{idx}"))
    rows, _ = await get_publication_blacklist(page=1, per_page=100)
    assert "лох " not in [r[0] for r in rows]


async def test_remove_blacklist_menu_uses_short_callback_data(app_ready, fake):
    from app.database import add_to_publication_blacklist
    await add_to_publication_blacklist("очень длинная запрещённая фраза для проверки лимита", ADMIN_ID)
    await _feed(app_ready, H.callback_update(ADMIN_ID, "remove_pub_blacklist"))
    markup = fake.of("EditMessageText")[-1].reply_markup
    for row in markup.inline_keyboard:
        for btn in row:
            assert len(btn.callback_data.encode()) <= 64


# ---------- Посты: нетекстовое сообщение и лимит ----------
async def test_sticker_instead_of_text_does_not_crash(app_ready, fake):
    from aiogram.types import Sticker
    uid = 3010
    await _new_user(uid)
    await _feed(app_ready, H.callback_update(uid, "offer"))
    await _feed(app_ready, H.callback_update(uid, "no_photo"))
    sticker = Sticker(file_id="s", file_unique_id="s", type="regular", width=1, height=1,
                      is_animated=False, is_video=False)
    await _feed(app_ready, H.message_update(uid, None, sticker=sticker))
    last = fake.of("SendMessage")[-1].text
    assert "Пришлите текст" in last, last


async def test_text_post_full_flow(app_ready, fake):
    uid = 3011
    await _new_user(uid)
    await _feed(app_ready, H.callback_update(uid, "offer"))
    await _feed(app_ready, H.callback_update(uid, "no_photo"))
    await _feed(app_ready, H.message_update(uid, "🧑 расскажите про парня <в кепке>"))
    await _feed(app_ready, H.callback_update(uid, "post_send"))
    texts = [c.text or "" for c in fake.of("SendMessage")]
    assert any("отправлен на модерацию" in t for t in texts), texts
    card = [c for c in fake.of("SendMessage") if int(c.chat_id) == -1001000000002]
    assert card and "&lt;в кепке&gt;" in card[-1].text


async def test_daily_limit_from_settings(app_ready, fake):
    from app.runtime_settings import SETTINGS
    uid = 3012
    await _new_user(uid)
    old = SETTINGS["USER_DAILY_POST_LIMIT"]
    SETTINGS["USER_DAILY_POST_LIMIT"] = 1
    try:
        await _make_post(uid)
        await _feed(app_ready, H.callback_update(uid, "offer"))
        assert "Лимит: 1" in (fake.of("AnswerCallbackQuery")[-1].text or "")
    finally:
        SETTINGS["USER_DAILY_POST_LIMIT"] = old


# ---------- Повторный клик не показывает «ошибку» ----------
async def test_message_not_modified_is_silent(app_ready, fake):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText
    uid = 3013
    await _new_user(uid)
    fake.fail_methods["EditMessageText"] = TelegramBadRequest(
        method=EditMessageText(text="x"), message="Bad Request: message is not modified")
    await _feed(app_ready, H.callback_update(uid, "rules"))
    answers = fake.of("AnswerCallbackQuery")
    assert not any("ошибка" in (a.text or "").lower() for a in answers)
