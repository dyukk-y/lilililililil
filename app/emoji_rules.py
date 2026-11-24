"""Распознавание обязательного эмодзи человека в посте.

Правило канала: в посте должен быть эмодзи, показывающий, о ком пост —
о парне или о девушке. Раньше принимались только два символа (🧑 и 👩):
посты с 👨, 👱‍♀️, 🙋‍♂️, 🧔 и т.п. отклонялись, хотя по смыслу верны, а
пост с оттенком кожи (🧑🏻) терял баллы ИИ за «неправильный формат».

Теперь:
- принимаются все эмодзи людей с понятным полом, включая оттенки кожи
  (🏻–🏿), варианты с ♂/♀ (🙋‍♂️, 💁‍♀️) и составные (👩‍💻, 🧑‍🎓);
- нейтральный 🧑 по правилам канала означает парня;
- функции возвращают пол, позицию и «чистый» текст без эмодзи.
"""
from typing import Optional

ZWJ = "‍"
VS16 = "️"
SKIN_TONES = {chr(c) for c in range(0x1F3FB, 0x1F400)}  # 🏻 🏼 🏽 🏾 🏿
MALE_SIGN, FEMALE_SIGN = "♂", "♀"

MALE = set("👨👦👴🧔🤴🤵👲🕺🧑")       # 🧑 по правилам канала = парень
FEMALE = set("👩👧👵👸🧕💃🤰👰🤱")
# Люди без пола: становятся мужскими/женскими только с ♂/♀ (🙋‍♂️, 🙋‍♀️).
NEUTRAL_PERSON = set("🙋💁🙆🙅🤷🤦🙇🙎🙍👱👮💂🕵👷🧙🧚🧛🧜🧝🏃🚶🧍🧎🏄🚴🤸⛹🏋🤽🤾🏌👳👼🧒🧓")

ALLOWED_EXAMPLES = "🧑 👨 👦 🧔 · 👩 👧 👱‍♀️ 🙋‍♀️"


def _sequences(text: str):
    """Разбивает текст на (позиция, эмодзи-последовательность) по ZWJ,
    модификаторам кожи и VS16 — достаточно для эмодзи людей."""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in MALE or ch in FEMALE or ch in NEUTRAL_PERSON:
            start = i
            i += 1
            while i < n and (text[i] in SKIN_TONES or text[i] == VS16 or text[i] == ZWJ):
                if text[i] == ZWJ and i + 1 < n:
                    i += 2  # ZWJ + следующий символ (♂, ♀, 💻, ❤ …)
                else:
                    i += 1
            yield start, text[start:i]
        else:
            i += 1


def _gender(seq: str) -> Optional[str]:
    base = seq[0]
    if FEMALE_SIGN in seq:
        return "f"
    if MALE_SIGN in seq:
        return "m"
    if base in FEMALE:
        return "f"
    if base in MALE:
        return "m"
    return None  # человек без пола (🙋, 💁 …)


def find_person_emoji(text: str) -> Optional[tuple[int, str, str]]:
    """Первый эмодзи человека с понятным полом: (позиция, эмодзи, 'm'|'f')."""
    for pos, seq in _sequences(text or ""):
        gender = _gender(seq)
        if gender:
            return pos, seq, gender
    return None


def has_person_emoji(text: str) -> bool:
    return find_person_emoji(text) is not None


def starts_with_person_emoji(text: str) -> bool:
    """Правильный формат поста: эмодзи человека в самом начале (допускаются
    пробелы перед ним и оттенок кожи/знак пола после него)."""
    stripped = (text or "").lstrip()
    found = find_person_emoji(stripped)
    return bool(found) and found[0] == 0


def add_person_emoji(text: str, gender: str) -> str:
    """Добавляет эмодзи в начало поста (кнопки «🧑 Парень» / «👩 Девушка»)."""
    emoji = "🧑" if gender == "m" else "👩"
    return f"{emoji} {(text or '').strip()}"


def missing_emoji_message() -> str:
    return (
        "❌ <b>Добавьте эмодзи, о ком пост:</b>\n"
        "• парень — 🧑, 👨, 👦, 🧔\n"
        "• девушка — 👩, 👧, 👱‍♀️, 🙋‍♀️\n\n"
        "Подойдёт любой оттенок кожи. Можно просто нажать кнопку ниже — "
        "бот сам добавит эмодзи в начало поста."
    )
