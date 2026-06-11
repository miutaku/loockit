import textwrap

import pytest

from loockit.config import ConfigError, load_config
from loockit.models import DeviceModel


def _write(tmp_path, body: str):
    p = tmp_path / "config.toml"
    p.write_text(textwrap.dedent(body))
    return p


def test_load_minimal(tmp_path):
    p = _write(
        tmp_path,
        """
        [[devices]]
        id = "front-door"
        model = "SESAME4"
        ble_address = "24:71:89:cc:09:05"
        secret_key = "aa"
        public_key = "bb"

        [[devices]]
        id = "desk-bot"
        model = "SESAMEBOT1"
        ble_address = "24:71:89:aa:bb:cc"
        """,
    )
    cfg = load_config(p, env={})
    assert cfg.grpc.port == 50051
    assert cfg.cloud_fallback is False
    assert [d.id for d in cfg.devices] == ["front-door", "desk-bot"]
    assert cfg.device("front-door").model is DeviceModel.SESAME4
    assert cfg.device("desk-bot").model is DeviceModel.SESAME_BOT1


def test_env_overrides_keys(tmp_path):
    p = _write(
        tmp_path,
        """
        [[devices]]
        id = "front-door"
        model = "SESAME4"
        ble_address = "x"
        secret_key = "file-secret"
        """,
    )
    env = {
        "LOOCKIT_FRONT_DOOR_SECRET_KEY": "env-secret",
        "LOOCKIT_FRONT_DOOR_PUBLIC_KEY": "env-public",
    }
    cfg = load_config(p, env=env)
    dev = cfg.device("front-door")
    assert dev.secret_key == "env-secret"
    assert dev.public_key == "env-public"


def test_model_alias_and_grpc_matter(tmp_path):
    p = _write(
        tmp_path,
        """
        [grpc]
        port = 6000
        [matter]
        enabled = true
        cloud_fallback = true

        [[devices]]
        id = "d"
        model = "SS4"
        ble_address = "x"
        secret_key = "a"
        public_key = "b"
        """,
    )
    cfg = load_config(p, env={})
    assert cfg.grpc.port == 6000
    assert cfg.matter.enabled is True
    assert cfg.device("d").model is DeviceModel.SESAME4


def test_duplicate_ids_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        [[devices]]
        id = "dup"
        model = "SESAME4"
        ble_address = "x"
        [[devices]]
        id = "dup"
        model = "SESAMEBOT1"
        ble_address = "y"
        """,
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(p, env={})


def test_unknown_model_rejected(tmp_path):
    p = _write(
        tmp_path,
        """
        [[devices]]
        id = "d"
        model = "SESAME5"
        ble_address = "x"
        """,
    )
    with pytest.raises(ConfigError, match="unsupported model"):
        load_config(p, env={})


def test_missing_devices_rejected(tmp_path):
    p = _write(tmp_path, "[grpc]\nport = 1\n")
    with pytest.raises(ConfigError, match="at least one"):
        load_config(p, env={})


def test_require_keys(tmp_path):
    p = _write(
        tmp_path,
        """
        [[devices]]
        id = "d"
        model = "SESAME4"
        ble_address = "x"
        """,
    )
    cfg = load_config(p, env={})
    with pytest.raises(ConfigError, match="missing secret_key"):
        cfg.device("d").require_keys()
