"""Application wiring.

Builds the manager and starts the enabled interfaces/bridges:
gRPC (always), history persistence, REST/WebSocket, Matter, and MQTT — each
optional and independent, so a failure or missing dependency in one never takes
down the core (BLE + gRPC).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from .api.server import serve
from .config import AppConfig
from .manager import DeviceManager

logger = logging.getLogger(__name__)


class Application:
    """Owns the manager and the lifecycle of all interfaces and bridges."""

    def __init__(
        self,
        config: AppConfig,
        *,
        simulate: bool = False,
        enable_matter: bool | None = None,
    ) -> None:
        self.config = config
        self.simulate = simulate
        # CLI flag overrides config; otherwise fall back to config.matter.enabled.
        self.enable_matter = (
            config.matter.enabled if enable_matter is None else enable_matter
        )
        self.manager = DeviceManager(config, simulate=simulate)
        self._server = None  # gRPC
        self._matter = None
        self._mqtt = None
        self._history_store = None
        self._history_recorder = None
        self._rest_server = None
        self._rest_task = None
        self._active = False
        self._leader = None
        self._leader_task = None

    async def start(self) -> None:
        # History first so it captures startup/replayed states and commands.
        if self.config.history.enabled:
            await self._start_history()
        self._server = await serve(
            self.manager,
            self.config.grpc.host,
            self.config.grpc.port,
            history=self._history_store,
        )
        if self.config.rest.enabled:
            await self._start_rest()
        if self.enable_matter:
            await self._start_matter()
        if self.config.mqtt.enabled:
            await self._start_mqtt()
        if os.environ.get("LOOCKIT_LEADER_ELECTION", "").lower() == "true":
            from .leader import KubernetesLeaseElector

            self._leader = KubernetesLeaseElector(
                os.environ.get("POD_NAME", os.uname().nodename),
                self._activate,
                self._deactivate,
                self._ble_healthy,
            )
            self._leader_task = asyncio.create_task(self._leader.run())
        else:
            await self._activate()

    async def _activate(self) -> None:
        if not self._active:
            await self.manager.start()
            self._active = True

    async def _deactivate(self) -> None:
        if self._active:
            self._active = False
            await self.manager.stop()

    def _ble_healthy(self) -> bool:
        states = self.manager.all_states()
        return bool(states) and all(state.online for state in states)

    async def _start_history(self) -> None:
        from .history import HistoryRecorder, HistoryStore

        self._history_store = HistoryStore(self.config.history.path)
        self._history_recorder = HistoryRecorder(self.manager, self._history_store)
        await self._history_recorder.start()
        logger.info("history persistence enabled at %s", self.config.history.path)

    async def _start_rest(self) -> None:
        try:
            from .api.rest import create_app, serve_rest

            app = create_app(
                self.manager,
                self._history_store,
                bearer_token=self.config.rest.bearer_token,
                rate_limit_requests=self.config.rest.rate_limit_requests,
                rate_limit_window_seconds=self.config.rest.rate_limit_window_seconds,
                is_active=lambda: self._active,
            )
            self._rest_server, self._rest_task = await serve_rest(
                app, self.config.rest.host, self.config.rest.port
            )
        except Exception as exc:
            logger.error("REST server disabled: %s", exc)
            self._rest_server = self._rest_task = None

    async def _start_matter(self) -> None:
        from .bridge.matter import MatterBridge

        self._matter = MatterBridge(self.manager)
        try:
            await self._matter.start()
        except Exception as exc:
            # Matter is best-effort: a missing dep, absent avahi, or a socket
            # bind failure must not take down the core (BLE + gRPC).
            logger.error("Matter bridge disabled: %s", exc)
            self._matter = None

    async def _start_mqtt(self) -> None:
        try:
            from .bridge.mqtt import MqttBridge

            self._mqtt = MqttBridge(self.manager, self.config.mqtt)
            await self._mqtt.start()
            logger.info(
                "MQTT bridge started (broker %s:%s)",
                self.config.mqtt.host,
                self.config.mqtt.port,
            )
        except Exception as exc:
            logger.error("MQTT bridge disabled: %s", exc)
            self._mqtt = None

    async def stop(self) -> None:
        if self._leader is not None:
            await self._leader.stop()
        if self._leader_task is not None:
            self._leader_task.cancel()
        for bridge in (self._matter, self._mqtt):
            if bridge is not None:
                await bridge.stop()
        if self._rest_server is not None:
            self._rest_server.should_exit = True
            if self._rest_task is not None:
                try:
                    await asyncio.wait_for(self._rest_task, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        if self._server is not None:
            await self._server.stop(grace=2.0)
        if self._history_recorder is not None:
            await self._history_recorder.stop()
        if self._history_store is not None:
            self._history_store.close()
        await self._deactivate()

    async def run_forever(self) -> None:
        """Start everything and block until SIGINT/SIGTERM."""
        await self.start()
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:  # pragma: no cover - non-unix
                pass
        logger.info(
            "loockit running (simulate=%s, matter=%s, rest=%s, mqtt=%s, "
            "history=%s). Ctrl-C to stop.",
            self.simulate,
            self.enable_matter,
            self.config.rest.enabled,
            self.config.mqtt.enabled,
            self.config.history.enabled,
        )
        try:
            await stop_event.wait()
        finally:
            await self.stop()
            logger.info("loockit stopped")
