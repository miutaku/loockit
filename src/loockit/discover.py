"""BLE discovery helper for onboarding real SESAME devices.

Wraps ``pysesameos2``'s scanner to list nearby SesameOS2 devices with the
information needed to fill in ``config.toml``: the BLE address (``ble_address``),
the product model, registration status, and signal strength.

``pysesameos2`` is imported lazily so the rest of the package works without BLE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class DiscoveredDevice:
    ble_address: str
    model: Optional[str]
    device_id: Optional[str]
    registered: bool
    rssi: int


# pysesameos2 product model -> loockit config model string.
_MODEL_NAMES = {
    "SS4": "SESAME4",
    "SS2": "SESAME4",  # SESAME2/3 share the lock profile; closest config alias
    "SesameBot1": "SESAMEBOT1",
}


async def scan(duration: int = 15) -> list[DiscoveredDevice]:
    """Active-scan for SESAME devices for ``duration`` seconds."""
    from pysesameos2.ble import CHBleManager

    found = await CHBleManager().scan(scan_duration=duration)
    out: list[DiscoveredDevice] = []
    for ble_address, device in found.items():
        model = device.productModel
        model_name = None
        if model is not None:
            model_name = _MODEL_NAMES.get(model.name, model.name)
        out.append(
            DiscoveredDevice(
                ble_address=str(ble_address),
                model=model_name,
                device_id=device.deviceId,
                registered=bool(device.getRegistered()),
                rssi=device.getRssi(),
            )
        )
    return out
