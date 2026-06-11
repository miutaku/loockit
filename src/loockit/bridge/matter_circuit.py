"""CircuitMatter-backed Matter adapter — a real, runnable Matter device.

`CircuitMatter <https://github.com/adafruit/CircuitMatter>`_ is a pure-Python
implementation of the Matter device side (no native ``connectedhomeip`` build),
so loockit can actually join a Matter fabric without a C++ toolchain. It exposes:

- SESAME4    -> a **Door Lock** endpoint (device type 0x000A) with a Door Lock
  cluster (0x0101). ``LockDoor`` / ``UnlockDoor`` route to the local BLE control;
  the ``LockState`` attribute is kept in sync with the real device.
- SESAME Bot1 -> an **On/Off** endpoint (device type 0x0100). ``On`` performs a
  single click (the Bot is momentary, so ``Off`` is a no-op).

CircuitMatter is synchronous; the packet loop runs in a worker thread and inbound
Matter commands are marshalled back onto the asyncio loop. The device/cluster
construction (:func:`build_devices`) is independent of the network server so it
can be unit-tested without sockets, mDNS, or a controller.

Note: CircuitMatter targets hobby use and is not Matter-certified. Real
commissioning requires ``avahi-daemon`` on the host for mDNS advertisement, and
must be validated against a real controller (Apple Home, Google, etc.).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

# Gate Matter support on CircuitMatter being importable.
from circuitmatter import data_model  # type: ignore
from circuitmatter.device_types.lighting.on_off import OnOffLight  # type: ignore
from circuitmatter.device_types.simple_device import SimpleDevice  # type: ignore

from .matter import (
    MATTER_LOCK_LOCKED,
    MATTER_LOCK_NOT_FULLY_LOCKED,
    MATTER_LOCK_UNLOCKED,
    BridgedEndpoint,
    MatterAdapter,
    MatterEndpointKind,
)

logger = logging.getLogger(__name__)

CommandHandler = Callable[[str, str], Awaitable[None]]

# Called by a cluster (in the packet-loop thread) with (device_id, command_name).
SyncDispatch = Callable[[str, str], None]


class LockStateEnum(data_model.Enum8):
    NOT_FULLY_LOCKED = MATTER_LOCK_NOT_FULLY_LOCKED
    LOCKED = MATTER_LOCK_LOCKED
    UNLOCKED = MATTER_LOCK_UNLOCKED


class LockTypeEnum(data_model.Enum8):
    DEAD_BOLT = 0


class OperatingModeEnum(data_model.Enum8):
    NORMAL = 0


class DoorLockCluster(data_model.Cluster):
    """Minimal Matter Door Lock cluster (0x0101) sufficient for lock/unlock."""

    CLUSTER_ID = 0x0101

    LockState = data_model.EnumAttribute(0x0000, LockStateEnum, X_nullable=True)
    LockType = data_model.EnumAttribute(0x0001, LockTypeEnum)
    ActuatorEnabled = data_model.BoolAttribute(0x0002, default=True)
    OperatingMode = data_model.EnumAttribute(0x0025, OperatingModeEnum)
    # Bitmap of supported operating modes; bit 0 (Normal) supported.
    SupportedOperatingModes = data_model.NumberAttribute(
        0x0026, signed=False, bits=16, default=0xFFFE
    )

    # request_type=None: controllers may include an optional PINCode we ignore.
    LockDoor = data_model.Command(0x00, None)
    UnlockDoor = data_model.Command(0x01, None)


class SesameDoorLock(SimpleDevice):
    """A Door Lock endpoint bound to a loockit device id."""

    DEVICE_TYPE_ID = 0x000A
    REVISION = 2

    def __init__(self, device_id: str, dispatch: SyncDispatch) -> None:
        super().__init__(device_id)
        self.device_id = device_id
        self._dispatch = dispatch

        self._lock = DoorLockCluster()
        self._lock.LockType = LockTypeEnum.DEAD_BOLT
        self._lock.OperatingMode = OperatingModeEnum.NORMAL
        self._lock.ActuatorEnabled = True
        self._lock.LockState = LockStateEnum.LOCKED
        self._lock.LockDoor = self._on_lock
        self._lock.UnlockDoor = self._on_unlock
        self.servers.append(self._lock)

    def _on_lock(self, session) -> None:
        self._dispatch(self.device_id, "LockDoor")

    def _on_unlock(self, session) -> None:
        self._dispatch(self.device_id, "UnlockDoor")

    def set_lock_state(self, matter_lock_state: int) -> None:
        self._lock.LockState = matter_lock_state


class SesameBotSwitch(OnOffLight):
    """An On/Off endpoint whose ``On`` triggers a momentary Bot click."""

    def __init__(self, device_id: str, dispatch: SyncDispatch) -> None:
        super().__init__(device_id)
        self.device_id = device_id
        self._dispatch = dispatch

    def on(self) -> None:
        self._dispatch(self.device_id, "On")

    def off(self) -> None:
        # A Bot is momentary; there is no persistent "off" action.
        pass


def build_devices(
    endpoints: list[BridgedEndpoint], dispatch: SyncDispatch
) -> tuple[list[SimpleDevice], dict[int, SesameDoorLock]]:
    """Construct CircuitMatter device objects for ``endpoints``.

    Returns the device list (to add to a CircuitMatter server) and a map from
    bridge endpoint id to the Door Lock device, used to push state. Pure: builds
    no sockets and starts no network — safe to unit-test.
    """
    devices: list[SimpleDevice] = []
    locks: dict[int, SesameDoorLock] = {}
    for ep in endpoints:
        if ep.kind == MatterEndpointKind.DOOR_LOCK:
            lock = SesameDoorLock(ep.device_id, dispatch)
            locks[ep.endpoint_id] = lock
            devices.append(lock)
        else:
            devices.append(SesameBotSwitch(ep.device_id, dispatch))
    return devices, locks


class CircuitMatterAdapter(MatterAdapter):
    """Bridges loockit onto a Matter fabric using CircuitMatter."""

    #: how often to drain the (non-blocking) Matter socket
    POLL_INTERVAL = 0.05

    def __init__(
        self, *, state_filename: str = "matter-device-state.json"
    ) -> None:
        self._state_filename = state_filename
        self._cm = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._command_handler: CommandHandler | None = None
        self._locks: dict[int, SesameDoorLock] = {}
        self._stopped = False

    def set_command_handler(self, handler: CommandHandler) -> None:
        self._command_handler = handler

    async def setup(self, endpoints: list[BridgedEndpoint]) -> None:
        from circuitmatter import CircuitMatter  # local import: optional dep

        self._loop = asyncio.get_running_loop()
        # Building the server binds sockets and (on first run) starts the
        # commissioning window + mDNS advertisement; do it off the event loop.
        try:
            self._cm = await asyncio.to_thread(
                CircuitMatter,
                state_filename=self._state_filename,
                product_name="loockit SESAME bridge",
            )
        except FileNotFoundError as exc:
            # CircuitMatter advertises over mDNS via avahi-publish-service.
            if "avahi" in str(exc):
                raise RuntimeError(
                    "Matter mDNS advertisement requires avahi-daemon "
                    "(install: `sudo apt install avahi-daemon avahi-utils`)."
                ) from exc
            raise
        devices, self._locks = build_devices(endpoints, self._dispatch)
        for device in devices:
            self._cm.add_device(device)
        logger.info(
            "CircuitMatter bridge created with %d endpoint(s)", len(devices)
        )

    def _dispatch(self, device_id: str, command: str) -> None:
        """Sync callback from the packet thread -> schedule on the loop."""
        if self._command_handler is None or self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._command_handler(device_id, command), self._loop
        )

    def commissioning_info(self) -> str:
        if self._cm is None:
            return "(Matter not started)"
        nv = getattr(self._cm, "nonvolatile", {})
        manual = nv.get("manual_code") if hasattr(nv, "get") else None
        return manual or "(see logs for the manual pairing code)"

    async def push_lock_state(self, endpoint_id: int, matter_lock_state: int) -> None:
        lock = self._locks.get(endpoint_id)
        if lock is not None:
            lock.set_lock_state(matter_lock_state)

    async def push_online(self, endpoint_id: int, online: bool) -> None:
        # CircuitMatter SimpleDevices have no reachability attribute; log only.
        logger.debug("endpoint %d online=%s", endpoint_id, online)

    async def run_forever(self) -> None:
        assert self._cm is not None
        while not self._stopped:
            try:
                await asyncio.to_thread(self._cm.process_packets)
            except Exception:  # pragma: no cover - keep the bridge alive
                logger.exception("error processing Matter packets")
            await asyncio.sleep(self.POLL_INTERVAL)

    async def close(self) -> None:
        self._stopped = True
        self._cm = None
