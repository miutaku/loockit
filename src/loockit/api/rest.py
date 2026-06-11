"""REST + WebSocket API (FastAPI), mirroring the gRPC surface.

Reuses :class:`~loockit.manager.DeviceManager` exactly like the gRPC server, so
the two interfaces stay behaviourally identical. FastAPI/uvicorn are an optional
dependency (the ``rest`` extra); this module imports them lazily-friendly at the
top because it is only imported when REST is enabled.

Endpoints:
    GET  /healthz
    GET  /devices
    GET  /devices/{id}
    POST /devices/{id}/lock | /unlock | /toggle | /click
    GET  /history
    WS   /ws            (real-time state stream; ?device_id= to filter)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect

from ..history import HistoryStore
from ..manager import DeviceManager
from ..models import (
    Action,
    ActionError,
    DeviceNotFoundError,
    DeviceState,
)

logger = logging.getLogger(__name__)


def state_to_dict(state: DeviceState) -> dict:
    """JSON-serializable view of a DeviceState (omitting N/A optional fields)."""
    data = {
        "device_id": state.device_id,
        "model": state.model.value,
        "lock_state": state.lock_state.value,
        "online": state.online,
        "source": state.source.value,
        "timestamp": state.timestamp,
    }
    if state.battery_percent is not None:
        data["battery_percent"] = state.battery_percent
    if state.battery_voltage is not None:
        data["battery_voltage"] = state.battery_voltage
    if state.position is not None:
        data["position"] = state.position
    if state.motor_status is not None:
        data["motor_status"] = state.motor_status
    return data


_ACTIONS = {
    "lock": Action.LOCK,
    "unlock": Action.UNLOCK,
    "toggle": Action.TOGGLE,
    "click": Action.CLICK,
}


def create_app(
    manager: DeviceManager,
    history: Optional[HistoryStore] = None,
    *,
    manage_lifecycle: bool = False,
) -> FastAPI:
    """Build the FastAPI app.

    When ``manage_lifecycle`` is True, the app starts/stops the manager in its
    own lifespan (handy for tests and standalone use). In the full app the
    :class:`~loockit.app.Application` owns the manager, so the default is False.
    """
    lifespan = None
    if manage_lifecycle:

        @asynccontextmanager
        async def lifespan(_app: FastAPI):  # noqa: F811
            await manager.start()
            try:
                yield
            finally:
                await manager.stop()

    app = FastAPI(title="loockit", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "devices": manager.device_ids()}

    @app.get("/devices")
    async def list_devices() -> list[dict]:
        return [state_to_dict(s) for s in manager.all_states()]

    @app.get("/devices/{device_id}")
    async def get_device(device_id: str) -> dict:
        try:
            return state_to_dict(manager.get_state(device_id))
        except DeviceNotFoundError:
            raise HTTPException(status_code=404, detail=f"unknown device: {device_id}")

    @app.post("/devices/{device_id}/{action}")
    async def command(
        device_id: str,
        action: str,
        history_tag: str = Body("rest", embed=True),
    ) -> dict:
        if action not in _ACTIONS:
            raise HTTPException(status_code=404, detail=f"unknown action: {action}")
        try:
            state = await manager.execute(device_id, _ACTIONS[action], history_tag)
        except DeviceNotFoundError:
            raise HTTPException(status_code=404, detail=f"unknown device: {device_id}")
        except ActionError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except Exception as exc:  # transport failure
            raise HTTPException(status_code=503, detail=str(exc))
        return {"ok": True, "state": state_to_dict(state)}

    @app.get("/history")
    async def get_history(
        device_id: Optional[str] = Query(default=None),
        kind: Optional[str] = Query(default=None, pattern="^(state|command)$"),
        limit: int = Query(default=100, ge=1, le=10000),
    ) -> list[dict]:
        if history is None:
            raise HTTPException(status_code=404, detail="history is not enabled")
        entries = await history.query(device_id=device_id, kind=kind, limit=limit)
        return [e.to_dict() for e in entries]

    @app.websocket("/ws")
    async def ws_stream(websocket: WebSocket) -> None:
        await websocket.accept()
        wanted = websocket.query_params.get("device_id") or None
        if wanted is not None and wanted not in manager.device_ids():
            await websocket.close(code=4404, reason=f"unknown device: {wanted}")
            return
        try:
            async for state in manager.subscribe():
                if wanted is None or state.device_id == wanted:
                    await websocket.send_json(state_to_dict(state))
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise

    return app


async def serve_rest(app: FastAPI, host: str, port: int):
    """Start a uvicorn server for ``app`` and return (server, task)."""
    import uvicorn

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    logger.info("REST/WebSocket server listening on %s:%s", host, port)
    return server, task
