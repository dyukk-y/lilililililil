"""Проверка обязательного эмодзи человека."""
import pytest

from app.emoji_rules import add_person_emoji, find_person_emoji, has_person_emoji, starts_with_person_emoji
from app.loader import bot
from tests import helpers as H


@pytest.mark.parametrize("text, gender", [
    ("🧑 расскажите про парня", "m"),
    ("👩 расскажите про девушку", "f"),
    ("👨 кто этот мужчина", "m"),          # раньше отклонялось
    ("👦 что за мальчик", "m"),
    ("🧔 кто этот бородач", "m"),
    ("👧 что за девочка", "f"),
    ("🧑🏻 светлый оттенок кожи", "m"),
    ("👩🏿 тёмный оттенок кожи", "f"),
    ("🙋‍♂️ кто знает его", "m"),           # ♂ делает нейтральный эмодзи мужским
    ("💁‍♀️ кто знает её", "f"),
    ("👱‍♀️ блондинка с остановки", "f"),
    ("👩‍💻 девушка с ноутбуком", "f"),
    ("🧑‍🎓 выпускник школы", "m"),
    ("расскажите про парня 👨", "m"),       # эмодзи в конце тоже засчитывается
])
def test_person_emoji_detected(text, gender):
    found = find_person_emoji(text)
    assert found is not None and found[2] == gender


@pytest.mark.parametrize("text", [
    "расскажите про парня",            # нет эмодзи
    "😀 расскажите про парня",          # не человек
    "🙋 кто это вообще",                # человек без пола
    "❤️🔥 кто это",
    "",
    None,
])
def test_no_person_emoji(text):
    assert not has_person_emoji(text)


def test_starts_with_person_emoji_handles_skin_tone_and_spaces():
    assert starts_with_person_emoji("🧑🏻 текст")
    assert starts_with_person_emoji("  👩 текст")
    assert starts_with_person_emoji("🙋‍♀️текст")
    assert not starts_with_person_emoji("текст 👩")


def test_skin_tone_no_longer_loses_format_bonus():
    from app.auto_score import score_post
    assert score_post("🧑🏻 расскажите про этого парня").score == score_post("🧑 расскажите про этого парня").score


def test_add_person_emoji():
    assert add_person_emoji("  кто этот парень ", "m") == "🧑 кто этот парень"
    assert add_person_emoji("кто эта девушка", "f") == "👩 кто эта девушка"


pytestmark_async = pytest.mark.asyncio(loop_scope="session")


@pytestmark_async
async def test_validator_accepts_new_emoji(app_ready):
    from app.validators import validate_post_text
    assert validate_post_text("👨 расскажите про этого мужчину")[0]
    assert validate_post_text("🙋‍♀️ кто знает эту девушку")[0]
    ok, msg = validate_post_text("расскажите про этого мужчину")
    assert not ok and "👨" in msg


@pytestmark_async
async def test_add_emoji_button_fixes_text_post(app_ready, fake):
    uid = 9001
    await app_ready.feed_update(bot, H.message_update(uid, "/start"))
    await app_ready.feed_update(bot, H.callback_update(uid, "no_photo"))
    await app_ready.feed_update(bot, H.message_update(uid, "расскажите про парня в синей куртке"))
    hint = fake.of("SendMessage")[-1]
    assert {"addemoji_m", "addemoji_f"} <= {b.callback_data for r in hint.reply_markup.inline_keyboard for b in r}
    await app_ready.feed_update(bot, H.callback_update(uid, "addemoji_m"))
    preview = fake.of("SendMessage")[-1]
    assert preview.text.startswith("🧑 расскажите про парня")
    assert preview.reply_markup.inline_keyboard[0][0].callback_data == "post_send"
    # Реакция ставится на сообщение пользователя, а не на сообщение бота.
    assert fake.of("SetMessageReaction")[-1].chat_id == uid


@pytestmark_async
async def test_add_emoji_button_keeps_photo(app_ready, fake):
    from aiogram.types import PhotoSize
    uid = 9002
    await app_ready.feed_update(bot, H.message_update(uid, "/start"))
    await app_ready.feed_update(bot, H.callback_update(uid, "with_photo"))
    photo = [PhotoSize(file_id="ph1", file_unique_id="u1", width=10, height=10)]
    await app_ready.feed_update(bot, H.message_update(uid, None, photo=photo, caption="что за девушка у фонтана"))
    await app_ready.feed_update(bot, H.callback_update(uid, "addemoji_f"))
    preview = fake.of("SendPhoto")[-1]
    assert preview.photo == "ph1" and preview.caption.startswith("👩 что за девушка")


@pytestmark_async
async def test_stale_add_emoji_button(app_ready, fake):
    uid = 9003
    await app_ready.feed_update(bot, H.message_update(uid, "/start"))
    await app_ready.feed_update(bot, H.callback_update(uid, "addemoji_m"))
    assert "устарел" in fake.of("AnswerCallbackQuery")[-1].text
