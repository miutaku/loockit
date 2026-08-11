"""Kubernetes Lease election used to fence the single BLE connection."""
from __future__ import annotations

import asyncio, json, logging, os, ssl, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class KubernetesLeaseElector:
    def __init__(self, identity, on_acquired, on_lost, *, lease_name="loockit-ble-leader", duration=15, retry_period=5.0):
        self.identity, self.on_acquired, self.on_lost = identity, on_acquired, on_lost
        self.lease_name, self.duration, self.retry_period = lease_name, duration, retry_period
        self.is_leader = self._stopping = False
        self._activation_task = None
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        root = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        namespace = (root / "namespace").read_text().strip()
        self.url = f"https://{host}:{port}/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases/{lease_name}"
        self.pod_url = f"https://{host}:{port}/api/v1/namespaces/{namespace}/pods/{identity}"
        # Projected ServiceAccount tokens are rotated by kubelet.  Keep the
        # path and read it for every request instead of caching an eventually
        # expired token for the lifetime of the process.
        self.token_path = root / "token"
        self.context = ssl.create_default_context(cafile=str(root / "ca.crt"))

    @staticmethod
    def _now(): return datetime.now(timezone.utc)
    @staticmethod
    def _stamp(value): return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _request(self, method, url, body=None):
        token = self.token_path.read_text().strip()
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, context=self.context, timeout=5) as response:
            return json.load(response)

    def _set_active_label(self, active):
        token = self.token_path.read_text().strip()
        body = {"metadata":{"labels":{"loockit.miutaku/active":"true" if active else None}}}
        data = json.dumps(body).encode()
        request = urllib.request.Request(self.pod_url, data=data, method="PATCH",
            headers={"Authorization": f"Bearer {token}", "Content-Type":"application/merge-patch+json"})
        with urllib.request.urlopen(request, context=self.context, timeout=5): pass

    def _try_acquire_or_renew(self):
        now = self._now()
        try:
            lease = self._request("GET", self.url)
        except urllib.error.HTTPError as exc:
            if exc.code != 404: raise
            body = {"apiVersion":"coordination.k8s.io/v1","kind":"Lease","metadata":{"name":self.lease_name},
                    "spec":{"holderIdentity":self.identity,"leaseDurationSeconds":self.duration,
                            "acquireTime":self._stamp(now),"renewTime":self._stamp(now)}}
            try:
                self._request("POST", self.url.rsplit("/", 1)[0], body); return True
            except urllib.error.HTTPError as create_error:
                if create_error.code == 409: return False
                raise
        spec = lease.get("spec", {}); holder = spec.get("holderIdentity")
        renew_raw = spec.get("renewTime") or spec.get("acquireTime"); expired = True
        if renew_raw:
            renewed = datetime.fromisoformat(renew_raw.replace("Z", "+00:00"))
            expired = (now - renewed).total_seconds() >= int(spec.get("leaseDurationSeconds") or self.duration)
        if holder != self.identity and not expired: return False
        spec.update(holderIdentity=self.identity, leaseDurationSeconds=self.duration, renewTime=self._stamp(now))
        if holder != self.identity:
            spec.update(acquireTime=self._stamp(now), leaseTransitions=int(spec.get("leaseTransitions") or 0) + 1)
        lease["spec"] = spec
        try:
            self._request("PUT", self.url, lease); return True
        except urllib.error.HTTPError as exc:
            if exc.code == 409: return False
            raise

    async def run(self):
        # Pod labels survive container restarts.  Clear a label left by the
        # previous process before participating in election, otherwise the
        # Service can route requests to a standby process until leadership
        # changes again.
        try:
            await asyncio.to_thread(self._set_active_label, False)
        except Exception:
            logger.exception("failed to clear stale BLE leader label")
        while not self._stopping:
            try: leader = await asyncio.to_thread(self._try_acquire_or_renew)
            except Exception:
                logger.exception("Kubernetes Lease operation failed"); leader = False
            if leader and not self.is_leader:
                # Do not await BLE discovery/login here.  A scan can take at
                # least as long as the Lease duration; blocking this loop would
                # stop renewals and allow the peer to acquire the same Lease.
                self.is_leader = True
                logger.info("acquired BLE leadership as %s", self.identity)
                self._activation_task = asyncio.create_task(self._activate())
            elif not leader and self.is_leader:
                await self._lose_leadership()
            await asyncio.sleep(self.retry_period)

    async def _activate(self):
        try:
            await self.on_acquired()
            if self.is_leader and not self._stopping:
                await asyncio.to_thread(self._set_active_label, True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("failed to activate BLE leader")

    async def _lose_leadership(self):
        self.is_leader = False
        logger.warning("lost BLE leadership as %s", self.identity)
        if self._activation_task is not None and not self._activation_task.done():
            self._activation_task.cancel()
            try:
                await self._activation_task
            except asyncio.CancelledError:
                pass
        self._activation_task = None
        try:
            await asyncio.to_thread(self._set_active_label, False)
        except Exception:
            # An API outage must not kill the election task.  BLE fencing is
            # local and still has to happen; the next loop can then recover.
            logger.exception("failed to clear BLE leader label")
        # Disconnect and fence BLE before another local activation can run.
        await self.on_lost()

    async def stop(self):
        self._stopping = True
        if self.is_leader:
            await self._lose_leadership()
