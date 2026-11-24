"""Мониторинг и самовосстановление.

Бот сам следит за своим здоровьем и пишет администраторам, только когда
что-то действительно требует внимания (с защитой от спама оповещениями):

- поток ошибок (≥ ALERT_ERRORS_PER_5MIN за 5 минут) — с примерами ошибок;
- потеря связи с Telegram — и отдельное сообщение, когда связь вернулась;
- память: при приближении к лимиту бот СНАЧАЛА сам освобождает её
  (выгружает NudeNet, чистит мусор), и пишет, только если не помогло;
- диск: при нехватке места сам чистит старые логи и бэкапы;
- фоновые задачи (публикация, бэкапы, реклама…) запускаются под
  «супервизором»: если задача упала, она перезапускается автоматически,
  а админы получают одно сообщение.

Ничего из этого не требует ручного перезапуска бота.
"""
import asyncio
import gc
import logging
import os
import shutil
import time
from collections import Counter, deque
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

ERRORS_PER_5MIN = int(os.getenv("ALERT_ERRORS_PER_5MIN", "15") or 15)
MEMORY_LIMIT_MB_FALLBACK = int(os.getenv("ALERT_MEMORY_LIMIT_MB", "1024") or 1024)
MEMORY_WARN_RATIO = 0.80
DISK_MIN_FREE_MB = int(os.getenv("ALERT_DISK_MIN_FREE_MB", "300") or 300)
TELEGRAM_FAILS_FOR_OUTAGE = 3

_last_sent: dict[str, float] = {}
_error_times: deque = deque(maxlen=5000)
_error_messages: deque = deque(maxlen=200)


