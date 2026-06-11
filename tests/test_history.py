import asyncio

from loockit.history import HistoryRecorder, HistoryStore
from loockit.manager import DeviceManager
from loockit.models import Action, DeviceModel, DeviceState, LockState, Source


async def test_store_record_and_query(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite3"))
    try:
        s = DeviceState(
            device_id="front-door",
            model=DeviceModel.SESAME4,
            lock_state=LockState.LOCKED,
            battery_percent=88,
            source=Source.SIM,
            online=True,
        )
        await store.record_state(s)
        await store.record_command("front-door", Action.UNLOCK, True, None)
        await store.record_command("desk-bot", Action.LOCK, False, "bad action")

        all_entries = await store.query()
        assert len(all_entries) == 3

        states = await store.query(kind="state")
        assert len(states) == 1
        assert states[0].lock_state == "LOCKED"
        assert states[0].battery_percent == 88

        cmds = await store.query(kind="command", device_id="front-door")
        assert len(cmds) == 1
        assert cmds[0].action == "unlock" and cmds[0].ok is True

        failed = await store.query(kind="command", device_id="desk-bot")
        assert failed[0].ok is False and failed[0].error == "bad action"
    finally:
        store.close()


async def test_query_limit_and_order(tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite3"))
    try:
        for i in range(5):
            await store.record_command("d", Action.LOCK, True, None)
        entries = await store.query(limit=3)
        assert len(entries) == 3
        # newest first
        assert entries[0].id > entries[-1].id
    finally:
        store.close()


async def test_recorder_persists_states_and_commands(config, tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite3"))
    mgr = DeviceManager(config, simulate=True)
    await mgr.start()
    recorder = HistoryRecorder(mgr, store)
    await recorder.start()
    try:
        await mgr.execute("front-door", Action.LOCK)
        await asyncio.sleep(0.1)  # let async writes flush

        cmds = await store.query(kind="command")
        assert any(c.action == "lock" and c.ok for c in cmds)

        states = await store.query(kind="state", device_id="front-door")
        assert any(s.lock_state == "LOCKED" for s in states)
    finally:
        await recorder.stop()
        await mgr.stop()
        store.close()


async def test_recorder_records_failed_command(config, tmp_path):
    store = HistoryStore(str(tmp_path / "h.sqlite3"))
    mgr = DeviceManager(config, simulate=True)
    await mgr.start()
    recorder = HistoryRecorder(mgr, store)
    await recorder.start()
    try:
        try:
            await mgr.execute("desk-bot", Action.LOCK)  # invalid -> ActionError
        except Exception:
            pass
        await asyncio.sleep(0.1)
        cmds = await store.query(kind="command", device_id="desk-bot")
        assert cmds and cmds[0].ok is False
    finally:
        await recorder.stop()
        await mgr.stop()
        store.close()
