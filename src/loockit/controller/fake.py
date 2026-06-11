"""In-memory simulator controller.

Lets the entire stack (manager, gRPC, Matter bridge, monitoring) run and be
tested without real hardware. It models the same command surface and emits state
changes — including a transient ``MOVING`` phase — so subscribers see realistic
transitions.
"""

from __future__ import annotations

import asyncio

from ..models import (
    Action,
    ActionError,
    DeviceState,
    LockState,
    Source,
)


class FakeController:
    """Simulates a SESAME4 or SESAME Bot1.

    Not a subclass of :class:`DeviceController` to avoid importing optional deps,
    but implements the same interface (structural typing is enough for the
    manager).
    """

    #: seconds spent in the MOVING state during a simulated actuation
    MOVE_DELAY = 0.05

    def __init__(self, config, *, battery_percent: int = 100) -> None:
        self.config = config
        self._on_state = None
        self._connected = False
        initial_lock = (
            LockState.LOCKED if config.model.is_lock else LockState.UNKNOWN
        )
        self._state = DeviceState(
            device_id=config.id,
            model=config.model,
            lock_state=initial_lock,
            battery_percent=battery_percent,
            battery_voltage=6.0,
            position=0 if config.model.is_lock else None,
            motor_status=0 if config.model.is_bot else None,
            source=Source.SIM,
            online=False,
        )

    @property
    def device_id(self) -> str:
        return self.config.id

    def set_state_callback(self, callback) -> None:
        self._on_state = callback

    def _emit(self, state: DeviceState) -> None:
        self._state = state
        if self._on_state is not None:
            self._on_state(state)

    async def connect(self) -> None:
        self._connected = True
        self._emit(self._state.evolve(online=True))

    async def disconnect(self) -> None:
        self._connected = False
        self._emit(self._state.evolve(online=False))

    def current_state(self) -> DeviceState:
        return self._state

    async def execute(self, action: Action, history_tag: str) -> DeviceState:
        model = self.config.model
        if action.requires_lock and not model.is_lock:
            raise ActionError(f"{action.value} is not supported on {model.value}")
        if action.requires_bot and not model.is_bot:
            raise ActionError(f"{action.value} is not supported on {model.value}")
        if not self._connected:
            await self.connect()

        if model.is_bot:
            return await self._simulate_click()
        return await self._simulate_lock(action)

    async def _simulate_lock(self, action: Action) -> DeviceState:
        if action is Action.TOGGLE:
            target = (
                LockState.UNLOCKED
                if self._state.lock_state is LockState.LOCKED
                else LockState.LOCKED
            )
        elif action is Action.LOCK:
            target = LockState.LOCKED
        else:
            target = LockState.UNLOCKED

        self._emit(self._state.evolve(lock_state=LockState.MOVING))
        await asyncio.sleep(self.MOVE_DELAY)
        position = 0 if target is LockState.LOCKED else 90
        self._emit(self._state.evolve(lock_state=target, position=position))
        return self._state

    async def _simulate_click(self) -> DeviceState:
        # A click is momentary: motor runs, then returns to idle.
        self._emit(self._state.evolve(lock_state=LockState.MOVING, motor_status=1))
        await asyncio.sleep(self.MOVE_DELAY)
        self._emit(self._state.evolve(lock_state=LockState.UNKNOWN, motor_status=0))
        return self._state
