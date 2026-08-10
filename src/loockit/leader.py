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
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        root = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        namespace = (root / "namespace").read_text().strip()
        self.url = f"https://{host}:{port}/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases/{lease_name}"
        self.pod_url = f"https://{host}:{port}/api/v1/namespaces/{namespace}/pods/{identity}"
        self.token = (root / "token").read_text().strip()
        self.context = ssl.create_default_context(cafile=str(root / "ca.crt"))

    @staticmethod
    def _now(): return datetime.now(timezone.utc)
    @staticmethod
    def _stamp(value): return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _request(self, method, url, body=None):
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, context=self.context, timeout=5) as response:
            return json.load(response)

    def _set_active_label(self, active):
        body = {"metadata":{"labels":{"loockit.miutaku/active":"true" if active else None}}}
        data = json.dumps(body).encode()
        request = urllib.request.Request(self.pod_url, data=data, method="PATCH",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type":"application/merge-patch+json"})
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
        while not self._stopping:
            try: leader = await asyncio.to_thread(self._try_acquire_or_renew)
            except Exception:
                logger.exception("Kubernetes Lease operation failed"); leader = False
            if leader and not self.is_leader:
                try:
                    await self.on_acquired(); await asyncio.to_thread(self._set_active_label, True)
                    self.is_leader = True; logger.info("acquired BLE leadership as %s", self.identity)
                except Exception: logger.exception("failed to activate BLE leader")
            elif not leader and self.is_leader:
                self.is_leader = False; logger.warning("lost BLE leadership as %s", self.identity)
                try: await asyncio.to_thread(self._set_active_label, False)
                finally: await self.on_lost()
            await asyncio.sleep(self.retry_period)

    async def stop(self):
        self._stopping = True
        if self.is_leader: self.is_leader = False; await self.on_lost()
