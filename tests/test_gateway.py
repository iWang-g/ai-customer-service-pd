import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import event
from qqbot.app import create_app
from qqbot.config import Settings
from qqbot.events import message_key
from qqbot.gateway import FatalGateway, Gateway, Reconnect
from qqbot.qq import QQClient, SendError


class FakeQQ:
    token = "test-access-token"

    def __init__(self, limits=None, shards=1):
        self.limits = limits or {"remaining": 10, "reset_after": 60000}
        self.shards = shards

    async def gateway(self):
        return {"url": "wss://api.sgroup.qq.com/websocket/", "shards": self.shards, "session_start_limit": self.limits}

    async def access_token(self):
        return self.token


class FakeSocket:
    def __init__(self, packets=()):
        self.incoming = asyncio.Queue()
        self.sent = []
        for packet in packets:
            self.incoming.put_nowait(packet)

    async def recv(self):
        packet = await self.incoming.get()
        if isinstance(packet, Exception):
            raise packet
        return json.dumps(packet)

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class FakeConnection:
    def __init__(self, socket):
        self.socket = socket
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *args):
        pass


HELLO = {"op": 10, "d": {"heartbeat_interval": 45000}}
READY = {"op": 0, "t": "READY", "s": 1, "d": {"session_id": "session-1"}}


def live_settings(settings):
    return settings.model_copy(update={"qq_transport": "websocket", "qq_gateway_enabled": True, "worker_enabled": True})


def test_identify_queues_supported_messages(ledger, settings):
    message = {**event(content="帮助"), "s": 2}
    socket = FakeSocket([HELLO, READY, message, Reconnect("test_end")])
    connection = FakeConnection(socket)
    gateway = Gateway(ledger.store, FakeQQ(), settings, connection)
    with pytest.raises(Reconnect, match="test_end"):
        asyncio.run(gateway.connect_once())
    auth = socket.sent[0]
    assert auth["op"] == 2
    assert auth["d"]["intents"] == 1 << 25
    assert auth["d"]["shard"] == [0, 1]
    assert auth["d"]["token"] == "QQBot test-access-token"
    assert connection.calls[0][1]["ping_interval"] is None
    assert "ssl" not in connection.calls[0][1]  # TLS uses system CA validation.
    row = ledger.store.rows("SELECT * FROM events")[0]
    assert row["state"] == "pending" and row["id"] == message_key(message)
    assert gateway.sequence == 2
    assert "test-access-token" not in json.dumps(gateway.snapshot())
    assert "session-1" not in json.dumps(gateway.snapshot())


def test_restart_resume_and_replayed_message_deduplication(ledger, settings):
    async def first():
        gateway = Gateway(ledger.store, FakeQQ(), settings)
        await gateway.handle(READY, FakeSocket())
        await gateway.handle({**event("m1"), "s": 2}, FakeSocket())
    asyncio.run(first())
    resumed = {"op": 0, "t": "RESUMED", "s": 3, "d": None}
    socket = FakeSocket([HELLO, resumed, {**event("m1"), "s": 4}, {**event("m2"), "s": 5}, Reconnect("test_end")])
    gateway = Gateway(ledger.store, FakeQQ(), settings, FakeConnection(socket))
    with pytest.raises(Reconnect):
        asyncio.run(gateway.connect_once())
    assert socket.sent[0] == {"op": 6, "d": {"token": "QQBot test-access-token", "session_id": "session-1", "seq": 2}}
    assert len(ledger.store.rows("SELECT * FROM events")) == 2
    assert gateway.sequence == 5
    checkpoint = json.loads(ledger.store.get_meta(gateway.checkpoint_key))
    assert checkpoint["sequence"] == 5


def test_session_is_scoped_to_bot_and_environment(ledger, settings):
    gateway = Gateway(ledger.store, FakeQQ(), settings)
    asyncio.run(gateway.handle(READY, FakeSocket()))
    another = Gateway(ledger.store, FakeQQ(), settings.model_copy(update={"qq_app_id": "another"}))
    sandbox = Gateway(ledger.store, FakeQQ(), settings.model_copy(update={"qq_api_base": "https://sandbox.api.sgroup.qq.com"}))
    assert another.session_id == sandbox.session_id == ""


