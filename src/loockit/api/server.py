"""grpc.aio server implementation, delegating to :class:`DeviceManager`."""

from __future__ import annotations

import logging
from typing import AsyncIterator

import grpc

from ..manager import DeviceManager
from ..models import (
    Action,
    ActionError,
    DeviceModel,
    DeviceNotFoundError,
    DeviceState,
    LockState,
    Source,
)
from . import sesame_pb2 as pb
from . import sesame_pb2_grpc as pb_grpc

logger = logging.getLogger(__name__)

_MODEL_TO_PB = {
    DeviceModel.SESAME4: pb.SESAME4,
    DeviceModel.SESAME_BOT1: pb.SESAME_BOT1,
}
_LOCK_TO_PB = {
    LockState.UNKNOWN: pb.LOCK_STATE_UNKNOWN,
    LockState.LOCKED: pb.LOCKED,
    LockState.UNLOCKED: pb.UNLOCKED,
    LockState.MOVING: pb.MOVING,
}
_SOURCE_TO_PB = {
    Source.BLE: pb.BLE,
    Source.CLOUD: pb.CLOUD,
    Source.SIM: pb.SIM,
}


def state_to_pb(state: DeviceState) -> pb.DeviceState:
    msg = pb.DeviceState(
        device_id=state.device_id,
        model=_MODEL_TO_PB.get(state.model, pb.MODEL_UNSPECIFIED),
        lock_state=_LOCK_TO_PB.get(state.lock_state, pb.LOCK_STATE_UNKNOWN),
        online=state.online,
        source=_SOURCE_TO_PB.get(state.source, pb.SOURCE_UNSPECIFIED),
        timestamp=state.timestamp,
    )
    if state.battery_percent is not None:
        msg.battery_percent = state.battery_percent
        msg.has_battery_percent = True
    if state.battery_voltage is not None:
        msg.battery_voltage = state.battery_voltage
        msg.has_battery_voltage = True
    if state.position is not None:
        msg.position = state.position
        msg.has_position = True
    if state.motor_status is not None:
        msg.motor_status = state.motor_status
        msg.has_motor_status = True
    return msg


class SesameServicer(pb_grpc.SesameServiceServicer):
    def __init__(self, manager: DeviceManager, history=None) -> None:
        self._manager = manager
        self._history = history

    async def ListDevices(self, request, context) -> pb.DeviceList:
        return pb.DeviceList(
            devices=[state_to_pb(s) for s in self._manager.all_states()]
        )

    async def GetStatus(self, request: pb.DeviceRef, context) -> pb.DeviceState:
        try:
            return state_to_pb(self._manager.get_state(request.device_id))
        except DeviceNotFoundError:
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"unknown device: {request.device_id}"
            )

    async def _command(self, request: pb.CommandRequest, context, action: Action):
        history_tag = request.history_tag or "loockit"
        try:
            state = await self._manager.execute(
                request.device_id, action, history_tag
            )
        except DeviceNotFoundError:
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"unknown device: {request.device_id}"
            )
        except ActionError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except Exception as exc:  # transport/connection failure
            logger.exception("command %s on %s failed", action, request.device_id)
            await context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        return pb.CommandResult(
            ok=True, message="ok", state=state_to_pb(state)
        )

    async def Lock(self, request, context):
        return await self._command(request, context, Action.LOCK)

    async def Unlock(self, request, context):
        return await self._command(request, context, Action.UNLOCK)

    async def Toggle(self, request, context):
        return await self._command(request, context, Action.TOGGLE)

    async def Click(self, request, context):
        return await self._command(request, context, Action.CLICK)

    async def StreamStatus(
        self, request: pb.StreamRequest, context
    ) -> AsyncIterator[pb.DeviceState]:
        wanted = request.device_id or None
        if wanted is not None and wanted not in self._manager.device_ids():
            await context.abort(
                grpc.StatusCode.NOT_FOUND, f"unknown device: {wanted}"
            )
        async for state in self._manager.subscribe():
            if wanted is None or state.device_id == wanted:
                yield state_to_pb(state)

    async def GetHistory(self, request: pb.HistoryRequest, context) -> pb.HistoryList:
        if self._history is None:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION, "history is not enabled"
            )
        entries = await self._history.query(
            device_id=request.device_id or None,
            kind=request.kind or None,
            limit=request.limit or 100,
        )
        return pb.HistoryList(
            entries=[
                pb.HistoryEntry(
                    id=e.id,
                    kind=e.kind,
                    device_id=e.device_id,
                    timestamp=e.timestamp,
                    lock_state=e.lock_state or "",
                    battery_percent=e.battery_percent or 0,
                    online=bool(e.online),
                    source=e.source or "",
                    action=e.action or "",
                    ok=bool(e.ok),
                    error=e.error or "",
                )
                for e in entries
            ]
        )


async def serve(
    manager: DeviceManager, host: str, port: int, history=None
) -> grpc.aio.Server:
    """Create, start, and return a running grpc.aio server."""
    server = grpc.aio.server()
    pb_grpc.add_SesameServiceServicer_to_server(
        SesameServicer(manager, history), server
    )
    server.add_insecure_port(f"{host}:{port}")
    await server.start()
    logger.info("gRPC server listening on %s:%s", host, port)
    return server
