"""Распознавание обязательного эмодзи в посте.

Правило канала: пост должен НАЧИНАТЬСЯ с одного из двух эмодзи —
🧑 (парень) или 👩 (девушка). Никакие другие эмодзи людей (👨, 👧, 🙋‍♀️…)
публикацию не разрешают.

Засчитываются только «чистые» жёлтые 🧑 и 👩: варианты с оттенком кожи
(🧑🏻 … 👩🏿) и составные эмодзи (👩‍💻, 🧑‍🎓, 👩‍❤️‍👨) не подходят.
"""
import re
from typing import Optional

ZWJ = "‍"
VS16 = "️"
SKIN_TONES = {chr(c) for c in range(0x1F3FB, 0x1F400)}  # 🏻 🏼 🏽 🏾 🏿

MALE_EMOJI = "🧑"
FEMALE_EMOJI = "👩"
_GENDER = {MALE_EMOJI: "m", FEMALE_EMOJI: "f"}


def find_person_emoji(text: str) -> Optional[tuple[int, str, str]]:
    """Первый допустимый эмодзи: (позиция, эмодзи, 'm'|'f') или None.

    Засчитываются только «чистые» жёлтые 🧑 и 👩. Вариант с оттенком кожи
    (🧑🏻) и составные эмодзи (👩‍💻) — нет."""
    text = text or ""
    n = len(text)
    for i, ch in enumerate(text):
        if ch not in _GENDER:
            continue
        j = i + 1
        if j < n and text[j] == VS16:
            j += 1  # невидимый селектор варианта не меняет вид эмодзи
        if j < n and text[j] in SKIN_TONES:
            continue  # оттенок кожи — не подходит
        if (j < n and text[j] == ZWJ) or (i > 0 and text[i - 1] == ZWJ):
            continue  # часть составного эмодзи
        return i, text[i:j], _GENDER[ch]
    return None


def has_person_emoji(text: str) -> bool:
    return find_person_emoji(text) is not None


def starts_with_person_emoji(text: str) -> bool:
    """Правильный формат: 🧑/👩 в самом начале поста."""
    stripped = (text or "").lstrip()
    found = find_person_emoji(stripped)
    return bool(found) and found[0] == 0


def add_person_emoji(text: str, gender: str) -> str:
    """Ставит эмодзи в начало поста (кнопки «🧑 Это парень» / «👩 Это девушка»).
    Если 🧑/👩 уже были где-то в тексте не на своём месте — убирает их,
    чтобы эмодзи остался только в начале."""
    emoji = MALE_EMOJI if gender == "m" else FEMALE_EMOJI
    body = (text or "").strip()
    while True:
        found = find_person_emoji(body)
        if not found:
            break
        pos, seq, _ = found
        body = (body[:pos] + body[pos + len(seq):]).strip()
    body = re.sub(r"[ \t]{2,}", " ", body)  # переносы строк не трогаем
    return f"{emoji} {body}"


def missing_emoji_message() -> str:
    return (
        "❌ <b>Начните пост с эмодзи, о ком он:</b>\n"
        "• 🧑 — парень\n"
        "• 👩 — девушка\n\n"
        "Эмодзи должен стоять в самом начале. Другие эмодзи и варианты с "
        "оттенком кожи (👨, 👧, 🧑🏻 и т.п.) не подходят. Можно просто нажать "
        "кнопку ниже — бот сам поставит эмодзи в начало."
    )
