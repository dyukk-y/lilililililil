"""Локальный семантический анализатор с бережным использованием RAM.

Модель загружается лениво, только при первом анализе текста. По умолчанию
используется компактная multilingual MiniLM-L3. Одновременно выполняется
только один inference, а длина входа ограничена. Это существенно снижает
пиковое потребление памяти на небольших VPS.
"""
import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import List

from app.config import LOCAL_AI_MODEL, LOCAL_AI_MAX_LENGTH, LOCAL_AI_THREADS

logger = logging.getLogger(__name__)
MODEL_NAME = LOCAL_AI_MODEL

POSITIVE_PROTOTYPES = [
    "Расскажите про этого человека", "Кто такой этот парень и кто его знает",
    "Кто такая эта девушка", "Что за мальчик на фотографии",
    "Что за девочка, расскажите о ней", "Кто знает этого человека",
    "Подскажите, как его зовут", "Подскажите, как её зовут",
    "Можно познакомиться с этим парнем", "Мне понравилась эта девушка, хочу познакомиться",
    "Есть ли у него девушка", "Есть ли у неё парень", "Дайте его юзернейм",
    "Скиньте её юз в личку", "Кто был на этом мероприятии", "Кто едет туда сегодня",
    "Видели этого человека в городе",
]
NEGATIVE_PROTOTYPES = [
    "Продам товар, цена и доставка", "Куплю вещь или услугу",
    "Отдам котят или щенков", "Потерял телефон, карту или документы",
    "Нашёл чужую вещь, помогите найти владельца",
    "Реклама услуги, маникюра, репетитора или ремонта",
    "Объявление о продаже или покупке", "Ищу работу или предлагаю работу",
]

_model = None
_embeddings = None
_lock = threading.Lock()
_inference_lock = asyncio.Lock()
_failed = False

@dataclass(frozen=True)
class LocalAIResult:
    available: bool
    score: int
    positive_similarity: float
    negative_similarity: float
    confidence: float
    reasons: List[str]

def _load_model():
    global _model, _embeddings, _failed
    if _model is not None or _failed:
        return _model
    with _lock:
        if _model is not None or _failed:
            return _model
        try:
            import torch
            torch.set_num_threads(max(1, LOCAL_AI_THREADS))
            try:
                torch.set_num_interop_threads(1)
            except RuntimeError:
                pass
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(MODEL_NAME, device="cpu")
            model.max_seq_length = max(32, LOCAL_AI_MAX_LENGTH)
            import numpy as np
            pos = model.encode(POSITIVE_PROTOTYPES, normalize_embeddings=True, batch_size=4,
                               show_progress_bar=False, convert_to_numpy=True)
            neg = model.encode(NEGATIVE_PROTOTYPES, normalize_embeddings=True, batch_size=4,
                               show_progress_bar=False, convert_to_numpy=True)
            _model = model
            _embeddings = (np.asarray(pos), np.asarray(neg))
            logger.info("Local AI model loaded lazily: %s", MODEL_NAME)
        except Exception:
            _failed = True
            logger.exception("Local AI model is unavailable: %s", MODEL_NAME)
    return _model

def _analyze_sync(text: str) -> LocalAIResult:
    model = _load_model()
    if model is None or _embeddings is None:
        return LocalAIResult(False, 0, 0.0, 0.0, 0.0, ["локальный ИИ недоступен"])
    import numpy as np
    vector = model.encode([text[:8000]], normalize_embeddings=True, batch_size=1,
                          show_progress_bar=False, convert_to_numpy=True)[0]
    pos_vectors, neg_vectors = _embeddings
    pos = float(np.max(pos_vectors @ vector))
    neg = float(np.max(neg_vectors @ vector))
    raw = (pos - neg) * 100.0
    score = int(max(-40, min(40, round(raw))))
    confidence = max(0.0, min(1.0, 0.5 + (pos - neg) * 2.0))
    reasons = [f"ИИ: положительная близость {pos:.2f}", f"ИИ: отрицательная близость {neg:.2f}"]
    if pos >= neg + 0.08:
        reasons.append("ИИ считает намерение похожим на допустимый пост о человеке")
    elif neg >= pos + 0.08:
        reasons.append("ИИ видит сходство с нежелательным типом объявления")
    else:
        reasons.append("ИИ не уверен в намерении")
    return LocalAIResult(True, score, pos, neg, confidence, reasons)

async def analyze_text(text: str) -> LocalAIResult:
    text = (text or "").strip()
    if not text:
        return LocalAIResult(False, 0, 0.0, 0.0, 0.0, ["пустой текст"])
    # Serializing inference prevents several simultaneous requests from
    # multiplying the model's temporary tensors and RAM usage.
    async with _inference_lock:
        return await asyncio.to_thread(_analyze_sync, text)

async def preload() -> bool:
    return await asyncio.to_thread(_load_model) is not None
