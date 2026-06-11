"""Tests for the CircuitMatter-backed Matter adapter.

These exercise device/cluster construction and the command/state mapping without
starting the CircuitMatter network server (no sockets, mDNS, or controller).
"""

import asyncio

import pytest

pytest.importorskip("circuitmatter")

from loockit.bridge.matter import (  # noqa: E402
    MATTER_LOCK_LOCKED,
    MATTER_LOCK_UNLOCKED,
    BridgedEndpoint,
    MatterEndpointKind,
)
from loockit.bridge.matter_circuit import (  # noqa: E402
    CircuitMatterAdapter,
    LockStateEnum,
    SesameBotSwitch,
    SesameDoorLock,
    build_devices,
)
from loockit.models import DeviceModel  # noqa: E402


def _endpoints():
    return [
        BridgedEndpoint(
            device_id="front-door",
            model=DeviceModel.SESAME4,
            kind=MatterEndpointKind.DOOR_LOCK,
            endpoint_id=1,
        ),
        BridgedEndpoint(
            device_id="desk-bot",
            model=DeviceModel.SESAME_BOT1,
            kind=MatterEndpointKind.ON_OFF,
            endpoint_id=2,
        ),
    ]


def test_build_devices_creates_correct_types():
    calls = []
    devices, locks = build_devices(_endpoints(), lambda d, c: calls.append((d, c)))

    by_id = {d.device_id: d for d in devices}
    assert isinstance(by_id["front-door"], SesameDoorLock)
    assert isinstance(by_id["desk-bot"], SesameBotSwitch)
    assert set(locks) == {1}
    assert isinstance(locks[1], SesameDoorLock)


def test_door_lock_cluster_attributes_and_commands():
    devices, locks = build_devices(_endpoints(), lambda d, c: None)
    lock = locks[1]
    # Door Lock cluster (0x0101) is present and accepts LockDoor/UnlockDoor.
    cluster = lock._lock
    assert cluster.CLUSTER_ID == 0x0101
    assert set(cluster.accepted_command_list) == {0x00, 0x01}
    assert int(cluster.LockState) == MATTER_LOCK_LOCKED  # default Locked


def test_lock_commands_dispatch():
    calls = []
    _, locks = build_devices(_endpoints(), lambda d, c: calls.append((d, c)))
    lock = locks[1]
    # Simulate the CircuitMatter invoke calling the bound callables.
    lock._lock.LockDoor(session=None)
    lock._lock.UnlockDoor(session=None)
    assert calls == [("front-door", "LockDoor"), ("front-door", "UnlockDoor")]


def test_bot_on_dispatches_click():
    calls = []
    devices, _ = build_devices(_endpoints(), lambda d, c: calls.append((d, c)))
    bot = next(d for d in devices if d.device_id == "desk-bot")
    bot.on()
    bot.off()  # momentary: no dispatch
    assert calls == [("desk-bot", "On")]


def test_set_lock_state():
    _, locks = build_devices(_endpoints(), lambda d, c: None)
    lock = locks[1]
    lock.set_lock_state(MATTER_LOCK_UNLOCKED)
    assert int(lock._lock.LockState) == MATTER_LOCK_UNLOCKED
    assert LockStateEnum.UNLOCKED == MATTER_LOCK_UNLOCKED


async def test_adapter_dispatch_marshals_to_loop():
    adapter = CircuitMatterAdapter()
    adapter._loop = asyncio.get_running_loop()
    received = []

    async def handler(device_id, command):
        received.append((device_id, command))

    adapter.set_command_handler(handler)
    adapter._dispatch("front-door", "LockDoor")
    await asyncio.sleep(0.01)
    assert received == [("front-door", "LockDoor")]


async def test_adapter_push_lock_state():
    adapter = CircuitMatterAdapter()
    _, adapter._locks = build_devices(_endpoints(), lambda d, c: None)
    await adapter.push_lock_state(1, MATTER_LOCK_UNLOCKED)
    assert int(adapter._locks[1]._lock.LockState) == MATTER_LOCK_UNLOCKED
    # Unknown endpoint is a safe no-op.
    await adapter.push_lock_state(99, MATTER_LOCK_LOCKED)
