import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from loockit.api.rest import create_app  # noqa: E402
from loockit.history import HistoryStore  # noqa: E402
from loockit.manager import DeviceManager  # noqa: E402


@pytest.fixture
def client(config, tmp_path):
    # Synchronous fixture: TestClient owns the event loop and drives the app
    # lifespan, which starts/stops the manager — keeping everything one loop.
    mgr = DeviceManager(config, simulate=True)
    store = HistoryStore(str(tmp_path / "h.sqlite3"))
    # Record commands so /history has content.
    mgr.add_command_listener(
        lambda d, a, ok, err: store._insert_command(d, a, ok, err)
    )
    app = create_app(mgr, store, manage_lifecycle=True)
    with TestClient(app) as c:
        yield c
    store.close()


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_list_and_get_devices(client):
    r = client.get("/devices")
    assert r.status_code == 200
    ids = {d["device_id"] for d in r.json()}
    assert ids == {"front-door", "desk-bot"}

    r = client.get("/devices/front-door")
    assert r.json()["model"] == "SESAME4"

    r = client.get("/devices/nope")
    assert r.status_code == 404


def test_lock_unlock_click(client):
    r = client.post("/devices/front-door/lock")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["state"]["lock_state"] == "LOCKED"

    r = client.post("/devices/front-door/unlock")
    assert r.json()["state"]["lock_state"] == "UNLOCKED"

    r = client.post("/devices/desk-bot/click")
    assert r.json()["ok"]


def test_model_mismatch_409(client):
    r = client.post("/devices/desk-bot/lock")
    assert r.status_code == 409


def test_unknown_action_404(client):
    r = client.post("/devices/front-door/explode")
    assert r.status_code == 404


def test_command_bearer_auth(config):
    mgr = DeviceManager(config, simulate=True)
    app = create_app(
        mgr,
        manage_lifecycle=True,
        bearer_token="correct-token",
        rate_limit_requests=0,
    )
    with TestClient(app) as protected:
        assert protected.post("/devices/desk-bot/click").status_code == 401
        assert (
            protected.post(
                "/devices/desk-bot/click",
                headers={"Authorization": "Bearer wrong-token"},
            ).status_code
            == 401
        )
        assert (
            protected.post(
                "/devices/desk-bot/click",
                headers={"Authorization": "Bearer correct-token"},
            ).status_code
            == 200
        )


def test_command_rate_limit(config):
    mgr = DeviceManager(config, simulate=True)
    app = create_app(
        mgr,
        manage_lifecycle=True,
        bearer_token="token",
        rate_limit_requests=1,
        rate_limit_window_seconds=60,
    )
    headers = {"Authorization": "Bearer token"}
    with TestClient(app) as protected:
        assert protected.post("/devices/desk-bot/click", headers=headers).status_code == 200
        response = protected.post("/devices/desk-bot/click", headers=headers)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"


def test_history_endpoint(client):
    client.post("/devices/front-door/lock")
    r = client.get("/history", params={"kind": "command"})
    assert r.status_code == 200
    entries = r.json()
    assert any(e["action"] == "lock" for e in entries)


def test_websocket_stream(client):
    with client.websocket_connect("/ws?device_id=front-door") as ws:
        snapshot = ws.receive_json()  # replayed current state
        assert snapshot["device_id"] == "front-door"
        client.post("/devices/front-door/lock")
        # read until LOCKED appears
        for _ in range(10):
            msg = ws.receive_json()
            if msg["lock_state"] == "LOCKED":
                break
        else:
            pytest.fail("did not observe LOCKED on websocket")
