"""Real BLE controller backed by ``pysesameos2``.

Wraps a single SesameOS2 device: scans for it by BLE address, authenticates with
the secret/public key, keeps a persistent connection, and translates the
library's synchronous state callback into normalized :class:`DeviceState`
emissions. On disconnect it reconnects with exponential backoff so real-time
monitoring (requirement 1.3) keeps working unattended.

``pysesameos2`` is imported lazily so the rest of the app (and the simulator)
works even where ``bleak``/BLE is unavailable.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import DeviceConfig
from ..models import (
    Action,
    ActionError,
    DeviceModel,
    DeviceState,
    LockState,
    Source,
    lock_state_from_ranges,
)
from .base import DeviceController

logger = logging.getLogger(__name__)

_RECONNECT_BASE = 2.0
_RECONNECT_MAX = 60.0


class BleController(DeviceController):
    """Drives one SESAME device over BLE via SesameOS2."""

    def __init__(self, config: DeviceConfig, *, scan_duration: int = 15) -> None:
        super().__init__(config)
        self._scan_duration = scan_duration
        self._device = None  # pysesameos2 CHSesame2 | CHSesameBot
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._closing = False
        self._state = DeviceState(
            device_id=config.id,
            model=config.model,
            lock_state=LockState.UNKNOWN,
            source=Source.BLE,
            online=False,
        )

    def current_state(self) -> DeviceState:
        return self._state

    async def connect(self) -> None:
        self.config.require_keys()
        self._closing = False
        self._loop = asyncio.get_running_loop()
        try:
            await self._open_session()
        except Exception:
            # A BLE advertisement can be missed during the initial scan. Keep
            # retrying just as we do after an established connection drops.
            self._schedule_reconnect()
            raise

    async def _open_session(self) -> None:
        from pysesameos2.ble import CHBleManager
        from pysesameos2.device import CHDeviceKey

        logger.info("scanning for %s (%s)", self.config.id, self.config.ble_address)
        device = await CHBleManager().scan_by_address(
            ble_device_identifier=self.config.ble_address,
            scan_duration=self._scan_duration,
        )

        key = CHDeviceKey()
        key.setSecretKey(self.config.secret_key)
        key.setSesame2PublicKey(self.config.public_key)
        device.setKey(key)
        device.setDeviceStatusCallback(self._on_device_state)

        await device.connect()
        await device.wait_for_login()
        self._device = device
        logger.info("%s logged in", self.config.id)
        # Push an immediate snapshot now that mech status is available.
        self._on_device_state(device)

    async def disconnect(self) -> None:
        self._closing = True
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        device = self._device
        self._device = None
        if device is not None:
            disconnect = getattr(device, "disconnect", None)
            try:
                if disconnect is not None:
                    result = disconnect()
                    if asyncio.iscoroutine(result):
                        await result
            except Exception:  # pragma: no cover - best-effort teardown
                logger.debug("error during disconnect", exc_info=True)
        self._state = self._state.evolve(online=False)
        self._emit(self._state)

    async def execute(self, action: Action, history_tag: str) -> DeviceState:
        model = self.config.model
        if action.requires_lock and not model.is_lock:
            raise ActionError(f"{action.value} is not supported on {model.value}")
        if action.requires_bot and not model.is_bot:
            raise ActionError(f"{action.value} is not supported on {model.value}")
        if self._device is None:
            raise ConnectionError(f"{self.config.id} is not connected")

        if action is Action.LOCK:
            await self._device.lock(history_tag=history_tag)
        elif action is Action.UNLOCK:
            await self._device.unlock(history_tag=history_tag)
        elif action is Action.TOGGLE:
            await self._device.toggle(history_tag=history_tag)
        elif action is Action.CLICK:
            await self._device.click(history_tag=history_tag)
        return self._state

    # -- callback bridge -------------------------------------------------

    def _on_device_state(self, device) -> None:
        """Synchronous callback from pysesameos2; translate & emit."""
        try:
            state = self._translate(device)
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to translate state for %s", self.config.id)
            return
        self._state = state
        self._emit(state)
        if not state.online and not self._closing:
            self._schedule_reconnect()

    def _translate(self, device) -> DeviceState:
        from pysesameos2.helper import CHProductModel

        mech = device.getMechStatus()
        dev_status = device.getDeviceStatus()
        # CHSesame2Status.value is CHDeviceLoginStatus.Login when authenticated.
        online = getattr(getattr(dev_status, "value", None), "name", "") == "Login"

        lock_state = LockState.UNKNOWN
        battery_percent = self._state.battery_percent
        battery_voltage = self._state.battery_voltage
        position = self._state.position
        motor_status = self._state.motor_status

        if mech is not None:
            lock_state = lock_state_from_ranges(
                mech.isInLockRange(), mech.isInUnlockRange()
            )
            battery_percent = mech.getBatteryPercentage()
            battery_voltage = mech.getBatteryVoltage()
            if device.productModel in (CHProductModel.SS2, CHProductModel.SS4):
                position = mech.getPosition()
            elif device.productModel == CHProductModel.SesameBot1:
                motor_status = mech.getMotorStatus()
        elif online and dev_status is not None:
            # Fall back to the status name (Locked / Unlocked / Moved).
            name = getattr(dev_status, "name", "")
            lock_state = {
                "Locked": LockState.LOCKED,
                "Unlocked": LockState.UNLOCKED,
                "Moved": LockState.MOVING,
            }.get(name, LockState.UNKNOWN)

        return DeviceState(
            device_id=self.config.id,
            model=self.config.model,
            lock_state=lock_state,
            battery_percent=battery_percent,
            battery_voltage=battery_voltage,
            position=position if self.config.model is DeviceModel.SESAME4 else None,
            motor_status=motor_status if self.config.model.is_bot else None,
            source=Source.BLE,
            online=online,
        )

    # -- reconnection ----------------------------------------------------

    def _schedule_reconnect(self) -> None:
        if self._closing or self._loop is None:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = self._loop.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = _RECONNECT_BASE
        while not self._closing:
            await asyncio.sleep(delay)
            if self._closing:
                return
            try:
                logger.info("reconnecting to %s", self.config.id)
                await self._open_session()
                logger.info("reconnected to %s", self.config.id)
                return
            except Exception as exc:
                logger.warning("reconnect to %s failed: %s", self.config.id, exc)
                delay = min(delay * 2, _RECONNECT_MAX)
