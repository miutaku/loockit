import asyncio

import pytest

from loockit.config import DeviceConfig
from loockit.controller import ble
from loockit.controller.ble import BleController
from loockit.models import DeviceModel


async def test_initial_scan_failure_schedules_reconnect(monkeypatch):
    controller = BleController(
        DeviceConfig(
            id="intercom-bot",
            model=DeviceModel.SESAME_BOT1,
            ble_address="00:11:22:33:44:55",
            secret_key="secret",
            public_key="public",
        )
    )
    attempts = 0
    reconnected = asyncio.Event()

    async def open_session():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("advertisement missed")
        reconnected.set()

    monkeypatch.setattr(controller, "_open_session", open_session)
    monkeypatch.setattr(ble, "_RECONNECT_BASE", 0.001)

    with pytest.raises(ConnectionError, match="advertisement missed"):
        await controller.connect()

    await asyncio.wait_for(reconnected.wait(), timeout=1)
    assert attempts == 2
    await controller.disconnect()
