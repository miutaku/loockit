import asyncio
import logging

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


async def test_disconnect_force_closes_connected_underlying_client():
    controller = BleController(
        DeviceConfig(
            id="intercom-bot",
            model=DeviceModel.SESAME_BOT1,
            ble_address="00:11:22:33:44:55",
            secret_key="secret",
            public_key="public",
        )
    )

    class Client:
        def __init__(self):
            self.is_connected = True
            self.disconnect_calls = 0

        async def disconnect(self):
            self.disconnect_calls += 1
            self.is_connected = False

    class Device:
        def __init__(self):
            self._client = Client()
            self.disconnect_calls = 0

        async def disconnect(self):
            # Simulate pysesameos2 swallowing a stop_notify failure before it
            # reaches its own BleakClient.disconnect() call.
            self.disconnect_calls += 1

    device = Device()
    await controller._disconnect_device(device)

    assert device.disconnect_calls == 1
    assert device._client.disconnect_calls == 1
    assert device._client.is_connected is False


async def test_disconnect_does_not_double_close_disconnected_client():
    controller = BleController(
        DeviceConfig(
            id="intercom-bot",
            model=DeviceModel.SESAME_BOT1,
            ble_address="00:11:22:33:44:55",
            secret_key="secret",
            public_key="public",
        )
    )

    class Client:
        is_connected = True
        disconnect_calls = 0

        async def disconnect(self):
            self.disconnect_calls += 1
            self.is_connected = False

    class Device:
        def __init__(self):
            self._client = Client()

        async def disconnect(self):
            await self._client.disconnect()

    device = Device()
    await controller._disconnect_device(device)

    assert device._client.disconnect_calls == 1


async def test_controllers_can_share_adapter_operation_lock():
    operation_lock = asyncio.Lock()
    first = BleController(
        DeviceConfig(
            id="first",
            model=DeviceModel.SESAME_BOT1,
            ble_address="00:11:22:33:44:55",
        ),
        operation_lock=operation_lock,
    )
    second = BleController(
        DeviceConfig(
            id="second",
            model=DeviceModel.SESAME4,
            ble_address="00:11:22:33:44:66",
        ),
        operation_lock=operation_lock,
    )

    assert first._operation_lock is operation_lock
    assert second._operation_lock is operation_lock


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


async def test_connection_observability_records_phase_and_elapsed_time(
    monkeypatch, caplog
):
    controller = BleController(
        DeviceConfig(
            id="intercom-bot",
            model=DeviceModel.SESAME_BOT1,
            ble_address="00:11:22:33:44:55",
            secret_key="secret",
            public_key="public",
        )
    )

    class DeviceKey:
        def setSecretKey(self, _value):
            pass

        def setSesame2PublicKey(self, _value):
            pass

    class Device(FakeDevice):
        def setKey(self, _key):
            pass

        def setDeviceStatusCallback(self, _callback):
            pass

        async def connect(self):
            pass

        async def wait_for_login(self):
            pass

    device = Device(FakeStatus("Unlocked", "Login"))

    async def scan_by_address(*_args):
        return device

    monkeypatch.setattr(ble, "_scan_by_address", scan_by_address)
    monkeypatch.setitem(
        __import__("sys").modules,
        "pysesameos2.device",
        type("DeviceModule", (), {"CHDeviceKey": DeviceKey}),
    )
    caplog.set_level(logging.INFO, logger="loockit.controller.ble")

    await controller._open_session()

    messages = [record.getMessage() for record in caplog.records]
    assert any("attempt started device=intercom-bot attempt=1" in m for m in messages)
    assert any("phase=scan elapsed_seconds=" in m for m in messages)
    assert any("phase=gatt_connect elapsed_seconds=" in m for m in messages)
    assert any("phase=login_wait elapsed_seconds=" in m for m in messages)
    assert any("attempt completed device=intercom-bot attempt=1" in m for m in messages)

async def test_hung_login_releases_shared_lock_and_disconnects(monkeypatch):
    class Key:
        def setSecretKey(self, value): pass
        def setSesame2PublicKey(self, value): pass
    class Device(FakeDevice):
        def setKey(self, key): pass
        def setDeviceStatusCallback(self, callback): pass
        async def connect(self): pass
        async def wait_for_login(self): await asyncio.Future()
    device = Device(FakeStatus('NoBleSignal', 'UnLogin'))
    async def scan(*args): return device
    monkeypatch.setitem(__import__('sys').modules, 'pysesameos2.device', type('Module', (), {'CHDeviceKey': Key}))
    monkeypatch.setattr(ble, '_scan_by_address', scan)
    monkeypatch.setattr(ble, '_LOGIN_TIMEOUT', 0.01)
    lock = asyncio.Lock()
    controller = BleController(DeviceConfig(id='bot', model=DeviceModel.SESAME_BOT1, ble_address='00:11:22:33:44:55', secret_key='secret', public_key='public'), operation_lock=lock)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(controller._open_session(), 1)
    assert device.disconnected
    assert not lock.locked()
    assert not controller._connecting

async def test_hung_disconnect_still_force_closes_client(monkeypatch):
    class Client:
        is_connected = True
        async def disconnect(self): self.is_connected = False
    class Device:
        _client = Client()
        async def disconnect(self): await asyncio.Future()
    monkeypatch.setattr(ble, '_DISCONNECT_TIMEOUT', 0.01)
    controller = BleController(DeviceConfig(id='bot', model=DeviceModel.SESAME_BOT1, ble_address='00:11:22:33:44:55'))
    device = Device()
    await asyncio.wait_for(controller._disconnect_device(device), 1)
    assert not device._client.is_connected
