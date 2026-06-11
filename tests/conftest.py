import pytest

from loockit.config import AppConfig, DeviceConfig, GrpcConfig
from loockit.models import DeviceModel


def make_config(*, cloud_fallback: bool = False) -> AppConfig:
    return AppConfig(
        devices=[
            DeviceConfig(
                id="front-door",
                model=DeviceModel.SESAME4,
                ble_address="x",
                secret_key="a",
                public_key="b",
            ),
            DeviceConfig(
                id="desk-bot",
                model=DeviceModel.SESAME_BOT1,
                ble_address="y",
                secret_key="a",
                public_key="b",
            ),
        ],
        grpc=GrpcConfig(host="127.0.0.1", port=0),
        cloud_fallback=cloud_fallback,
    )


@pytest.fixture
def config() -> AppConfig:
    return make_config()
