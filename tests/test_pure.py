"""Тесты чистой логики (без Telegram и сети)."""
import pytest

from app.utils import h, shorten
from app.text_quality import detect_gibberish
from app.auto_score import score_post


def test_h_escapes_html_and_handles_none():
    assert h("<b>&") == "&lt;b&gt;&amp;"
    assert h(None) == ""
    assert h(5) == "5"


def test_shorten():
    assert shorten("abc", 10) == "abc"
    assert shorten("abcdef", 4) == "abc…"
    assert shorten(None, 3) == ""


def test_gibberish_detects_keyboard_mash_but_allows_slang():
    assert detect_gibberish("фывапрлджэ ждлорпвфы йцкнгшщз")[0]
    assert not detect_gibberish("🧑 кринж жиза кто знает этого парня")[0]


def test_auto_score_positive_and_negative():
    assert score_post("🧑 расскажите про этого парня").score > 30
    assert score_post("👩 продам телефон дешево").score < 0


def test_validate_post_text_uses_setting_and_photo_limit(app_ready):
    from app.validators import validate_post_text
    from app.runtime_settings import SETTINGS
    ok, _ = validate_post_text("🧑 расскажите про этого парня")
    assert ok
    assert not validate_post_text("🧑 " + "слово " * 40)[0]  # > 100 по умолчанию
    old = SETTINGS["MAX_POST_LENGTH"]
    try:
        SETTINGS["MAX_POST_LENGTH"] = 4000
        long_text = "🧑 расскажите про " + "а" * 1500
        assert validate_post_text(long_text)[0]
        # Подпись к фото в Telegram не длиннее 1024 символов.
        assert not validate_post_text(long_text, has_photo=True)[0]
    finally:
        SETTINGS["MAX_POST_LENGTH"] = old
    assert not validate_post_text("кто это такой вообще")[0]  # нет 🧑/👩
    assert not validate_post_text(None)[0]


def test_review_card_fits_caption_limit(app_ready):
    from app.services import _build_review_text, _TAG_RE
    from html import unescape
    text = _build_review_text(
        "<b>ПОСТ #1</b>\n", "🤖 <b>ИИ</b>\n\n", "🧑 текст <script>" * 20,
        ocr_text="ocr & <stuff> " * 200, ai_reason="причина " * 200, limit=1024,
    )
    assert len(unescape(_TAG_RE.sub("", text))) <= 1024
    assert "&lt;script&gt;" in text  # пользовательский текст экранирован
