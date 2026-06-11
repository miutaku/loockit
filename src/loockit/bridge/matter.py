"""Matter bridge for loockit (local BLE only — never the CANDY HOUSE cloud API).

Maps each SESAME device onto a Matter endpoint and bridges both directions:

- SESAME4  -> Door Lock cluster (LockDoor / UnlockDoor; LockState attribute).
- SESAME Bot1 -> On/Off cluster (On = push the button via a click; Off is a no-op
  acknowledgement, since a Bot is momentary and has no persistent on/off state).

Matter commands from a controller (Apple Home, Google, etc.) are translated to
:class:`~loockit.models.Action` and run through the :class:`DeviceManager`.
Device state changes from the manager are pushed back as Matter attribute
updates so controllers stay in sync with manual/other-app operations.

The device-side Matter SDK is a heavy, platform-specific native dependency and is
imported lazily via a pluggable :class:`MatterAdapter`. The mapping logic below is
plain Python and is unit-tested without the SDK. Install with the ``matter``
extra and run with ``--enable-matter``; the manual pairing / commissioning code
is logged on start.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from dataclasses import dataclass

from ..manager import DeviceManager
from ..models import Action, DeviceModel, DeviceState, LockState
from .base import Bridge

logger = logging.getLogger(__name__)


# -- Matter constants (avoid importing the SDK just for enum values) --------
# Matter Door Lock cluster, LockState attribute (0x0101 / attr 0x0000).
MATTER_LOCK_NOT_FULLY_LOCKED = 0
MATTER_LOCK_LOCKED = 1
MATTER_LOCK_UNLOCKED = 2
MATTER_LOCK_UNKNOWN = 3  # firmware "undefined" sentinel


class MatterEndpointKind(str):
    DOOR_LOCK = "door-lock"
    ON_OFF = "on-off"


def endpoint_kind_for(model: DeviceModel) -> str:
    """Which Matter device type a SESAME model is exposed as."""
    if model.is_lock:
        return MatterEndpointKind.DOOR_LOCK
    return MatterEndpointKind.ON_OFF


def lock_state_to_matter(state: LockState) -> int:
    """Translate normalized lock state to the Matter DoorLock LockState value."""
    return {
        LockState.LOCKED: MATTER_LOCK_LOCKED,
        LockState.UNLOCKED: MATTER_LOCK_UNLOCKED,
        LockState.MOVING: MATTER_LOCK_NOT_FULLY_LOCKED,
        LockState.UNKNOWN: MATTER_LOCK_UNKNOWN,
    }[state]


def matter_command_to_action(model: DeviceModel, command: str) -> Action:
    """Translate an inbound Matter command name to a loockit Action.

    ``command`` is one of: ``LockDoor``, ``UnlockDoor`` (door lock); ``On``,
    ``Off`` (on/off). Raises :class:`ValueError` for unsupported combinations.
    """
    if model.is_lock:
        if command == "LockDoor":
            return Action.LOCK
        if command == "UnlockDoor":
            return Action.UNLOCK
        raise ValueError(f"unsupported door-lock command: {command}")
    # Bot: a momentary push.
    if command == "On":
        return Action.CLICK
    if command == "Off":
        raise ValueError("Off is a no-op for a momentary Bot")
    raise ValueError(f"unsupported on/off command: {command}")


@dataclass
class BridgedEndpoint:
    """One device's place in the Matter bridge."""

    device_id: str
    model: DeviceModel
    kind: str
    endpoint_id: int


class MatterAdapter(abc.ABC):
    """Pluggable seam over the device-side Matter SDK.

    Keeping the SDK behind this interface lets the bridge logic be tested with a
    fake, and lets the heavy native dependency stay optional.
    """

    @abc.abstractmethod
    async def setup(self, endpoints: list[BridgedEndpoint]) -> None:
        """Create the bridge node and its endpoints; begin advertising."""

    @abc.abstractmethod
    def commissioning_info(self) -> str:
        """Human-readable manual pairing code / QR payload for logging."""

    @abc.abstractmethod
    async def push_lock_state(self, endpoint_id: int, matter_lock_state: int) -> None:
        """Update a Door Lock endpoint's LockState attribute."""

    @abc.abstractmethod
    async def push_online(self, endpoint_id: int, online: bool) -> None:
        """Update a bridged endpoint's reachability."""

    @abc.abstractmethod
    async def run_forever(self) -> None:
        """Run the Matter transport until cancelled."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release SDK resources."""


