"""
Эвристическая проверка текста поста на "полный бред" — случайный набор
символов (например, мешанина по клавиатуре: "фываолдж", "ждкжкдж").

Сознательно НЕ используется проверка по словарю (напр. pymorphy) — на
тесте она даёт слишком много ложных срабатываний на совершенно нормальном
молодёжном сленге и опечатках ("кринж", "жиза", "го", "превет" вместо
"привет" и т.п.), которые в этом боте — норма, а не исключение. Вместо
этого используются фонетические признаки, не зависящие от словаря:

- В "слове" из 4+ букв вообще нет гласных — для русского языка это
  практически гарантированный признак мешанины по клавиатуре, а не
  реального слова (даже сленгового).
- Слишком длинная последовательность согласных подряд (5+).
- Один и тот же символ повторяется подряд 4+ раза ("аааааа", "жжжжж").

Решение принимается по ДОЛЕ таких "плохих" слов среди всех достаточно
длинных слов в тексте — единичное экзотическое слово (аббревиатура,
редкое имя) не режет весь пост.
"""
import re
from typing import Tuple

VOWELS = set("аеёиоуыэюяaeiouy")


def _analyze_word(word: str) -> dict:
    letters = [c for c in word.lower() if c.isalpha()]
    if len(letters) < 4:
        return {"vowel_ratio": 1.0, "max_consonant_run": 0, "max_char_run": 1}

    vowels = sum(1 for c in letters if c in VOWELS)

    max_consonant_run = 0
    run = 0
    for c in letters:
        if c in VOWELS:
            run = 0
        else:
            run += 1
            max_consonant_run = max(max_consonant_run, run)

    max_char_run = 1
    run2 = 1
    for i in range(1, len(letters)):
        if letters[i] == letters[i - 1]:
            run2 += 1
            max_char_run = max(max_char_run, run2)
        else:
            run2 = 1

    return {
        "vowel_ratio": vowels / len(letters),
        "max_consonant_run": max_consonant_run,
        "max_char_run": max_char_run,
    }


def _is_bad_word(word: str) -> bool:
    info = _analyze_word(word)
    return info["vowel_ratio"] == 0 or info["max_consonant_run"] >= 5 or info["max_char_run"] >= 4


def detect_gibberish(text: str) -> Tuple[bool, str]:
    """Возвращает (is_gibberish, причина-для-лога)."""
    if not text:
        return False, ""

    # Ссылки и упоминания не разбираем как "слова"
    clean = re.sub(r"(https?://\S+|t\.me/\S+|@\w+)", " ", text)
    raw_words = re.findall(r"[^\W\d_]+", clean, flags=re.UNICODE)
    words = [w for w in raw_words if len(w) >= 4]

    if not words:
        return False, ""

    bad_words = [w for w in words if _is_bad_word(w)]

    if len(words) == 1:
        is_bad = bool(bad_words)
    else:
        is_bad = len(bad_words) >= 2 and (len(bad_words) / len(words)) >= 0.4

    if is_bad:
        return True, f"не похоже на осмысленный текст: {', '.join(bad_words[:3])}"

    return False, ""
