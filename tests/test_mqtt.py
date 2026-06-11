from loockit.bridge.mqtt import (
    PAYLOAD_LOCK,
    PAYLOAD_PRESS,
    PAYLOAD_UNLOCK,
    STATE_LOCKED,
    STATE_UNLOCKED,
    Topics,
    command_for,
    discovery_payloads,
    state_messages,
)
from loockit.models import Action, DeviceModel, DeviceState, LockState, Source


def test_topics():
    t = Topics("loockit", "front-door")
    assert t.state == "loockit/front-door/state"
    assert t.set == "loockit/front-door/set"
    assert t.press == "loockit/front-door/press"
    assert t.availability == "loockit/front-door/availability"
    assert t.battery == "loockit/front-door/battery"


def test_discovery_lock():
    payloads = discovery_payloads(
        "front-door", DeviceModel.SESAME4, "loockit", "homeassistant"
    )
    topics = {t for t, _ in payloads}
    assert "homeassistant/lock/loockit_front-door/config" in topics
    assert "homeassistant/sensor/loockit_front-door_battery/config" in topics
    lock_cfg = next(p for t, p in payloads if "/lock/" in t)
    assert lock_cfg["command_topic"] == "loockit/front-door/set"
    assert lock_cfg["payload_lock"] == PAYLOAD_LOCK
    assert lock_cfg["state_locked"] == STATE_LOCKED


def test_discovery_bot():
    payloads = discovery_payloads(
        "desk-bot", DeviceModel.SESAME_BOT1, "loockit", "homeassistant"
    )
    topics = {t for t, _ in payloads}
    assert "homeassistant/button/loockit_desk-bot/config" in topics
    btn = next(p for t, p in payloads if "/button/" in t)
    assert btn["command_topic"] == "loockit/desk-bot/press"
    assert btn["payload_press"] == PAYLOAD_PRESS


def test_state_messages():
    s = DeviceState(
        device_id="front-door",
        model=DeviceModel.SESAME4,
        lock_state=LockState.LOCKED,
        battery_percent=77,
        source=Source.SIM,
        online=True,
    )
    msgs = {(t, p) for t, p, _ in state_messages(s, "loockit")}
    assert ("loockit/front-door/availability", "online") in msgs
    assert ("loockit/front-door/state", STATE_LOCKED) in msgs
    assert ("loockit/front-door/battery", "77") in msgs


def test_state_messages_unlocked_and_moving():
    base = DeviceState(
        device_id="d", model=DeviceModel.SESAME4, lock_state=LockState.UNLOCKED
    )
    msgs = {t: p for t, p, _ in state_messages(base, "loockit")}
    assert msgs["loockit/d/state"] == STATE_UNLOCKED

    moving = base.evolve(lock_state=LockState.MOVING)
    topics = {t for t, _, _ in state_messages(moving, "loockit")}
    # MOVING: no state topic published (leave last settled state).
    assert "loockit/d/state" not in topics


def test_command_for():
    assert command_for("loockit/front-door/set", "LOCK", "loockit") == (
        "front-door",
        Action.LOCK,
    )
    assert command_for("loockit/front-door/set", "unlock", "loockit") == (
        "front-door",
        Action.UNLOCK,
    )
    assert command_for("loockit/desk-bot/press", "PRESS", "loockit") == (
        "desk-bot",
        Action.CLICK,
    )
    assert command_for("loockit/x/set", "BOGUS", "loockit") is None
    assert command_for("other/x/set", "LOCK", "loockit") is None
