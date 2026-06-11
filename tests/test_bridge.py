import pytest

from loockit.bridge.matter import (
    MATTER_LOCK_LOCKED,
    MATTER_LOCK_NOT_FULLY_LOCKED,
    MATTER_LOCK_UNLOCKED,
    MatterAdapter,
    MatterBridge,
    MatterEndpointKind,
    endpoint_kind_for,
    lock_state_to_matter,
    matter_command_to_action,
)
from loockit.manager import DeviceManager
from loockit.models import Action, DeviceModel, LockState


def test_endpoint_kind_for():
    assert endpoint_kind_for(DeviceModel.SESAME4) == MatterEndpointKind.DOOR_LOCK
    assert endpoint_kind_for(DeviceModel.SESAME_BOT1) == MatterEndpointKind.ON_OFF


def test_lock_state_to_matter():
    assert lock_state_to_matter(LockState.LOCKED) == MATTER_LOCK_LOCKED
    assert lock_state_to_matter(LockState.UNLOCKED) == MATTER_LOCK_UNLOCKED
    assert lock_state_to_matter(LockState.MOVING) == MATTER_LOCK_NOT_FULLY_LOCKED


def test_matter_command_to_action():
    assert matter_command_to_action(DeviceModel.SESAME4, "LockDoor") is Action.LOCK
    assert (
        matter_command_to_action(DeviceModel.SESAME4, "UnlockDoor") is Action.UNLOCK
    )
    assert matter_command_to_action(DeviceModel.SESAME_BOT1, "On") is Action.CLICK
    with pytest.raises(ValueError):
        matter_command_to_action(DeviceModel.SESAME_BOT1, "Off")
    with pytest.raises(ValueError):
        matter_command_to_action(DeviceModel.SESAME4, "On")


class _FakeAdapter(MatterAdapter):
    def __init__(self):
        self.endpoints = []
        self.lock_pushes = []
        self.online_pushes = []
        self.command_handler = None
        self.closed = False

    def set_command_handler(self, handler):
        self.command_handler = handler

    async def setup(self, endpoints):
        self.endpoints = endpoints

    def commissioning_info(self):
        return "fake-pairing-code"

    async def push_lock_state(self, endpoint_id, matter_lock_state):
        self.lock_pushes.append((endpoint_id, matter_lock_state))

    async def push_online(self, endpoint_id, online):
        self.online_pushes.append((endpoint_id, online))

    async def run_forever(self):
        import asyncio

        await asyncio.Event().wait()

    async def close(self):
        self.closed = True


async def test_bridge_with_fake_adapter(config):
    mgr = DeviceManager(config, simulate=True)
    await mgr.start()
    adapter = _FakeAdapter()
    bridge = MatterBridge(mgr, adapter=adapter)
    await bridge.start()
    try:
        # Endpoints assigned: lock -> door-lock, bot -> on-off.
        kinds = {e.device_id: e.kind for e in adapter.endpoints}
        assert kinds["front-door"] == MatterEndpointKind.DOOR_LOCK
        assert kinds["desk-bot"] == MatterEndpointKind.ON_OFF

        # Inbound Matter command actuates via the manager.
        await bridge.handle_matter_command("front-door", "LockDoor")
        assert mgr.get_state("front-door").lock_state is LockState.LOCKED

        # Outbound: a manager state change reaches the adapter as a lock push.
        await mgr.execute("front-door", Action.UNLOCK)
        import asyncio

        await asyncio.sleep(0.05)
        assert any(
            v == MATTER_LOCK_UNLOCKED for _, v in adapter.lock_pushes
        )
    finally:
        await bridge.stop()
        await mgr.stop()
    assert adapter.closed
