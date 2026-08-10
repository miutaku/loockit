import asyncio

import pytest

from loockit.leader import KubernetesLeaseElector


@pytest.mark.asyncio
async def test_lease_is_renewed_while_ble_activation_is_running():
    activation_started = asyncio.Event()
    release_activation = asyncio.Event()
    renewals = 0

    async def activate():
        activation_started.set()
        await release_activation.wait()

    async def deactivate():
        pass

    elector = KubernetesLeaseElector.__new__(KubernetesLeaseElector)
    elector.identity = "pod-a"
    elector.on_acquired = activate
    elector.on_lost = deactivate
    elector.retry_period = 0.01
    elector.is_leader = False
    elector._stopping = False
    elector._activation_task = None

    def renew():
        nonlocal renewals
        renewals += 1
        return True

    elector._try_acquire_or_renew = renew
    elector._set_active_label = lambda active: None

    run_task = asyncio.create_task(elector.run())
    await asyncio.wait_for(activation_started.wait(), timeout=1)
    while renewals < 3:
        await asyncio.sleep(0.01)

    assert not release_activation.is_set()
    assert renewals >= 3

    release_activation.set()
    await asyncio.sleep(0)
    await elector.stop()
    await asyncio.wait_for(run_task, timeout=1)


@pytest.mark.asyncio
async def test_stale_active_label_is_cleared_before_election():
    labels = []
    attempted_election = asyncio.Event()

    async def activate():
        pass

    async def deactivate():
        pass

    elector = KubernetesLeaseElector.__new__(KubernetesLeaseElector)
    elector.identity = "pod-a"
    elector.on_acquired = activate
    elector.on_lost = deactivate
    elector.retry_period = 0.01
    elector.is_leader = False
    elector._stopping = False
    elector._activation_task = None

    def elect():
        attempted_election.set()
        return False

    elector._try_acquire_or_renew = elect
    elector._set_active_label = labels.append

    run_task = asyncio.create_task(elector.run())
    await asyncio.wait_for(attempted_election.wait(), timeout=1)
    elector._stopping = True
    await asyncio.wait_for(run_task, timeout=1)

    assert labels[0] is False


@pytest.mark.asyncio
async def test_leadership_loss_cancels_activation_before_disconnect():
    activation_started = asyncio.Event()
    activation_cancelled = asyncio.Event()
    disconnected = asyncio.Event()
    attempts = 0

    async def activate():
        activation_started.set()
        try:
            await asyncio.Future()
        finally:
            activation_cancelled.set()

    async def deactivate():
        assert activation_cancelled.is_set()
        disconnected.set()

    elector = KubernetesLeaseElector.__new__(KubernetesLeaseElector)
    elector.identity = "pod-a"
    elector.on_acquired = activate
    elector.on_lost = deactivate
    elector.retry_period = 0.01
    elector.is_leader = False
    elector._stopping = False
    elector._activation_task = None

    def acquire_then_lose():
        nonlocal attempts
        attempts += 1
        return attempts == 1

    elector._try_acquire_or_renew = acquire_then_lose
    elector._set_active_label = lambda active: None

    run_task = asyncio.create_task(elector.run())
    await asyncio.wait_for(activation_started.wait(), timeout=1)
    await asyncio.wait_for(disconnected.wait(), timeout=1)

    elector._stopping = True
    await asyncio.wait_for(run_task, timeout=1)