class _ErrorCollector(logging.Handler):
    """Считает ошибки во всех логгерах (кроме собственного — без петель)."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return
        try:
            _error_times.append(time.monotonic())
            msg = record.getMessage().splitlines()[0][:150]
            _error_messages.append(f"{record.name}: {msg}")
        except Exception:
            pass


def install() -> None:
    root = logging.getLogger()
    if not any(isinstance(hd, _ErrorCollector) for hd in root.handlers):
        handler = _ErrorCollector(level=logging.ERROR)
        root.addHandler(handler)


async def notify_admins(text: str, key: Optional[str] = None, cooldown: float = 1800) -> bool:
    """Сообщение администрации. key — защита от повторов (не чаще cooldown).
    Никогда не выбрасывает исключений."""
    now = time.monotonic()
    if key is not None:
        if now - _last_sent.get(key, -1e9) < cooldown:
            return False
        _last_sent[key] = now
    try:
        from app.config import ADMINS
        from app.loader import bot
        from app.runtime_settings import get as get_setting

        chat_id = get_setting("ADMINS_CHAT_ID")
        if chat_id:
            topic = get_setting("ADMINS_TOPIC_ID") or None
            try:
                await bot.send_message(chat_id, text, parse_mode="HTML", message_thread_id=topic)
                return True
            except Exception:
                try:
                    await bot.send_message(chat_id, text, parse_mode="HTML")
                    return True
                except Exception:
                    pass
        sent = False
        for admin_id in list(ADMINS):
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
                sent = True
            except Exception:
                pass
        return sent
    except Exception:
        logger.warning("Не удалось отправить оповещение администрации", exc_info=True)
        return False


# ------------------------------------------------------------------
# Супервизор фоновых задач
# ------------------------------------------------------------------
async def supervise(name: str, factory: Callable[[], Awaitable[None]]) -> None:
    """Держит фоновую задачу живой: при падении перезапускает её с
    нарастающей паузой (5 с … 5 мин) и один раз сообщает админам."""
    delay = 5
    while True:
        started = time.monotonic()
        try:
            await factory()
            logger.error("Фоновая задача %s неожиданно завершилась — перезапускаю", name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Фоновая задача %s упала — перезапускаю", name)
            from app.utils import h
            await notify_admins(
                f"🔁 Фоновая задача <b>{h(name)}</b> упала и перезапущена автоматически.\n"
                f"<code>{h(type(exc).__name__)}: {h(str(exc)[:300])}</code>",
                key=f"task:{name}", cooldown=3600,
            )
        if time.monotonic() - started > 600:
            delay = 5  # задача долго работала нормально — сбрасываем паузу
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


# ------------------------------------------------------------------
# Метрики
# ------------------------------------------------------------------
def memory_rss_mb() -> Optional[float]:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def memory_limit_mb() -> float:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = open(path).read().strip()
            if raw != "max" and int(raw) < 1 << 50:
                return int(raw) / (1024 * 1024)
        except (OSError, ValueError):
            continue
    return float(MEMORY_LIMIT_MB_FALLBACK)


def disk_free_mb(path: str) -> Optional[float]:
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except OSError:
        return None


def recent_errors(window: float = 300) -> tuple[int, list[str]]:
    now = time.monotonic()
    count = sum(1 for t in _error_times if now - t <= window)
    top = [f"{n}× {m}" for m, n in Counter(list(_error_messages)[-max(count, 1):]).most_common(3)]
    return count, top


# ------------------------------------------------------------------
# Цикл проверки здоровья
# ------------------------------------------------------------------
class HealthMonitor:
    def __init__(self) -> None:
        self.telegram_fails = 0
        self.outage_started: Optional[float] = None

    async def check_errors(self) -> None:
        count, top = recent_errors()
        if count >= ERRORS_PER_5MIN:
            from app.utils import h
            details = "\n".join(f"• <code>{h(t)}</code>" for t in top)
            await notify_admins(
                f"⚠️ <b>Много ошибок</b>: {count} за 5 минут. Бот продолжает работать.\n\n{details}",
                key="errors", cooldown=1800,
            )

    async def check_telegram(self) -> None:
        from app.loader import bot
        try:
            await asyncio.wait_for(bot.get_me(), timeout=20)
            ok = True
        except Exception:
            ok = False
        if ok:
            if self.outage_started is not None:
                minutes = max(1, int((time.monotonic() - self.outage_started) / 60))
                self.outage_started = None
                await notify_admins(
                    f"✅ Связь с Telegram восстановлена (простой ~{minutes} мин.). "
                    "Накопившиеся сообщения обработаны."
                )
            self.telegram_fails = 0
            return
        self.telegram_fails += 1
        if self.telegram_fails >= TELEGRAM_FAILS_FOR_OUTAGE and self.outage_started is None:
            self.outage_started = time.monotonic() - 60 * TELEGRAM_FAILS_FOR_OUTAGE
            logger.error("Нет связи с Telegram %s мин. подряд", self.telegram_fails)

    async def check_memory(self) -> None:
        rss = memory_rss_mb()
        if rss is None:
            return
        limit = memory_limit_mb()
        if rss < limit * MEMORY_WARN_RATIO:
            return
        # Самовосстановление: выгружаем тяжёлую модель и чистим мусор.
        from app.moderation_photo import release_if_idle
        await asyncio.to_thread(release_if_idle, 0)
        gc.collect()
        after = memory_rss_mb() or rss
        logger.warning("Память %.0f/%.0f МБ, после очистки %.0f МБ", rss, limit, after)
        if after >= limit * MEMORY_WARN_RATIO:
            await notify_admins(
                f"🧠 <b>Мало памяти</b>: {after:.0f} из {limit:.0f} МБ даже после очистки. "
                "Бот работает; если сообщение повторяется — уменьшите "
                "<code>NUDENET_IDLE_UNLOAD_SECONDS</code> или отключите NudeNet.",
                key="memory", cooldown=3 * 3600,
            )

    async def check_disk(self) -> None:
        from app.config import DB_NAME
        free = disk_free_mb(os.path.dirname(os.path.abspath(DB_NAME)) or ".")
        if free is None or free >= DISK_MIN_FREE_MB:
            return
        # Самовосстановление: чистим журнал и лишние бэкапы.
        from app.database import prune_logs
        from app.scheduler import _enforce_backup_size_limit
        await prune_logs(14)
        await asyncio.to_thread(_enforce_backup_size_limit)
        free_after = disk_free_mb(os.path.dirname(os.path.abspath(DB_NAME)) or ".") or free
        if free_after < DISK_MIN_FREE_MB:
            await notify_admins(
                f"💾 <b>Мало места на диске</b>: свободно {free_after:.0f} МБ. "
                "Старые логи и бэкапы уже очищены автоматически.",
                key="disk", cooldown=6 * 3600,
            )

    async def run(self) -> None:
        while True:
            for check in (self.check_telegram, self.check_errors, self.check_memory, self.check_disk):
                try:
                    await check()
                except Exception:
                    logger.warning("Ошибка проверки %s", check.__name__, exc_info=True)
            await asyncio.sleep(60)


async def health_loop() -> None:
    await HealthMonitor().run()
