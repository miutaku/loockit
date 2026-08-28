import asyncio

import pytest

from loockit.config import DeviceConfig
from loockit.controller import ble
from loockit.controller.ble import BleController
from loockit.models import DeviceModel


class FakeStatus:
    def __init__(self, name, login_name):
        self.name = name
        self.value = type("LoginStatus", (), {"name": login_name})()


class FakeDevice:
    def __init__(self, status):
        self.status = status
        self.disconnected = False

    def getDeviceStatus(self):
        return self.status

    def getMechStatus(self):
        return None

    async def disconnect(self):
        self.disconnected = True


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


async def test_busy_status_keeps_authenticated_current_session_online():
    controller = BleController(
        DeviceConfig(
            id="intercom-bot",
            model=DeviceModel.SESAME_BOT1,
            ble_address="00:11:22:33:44:55",
            secret_key="secret",
            public_key="public",
        )
    )
    device = FakeDevice(FakeStatus("Busy", "UnLogin"))
    controller._device = device
    state = controller._translate(device)

    assert state.online is True


async def test_reconnect_closes_previous_session(monkeypatch):
    controller = BleController(
        DeviceConfig(
            id="intercom-bot",
            model=DeviceModel.SESAME_BOT1,
            ble_address="00:11:22:33:44:55",
            secret_key="secret",
            public_key="public",
        )
    )
    old_device = FakeDevice(FakeStatus("NoBleSignal", "UnLogin"))
    controller._device = old_device

    async def scan_by_address(*_args):
        raise ConnectionError("advertisement missed")

    monkeypatch.setattr(ble, "_scan_by_address", scan_by_address)
    monkeypatch.setitem(
        __import__("sys").modules,
        "pysesameos2.device",
        type("DeviceModule", (), {"CHDeviceKey": object}),
    )

    with pytest.raises(ConnectionError, match="advertisement missed"):
        await controller._open_session()

    assert old_device.disconnected is True
    assert controller._device is None


async def test_scan_uses_configured_duration(monkeypatch):
    discovered = type("BLEDevice", (), {"address": "00:11:22:33:44:55"})()
    calls = []

    class Scanner:
        @staticmethod
        async def discover(*, timeout):
            calls.append(timeout)
            return [discovered]

    expected = object()

    class Manager:
        def device_factory(self, device):
            assert device is discovered
            return expected

    monkeypatch.setitem(
        __import__("sys").modules,
        "bleak",
        type("BleakModule", (), {"BleakScanner": Scanner}),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "pysesameos2.ble",
        type("BleModule", (), {"CHBleManager": Manager}),
    )

    result = await ble._scan_by_address("00:11:22:33:44:55", 30)

    assert result is expected
    assert calls == [30]
