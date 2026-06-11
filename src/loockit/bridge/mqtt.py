"""MQTT bridge with Home Assistant MQTT Discovery.

Publishes auto-discovery configs so Home Assistant creates the right entities
without manual YAML, mirrors device state to MQTT, and turns inbound command
messages into local BLE actions via the :class:`DeviceManager`. Like the Matter
bridge, it only ever talks to the manager — never to any cloud API.

Entity mapping (Home Assistant):
- SESAME4    -> ``lock`` entity + a battery ``sensor``.
- SESAME Bot1 -> ``button`` entity (press = one click) + a battery ``sensor``.

The topic/payload/discovery logic is pure and unit-tested; :class:`MqttBridge`
wires it to an ``aiomqtt`` client with automatic reconnection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from ..config import MqttConfig
from ..manager import DeviceManager
from ..models import Action, DeviceModel, DeviceState, LockState
from .base import Bridge

logger = logging.getLogger(__name__)

_RECONNECT_DELAY = 5.0

# Lock entity state/command payloads (Home Assistant defaults align with these).
PAYLOAD_LOCK = "LOCK"
PAYLOAD_UNLOCK = "UNLOCK"
PAYLOAD_PRESS = "PRESS"
STATE_LOCKED = "LOCKED"
STATE_UNLOCKED = "UNLOCKED"
ONLINE = "online"
OFFLINE = "offline"


def _slug(device_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", device_id)


class Topics:
    """Computes the MQTT topics for one device under a base topic."""

    def __init__(self, base_topic: str, device_id: str) -> None:
        self.base = f"{base_topic}/{device_id}"

    @property
    def availability(self) -> str:
        return f"{self.base}/availability"

    @property
    def state(self) -> str:
        return f"{self.base}/state"

    @property
    def battery(self) -> str:
        return f"{self.base}/battery"

    @property
    def set(self) -> str:  # lock command
        return f"{self.base}/set"

    @property
    def press(self) -> str:  # bot command
        return f"{self.base}/press"


def _device_block(device_id: str, model: DeviceModel) -> dict:
    return {
        "identifiers": [f"loockit_{_slug(device_id)}"],
        "name": f"loockit {device_id}",
        "manufacturer": "CANDY HOUSE",
        "model": model.value,
    }


def discovery_payloads(
    device_id: str, model: DeviceModel, base_topic: str, discovery_prefix: str
) -> list[tuple[str, dict]]:
    """Build (config_topic, config_payload) pairs for HA MQTT Discovery."""
    slug = _slug(device_id)
    t = Topics(base_topic, device_id)
    device = _device_block(device_id, model)
    out: list[tuple[str, dict]] = []

    if model.is_lock:
        out.append(
            (
                f"{discovery_prefix}/lock/loockit_{slug}/config",
                {
                    "name": "Lock",
                    "unique_id": f"loockit_{slug}_lock",
                    "command_topic": t.set,
                    "state_topic": t.state,
                    "payload_lock": PAYLOAD_LOCK,
                    "payload_unlock": PAYLOAD_UNLOCK,
                    "state_locked": STATE_LOCKED,
                    "state_unlocked": STATE_UNLOCKED,
                    "optimistic": False,
                    "availability_topic": t.availability,
                    "payload_available": ONLINE,
                    "payload_not_available": OFFLINE,
                    "device": device,
                },
            )
        )
    else:
        out.append(
            (
                f"{discovery_prefix}/button/loockit_{slug}/config",
                {
                    "name": "Click",
                    "unique_id": f"loockit_{slug}_button",
                    "command_topic": t.press,
                    "payload_press": PAYLOAD_PRESS,
                    "availability_topic": t.availability,
                    "payload_available": ONLINE,
                    "payload_not_available": OFFLINE,
                    "device": device,
                },
            )
        )

    out.append(
        (
            f"{discovery_prefix}/sensor/loockit_{slug}_battery/config",
            {
                "name": "Battery",
                "unique_id": f"loockit_{slug}_battery",
                "state_topic": t.battery,
                "device_class": "battery",
                "unit_of_measurement": "%",
                "availability_topic": t.availability,
                "payload_available": ONLINE,
                "payload_not_available": OFFLINE,
                "device": device,
            },
        )
    )
    return out


def state_messages(
    state: DeviceState, base_topic: str
) -> list[tuple[str, str, bool]]:
    """(topic, payload, retain) messages reflecting a device state."""
    t = Topics(base_topic, state.device_id)
    msgs: list[tuple[str, str, bool]] = [
        (t.availability, ONLINE if state.online else OFFLINE, True),
    ]
    if state.lock_state is LockState.LOCKED:
        msgs.append((t.state, STATE_LOCKED, True))
    elif state.lock_state is LockState.UNLOCKED:
        msgs.append((t.state, STATE_UNLOCKED, True))
    # MOVING/UNKNOWN: leave the last settled state in place.
    if state.battery_percent is not None:
        msgs.append((t.battery, str(state.battery_percent), True))
    return msgs


def command_for(topic: str, payload: str, base_topic: str) -> Optional[
    tuple[str, Action]
]:
    """Map an inbound (topic, payload) to (device_id, Action), or None."""
    m = re.fullmatch(rf"{re.escape(base_topic)}/(.+)/(set|press)", topic)
    if not m:
        return None
    device_id, kind = m.group(1), m.group(2)
    if kind == "press":
        return device_id, Action.CLICK
    payload = payload.strip().upper()
    if payload == PAYLOAD_LOCK:
        return device_id, Action.LOCK
    if payload == PAYLOAD_UNLOCK:
        return device_id, Action.UNLOCK
    return None


class MqttBridge(Bridge):
    """Bridges loockit devices to MQTT / Home Assistant."""

    def __init__(self, manager: DeviceManager, config: MqttConfig) -> None:
        super().__init__(manager)
        self._config = config
        self._task: asyncio.Task | None = None
        self._client = None
        self._stopped = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        import aiomqtt

        while not self._stopped:
            try:
                async with aiomqtt.Client(
                    hostname=self._config.host,
                    port=self._config.port,
                    username=self._config.username,
                    password=self._config.password,
                ) as client:
                    self._client = client
                    logger.info(
                        "MQTT connected to %s:%s",
                        self._config.host,
                        self._config.port,
                    )
                    await self._publish_discovery(client)
                    await client.subscribe(f"{self._config.base_topic}/+/set")
                    await client.subscribe(f"{self._config.base_topic}/+/press")
                    await asyncio.gather(
                        self._publish_states(client),
                        self._handle_messages(client),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._client = None
                if self._stopped:
                    return
                logger.warning("MQTT connection lost (%s); retrying", exc)
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _publish_discovery(self, client) -> None:
        for device_id in self.manager.device_ids():
            model = self.manager.device_config(device_id).model
            for topic, payload in discovery_payloads(
                device_id,
                model,
                self._config.base_topic,
                self._config.discovery_prefix,
            ):
                await client.publish(topic, json.dumps(payload), retain=True)

    async def _publish_states(self, client) -> None:
        async for state in self.manager.subscribe():
            for topic, payload, retain in state_messages(
                state, self._config.base_topic
            ):
                await client.publish(topic, payload, retain=retain)

    async def _handle_messages(self, client) -> None:
        async for message in client.messages:
            topic = str(message.topic)
            payload = message.payload
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8", "replace")
            mapped = command_for(topic, str(payload), self._config.base_topic)
            if mapped is None:
                continue
            device_id, action = mapped
            try:
                await self.manager.execute(device_id, action, history_tag="mqtt")
            except Exception as exc:
                logger.warning("MQTT command %s on %s failed: %s", action, device_id, exc)
