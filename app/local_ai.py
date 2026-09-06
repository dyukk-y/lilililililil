"""Лёгкий локальный анализатор текста без тяжёлых ML-моделей.

Предыдущая реализация загружала SentenceTransformer/PyTorch и могла занимать
сотни мегабайт RAM. Для модерации этого проекта такая модель не обязательна:
основной скоринг уже выполняется в auto_score.py. Здесь используется компактный
лексический семантический слой с кэшированными наборами токенов, который почти
не потребляет память и не требует загрузки моделей из сети.
"""
import asyncio
import re
from dataclasses import dataclass
from typing import List

TOKEN_RE = re.compile(r"[\w@#-]+", re.UNICODE)

POSITIVE_PROTOTYPES = [
    "Расскажите про этого человека", "Кто такой этот парень и кто его знает",
    "Кто такая эта девушка", "Что за мальчик на фотографии",
    "Что за девочка расскажите о ней", "Кто знает этого человека",
    "Подскажите как его зовут", "Подскажите как её зовут",
    "Можно познакомиться с этим парнем", "Мне понравилась эта девушка хочу познакомиться",
    "Есть ли у него девушка", "Есть ли у неё парень", "Дайте его юзернейм",
    "Скиньте её юз в личку", "Кто был на этом мероприятии", "Кто едет туда сегодня",
    "Видели этого человека в городе",
]
NEGATIVE_PROTOTYPES = [
    "Продам товар цена и доставка", "Куплю вещь или услугу",
    "Отдам котят или щенков", "Потерял телефон карту или документы",
    "Нашёл чужую вещь помогите найти владельца",
    "Реклама услуги маникюра репетитора или ремонта",
    "Объявление о продаже или покупке", "Ищу работу или предлагаю работу",
]


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in TOKEN_RE.findall(text or "") if len(t) > 2}


def _prototype_tokens(items: list[str]) -> list[set[str]]:
    return [_tokens(x) for x in items]

POSITIVE = _prototype_tokens(POSITIVE_PROTOTYPES)
NEGATIVE = _prototype_tokens(NEGATIVE_PROTOTYPES)


@dataclass(frozen=True)
class LocalAIResult:
    available: bool
    score: int
    positive_similarity: float
    negative_similarity: float
    confidence: float
    reasons: List[str]


def _similarity(tokens: set[str], prototype: set[str]) -> float:
    if not tokens or not prototype:
        return 0.0
    # Overlap coefficient is stable for short Telegram messages.
    return len(tokens & prototype) / max(1, min(len(tokens), len(prototype)))


def _analyze_sync(text: str) -> LocalAIResult:
    tokens = _tokens(text[:4000])
    if not tokens:
        return LocalAIResult(True, 0, 0.0, 0.0, 0.0, ["пустой текст"])

    pos = max(_similarity(tokens, p) for p in POSITIVE)
    neg = max(_similarity(tokens, p) for p in NEGATIVE)

    # Small margin keeps this layer conservative; auto_score remains primary.
    raw = (pos - neg) * 40.0
    score = int(max(-40, min(40, round(raw))))
    margin = abs(pos - neg)
    confidence = max(0.25, min(0.95, 0.30 + margin * 0.9))

    reasons = [
        f"Локальный анализ: положительное сходство {pos:.2f}",
        f"Локальный анализ: отрицательное сходство {neg:.2f}",
    ]
    if pos >= neg + 0.15:
        reasons.append("лексический анализ ближе к допустимому посту о человеке")
    elif neg >= pos + 0.15:
        reasons.append("лексический анализ ближе к нежелательному объявлению")
    else:
        reasons.append("локальный анализ не уверен")
    return LocalAIResult(True, score, pos, neg, confidence, reasons)


async def analyze_text(text: str) -> LocalAIResult:
    text = (text or "").strip()
    if not text:
        return LocalAIResult(True, 0, 0.0, 0.0, 0.25, ["пустой текст"])
    return await asyncio.to_thread(_analyze_sync, text)


async def preload() -> bool:
    """Совместимость со старым startup-кодом: ничего тяжёлого не загружает."""
    return True
