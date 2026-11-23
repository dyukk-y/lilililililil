"""Единая точка создания Bot/Dispatcher, чтобы избежать циклических импортов."""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import BOT_TOKEN, DB_NAME
from app.fsm_storage import SQLiteStorage


def _log_file_path() -> str:
    """Лог хранится рядом с базой (по умолчанию /app/data/bot.log), чтобы
    переживать передеплой — как и описано в DEPLOYMENT.md. Раньше лог
    писался в рабочую папку (/app/bot.log) и пропадал при обновлении."""
    explicit = os.getenv("LOG_FILE")
    if explicit:
        return explicit
    return os.path.join(os.path.dirname(os.path.abspath(DB_NAME)) or ".", "bot.log")


def _build_handlers() -> list:
    handlers: list = [logging.StreamHandler(sys.stdout)]
    path = _log_file_path()
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # Ротация: раньше лог рос бесконечно и мог занять весь диск.
        handlers.append(RotatingFileHandler(path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"))
    except OSError as exc:
        print(f"Не удалось открыть файл лога {path}: {exc}. Логирую только в stdout.", file=sys.stderr)
    return handlers


logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=_build_handlers(),
)
logger = logging.getLogger("smotrbot")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
# Состояния диалогов хранятся в той же SQLite-базе и переживают перезапуск.
dp = Dispatcher(storage=SQLiteStorage(DB_NAME))
