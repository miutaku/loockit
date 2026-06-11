"""Configuration loading for loockit.

Settings come from a TOML file, with secrets overridable by environment
variables so that keys never have to live in the file. For a device whose id is
``front-door`` the overrides are:

    LOOCKIT_FRONT_DOOR_SECRET_KEY
    LOOCKIT_FRONT_DOOR_PUBLIC_KEY

(id uppercased, non-alphanumerics -> ``_``). Env vars win over file values.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - exercised only on <3.11
    import tomli as _toml  # type: ignore[no-redefine]

from .models import DeviceModel


class ConfigError(Exception):
    """Raised for any malformed or incomplete configuration."""


def _env_key(device_id: str, suffix: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]", "_", device_id).upper()
    return f"LOOCKIT_{slug}_{suffix}"


@dataclass
class CloudConfig:
    """Optional CANDY HOUSE Web API v4 fallback credentials (disabled by default)."""

    uuid: str
    api_key: str
    secret_key: str


@dataclass
class DeviceConfig:
    """A single SESAME device definition."""

    id: str
    model: DeviceModel
    ble_address: str
    secret_key: Optional[str] = None
    public_key: Optional[str] = None
    cloud: Optional[CloudConfig] = None

    def require_keys(self) -> None:
        """Validate that BLE credentials are present (skipped in simulate mode)."""
        if not self.secret_key:
            raise ConfigError(
                f"device '{self.id}': missing secret_key "
                f"(set in config or env {_env_key(self.id, 'SECRET_KEY')})"
            )
        if not self.public_key:
            raise ConfigError(
                f"device '{self.id}': missing public_key "
                f"(set in config or env {_env_key(self.id, 'PUBLIC_KEY')})"
            )


@dataclass
class GrpcConfig:
    host: str = "0.0.0.0"
    port: int = 50051


@dataclass
class MatterConfig:
    enabled: bool = False
    port: int = 5540


@dataclass
class RestConfig:
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8080


@dataclass
class MqttConfig:
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    username: Optional[str] = None
    password: Optional[str] = None
    # Home Assistant MQTT Discovery prefix and this bridge's own topic root.
    discovery_prefix: str = "homeassistant"
    base_topic: str = "loockit"


@dataclass
class HistoryConfig:
    enabled: bool = False
    path: str = "loockit-history.sqlite3"


@dataclass
class AppConfig:
    devices: list[DeviceConfig] = field(default_factory=list)
    grpc: GrpcConfig = field(default_factory=GrpcConfig)
    matter: MatterConfig = field(default_factory=MatterConfig)
    rest: RestConfig = field(default_factory=RestConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    history: HistoryConfig = field(default_factory=HistoryConfig)
    cloud_fallback: bool = False

    def device(self, device_id: str) -> DeviceConfig:
        for d in self.devices:
            if d.id == device_id:
                return d
        raise ConfigError(f"unknown device id: {device_id}")


def _parse_model(raw: str, device_id: str) -> DeviceModel:
    normalized = raw.strip().upper().replace(" ", "").replace("_", "")
    aliases = {
        "SESAME4": DeviceModel.SESAME4,
        "SS4": DeviceModel.SESAME4,
        "SESAMEBOT1": DeviceModel.SESAME_BOT1,
        "SESAMEBOT": DeviceModel.SESAME_BOT1,
        "BOT1": DeviceModel.SESAME_BOT1,
        "BOT": DeviceModel.SESAME_BOT1,
    }
    if normalized not in aliases:
        raise ConfigError(
            f"device '{device_id}': unsupported model '{raw}' "
            f"(supported: SESAME4, SESAMEBOT1)"
        )
    return aliases[normalized]


def _load_cloud(raw: dict, device_id: str) -> CloudConfig:
    try:
        return CloudConfig(
            uuid=str(raw["uuid"]),
            api_key=str(raw["api_key"]),
            secret_key=str(raw["secret_key"]),
        )
    except KeyError as exc:
        raise ConfigError(
            f"device '{device_id}': cloud config missing field {exc}"
        ) from exc


def _load_device(raw: dict, env: dict) -> DeviceConfig:
    try:
        device_id = str(raw["id"])
    except KeyError as exc:
        raise ConfigError("device entry missing 'id'") from exc

    try:
        model = _parse_model(str(raw["model"]), device_id)
        ble_address = str(raw["ble_address"])
    except KeyError as exc:
        raise ConfigError(f"device '{device_id}': missing field {exc}") from exc

    secret_key = env.get(_env_key(device_id, "SECRET_KEY")) or raw.get("secret_key")
    public_key = env.get(_env_key(device_id, "PUBLIC_KEY")) or raw.get("public_key")

    cloud = None
    if isinstance(raw.get("cloud"), dict):
        cloud = _load_cloud(raw["cloud"], device_id)

    return DeviceConfig(
        id=device_id,
        model=model,
        ble_address=ble_address,
        secret_key=str(secret_key) if secret_key else None,
        public_key=str(public_key) if public_key else None,
        cloud=cloud,
    )


def load_config(path: str | os.PathLike, env: Optional[dict] = None) -> AppConfig:
    """Parse ``path`` into an :class:`AppConfig`. Raises :class:`ConfigError`."""
    env = dict(os.environ if env is None else env)
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")

    with p.open("rb") as fh:
        try:
            raw = _toml.load(fh)
        except Exception as exc:  # tomllib raises TOMLDecodeError
            raise ConfigError(f"invalid TOML in {p}: {exc}") from exc

    raw_devices = raw.get("devices", [])
    if not isinstance(raw_devices, list) or not raw_devices:
        raise ConfigError("config must define at least one [[devices]] entry")

    devices = [_load_device(d, env) for d in raw_devices]

    ids = [d.id for d in devices]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ConfigError(f"duplicate device id(s): {', '.join(sorted(dupes))}")

    grpc_raw = raw.get("grpc", {})
    grpc = GrpcConfig(
        host=str(grpc_raw.get("host", "0.0.0.0")),
        port=int(grpc_raw.get("port", 50051)),
    )

    matter_raw = raw.get("matter", {})
    matter = MatterConfig(
        enabled=bool(matter_raw.get("enabled", False)),
        port=int(matter_raw.get("port", 5540)),
    )

    rest_raw = raw.get("rest", {})
    rest = RestConfig(
        enabled=bool(rest_raw.get("enabled", False)),
        host=str(rest_raw.get("host", "0.0.0.0")),
        port=int(rest_raw.get("port", 8080)),
    )

    mqtt_raw = raw.get("mqtt", {})
    mqtt = MqttConfig(
        enabled=bool(mqtt_raw.get("enabled", False)),
        host=str(mqtt_raw.get("host", "localhost")),
        port=int(mqtt_raw.get("port", 1883)),
        username=(str(mqtt_raw["username"]) if mqtt_raw.get("username") else None),
        password=(str(mqtt_raw["password"]) if mqtt_raw.get("password") else None),
        discovery_prefix=str(mqtt_raw.get("discovery_prefix", "homeassistant")),
        base_topic=str(mqtt_raw.get("base_topic", "loockit")),
    )

    history_raw = raw.get("history", {})
    history = HistoryConfig(
        enabled=bool(history_raw.get("enabled", False)),
        path=str(history_raw.get("path", "loockit-history.sqlite3")),
    )

    cloud_fallback = bool(raw.get("cloud_fallback", False))

    return AppConfig(
        devices=devices,
        grpc=grpc,
        matter=matter,
        rest=rest,
        mqtt=mqtt,
        history=history,
        cloud_fallback=cloud_fallback,
    )
