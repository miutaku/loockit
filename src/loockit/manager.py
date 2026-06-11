"""DeviceManager — the single orchestration point for all devices.

Responsibilities:
- Own one primary controller per device (BLE or, in simulate mode, Fake) plus an
  optional cloud controller.
- Route commands BLE-first, falling back to cloud only on failure when enabled.
- Cache the latest :class:`DeviceState` per device.
- Publish state changes to async subscribers (gRPC ``StreamStatus``, Matter
  bridge) via per-subscriber queues.

Controller callbacks may fire from a different thread/loop (bleak), so fan-out is
marshalled onto the manager's loop with ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Callable, Optional

from .config import AppConfig, DeviceConfig
from .controller.base import DeviceController
from .controller.fake import FakeController
from .models import (
    Action,
    ActionError,
    DeviceNotFoundError,
    DeviceState,
)

logger = logging.getLogger(__name__)

# Notified after every command attempt: (device_id, action, ok, error).
CommandListener = Callable[[str, Action, bool, Optional[str]], None]


class _Entry:
    __slots__ = ("config", "primary", "cloud")

    def __init__(
        self,
        config: DeviceConfig,
        primary,
        cloud: Optional[DeviceController],
    ) -> None:
        self.config = config
        self.primary = primary
        self.cloud = cloud


class DeviceManager:
    """Coordinates controllers and broadcasts state."""

    def __init__(self, config: AppConfig, *, simulate: bool = False) -> None:
        self._config = config
        self._simulate = simulate
        self._entries: dict[str, _Entry] = {}
        self._states: dict[str, DeviceState] = {}
        self._subscribers: set[asyncio.Queue[DeviceState]] = set()
        self._command_listeners: list[CommandListener] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._build_entries()

    def _build_entries(self) -> None:
        for dev in self._config.devices:
            primary = self._make_primary(dev)
            cloud = self._make_cloud(dev)
            primary.set_state_callback(self._on_state)
            if cloud is not None:
                cloud.set_state_callback(self._on_state)
            self._entries[dev.id] = _Entry(dev, primary, cloud)
            self._states[dev.id] = primary.current_state()

    def _make_primary(self, dev: DeviceConfig):
        if self._simulate:
            return FakeController(dev)
        from .controller.ble import BleController

        return BleController(dev)

    def _make_cloud(self, dev: DeviceConfig) -> Optional[DeviceController]:
        if self._simulate or not self._config.cloud_fallback or dev.cloud is None:
            return None
        from .controller.cloud import CloudController

        return CloudController(dev)

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        results = await asyncio.gather(
            *(self._connect_entry(e) for e in self._entries.values()),
            return_exceptions=True,
        )
        for entry, result in zip(self._entries.values(), results):
            if isinstance(result, Exception):
                logger.warning(
                    "device '%s' failed to connect at startup: %s",
                    entry.config.id,
                    result,
                )

    async def _connect_entry(self, entry: _Entry) -> None:
        await entry.primary.connect()

    async def stop(self) -> None:
        await asyncio.gather(
            *(e.primary.disconnect() for e in self._entries.values()),
            *(e.cloud.disconnect() for e in self._entries.values() if e.cloud),
            return_exceptions=True,
        )

    # -- queries ---------------------------------------------------------

    def device_ids(self) -> list[str]:
        return list(self._entries.keys())

    def device_config(self, device_id: str) -> DeviceConfig:
        self._require(device_id)
        return self._entries[device_id].config

    def get_state(self, device_id: str) -> DeviceState:
        self._require(device_id)
        return self._states[device_id]

    def all_states(self) -> list[DeviceState]:
        return list(self._states.values())

    def _require(self, device_id: str) -> None:
        if device_id not in self._entries:
            raise DeviceNotFoundError(device_id)

    # -- commands --------------------------------------------------------

    async def execute(
        self, device_id: str, action: Action, history_tag: str = "loockit"
    ) -> DeviceState:
        """Run ``action``; BLE first, cloud fallback on transport failure.

        Raises :class:`ActionError` for model/action mismatches (no fallback),
        :class:`DeviceNotFoundError` for unknown ids.
        """
        self._require(device_id)
        entry = self._entries[device_id]
        try:
            state = await self._execute_with_fallback(entry, action, history_tag)
        except ActionError as exc:
            self._notify_command(device_id, action, False, str(exc))
            raise
        except Exception as exc:
            self._notify_command(device_id, action, False, str(exc))
            raise
        self._notify_command(device_id, action, True, None)
        return state

    async def _execute_with_fallback(
        self, entry: _Entry, action: Action, history_tag: str
    ) -> DeviceState:
        try:
            return await entry.primary.execute(action, history_tag)
        except ActionError:
            raise  # model mismatch — fallback won't help
        except Exception as exc:
            if entry.cloud is None:
                raise
            logger.warning(
                "primary control of '%s' failed (%s); trying cloud fallback",
                entry.config.id,
                exc,
            )
            await entry.cloud.connect()
            return await entry.cloud.execute(action, history_tag)

    # -- command listeners ----------------------------------------------

    def add_command_listener(self, listener: CommandListener) -> None:
        """Register a callback invoked after each command attempt."""
        self._command_listeners.append(listener)

    def _notify_command(
        self, device_id: str, action: Action, ok: bool, error: Optional[str]
    ) -> None:
        for listener in self._command_listeners:
            try:
                listener(device_id, action, ok, error)
            except Exception:  # pragma: no cover - listener must not break cmds
                logger.exception("command listener failed")

    # -- pub/sub ---------------------------------------------------------

    def _on_state(self, state: DeviceState) -> None:
        """Controller callback (possibly off-loop). Cache and fan out."""
        self._states[state.device_id] = state
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._dispatch(state)
        else:
            loop.call_soon_threadsafe(self._dispatch, state)

    def _dispatch(self, state: DeviceState) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(state)
            except asyncio.QueueFull:  # pragma: no cover - slow consumer
                logger.debug("dropping state for slow subscriber")

    async def subscribe(
        self, *, replay: bool = True
    ) -> AsyncIterator[DeviceState]:
        """Yield state changes as they happen.

        When ``replay`` is true (default), the current state of every device is
        emitted first so a new subscriber gets an immediate snapshot.
        """
        queue: asyncio.Queue[DeviceState] = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        try:
            if replay:
                for state in self.all_states():
                    yield state
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)
