"""Единый движок решения по посту: hard-filters -> heuristics -> local AI -> trust -> media.
Пороговые значения намеренно консервативны: неопределённость уходит человеку.
"""
from dataclasses import dataclass
from typing import Optional

from app.auto_score import score_post
from app.local_ai import analyze_text, LocalAIResult

@dataclass(frozen=True)
class Decision:
    score: int
    decision: str
    confidence: float
    reason: str
    ai: LocalAIResult

async def evaluate(text: str, trust: float = 0.0, has_trigger: bool = False, media_ok: bool = True) -> Decision:
    heuristic = score_post(text)
    base = heuristic.score + (10 if has_trigger else 0)
    ai = await analyze_text(text)
    if ai.available:
        semantic = max(-40, min(40, ai.score))
        # AI и эвристика имеют примерно одинаковый вес, trust лишь слегка
        # сдвигает порог для проверенных пользователей.
        combined = round(base * 0.55 + semantic * 1.10 + trust * 0.10)
        positive = ai.positive_similarity > ai.negative_similarity + 0.055
        negative = ai.negative_similarity > ai.positive_similarity + 0.10
        confidence = max(0.0, min(1.0, 0.55 * ai.confidence + 0.45 * (max(0, combined) / 100)))
        if negative:
            decision = "manual"
            reason = "ИИ видит нежелательное намерение"
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
    return Decision(max(-100, min(100, combined)), decision, confidence, reason, ai)
