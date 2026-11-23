"""Единый движок решения по посту: hard-filters -> heuristics -> local AI -> trust -> media.
Пороговые значения намеренно консервативны: неопределённость уходит человеку.
"""
from dataclasses import dataclass
from typing import Optional

from app.auto_score import score_post
from app.local_ai import analyze_text, LocalAIResult
from app import ai_learning

# Пороги самообучаемой модели (вероятность одобрения модератором).
LEARNED_REJECT_BELOW = 0.20   # уверена, что такое отклоняют -> на ручную проверку
LEARNED_BOOST_ABOVE = 0.85    # уверена, что такое одобряют -> небольшой бонус

@dataclass(frozen=True)
class Decision:
    score: int
    decision: str
    confidence: float
    reason: str
    ai: LocalAIResult
    learned: Optional[float] = None  # вероятность одобрения по обученной модели
    blocked: bool = False  # ИИ или модель видят явную проблему — автопилоту нельзя

async def evaluate(text: str, trust: float = 0.0, has_trigger: bool = False, media_ok: bool = True) -> Decision:
    heuristic = score_post(text)
    base = heuristic.score + (10 if has_trigger else 0)
    learned = ai_learning.predict(text)
    if learned is not None and learned >= LEARNED_BOOST_ABOVE:
        base += 8
    ai = await analyze_text(text)
    if ai.available:
        semantic = max(-40, min(40, ai.score))
        # AI и эвристика имеют примерно одинаковый вес, trust лишь слегка
        # сдвигает порог для проверенных пользователей.
        combined = round(base * 0.55 + semantic * 1.10 + trust * 0.10)
        positive = ai.positive_similarity > ai.negative_similarity + 0.055
        negative = ai.negative_similarity > ai.positive_similarity + 0.10
        confidence = max(0.0, min(1.0, 0.55 * ai.confidence + 0.45 * (max(0, combined) / 100)))

        # Ключевая фраза — только кандидат на автопубликацию, а не безусловный
        # пропуск. Смотрим на весь контекст: если одновременно нет признаков
        # нежелательного объявления/услуги/поиска вещей и ИИ не видит
        # отрицательного намерения, такой пост можно пропустить даже при новой
        # формулировке, которой ещё нет в эвристических шаблонах.
        contextual_trigger = (
            has_trigger
            and media_ok
            and heuristic.score >= 15
            and not any(reason.lstrip().startswith("-") for reason in heuristic.reasons)
            and ai.negative_similarity < 0.45
            and not negative
            and len((text or '').strip()) >= 12
        )

        if negative:
            decision = "manual"
            reason = "ИИ видит нежелательное намерение"
        elif learned is not None and learned < LEARNED_REJECT_BELOW:
            decision = "manual"
            reason = f"обученная модель: похожие посты модераторы отклоняли ({learned:.0%} за одобрение)"
        elif contextual_trigger and (positive or combined >= 30 or ai.confidence >= 0.55):
            decision = "auto"
            reason = "ключевая фраза подтверждена безопасным контекстом"
        elif combined >= 58 and positive and media_ok and confidence >= 0.68:
            decision = "auto"
            reason = "высокая совокупная уверенность"
        elif combined >= 72 and positive and media_ok and confidence >= 0.62 and trust >= 25:
            decision = "auto"
            reason = "высокая уверенность с учётом репутации пользователя"
        else:
            decision = "manual"
            reason = "недостаточная совокупная уверенность"
    else:
        combined = round(base + trust * 0.05)
        confidence = 0.25
        decision = "manual"
        reason = "локальный ИИ недоступен"
    if learned is not None and decision == "auto":
        reason += f"; обученная модель: {learned:.0%} за одобрение"
    blocked = reason.startswith("ИИ видит") or reason.startswith("обученная модель: похожие")
    return Decision(max(-100, min(100, combined)), decision, confidence, reason, ai, learned, blocked)
