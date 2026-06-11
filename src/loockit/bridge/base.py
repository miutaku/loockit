"""Bridge protocol.

A bridge subscribes to the :class:`~loockit.manager.DeviceManager` for state and
issues commands back through it. Bridges only ever talk to the manager (local BLE
control) — never to any cloud API.
"""

from __future__ import annotations

import abc

from ..manager import DeviceManager


class Bridge(abc.ABC):
    """Base class for smart-home bridges (e.g. Matter)."""

    def __init__(self, manager: DeviceManager) -> None:
        self.manager = manager

    @abc.abstractmethod
    async def start(self) -> None:
        """Start the bridge and begin exposing devices. Idempotent."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop the bridge and release resources. Idempotent."""
