"""QQ outbound WebSocket receiver; order work stays in the durable worker queue."""
import asyncio
import hashlib
import json
import logging
import time

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .domain import now
from .events import message_key
from .qq import SendError
from .storage import dumps


log = logging.getLogger(__name__)
wire_log = logging.getLogger("qqbot.gateway.wire")
# Identify/Resume frames contain an access token; don't log wire-level frames.
wire_log.setLevel(logging.WARNING)


class Reconnect(Exception):
    def __init__(self, reason, delay=0, clear=False):
        super().__init__(reason)
        self.delay, self.clear = delay, clear


class FatalGateway(Exception):
    pass


class Gateway:
    def __init__(self, store, qq, settings, connector=connect, sleep=asyncio.sleep):
        self.store, self.qq, self.settings = store, qq, settings
        self.connector, self.sleep = connector, sleep
        scope = f"{settings.qq_app_id}|{settings.qq_api_base}|{settings.qq_gateway_intents}"
        self.checkpoint_key = "gateway_session:" + hashlib.sha256(scope.encode()).hexdigest()[:24]
        self.session_id, self.sequence = "", None
        self.awaiting_ack = False
        self.ready_since = None
        self.status = {"enabled": settings.qq_gateway_enabled and settings.qq_transport == "websocket",
                       "state": "disabled", "connected": False, "ready": False, "retries": 0,
                       "last_error": None, "last_event_at": None, "last_heartbeat_ack_at": None}
        saved = store.get_meta(self.checkpoint_key)
        if saved:
            try:
                saved = json.loads(saved)
                session, seq = saved["session_id"], saved["sequence"]
                if isinstance(session, str) and session and type(seq) is int and seq >= 0:
                    self.session_id, self.sequence = session, seq
            except (ValueError, TypeError, KeyError):
                pass

    def snapshot(self):
        # No access token, AppSecret, session ID or gateway URL in status responses.
        return dict(self.status)

    def clear_session(self):
        self.store.gateway_checkpoint(self.checkpoint_key, {"session_id": "", "sequence": None})
        self.session_id, self.sequence = "", None

    @staticmethod
    def packet(raw):
        value = json.loads(raw)
        if not isinstance(value, dict) or type(value.get("op")) is not int:
            raise ValueError("invalid_gateway_packet")
        return value

    async def send_heartbeat(self, ws):
        self.awaiting_ack = True
        await ws.send(dumps({"op": 1, "d": self.sequence}))

    async def heartbeats(self, ws, interval):
        while True:
            await self.sleep(interval)
            if self.awaiting_ack:
                raise Reconnect("heartbeat_ack_timeout")
            await self.send_heartbeat(ws)

    async def handle(self, packet, ws):
        op = packet["op"]
        if op == 11:
            self.awaiting_ack = False
            self.status["last_heartbeat_ack_at"] = now()
            return
        if op == 1:
            await self.send_heartbeat(ws)
            return
        if op == 7:
            raise Reconnect("server_requested_reconnect")
        if op == 9:
            # Clear even a previously persisted session; next connection must Identify.
            raise Reconnect("invalid_session", delay=5, clear=True)
        if op != 0:
            return
        seq = packet.get("s")
        if type(seq) is not int or seq < 0:
            raise ValueError("invalid_gateway_sequence")
        event_type = packet.get("t")
        session = self.session_id
        if event_type == "READY":
            data = packet.get("d")
            if not isinstance(data, dict) or not isinstance(data.get("session_id"), str) or not data["session_id"]:
                raise ValueError("invalid_ready_session")
            session = data["session_id"]
        elif self.sequence is not None and seq <= self.sequence:
            return
        if not session:
            raise Reconnect("dispatch_without_session", clear=True)
        event = None
        malformed = False
        try:
            key = message_key(packet)
            if key is not None:
                event = packet
        except ValueError:
            # Preserve malformed business messages for inspection without blocking later events.
            key = "gateway:malformed:" + hashlib.sha256(dumps(packet).encode()).hexdigest()
            event, malformed = packet, True
        checkpoint = {"session_id": session, "sequence": seq}
        self.store.gateway_checkpoint(self.checkpoint_key, checkpoint, event, key, malformed)
        # In-memory resume position advances only after the database transaction commits.
        self.session_id, self.sequence = session, seq
        if event_type in {"READY", "RESUMED"}:
            self.ready_since = time.monotonic()
            self.status.update(state="ready", ready=True, last_error=None)
        if event:
            self.status["last_event_at"] = now()

    async def receive(self, ws):
        deadline = time.monotonic() + 30
        while True:
            if self.status["ready"]:
                raw = await ws.recv()
            else:
                raw = await asyncio.wait_for(ws.recv(), max(.001, deadline - time.monotonic()))
            await self.handle(self.packet(raw), ws)

    async def connect_once(self):
        data = await self.qq.gateway()
        if data.get("shards", 1) > 1:
            raise FatalGateway("multiple_shards_required")
        limits = data.get("session_start_limit", {})
        if not self.session_id and limits.get("remaining") == 0:
            raise Reconnect("session_start_limit", delay=max(5, limits.get("reset_after", 60000) / 1000))
        token = await self.qq.access_token()
        # QQ provides the WSS server certificate. System CA validation remains enabled.
        async with self.connector(data["url"], open_timeout=20, close_timeout=5,
                                  ping_interval=None, max_size=1024 * 1024, proxy=None, logger=wire_log) as ws:
            self.status.update(connected=True, state="authenticating", ready=False)
            hello = self.packet(await asyncio.wait_for(ws.recv(), 20))
            if hello["op"] != 10:
                raise ValueError("missing_gateway_hello")
            interval = hello.get("d", {}).get("heartbeat_interval")
            if type(interval) not in (int, float) or not 0 < interval <= 300000:
                raise ValueError("invalid_heartbeat_interval")
            self.awaiting_ack = False
            if self.session_id and self.sequence is not None:
                auth = {"op": 6, "d": {"token": f"QQBot {token}", "session_id": self.session_id, "seq": self.sequence}}
            else:
                auth = {"op": 2, "d": {"token": f"QQBot {token}", "intents": self.settings.qq_gateway_intents,
                                       "shard": [0, 1], "properties": {"$os": "linux", "$browser": "qq-order-bot", "$device": "qq-order-bot"}}}
            await ws.send(dumps(auth))
            tasks = [asyncio.create_task(self.receive(ws)), asyncio.create_task(self.heartbeats(ws, interval / 1000))]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    def closed(self, code):
        if code in {4003, 4010, 4011, 4012, 4013, 4014, 4914, 4915}:
            self.clear_session()
            raise FatalGateway(f"qq_close_{code}")
        if code == 4004:
            self.qq.token = ""
        if code in {4006, 4007, 4009} or 4900 <= code <= 4913:
            self.clear_session()
        return 60 if code == 4008 else 0

    async def run(self):
        if not self.status["enabled"]:
            return
        failures = 0
        try:
            while True:
                delay = 0
                self.ready_since = None
                self.status.update(state="connecting", connected=False, ready=False)
                try:
                    await self.connect_once()
                except asyncio.CancelledError:
                    raise
                except FatalGateway as exc:
                    self.status.update(state="fatal", last_error=str(exc))
                    return
                except Reconnect as exc:
                    if exc.clear:
                        self.clear_session()
                    delay = exc.delay
                    self.status["last_error"] = str(exc)
                except ConnectionClosed as exc:
                    code = exc.rcvd.code if exc.rcvd else 1006
                    self.status["last_error"] = f"qq_close_{code}"
                    try:
                        delay = self.closed(code)
                    except FatalGateway as fatal:
                        self.status.update(state="fatal", last_error=str(fatal))
                        return
                except SendError as exc:
                    self.status["last_error"] = str(exc)
                    if not exc.retryable:
                        self.status["state"] = "fatal"
                        return
                    delay = 60
                except Exception as exc:
                    self.status["last_error"] = type(exc).__name__
                finally:
                    self.status.update(connected=False, ready=False)
                if self.ready_since and time.monotonic() - self.ready_since >= 60:
                    failures = 0
                failures += 1
                self.status["retries"] = failures
                if failures >= self.settings.qq_gateway_max_retries:
                    self.status["state"] = "fatal"
                    log.error("QQ gateway retry budget exhausted: %s", self.status["last_error"])
                    return
                delay = max(delay, min(60, 2 ** min(failures, 6)))
                self.status["state"] = "reconnecting"
                log.warning("QQ gateway reconnect in %.0fs: %s", delay, self.status["last_error"])
                await self.sleep(delay)
        except Exception as exc:
            self.status.update(state="fatal", last_error=type(exc).__name__)
            log.error("QQ gateway stopped: %s", type(exc).__name__)
        finally:
            self.status.update(connected=False, ready=False)
            if self.status["state"] != "fatal":
                self.status["state"] = "stopped"
