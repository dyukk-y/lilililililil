"""Распознавание обязательного эмодзи в посте.

Правило канала: в посте должен быть ровно один из двух эмодзи —
🧑 (парень) или 👩 (девушка). Никакие другие эмодзи людей (👨, 👧, 🙋‍♀️…)
публикацию не разрешают.

Допускается оттенок кожи (🧑🏻 … 👩🏿) — это тот же эмодзи другого цвета.
Составные эмодзи (👩‍💻, 🧑‍🎓, 👩‍❤️‍👨) НЕ засчитываются: это уже другие
изображения, а не 🧑/👩.
"""
from typing import Optional

ZWJ = "‍"
VS16 = "️"
SKIN_TONES = {chr(c) for c in range(0x1F3FB, 0x1F400)}  # 🏻 🏼 🏽 🏾 🏿

MALE_EMOJI = "🧑"
FEMALE_EMOJI = "👩"
_GENDER = {MALE_EMOJI: "m", FEMALE_EMOJI: "f"}


def find_person_emoji(text: str) -> Optional[tuple[int, str, str]]:
    """Первый допустимый эмодзи: (позиция, эмодзи, 'm'|'f') или None."""
    text = text or ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _GENDER:
            start, j = i, i + 1
            while j < n and (text[j] in SKIN_TONES or text[j] == VS16):
                j += 1
            joined = (j < n and text[j] == ZWJ) or (start > 0 and text[start - 1] == ZWJ)
            if not joined:
                return start, text[start:j], _GENDER[ch]
            # Часть составного эмодзи (👩‍💻) — пропускаем всю последовательность.
            while j < n and text[j] == ZWJ:
                j += 2
                while j < n and (text[j] in SKIN_TONES or text[j] == VS16):
                    j += 1
            i = j
            continue
        i += 1
    return None


def has_person_emoji(text: str) -> bool:
    return find_person_emoji(text) is not None


def starts_with_person_emoji(text: str) -> bool:
    """Правильный формат: 🧑/👩 в самом начале поста."""
    stripped = (text or "").lstrip()
    found = find_person_emoji(stripped)
    return bool(found) and found[0] == 0


def add_person_emoji(text: str, gender: str) -> str:
    """Добавляет эмодзи в начало поста (кнопки «🧑 Это парень» / «👩 Это девушка»)."""
    emoji = MALE_EMOJI if gender == "m" else FEMALE_EMOJI
    return f"{emoji} {(text or '').strip()}"


def missing_emoji_message() -> str:
    return (
        "❌ <b>Добавьте эмодзи, о ком пост:</b>\n"
        "• 🧑 — парень\n"
        "• 👩 — девушка\n\n"
        "Другие эмодзи (👨, 👧, 🙋‍♀️ и т.п.) не подходят. Можно просто нажать "
        "кнопку ниже — бот сам добавит нужный эмодзи в начало поста."
    )
