"""Хранилище состояний диалогов (FSM) в SQLite.

Раньше использовался MemoryStorage: при любом перезапуске/передеплое бота
пользователь, который был на середине сценария (отправка поста, заявка на
удаление, ввод причины отказа…), «выпадал» — бот переставал понимать его
следующее сообщение.

Устройство:
- одно постоянное соединение aiosqlite (а не новое на каждое обращение);
- кэш в памяти: чтение состояния — без обращения к диску, запись —
  сквозная (write-through), поэтому после рестарта всё восстанавливается;
- данные хранятся в JSON; datetime и объекты Telegram (например,
  MessageEntity из рассылки) сериализуются с пометкой типа и
  восстанавливаются обратно;
- устаревшие состояния (старше STALE_DAYS) удаляются при старте.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

logger = logging.getLogger(__name__)

STALE_DAYS = 7


def _encode(value: Any) -> Any:
    from pydantic import BaseModel

    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, BaseModel):
        return {"__tg__": type(value).__name__, "v": _encode(value.model_dump(exclude_none=True))}
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__dt__"}:
            return datetime.fromisoformat(value["__dt__"])
        if "__tg__" in value and set(value) == {"__tg__", "v"}:
            from aiogram import types

            cls = getattr(types, value["__tg__"], None)
            data = _decode(value["v"])
            if cls is not None:
                try:
                    return cls.model_validate(data)
                except Exception:
                    logger.warning("Не удалось восстановить объект %s из состояния", value["__tg__"])
            return data
        return {k: _decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def _key(key: StorageKey) -> str:
    return ":".join(str(part) for part in (
        key.bot_id, key.chat_id, key.user_id, key.thread_id or "",
        key.business_connection_id or "", key.destiny,
    ))


class SQLiteStorage(BaseStorage):
    def __init__(self, path: str) -> None:
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._cache: Dict[str, tuple[Optional[str], Dict[str, Any]]] = {}

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            async with self._lock:
                if self._db is None:
                    db = await aiosqlite.connect(self.path, timeout=30)
                    await db.execute("PRAGMA journal_mode=WAL")
                    await db.execute("""
                        CREATE TABLE IF NOT EXISTS fsm_states(
                            key TEXT PRIMARY KEY,
                            state TEXT,
                            data TEXT,
                            updated_at TEXT
                        )""")
                    cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).isoformat()
                    await db.execute("DELETE FROM fsm_states WHERE updated_at < ?", (cutoff,))
                    await db.commit()
                    self._db = db
        return self._db

    async def _load(self, k: str) -> tuple[Optional[str], Dict[str, Any]]:
        if k in self._cache:
            return self._cache[k]
        db = await self._conn()
        cur = await db.execute("SELECT state, data FROM fsm_states WHERE key=?", (k,))
        row = await cur.fetchone()
        if row:
            try:
                data = _decode(json.loads(row[1])) if row[1] else {}
            except (ValueError, TypeError):
                data = {}
            entry = (row[0], data)
        else:
            entry = (None, {})
        self._cache[k] = entry
        return entry

    async def _save(self, k: str, state: Optional[str], data: Dict[str, Any]) -> None:
        self._cache[k] = (state, data)
        db = await self._conn()
        async with self._lock:
            if state is None and not data:
                await db.execute("DELETE FROM fsm_states WHERE key=?", (k,))
            else:
                await db.execute(
                    "INSERT OR REPLACE INTO fsm_states(key, state, data, updated_at) VALUES(?,?,?,?)",
                    (k, state, json.dumps(_encode(data), ensure_ascii=False), datetime.now().isoformat()),
                )
            await db.commit()
        # Пустые записи не держим в кэше бесконечно.
        if state is None and not data:
            self._cache.pop(k, None)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        k = _key(key)
        _, data = await self._load(k)
        value = state.state if isinstance(state, State) else state
        await self._save(k, value, dict(data))

    async def get_state(self, key: StorageKey) -> Optional[str]:
        state, _ = await self._load(_key(key))
        return state

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        k = _key(key)
        state, _ = await self._load(k)
        await self._save(k, state, dict(data))

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        _, data = await self._load(_key(key))
        return dict(data)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
