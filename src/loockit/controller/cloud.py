"""Optional CANDY HOUSE Web API v4 fallback controller.

Used only when ``cloud_fallback = true`` and a device has a ``[devices.cloud]``
section. The core and the Matter bridge never depend on this module. Backed by
``pysesame3`` (the cloud counterpart of ``pysesameos2``), imported lazily.

State here is command-driven (request/response): the cloud API does not push BLE
advertisements, so real-time monitoring still comes from the BLE path.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import DeviceConfig
from ..models import (
    Action,
    ActionError,
    DeviceState,
    LockState,
    Source,
)
from .base import DeviceController

logger = logging.getLogger(__name__)


class CloudController(DeviceController):
    """Drives one device through the CANDY HOUSE cloud Web API v4."""

    def __init__(self, config: DeviceConfig) -> None:
        super().__init__(config)
        if config.cloud is None:
            raise ValueError(f"device '{config.id}' has no cloud config")
        self._device = None
        self._state = DeviceState(
            device_id=config.id,
            model=config.model,
            lock_state=LockState.UNKNOWN,
            source=Source.CLOUD,
            online=False,
        )

    def current_state(self) -> DeviceState:
        return self._state

    async def connect(self) -> None:
        from pysesame3.auth import WebAPIAuth
        from pysesame3.lock import CHSesame2

        cloud = self.config.cloud
        assert cloud is not None
        auth = WebAPIAuth(apikey=cloud.api_key)
        # pysesame3 is synchronous; build the handle off the event loop.
        self._device = await asyncio.to_thread(
            CHSesame2,
            authenticator=auth,
            device_uuid=cloud.uuid,
            secret_key=cloud.secret_key,
        )
        self._state = self._state.evolve(online=True)
        self._emit(self._state)

    async def disconnect(self) -> None:
        self._device = None
        self._state = self._state.evolve(online=False)
        self._emit(self._state)

    async def execute(self, action: Action, history_tag: str) -> DeviceState:
        model = self.config.model
        if action.requires_lock and not model.is_lock:
            raise ActionError(f"{action.value} is not supported on {model.value}")
        if action.requires_bot and not model.is_bot:
            raise ActionError(f"{action.value} is not supported on {model.value}")
        if self._device is None:
            await self.connect()
        assert self._device is not None

        if action is Action.LOCK:
            await asyncio.to_thread(self._device.lock, history_tag)
            new = LockState.LOCKED
        elif action is Action.UNLOCK:
            await asyncio.to_thread(self._device.unlock, history_tag)
            new = LockState.UNLOCKED
        elif action is Action.TOGGLE:
            await asyncio.to_thread(self._device.toggle, history_tag)
            new = (
                LockState.UNLOCKED
                if self._state.lock_state is LockState.LOCKED
                else LockState.LOCKED
            )
        else:  # CLICK
            await asyncio.to_thread(self._device.click, history_tag)
            new = LockState.UNKNOWN

        self._state = self._state.evolve(lock_state=new)
        self._emit(self._state)
        return self._state
