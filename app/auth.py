"""Единые проверки административного доступа."""
import os

from app.config import ADMINS


def admin_ids_from_environment() -> set[int]:
    """Читает актуальный ADMINS из окружения без падения на мусорных значениях."""
    raw = os.getenv("ADMINS", "")
    result: set[int] = set()
    for part in raw.replace("\ufeff", "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def is_admin(user_id: int) -> bool:
    """Та же проверка, что используется командой /admin.

    Учитываются и значения, загруженные из конфигурации, и актуальное
    значение переменной окружения ADMINS.
    """
    return user_id in ADMINS or user_id in admin_ids_from_environment()
