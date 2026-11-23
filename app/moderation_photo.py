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
import gc
import io
import logging
import threading
import time
from typing import List, Optional, Tuple

from app.runtime_settings import get as get_setting

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
_last_used = 0.0
# Одновременно — только одна проверка: две параллельные копии ONNX-инференса
# на 2 vCPU / 1 ГБ RAM дают пик памяти и не ускоряют работу.
_detect_lock = threading.Lock()


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
    global _last_used
    with _detect_lock:
        detector = _get_detector()
        if detector is None:
            return []
        _last_used = time.monotonic()
        try:
            return detector.detect(image_bytes)
        finally:
            _last_used = time.monotonic()


def release_if_idle(idle_seconds: float) -> bool:
    """Выгружает модель NudeNet из памяти, если ей давно не пользовались.

    Модель занимает сотни мегабайт RAM, а фото-посты приходят редко. На
    тарифе с 1 ГБ памяти выгодно держать её загруженной только во время
    всплесков активности: следующая проверка загрузит её заново (несколько
    секунд). Возвращает True, если модель была выгружена.
    """
    global _detector
    if _detector is None or time.monotonic() - _last_used < idle_seconds:
        return False
    if not _detect_lock.acquire(blocking=False):
        return False  # прямо сейчас идёт проверка
    try:
        with _detector_lock:
            if _detector is None:
                return False
            _detector = None
    finally:
        _detect_lock.release()
    gc.collect()
    logger.info("NudeNet выгружен из памяти после %s сек. простоя", int(idle_seconds))
    return True


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
    global _last_used
    _last_used = time.monotonic()  # чтобы модель не выгрузили между проверкой и детекцией
    """Проверяет, доступен ли локальный детектор фото.

    Функция оставлена синхронной, потому что сервис вызывает её через
    asyncio.to_thread(). При недоступности NudeNet возвращается False,
    приложение при этом продолжает запускаться и отправляет фото на ручную
    модерацию.
    """
    return _get_detector() is not None
