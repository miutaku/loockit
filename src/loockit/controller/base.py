"""Abstract controller interface.

A controller owns the connection to exactly one device and exposes a uniform,
model-agnostic command surface. Concrete implementations:

- :class:`~loockit.controller.fake.FakeController` — in-memory simulator.
- :class:`~loockit.controller.ble.BleController` — real BLE via ``pysesameos2``.
- :class:`~loockit.controller.cloud.CloudController` — optional Web API fallback.

Callers should go through :class:`~loockit.manager.DeviceManager`, not talk to
controllers directly.
"""

from __future__ import annotations

import abc
from typing import Callable

from ..config import DeviceConfig
from ..models import Action, DeviceState

# Invoked by a controller whenever the device reports a new state. Must be cheap
# and non-blocking; the manager fans it out to async subscribers.
StateCallback = Callable[[DeviceState], None]


class DeviceController(abc.ABC):
    """Drives one device and reports state changes via a callback."""

    def __init__(self, config: DeviceConfig) -> None:
        self.config = config
        self._on_state: StateCallback | None = None

    @property
    def device_id(self) -> str:
        return self.config.id

    def set_state_callback(self, callback: StateCallback) -> None:
        """Register the callback invoked on every state change."""
        self._on_state = callback

    def _emit(self, state: DeviceState) -> None:
        if self._on_state is not None:
            self._on_state(state)

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish the session and begin reporting state. Idempotent."""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Tear down the session. Idempotent."""

    @abc.abstractmethod
    async def execute(self, action: Action, history_tag: str) -> DeviceState:
        """Apply ``action`` and return the resulting (best-known) state.

        Raises :class:`~loockit.models.ActionError` if the action is not valid
        for this device's model, and other exceptions on transport failure.
        """

    @abc.abstractmethod
    def current_state(self) -> DeviceState:
        """Return the last known state without performing I/O."""
