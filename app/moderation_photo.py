"""
Автоматическая проверка фото на явную наготу (NudeNet, полностью бесплатно
и локально — модель уже идёт в комплекте пакета, интернет при работе не нужен).

Важно понимать границы применимости:
- Ловит только откровенную наготу (обнажённая грудь/гениталии/ягодицы) —
  никаких других категорий (насилие, наркотики и т.п.) не проверяет.
- НЕ умеет и не пытается определять возраст человека на фото — это
  сознательное решение: не существует бесплатного (да и вообще надёжного)
  способа сделать это без риска как пропустить нарушение, так и создать
  ложное обвинение. Именно поэтому любое фото, не попавшее под явный
  автоотказ, обязательно уходит на ручную модерацию (см. handlers/posting.py) —
  автоматика здесь только фильтрует самый очевидный треш, а не заменяет
  человека в вопросах, которые он один может решить ответственно.
"""
import asyncio
import io
import logging
import threading
from typing import List, Optional, Tuple

from app.runtime_settings import get as get_setting
from app.config import LOW_MEMORY_MODE

logger = logging.getLogger(__name__)

# Классы NudeNet, которые считаем однозначно откровенными.
# Специально не включены MALE_BREAST_EXPOSED / BELLY_EXPOSED / FEET_EXPOSED /
# ARMPITS_EXPOSED / FACE_* — это бытовые фото (пляж, спорт и т.п.), а не то,
# что реально запрещают правила канала.
EXPLICIT_CLASSES = {
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
}

_detector = None
_detector_lock = threading.Lock()
_unavailable = False  # если модель не загрузилась — не пытаемся на каждом фото заново


def _get_detector():
    """Ленивая однократная загрузка модели (thread-safe)."""
    global _detector, _unavailable
    if _detector is not None or _unavailable:
        return _detector

    with _detector_lock:
        if _detector is not None or _unavailable:
            return _detector
        try:
            from nudenet import NudeDetector
            _detector = NudeDetector()
            logger.info("NudeNet: модель загружена")
        except Exception:
            logger.exception(
                "NudeNet недоступен (библиотека не установлена или модель не "
                "загрузилась) — автоматическая проверка фото на наготу отключена, "
                "все фото-посты будут уходить на ручную модерацию как обычно."
            )
            _unavailable = True
    return _detector


def _detect_sync(image_bytes: bytes) -> List[dict]:
    global _detector
    detector = _get_detector()
    if detector is None:
        return []
    try:
        return detector.detect(image_bytes)
    finally:
        if LOW_MEMORY_MODE:
            # NudeNet is the heaviest photo-side model. Keep it only for the
            # duration of a detection to avoid permanent RAM occupation.
            _detector = None


async def check_photo_nsfw(image_bytes: bytes) -> Tuple[bool, Optional[str], float]:
    """
    Возвращает (is_explicit, class_name, score).

    is_explicit=True означает, что найдена откровенная нагота с уверенностью
    выше NSFW_EXPLICIT_THRESHOLD — такой пост нужно отклонять автоматически.
    Если is_explicit=False — это НЕ значит "фото безопасно", это значит лишь
    "не найдено однозначного повода для автоотказа"; окончательное решение
    по такому фото всё равно принимает модератор.
    """
    try:
        detections = await asyncio.to_thread(_detect_sync, image_bytes)
    except Exception:
        logger.exception("Ошибка при проверке фото через NudeNet")
        return False, None, 0.0

    best_class, best_score = None, 0.0
    for det in detections:
        if det["class"] in EXPLICIT_CLASSES and det["score"] > best_score:
            best_class, best_score = det["class"], det["score"]

    if best_class and best_score >= get_setting("NSFW_EXPLICIT_THRESHOLD"):
        return True, best_class, best_score

    return False, best_class, best_score


def photo_detector_available() -> bool:
    """Возвращает True, если локальный NudeNet реально загрузился."""
    return _get_detector() is not None