def _load_default_adapter() -> MatterAdapter:
    """Instantiate the CircuitMatter-backed adapter, clear error if missing."""
    try:
        from .matter_circuit import CircuitMatterAdapter
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "Matter support requires the optional 'matter' extra (CircuitMatter). "
            "Install with `pip install loockit[matter]`, or run without "
            "--enable-matter."
        ) from exc
    return CircuitMatterAdapter()


class MatterBridge(Bridge):
    """Bridges loockit devices onto a Matter fabric via a :class:`MatterAdapter`."""

    def __init__(
        self, manager: DeviceManager, *, adapter: MatterAdapter | None = None
    ) -> None:
        super().__init__(manager)
        self._adapter = adapter
        self._endpoints: dict[str, BridgedEndpoint] = {}
        self._pump_task: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None

    def _build_endpoints(self) -> list[BridgedEndpoint]:
        endpoints: list[BridgedEndpoint] = []
        # Endpoint 0 is the Matter root; bridged endpoints start at 1.
        for index, device_id in enumerate(self.manager.device_ids(), start=1):
            cfg = self.manager.device_config(device_id)
            ep = BridgedEndpoint(
                device_id=device_id,
                model=cfg.model,
                kind=endpoint_kind_for(cfg.model),
                endpoint_id=index,
            )
            self._endpoints[device_id] = ep
            endpoints.append(ep)
        return endpoints

    async def start(self) -> None:
        if self._run_task is not None:
            return
        if self._adapter is None:
            self._adapter = _load_default_adapter()

        endpoints = self._build_endpoints()
        # Wire inbound Matter commands back to the manager.
        self._adapter_set_command_handler()
        await self._adapter.setup(endpoints)
        logger.info(
            "Matter bridge ready. Commission with: %s",
            self._adapter.commissioning_info(),
        )
        self._run_task = asyncio.create_task(self._adapter.run_forever())
        self._pump_task = asyncio.create_task(self._pump_states())

    def _adapter_set_command_handler(self) -> None:
        # Adapters that accept a command handler get one; testing fakes may not.
        setter = getattr(self._adapter, "set_command_handler", None)
        if callable(setter):
            setter(self.handle_matter_command)

    async def handle_matter_command(self, device_id: str, command: str) -> None:
        """Entry point for inbound Matter commands (called by the adapter)."""
        ep = self._endpoints.get(device_id)
        if ep is None:
            logger.warning("Matter command for unknown device: %s", device_id)
            return
        try:
            action = matter_command_to_action(ep.model, command)
        except ValueError as exc:
            logger.warning("ignoring Matter command %s: %s", command, exc)
            return
        await self.manager.execute(device_id, action, history_tag="matter")

    async def _pump_states(self) -> None:
        """Forward manager state changes to Matter attributes."""
        assert self._adapter is not None
        async for state in self.manager.subscribe():
            await self._apply_state(state)

    async def _apply_state(self, state: DeviceState) -> None:
        assert self._adapter is not None
        ep = self._endpoints.get(state.device_id)
        if ep is None:
            return
        await self._adapter.push_online(ep.endpoint_id, state.online)
        if ep.kind == MatterEndpointKind.DOOR_LOCK:
            await self._adapter.push_lock_state(
                ep.endpoint_id, lock_state_to_matter(state.lock_state)
            )

    async def stop(self) -> None:
        for task in (self._pump_task, self._run_task):
            if task is not None:
                task.cancel()
        self._pump_task = None
        self._run_task = None
        if self._adapter is not None:
            await self._adapter.close()
