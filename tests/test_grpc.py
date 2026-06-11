import grpc
import pytest

from loockit.api import sesame_pb2 as pb
from loockit.api import sesame_pb2_grpc as pb_grpc
from loockit.api.server import SesameServicer, state_to_pb
from loockit.history import HistoryStore
from loockit.manager import DeviceManager
from loockit.models import DeviceModel, DeviceState, LockState, Source


def test_state_to_pb_optional_presence():
    s = DeviceState(
        device_id="d",
        model=DeviceModel.SESAME4,
        lock_state=LockState.LOCKED,
        battery_percent=80,
        battery_voltage=5.9,
        position=10,
        source=Source.SIM,
        online=True,
    )
    msg = state_to_pb(s)
    assert msg.model == pb.SESAME4
    assert msg.lock_state == pb.LOCKED
    assert msg.has_battery_percent and msg.battery_percent == 80
    assert msg.has_position and msg.position == 10
    assert not msg.has_motor_status  # not set for a lock


@pytest.fixture
async def grpc_channel(config):
    mgr = DeviceManager(config, simulate=True)
    await mgr.start()
    server = grpc.aio.server()
    pb_grpc.add_SesameServiceServicer_to_server(SesameServicer(mgr), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield channel
    finally:
        await channel.close()
        await server.stop(None)
        await mgr.stop()


async def test_list_devices(grpc_channel):
    stub = pb_grpc.SesameServiceStub(grpc_channel)
    resp = await stub.ListDevices(pb.Empty())
    ids = {d.device_id for d in resp.devices}
    assert ids == {"front-door", "desk-bot"}


async def test_lock_unlock_rpc(grpc_channel):
    stub = pb_grpc.SesameServiceStub(grpc_channel)
    res = await stub.Lock(pb.CommandRequest(device_id="front-door"))
    assert res.ok
    assert res.state.lock_state == pb.LOCKED
    res = await stub.Unlock(pb.CommandRequest(device_id="front-door"))
    assert res.state.lock_state == pb.UNLOCKED


async def test_click_rpc(grpc_channel):
    stub = pb_grpc.SesameServiceStub(grpc_channel)
    res = await stub.Click(pb.CommandRequest(device_id="desk-bot"))
    assert res.ok


async def test_model_mismatch_failed_precondition(grpc_channel):
    stub = pb_grpc.SesameServiceStub(grpc_channel)
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await stub.Lock(pb.CommandRequest(device_id="desk-bot"))
    assert ei.value.code() == grpc.StatusCode.FAILED_PRECONDITION


async def test_unknown_device_not_found(grpc_channel):
    stub = pb_grpc.SesameServiceStub(grpc_channel)
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await stub.GetStatus(pb.DeviceRef(device_id="nope"))
    assert ei.value.code() == grpc.StatusCode.NOT_FOUND


async def test_stream_status(grpc_channel):
    stub = pb_grpc.SesameServiceStub(grpc_channel)
    stream = stub.StreamStatus(pb.StreamRequest(device_id="front-door"))
    # First message is the replayed snapshot.
    snapshot = await stream.read()
    assert snapshot.device_id == "front-door"

    res = await stub.Lock(pb.CommandRequest(device_id="front-door"))
    assert res.ok
    # Drain until we observe the LOCKED state pushed to the stream.
    for _ in range(10):
        msg = await stream.read()
        if msg.lock_state == pb.LOCKED:
            break
    else:
        pytest.fail("did not receive LOCKED state on stream")
    stream.cancel()


async def test_get_history_disabled(grpc_channel):
    stub = pb_grpc.SesameServiceStub(grpc_channel)
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await stub.GetHistory(pb.HistoryRequest())
    assert ei.value.code() == grpc.StatusCode.FAILED_PRECONDITION


@pytest.fixture
async def grpc_channel_with_history(config, tmp_path):
    mgr = DeviceManager(config, simulate=True)
    await mgr.start()
    store = HistoryStore(str(tmp_path / "h.sqlite3"))
    mgr.add_command_listener(
        lambda d, a, ok, err: store._insert_command(d, a, ok, err)
    )
    server = grpc.aio.server()
    pb_grpc.add_SesameServiceServicer_to_server(
        SesameServicer(mgr, history=store), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield channel
    finally:
        await channel.close()
        await server.stop(None)
        await mgr.stop()
        store.close()


async def test_get_history_returns_commands(grpc_channel_with_history):
    stub = pb_grpc.SesameServiceStub(grpc_channel_with_history)
    await stub.Lock(pb.CommandRequest(device_id="front-door"))
    resp = await stub.GetHistory(pb.HistoryRequest(kind="command"))
    assert any(e.action == "lock" and e.ok for e in resp.entries)
