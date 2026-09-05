from loockit.app import Application


def test_ble_health_allows_one_online_device(config):
    app = Application(config, simulate=True)
    assert app._ble_healthy() is False

    first = next(iter(app.manager._states))
    app.manager._states[first] = app.manager._states[first].evolve(online=True)

    assert app._ble_healthy() is True
