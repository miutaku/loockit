import asyncio

import pytest

from loockit.config import DeviceConfig
from loockit.controller.fake import FakeController
from loockit.manager import DeviceManager
from loockit.models import (
    Action,
    ActionError,
    DeviceModel,
    DeviceNotFoundError,
    LockState,
)


async def test_lock_unlock_toggle(config):
    mgr = DeviceManager(config, simulate=True)
    await mgr.start()
    try:
        s = await mgr.execute("front-door", Action.UNLOCK)
        assert s.lock_state is LockState.UNLOCKED
        s = await mgr.execute("front-door", Action.LOCK)
        assert s.lock_state is LockState.LOCKED
        s = await mgr.execute("front-door", Action.TOGGLE)
        assert s.lock_state is LockState.UNLOCKED
    finally:
        await mgr.stop()


async def test_click_on_bot(config):
    mgr = DeviceManager(config, simulate=True)
    await mgr.start()
    try:
        s = await mgr.execute("desk-bot", Action.CLICK)
        assert s.motor_status == 0  # returns to idle after the momentary push
    finally:
        await mgr.stop()


async def test_model_mismatch_raises(config):
    mgr = DeviceManager(config, simulate=True)
    await mgr.start()
    try:
        with pytest.raises(ActionError):
            await mgr.execute("desk-bot", Action.LOCK)
        with pytest.raises(ActionError):
            await mgr.execute("front-door", Action.CLICK)
    finally:
        await mgr.stop()


async def test_unknown_device(config):
    mgr = DeviceManager(config, simulate=True)
    with pytest.raises(DeviceNotFoundError):
        await mgr.execute("nope", Action.LOCK)


async def test_subscribe_receives_changes(config):
    mgr = DeviceManager(config, simulate=True)
    await mgr.start()
    received: list = []

    async def consume():
        async for state in mgr.subscribe(replay=False):
            received.append(state)
            if state.lock_state is LockState.LOCKED:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await mgr.execute("front-door", Action.LOCK)
    await asyncio.wait_for(task, timeout=2)
    # We should have seen MOVING then LOCKED.
    assert any(s.lock_state is LockState.MOVING for s in received)
    assert received[-1].lock_state is LockState.LOCKED
    await mgr.stop()


async def test_subscribe_replays_snapshot(config):
    mgr = DeviceManager(config, simulate=True)
    await mgr.start()
    gen = mgr.subscribe(replay=True)
    first = await asyncio.wait_for(gen.__anext__(), timeout=1)
    assert first.device_id in ("front-door", "desk-bot")
    await gen.aclose()
    await mgr.stop()


class _FailingController(FakeController):
    async def execute(self, action, history_tag):
        raise ConnectionError("ble down")


async def test_cloud_fallback(config):
    cfg = config
    cfg.cloud_fallback = True
    # Attach cloud config to the lock so a cloud controller would be built.
    mgr = DeviceManager(cfg, simulate=True)
    await mgr.start()
    # Swap the front-door entry to a failing primary + a working fake "cloud".
    entry = mgr._entries["front-door"]
    failing = _FailingController(entry.config)
    failing.set_state_callback(mgr._on_state)
    cloud = FakeController(entry.config)
    cloud.set_state_callback(mgr._on_state)
    entry.primary = failing
    entry.cloud = cloud

    s = await mgr.execute("front-door", Action.LOCK)
    assert s.lock_state is LockState.LOCKED
    await mgr.stop()


async def test_action_error_does_not_fallback(config):
    cfg = config
    mgr = DeviceManager(cfg, simulate=True)
    await mgr.start()
    entry = mgr._entries["desk-bot"]
    entry.cloud = FakeController(entry.config)  # would succeed if reached
    # LOCK on a bot is an ActionError and must not fall back.
    with pytest.raises(ActionError):
        await mgr.execute("desk-bot", Action.LOCK)
    await mgr.stop()
