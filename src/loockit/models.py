"""Normalized domain models shared across controllers, gRPC, and the bridge.

SESAME4 (a lock) and SESAME Bot1 (a button pusher) expose different mechanical
status objects in ``pysesameos2``. This module flattens both into a single
:class:`DeviceState` so the rest of the app never has to special-case the wire
representation — only the ``model`` field and a couple of optional attributes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DeviceModel(str, Enum):
    """Supported SesameOS2 product models."""

    SESAME4 = "SESAME4"
    SESAME_BOT1 = "SESAMEBOT1"

    @property
    def is_lock(self) -> bool:
        """True for models that support lock/unlock/toggle semantics."""
        return self is DeviceModel.SESAME4

    @property
    def is_bot(self) -> bool:
        """True for the button-pusher (click) model."""
        return self is DeviceModel.SESAME_BOT1


class Action(str, Enum):
    """A control command requested against a device."""

    LOCK = "lock"
    UNLOCK = "unlock"
    TOGGLE = "toggle"
    CLICK = "click"

    @property
    def requires_lock(self) -> bool:
        return self in (Action.LOCK, Action.UNLOCK, Action.TOGGLE)

    @property
    def requires_bot(self) -> bool:
        return self is Action.CLICK


class LockState(str, Enum):
    """Normalized mechanical state across both device types."""

    LOCKED = "LOCKED"
    UNLOCKED = "UNLOCKED"
    MOVING = "MOVING"
    UNKNOWN = "UNKNOWN"


class Source(str, Enum):
    """Where a state value / command result came from."""

    BLE = "BLE"
    CLOUD = "CLOUD"
    SIM = "SIM"


class ActionError(Exception):
    """Raised when an action cannot be applied (e.g. lock on a Bot)."""


class DeviceNotFoundError(KeyError):
    """Raised when a device id is not registered."""


@dataclass(frozen=True)
class DeviceState:
    """Immutable snapshot of a device's mechanical/battery status.

    ``position`` is meaningful for SESAME4 only; ``motor_status`` for the Bot only.
    Both are ``None`` when not applicable or not yet known.
    """

    device_id: str
    model: DeviceModel
    lock_state: LockState = LockState.UNKNOWN
    battery_percent: Optional[int] = None
    battery_voltage: Optional[float] = None
    position: Optional[int] = None
    motor_status: Optional[int] = None
    source: Source = Source.BLE
    online: bool = False
    timestamp: float = field(default_factory=time.time)

    def evolve(self, **changes) -> "DeviceState":
        """Return a copy with ``changes`` applied and a refreshed timestamp."""
        data = {
            "device_id": self.device_id,
            "model": self.model,
            "lock_state": self.lock_state,
            "battery_percent": self.battery_percent,
            "battery_voltage": self.battery_voltage,
            "position": self.position,
            "motor_status": self.motor_status,
            "source": self.source,
            "online": self.online,
        }
        data.update(changes)
        data["timestamp"] = time.time()
        return DeviceState(**data)


def lock_state_from_ranges(
    is_in_lock_range: bool, is_in_unlock_range: bool
) -> LockState:
    """Map SesameOS2's two range booleans to a single :class:`LockState`.

    The firmware reports lock/unlock as independent range flags. Both true is
    impossible in practice; both false means the mechanism is between the two
    configured positions, i.e. moving / intermediate.
    """
    if is_in_lock_range and not is_in_unlock_range:
        return LockState.LOCKED
    if is_in_unlock_range and not is_in_lock_range:
        return LockState.UNLOCKED
    if not is_in_lock_range and not is_in_unlock_range:
        return LockState.MOVING
    return LockState.UNKNOWN
