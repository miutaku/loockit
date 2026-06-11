"""Operation history & state persistence (SQLite).

SesameOS2 devices do not keep an operation log on the device itself, so loockit
records state changes and command attempts locally. The :class:`HistoryStore`
wraps a SQLite database (stdlib ``sqlite3``, no extra deps); the
:class:`HistoryRecorder` subscribes to the :class:`~loockit.manager.DeviceManager`
and persists every state change and command result.

All DB calls run via ``asyncio.to_thread`` against a single connection guarded by
a lock, keeping the event loop responsive without a heavyweight async driver.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from typing import Optional

from .manager import DeviceManager
from .models import Action, DeviceState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistoryEntry:
    """One recorded event (a state change or a command attempt)."""

    id: int
    kind: str  # "state" | "command"
    device_id: str
    timestamp: float
    # state fields (kind == "state")
    lock_state: Optional[str] = None
    battery_percent: Optional[int] = None
    online: Optional[bool] = None
    source: Optional[str] = None
    # command fields (kind == "command")
    action: Optional[str] = None
    ok: Optional[bool] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,
    device_id    TEXT    NOT NULL,
    timestamp    REAL    NOT NULL,
    lock_state   TEXT,
    battery_percent INTEGER,
    online       INTEGER,
    source       TEXT,
    action       TEXT,
    ok           INTEGER,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_device_ts ON events (device_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (timestamp);
"""


class HistoryStore:
    """Thread-safe SQLite-backed event store."""

    def __init__(self, path: str) -> None:
        self._path = path
        # check_same_thread=False + a lock so to_thread workers can share one conn.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- writes ----------------------------------------------------------

    async def record_state(self, state: DeviceState) -> None:
        await asyncio.to_thread(self._insert_state, state)

    def _insert_state(self, state: DeviceState) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events "
                "(kind, device_id, timestamp, lock_state, battery_percent, "
                " online, source) VALUES ('state', ?, ?, ?, ?, ?, ?)",
                (
                    state.device_id,
                    state.timestamp,
                    state.lock_state.value,
                    state.battery_percent,
                    1 if state.online else 0,
                    state.source.value,
                ),
            )
            self._conn.commit()

    async def record_command(
        self, device_id: str, action: Action, ok: bool, error: Optional[str]
    ) -> None:
        await asyncio.to_thread(self._insert_command, device_id, action, ok, error)

    def _insert_command(
        self, device_id: str, action: Action, ok: bool, error: Optional[str]
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events "
                "(kind, device_id, timestamp, action, ok, error) "
                "VALUES ('command', ?, ?, ?, ?, ?)",
                (device_id, time.time(), action.value, 1 if ok else 0, error),
            )
            self._conn.commit()

    # -- reads -----------------------------------------------------------

    async def query(
        self,
        *,
        device_id: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 100,
        since: Optional[float] = None,
    ) -> list[HistoryEntry]:
        return await asyncio.to_thread(
            self._query, device_id, kind, limit, since
        )

    def _query(
        self,
        device_id: Optional[str],
        kind: Optional[str],
        limit: int,
        since: Optional[float],
    ) -> list[HistoryEntry]:
        clauses, params = [], []
        if device_id is not None:
            clauses.append("device_id = ?")
            params.append(device_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(limit), 10000))
        sql = (
            "SELECT * FROM events" + where
            + " ORDER BY id DESC LIMIT ?"
        )
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_entry(r) for r in rows]

    @staticmethod
    def _row_to_entry(r: sqlite3.Row) -> HistoryEntry:
        return HistoryEntry(
            id=r["id"],
            kind=r["kind"],
            device_id=r["device_id"],
            timestamp=r["timestamp"],
            lock_state=r["lock_state"],
            battery_percent=r["battery_percent"],
            online=None if r["online"] is None else bool(r["online"]),
            source=r["source"],
            action=r["action"],
            ok=None if r["ok"] is None else bool(r["ok"]),
            error=r["error"],
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class HistoryRecorder:
    """Subscribes to a DeviceManager and persists events to a HistoryStore."""

    def __init__(self, manager: DeviceManager, store: HistoryStore) -> None:
        self._manager = manager
        self._store = store
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        # Commands are reported synchronously by the manager; schedule the write.
        self._manager.add_command_listener(self._on_command)
        self._task = asyncio.create_task(self._consume_states())

    async def _consume_states(self) -> None:
        async for state in self._manager.subscribe(replay=False):
            try:
                await self._store.record_state(state)
            except Exception:  # pragma: no cover - persistence must not crash app
                logger.exception("failed to record state for %s", state.device_id)

    def _on_command(self, device_id: str, action: Action, ok: bool, error) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(
                self._store.record_command(device_id, action, ok, error)
            )
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
