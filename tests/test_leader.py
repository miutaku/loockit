import asyncio

import pytest

from loockit.leader import KubernetesLeaseElector


def test_kubernetes_token_is_reloaded_for_each_request(tmp_path, monkeypatch):
    token_path = tmp_path / "token"
    token_path.write_text("first")
    authorizations = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def urlopen(request, **kwargs):
        authorizations.append(request.get_header("Authorization"))
        response = Response()
        response.read = lambda: b"{}"
        return response

    elector = KubernetesLeaseElector.__new__(KubernetesLeaseElector)
    elector.token_path = token_path
    elector.context = None
    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    elector._request("GET", "https://kubernetes.invalid/lease")
    token_path.write_text("second")
    elector._request("GET", "https://kubernetes.invalid/lease")

    assert authorizations == ["Bearer first", "Bearer second"]


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


@pytest.mark.asyncio
async def test_label_patch_failure_does_not_prevent_local_fencing():
    disconnected = asyncio.Event()

    async def deactivate():
        disconnected.set()

    elector = KubernetesLeaseElector.__new__(KubernetesLeaseElector)
    elector.identity = "pod-a"
    elector.on_lost = deactivate
    elector.is_leader = True
    elector._activation_task = None
    elector._set_active_label = lambda active: (_ for _ in ()).throw(
        RuntimeError("API unavailable")
    )

    await elector._lose_leadership()

    assert disconnected.is_set()


@pytest.mark.asyncio
async def test_unhealthy_ble_is_not_added_to_service_and_requests_yield():
    labels = []

    async def activate():
        pass

    elector = KubernetesLeaseElector.__new__(KubernetesLeaseElector)
    elector.identity = "pod-a"
    elector.on_acquired = activate
    elector.is_healthy = lambda: False
    elector.is_leader = True
    elector._stopping = False
    elector.retry_period = 0.001
    elector.activation_timeout = 0.005
    elector._yield_requested = False
    elector._serving = False
    elector._unhealthy_since = None
    elector._set_active_label = labels.append

    await elector._activate()

    assert labels == []
    assert elector._yield_requested is True
    assert elector._serving is False


@pytest.mark.asyncio
async def test_ambiguous_lease_failure_keeps_leadership_before_lease_deadline():
    disconnected = asyncio.Event()

    async def deactivate():
        disconnected.set()

    elector = KubernetesLeaseElector.__new__(KubernetesLeaseElector)
    elector.identity = "pod-a"
    elector.on_lost = deactivate
    elector.retry_period = 0.01
    elector.duration = 1
    elector.is_leader = True
    elector._stopping = False
    elector._activation_task = None
    elector._yield_requested = False
    elector._cooldown_until = 0.0
    elector._last_confirmed_lease = asyncio.get_running_loop().time()
    elector._set_active_label = lambda active: None

    def raise_timeout():
        raise TimeoutError("timed out")

    elector._try_acquire_or_renew = raise_timeout

    run_task = asyncio.create_task(elector.run())
    await asyncio.sleep(0.05)
    elector._stopping = True
    await asyncio.wait_for(run_task, timeout=1)

    assert elector.is_leader is True
    assert not disconnected.is_set()


@pytest.mark.asyncio
async def test_ambiguous_lease_failure_fences_ble_after_lease_deadline():
    disconnected = asyncio.Event()

    async def deactivate():
        disconnected.set()

    elector = KubernetesLeaseElector.__new__(KubernetesLeaseElector)
    elector.identity = "pod-a"
    elector.on_lost = deactivate
    elector.retry_period = 0.001
    elector.duration = 0.01
    elector.is_leader = True
    elector._stopping = False
    elector._activation_task = None
    elector._yield_requested = False
    elector._cooldown_until = 0.0
    elector._last_confirmed_lease = asyncio.get_running_loop().time()
    elector._set_active_label = lambda active: None

    def raise_timeout():
        raise TimeoutError("timed out")

    elector._try_acquire_or_renew = raise_timeout

    run_task = asyncio.create_task(elector.run())
    await asyncio.wait_for(disconnected.wait(), timeout=1)
    elector._stopping = True
    await asyncio.wait_for(run_task, timeout=1)

    assert elector.is_leader is False


def test_set_active_label_retries_transient_failures(tmp_path, monkeypatch):
    token_path = tmp_path / "token"
    token_path.write_text("secret")
    attempts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def urlopen(request, **kwargs):
        attempts.append(request)
        if len(attempts) < 3:
            raise TimeoutError("timed out")
        return Response()

    elector = KubernetesLeaseElector.__new__(KubernetesLeaseElector)
    elector.identity = "pod-a"
    elector.pod_url = "https://kubernetes.invalid/api/v1/namespaces/ns/pods/pod-a"
    elector.token_path = token_path
    elector.context = None
    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    elector._set_active_label(True, attempts=3, backoff=0.001)

    assert len(attempts) == 3


def test_set_active_label_raises_after_exhausting_retries(tmp_path, monkeypatch):
    token_path = tmp_path / "token"
    token_path.write_text("secret")

    def urlopen(request, **kwargs):
        raise TimeoutError("timed out")

    elector = KubernetesLeaseElector.__new__(KubernetesLeaseElector)
    elector.identity = "pod-a"
    elector.pod_url = "https://kubernetes.invalid/api/v1/namespaces/ns/pods/pod-a"
    elector.token_path = token_path
    elector.context = None
    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    with pytest.raises(TimeoutError):
        elector._set_active_label(True, attempts=2, backoff=0.001)


@pytest.mark.asyncio
async def test_ble_becomes_service_endpoint_only_after_online():
    labels = []
    healthy = False

    async def activate():
        nonlocal healthy
        healthy = True

    elector = KubernetesLeaseElector.__new__(KubernetesLeaseElector)
    elector.identity = "pod-a"
    elector.on_acquired = activate
    elector.is_healthy = lambda: healthy
    elector.is_leader = True
    elector._stopping = False
    elector.retry_period = 0.001
    elector.activation_timeout = 1
    elector._yield_requested = False
    elector._serving = False
    elector._unhealthy_since = None
    elector._set_active_label = labels.append

    await elector._activate()

    assert labels == [True]
    assert elector._serving is True