def test_message_and_resume_checkpoint_commit_atomically(ledger, settings):
    gateway = Gateway(ledger.store, FakeQQ(), settings)
    asyncio.run(gateway.handle(READY, FakeSocket()))
    with ledger.store.transaction() as db:
        db.execute("CREATE TRIGGER fail_checkpoint BEFORE UPDATE ON metadata BEGIN SELECT RAISE(ABORT, 'test_failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(gateway.handle({**event(), "s": 2}, FakeSocket()))
    assert not ledger.store.rows("SELECT * FROM events")
    assert gateway.sequence == 1
    assert json.loads(ledger.store.get_meta(gateway.checkpoint_key))["sequence"] == 1


def test_malformed_message_is_preserved_without_blocking_later_events(ledger, settings):
    gateway = Gateway(ledger.store, FakeQQ(), settings)
    async def run():
        await gateway.handle(READY, FakeSocket())
        await gateway.handle({"op": 0, "t": "C2C_MESSAGE_CREATE", "s": 2, "d": {}}, FakeSocket())
        await gateway.handle({**event(), "s": 3}, FakeSocket())
    asyncio.run(run())
    rows = ledger.store.rows("SELECT state,error FROM events ORDER BY rowid")
    assert rows[0] == {"state": "dead", "error": "invalid_message_envelope"}
    assert rows[1]["state"] == "pending"
    assert gateway.sequence == 3


def test_heartbeat_uses_sequence_and_ack_timeout_reconnects(ledger, settings):
    gateway = Gateway(ledger.store, FakeQQ(), settings)
    socket = FakeSocket()
    async def run():
        await gateway.handle(READY, socket)
        await gateway.handle({"op": 1}, socket)
        assert socket.sent[-1] == {"op": 1, "d": 1}
        assert gateway.awaiting_ack
        await gateway.handle({"op": 11}, socket)
        assert not gateway.awaiting_ack
        assert gateway.status["last_heartbeat_ack_at"]
        with pytest.raises(Reconnect, match="heartbeat_ack_timeout"):
            await asyncio.wait_for(gateway.heartbeats(socket, .001), timeout=1)
    asyncio.run(run())


def test_invalid_session_forces_identify_on_next_connection(ledger, settings):
    gateway = Gateway(ledger.store, FakeQQ(), live_settings(settings))
    sleeps = []
    async def sleep(delay):
        sleeps.append(delay)
    async def connection():
        if gateway.session_id:
            await gateway.handle({"op": 9, "d": False}, FakeSocket())
        raise FatalGateway("test_finished")
    asyncio.run(gateway.handle(READY, FakeSocket()))
    gateway.sleep, gateway.connect_once = sleep, connection
    asyncio.run(gateway.run())
    assert gateway.session_id == "" and gateway.sequence is None
    assert sleeps == [5]
    assert gateway.status["state"] == "fatal"


@pytest.mark.parametrize("code", [4010, 4011, 4013, 4014, 4914, 4915])
def test_permission_and_disabled_gateway_errors_do_not_loop(ledger, settings, code):
    gateway = Gateway(ledger.store, FakeQQ(), settings)
    asyncio.run(gateway.handle(READY, FakeSocket()))
    with pytest.raises(FatalGateway, match=f"qq_close_{code}"):
        gateway.closed(code)
    assert not gateway.session_id


def test_rate_limit_and_expired_session_policy(ledger, settings):
    qq = FakeQQ()
    gateway = Gateway(ledger.store, qq, settings)
    asyncio.run(gateway.handle(READY, FakeSocket()))
    assert gateway.closed(4008) == 60
    assert gateway.session_id == "session-1"
    gateway.closed(4004)
    assert qq.token == ""
    gateway.closed(4009)
    assert gateway.session_id == ""


def test_retry_budget_and_status_never_echo_exception_secrets(ledger, settings):
    gateway = Gateway(ledger.store, FakeQQ(), live_settings(settings).model_copy(update={"qq_gateway_max_retries": 3}))
    delays = []
    async def fail():
        raise OSError("sensitive-test-marker")
    async def sleep(delay):
        delays.append(delay)
    gateway.connect_once, gateway.sleep = fail, sleep
    asyncio.run(gateway.run())
    assert delays == [2, 4]
    assert gateway.snapshot()["state"] == "fatal"
    assert gateway.snapshot()["retries"] == 3
    assert "sensitive-test-marker" not in json.dumps(gateway.snapshot())


def test_session_budget_waits_before_identifying(ledger, settings):
    connection = FakeConnection(FakeSocket())
    gateway = Gateway(ledger.store, FakeQQ({"remaining": 0, "reset_after": 120000}), settings, connection)
    with pytest.raises(Reconnect) as caught:
        asyncio.run(gateway.connect_once())
    assert caught.value.delay == 120
    assert not connection.calls


def test_multi_shard_account_is_reported_explicitly(ledger, settings):
    connection = FakeConnection(FakeSocket())
    gateway = Gateway(ledger.store, FakeQQ(shards=2), settings, connection)
    with pytest.raises(FatalGateway, match="multiple_shards_required"):
        asyncio.run(gateway.connect_once())
    assert not connection.calls


@pytest.mark.parametrize("url", ["ws://api.sgroup.qq.com/ws", "wss://example.com/ws", "wss://token@api.sgroup.qq.com/ws"])
def test_gateway_url_and_token_contract(settings, url):
    def handler(request):
        if request.url.path.endswith("getAppAccessToken"):
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": "7200"})
        assert request.url.path == "/gateway/bot"
        assert request.headers["Authorization"] == "QQBot test-token"
        assert request.headers["X-Union-Appid"] == "test-app"
        return httpx.Response(200, json={"url": url, "shards": 1})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with pytest.raises(SendError, match="WSS"):
                await QQClient(settings, http).gateway()
    asyncio.run(run())


def test_gateway_api_refreshes_expired_token(settings):
    tokens = []
    queries = []
    def handler(request):
        if request.url.path.endswith("getAppAccessToken"):
            tokens.append(len(tokens) + 1)
            return httpx.Response(200, json={"access_token": f"token-{len(tokens)}", "expires_in": "7200"})
        queries.append(request.headers["Authorization"])
        return httpx.Response(401) if len(queries) == 1 else httpx.Response(200, json={"url": "wss://api.sgroup.qq.com/websocket/", "shards": 1})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert (await QQClient(settings, http).gateway())["shards"] == 1
    asyncio.run(run())
    assert queries == ["QQBot token-1", "QQBot token-2"]


def test_gateway_switch_is_explicit_and_requires_credentials():
    for fields in ({"qq_gateway_enabled": True}, {"qq_transport": "websocket", "qq_gateway_enabled": True}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **fields)


def test_disabled_gateway_never_connects_with_real_credentials(settings, monkeypatch):
    settings.worker_enabled = True
    settings.qq_transport = "websocket"
    calls = []
    async def connect(self):
        calls.append("unexpected")
    monkeypatch.setattr(Gateway, "connect_once", connect)
    with TestClient(create_app(settings)) as client:
        status = client.get("/admin/status", headers={"Authorization": f"Bearer {settings.admin_token}"}).json()
        assert status["qq_transport"] == "websocket"
        assert status["gateway"]["state"] == "disabled"
    assert not calls


def test_websocket_event_reuses_order_worker(settings):
    text = "QQ号：123456789\n淘宝账号名：测试买家\n店铺：测试旗舰店\n订单号：000123456789\n下单日期：2026-10-01\n下单金额：100.00\n实付款：99.90\n订单状态：待收货"
    app = create_app(settings)
    with TestClient(app):
        async def run():
            gateway = app.state.gateway
            await gateway.handle(READY, FakeSocket())
            await gateway.handle({**event(content=text), "s": 2}, FakeSocket())
            await app.state.service.tick()
        asyncio.run(run())
        assert app.state.store.totals()[0]["net_cents"] == 9990
        assert app.state.store.rows("SELECT state FROM events")[0]["state"] == "done"
        assert app.state.store.rows("SELECT state FROM notifications")[0]["state"] == "simulated"
