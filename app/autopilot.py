"""Автопилот модерации.

Цель — почти полная автоматизация без потери контроля:

ТЕКСТ (AUTOPILOT_TEXT=1):
- обученная модель уверена, что пост одобрят (≥ AUTO_PUBLISH_PROB) →
  пост сразу встаёт в очередь публикации;
- модель уверена, что такое отклоняют (≤ AUTO_REJECT_PROB) → мгновенный
  отказ с вежливым объяснением;
- остальное — модератору, но не на 24 ч: через AUTOPILOT_DECIDE_HOURS
  (по умолчанию 2 ч) решение без модератора принимает автопилот.

ФОТО (AUTOPILOT_TRUSTED_PHOTOS=1):
- автоматически ставятся в очередь только фото ДОВЕРЕННЫХ авторов
  (≥ TRUSTED_MIN_PUBLISHED опубликованных постов, ни одного отказа
  модератором за 30 дней, не в бане) и только если все проверки чистые
  (NudeNet, текст на фото, повторы, ИИ);
- карточка всё равно приходит модераторам: до времени публикации пост
  можно отклонить. Возраст человека на фото автоматика НЕ проверяет —
  это осознанный риск режима, поэтому он ограничен доверенными авторами.

Решения автопилота записываются с moderator_id=0 и НЕ попадают в
обучающую выборку — модель учится только на решениях людей.
"""
import logging
from datetime import datetime, timedelta, date
from typing import Optional

import aiosqlite

from app import ai_learning
from app.config import DB_NAME, TIMEZONE
from app.runtime_settings import get as get_setting

logger = logging.getLogger(__name__)

AUTO_PUBLISH_PROB = 0.80
AUTO_REJECT_PROB = 0.10
TIMEOUT_PUBLISH_PROB = 0.50   # на таймауте: публикуем, если «скорее да»
TIMEOUT_MIN_SCORE = 30        # если модель ещё не обучена — по баллу ИИ

AUTO_REJECT_REASON = (
    "Пост не прошёл автоматическую проверку: похожие публикации ранее "
    "отклонялись. Если считаете это ошибкой — напишите администрации."
)


def text_decision(decision) -> Optional[str]:
    """'publish' / 'reject' / None (нужен человек) для текстового поста."""
    if not get_setting("AUTOPILOT_TEXT"):
        return None
    p = decision.learned
    if p is None:
        return None
    if p <= AUTO_REJECT_PROB:
        return "reject"
    if p >= AUTO_PUBLISH_PROB and not decision.blocked and decision.score >= 20:
        return "publish"
    return None


def timeout_should_publish(text: str, ai_score: Optional[int]) -> bool:
    """Решение по тексту, на который модератор не ответил вовремя."""
    p = ai_learning.predict(text or "")
    if p is not None:
        return p >= TIMEOUT_PUBLISH_PROB
    return (ai_score or 0) >= TIMEOUT_MIN_SCORE


async def is_trusted_author(user_id: int) -> bool:
    min_published = int(get_setting("TRUSTED_MIN_PUBLISHED"))
    since = (datetime.now(TIMEZONE).replace(tzinfo=None) - timedelta(days=30)).isoformat()
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT 1 FROM bans WHERE user_id=?", (user_id,))
        if await cur.fetchone():
            return False
        cur = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE user_id=? AND status='published'", (user_id,))
        published = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM posts WHERE user_id=? AND status IN ('rejected','deleted') "
            "AND moderator_id > 0 AND moderation_time >= ?", (user_id, since))
        rejected_recently = (await cur.fetchone())[0]
    return published >= min_published and rejected_recently == 0


def photo_allowed(decision, media_ok: bool) -> bool:
    if not get_setting("AUTOPILOT_TRUSTED_PHOTOS") or not media_ok or decision.blocked:
        return False
    return decision.learned is None or decision.learned >= TIMEOUT_PUBLISH_PROB
