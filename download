"""Бесплатный локальный анализ медиа: OCR + perceptual hash."""
import asyncio, io, logging, re
from typing import Optional, Tuple
import numpy as np

logger = logging.getLogger(__name__)

BAD_OCR = re.compile(r"(?:куплю|продам|реклама|услуг[аи]|звоните|цена|доставка|казино|ставк|кредит|займ|подписывай|розыгрыш)", re.I)

def _phash_sync(data: bytes) -> str:
    import cv2
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None: return ""
    img = cv2.resize(img, (32, 32)).astype(np.float32)
    dct = cv2.dct(img)[:8, :8]
    med = np.median(dct[1:])
    bits = (dct > med).flatten()
    return ''.join('1' if x else '0' for x in bits)

def _hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b): return 999
    return sum(x != y for x, y in zip(a,b))

def _ocr_sync(data: bytes) -> str:
    try:
        from PIL import Image
        import pytesseract
        img = Image.open(io.BytesIO(data))
        return pytesseract.image_to_string(img, lang="rus+eng")[:3000]
    except Exception:
        return ""

async def analyze_media(data: bytes) -> Tuple[str, str, bool, str]:
    phash = await asyncio.to_thread(_phash_sync, data)
    ocr = await asyncio.to_thread(_ocr_sync, data)
    suspicious = bool(BAD_OCR.search(ocr))
    return phash, ocr, not suspicious, ("OCR: найден рекламный/нежелательный текст" if suspicious else "")

def phash_distance(a: str, b: str) -> int:
    return _hamming(a,b)
