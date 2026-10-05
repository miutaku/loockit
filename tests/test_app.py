from loockit.app import Application


def test_ble_health_allows_one_online_device(config):
    app = Application(config, simulate=True)
    assert app._ble_healthy() is False

    first = next(iter(app.manager._states))
    app.manager._states[first] = app.manager._states[first].evolve(online=True)

    assert app._ble_healthy() is True

async def test_cancelled_activation_cleans_partial_connections(config):
    import asyncio
    from unittest.mock import AsyncMock
    import pytest
    app = Application(config, simulate=True)
    app.manager.start = AsyncMock(side_effect=asyncio.CancelledError)
    app.manager.stop = AsyncMock()
    with pytest.raises(asyncio.CancelledError):
        await app._activate()
    app.manager.stop.assert_awaited_once()
    assert not app._active
