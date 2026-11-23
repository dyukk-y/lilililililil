"""Самообучающийся классификатор постов (наивный Байес).

Учится на РЕШЕНИЯХ ЛЮДЕЙ: какие посты модераторы опубликовали, а какие
отклонили. Чем дольше работает бот, тем точнее модель предсказывает решение
модератора для нового поста.

Почему именно так (тариф 1 ГБ RAM / 2 vCPU):
- чистый Python, без torch/sklearn — модель занимает единицы мегабайт;
- обучение на 5000 постах занимает доли секунды и идёт в отдельном потоке;
- предсказание — микросекунды, сеть и внешние API не нужны (бесплатно).

Модель работает консервативно (см. decision_engine): она может отправить
подозрительный пост на ручную проверку или немного усилить уверенность,
но сама по себе никогда не публикует пост в обход остальных проверок.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

import aiosqlite

from app.config import DB_NAME

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-zа-яё0-9@]+", re.IGNORECASE)

MAX_SAMPLES = 5000          # последние N решений — свежие предпочтения модераторов
MIN_SAMPLES_PER_CLASS = 25  # меньше — модель «не готова» и не влияет на решения
RETRAIN_INTERVAL_SECONDS = 30 * 60


def features(text: str) -> list[str]:
    """Слова + символьные 3-граммы слов. Триграммы делают модель устойчивой
    к русской морфологии и опечаткам ("понравилась"/"понравился")."""
    words = [w.lower().replace("ё", "е") for w in _WORD_RE.findall(text or "")]
    result: list[str] = []
    for w in words:
        result.append("w:" + w)
        if len(w) >= 4 and not w.startswith("@"):
            padded = f"^{w}$"
            result.extend("c:" + padded[i:i + 3] for i in range(len(padded) - 2))
    return result


@dataclass
class NaiveBayesModel:
    counts: dict[str, Counter] = field(default_factory=lambda: {"ok": Counter(), "bad": Counter()})
    totals: dict[str, int] = field(default_factory=lambda: {"ok": 0, "bad": 0})
    docs: dict[str, int] = field(default_factory=lambda: {"ok": 0, "bad": 0})
    vocab_size: int = 0
    trained_at: float = 0.0

    @property
    def ready(self) -> bool:
        return min(self.docs["ok"], self.docs["bad"]) >= MIN_SAMPLES_PER_CLASS

    @property
    def samples(self) -> int:
        return self.docs["ok"] + self.docs["bad"]

    def fit(self, samples: Iterable[tuple[str, str]]) -> "NaiveBayesModel":
        for text, label in samples:
            feats = features(text)
            self.counts[label].update(feats)
            self.totals[label] += len(feats)
            self.docs[label] += 1
        self.vocab_size = len(set(self.counts["ok"]) | set(self.counts["bad"])) or 1
        self.trained_at = time.time()
        return self

    def prob_ok(self, text: str) -> float:
        """Вероятность того, что модератор ОДОБРИТ пост (0..1)."""
        feats = features(text)
        total_docs = self.samples or 1
        log_scores = {}
        for label in ("ok", "bad"):
            # Априорная вероятность сглажена: перекос в данных (например,
            # 90% одобрений) не должен сам по себе диктовать решение.
            prior = (self.docs[label] + 1) / (total_docs + 2)
            score = math.log(prior)
            denom = self.totals[label] + self.vocab_size
            counter = self.counts[label]
            for f in feats:
                score += math.log((counter.get(f, 0) + 1) / denom)
            log_scores[label] = score
        diff = log_scores["bad"] - log_scores["ok"]
        diff = max(-50.0, min(50.0, diff))
        return 1.0 / (1.0 + math.exp(diff))


_model: Optional[NaiveBayesModel] = None
_lock = asyncio.Lock()


async def _load_samples() -> list[tuple[str, str]]:
    """Обучающая выборка — только решения людей.

    - published, опубликованные человеком (moderator_id > 0) -> ok;
    - rejected человеком (moderator_id > 0; автоотказ по таймауту — это не
      мнение модератора, его не берём) -> bad;
    - удалённые по заявке посты -> bad;
    - посты, где админ нажал «🤖 ИИ ошибся» после автопубликации -> bad.
    Автопубликации без участия человека не берём, чтобы модель не училась
    сама у себя.
    """
    query = """
        SELECT p.text,
               CASE
                   WHEN EXISTS (SELECT 1 FROM ai_corrections c WHERE c.post_id = p.id) THEN 'bad'
                   WHEN p.status = 'published' THEN 'ok'
                   ELSE 'bad'
               END AS label
        FROM posts p
        WHERE p.text IS NOT NULL AND p.text != ''
          AND (
                (p.status = 'published' AND p.moderator_id > 0)
             OR (p.status = 'rejected' AND p.moderator_id > 0)
             OR p.status = 'deleted'
             OR EXISTS (SELECT 1 FROM ai_corrections c WHERE c.post_id = p.id)
          )
        ORDER BY p.id DESC
        LIMIT ?
    """
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(query, (MAX_SAMPLES,))
        return [(row[0], row[1]) for row in await cur.fetchall()]


async def retrain() -> NaiveBayesModel:
    """Переобучает модель (в отдельном потоке, бот не блокируется)."""
    global _model
    async with _lock:
        samples = await _load_samples()
        model = await asyncio.to_thread(lambda: NaiveBayesModel().fit(samples))
        _model = model
        logger.info(
            "Самообучаемая модель: %s примеров (одобрено %s, отклонено %s), готова: %s",
            model.samples, model.docs["ok"], model.docs["bad"], model.ready,
        )
        return model


def get_model() -> Optional[NaiveBayesModel]:
    return _model


def predict(text: str) -> Optional[float]:
    """Вероятность одобрения или None, если модель ещё не набрала данных."""
    model = _model
    if model is None or not model.ready:
        return None
    try:
        return model.prob_ok(text)
    except Exception:
        logger.exception("Ошибка предсказания самообучаемой модели")
        return None


async def learning_loop() -> None:
    """Фоновое переобучение раз в 30 минут — подхватывает новые решения."""
    while True:
        try:
            await retrain()
        except Exception:
            logger.exception("Ошибка переобучения самообучаемой модели")
        await asyncio.sleep(RETRAIN_INTERVAL_SECONDS)
