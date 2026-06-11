from loockit.models import (
    Action,
    DeviceModel,
    DeviceState,
    LockState,
    Source,
    lock_state_from_ranges,
)


def test_model_role_flags():
    assert DeviceModel.SESAME4.is_lock
    assert not DeviceModel.SESAME4.is_bot
    assert DeviceModel.SESAME_BOT1.is_bot
    assert not DeviceModel.SESAME_BOT1.is_lock


def test_action_role_requirements():
    assert Action.LOCK.requires_lock
    assert Action.UNLOCK.requires_lock
    assert Action.TOGGLE.requires_lock
    assert not Action.CLICK.requires_lock
    assert Action.CLICK.requires_bot


def test_lock_state_from_ranges():
    assert lock_state_from_ranges(True, False) is LockState.LOCKED
    assert lock_state_from_ranges(False, True) is LockState.UNLOCKED
    assert lock_state_from_ranges(False, False) is LockState.MOVING
    assert lock_state_from_ranges(True, True) is LockState.UNKNOWN


def test_device_state_evolve_is_immutable_and_bumps_timestamp():
    s1 = DeviceState(
        device_id="d", model=DeviceModel.SESAME4, lock_state=LockState.LOCKED
    )
    s2 = s1.evolve(lock_state=LockState.UNLOCKED)
    assert s1.lock_state is LockState.LOCKED  # original unchanged
    assert s2.lock_state is LockState.UNLOCKED
    assert s2.timestamp >= s1.timestamp
    assert s2.source is Source.BLE
