import asyncio
import json
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from qqbot.app import create_app
from qqbot.security import signing_key
from conftest import event


def signed(settings, payload, timestamp=None):
    body = json.dumps(payload, ensure_ascii=False).encode()
    ts = str(int(time.time())) if timestamp is None else str(timestamp)
    key = Ed25519PrivateKey.from_private_bytes((settings.qq_app_secret.encode() * 32)[:32])
    return body, {"X-Signature-Timestamp": ts, "X-Signature-Ed25519": key.sign(ts.encode() + body).hex(), "X-Bot-Appid": settings.qq_app_id}


def test_challenge_and_raw_body_security(settings):
    with TestClient(create_app(settings)) as client:
        request = {"op": 13, "d": {"plain_token": "token", "event_ts": "1725442341"}}
        body, headers = signed(settings, request)
        result = client.post("/webhooks/qq", content=body, headers=headers)
        assert result.status_code == 200
        signing_key(settings.qq_app_secret).public_key().verify(bytes.fromhex(result.json()["signature"]), b"1725442341token")
        assert client.post("/webhooks/qq", content=body + b" ", headers=headers).status_code == 401
        old_body, old_headers = signed(settings, request, time.time() - 600)
        assert client.post("/webhooks/qq", content=old_body, headers=old_headers).status_code == 401
        assert client.post("/webhooks/qq", json=request).status_code == 401


def test_webhook_persists_and_deduplicates_before_work(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        payload = event(content="帮助")
        for event_id in ("first", "retry-with-different-id"):
            payload["id"] = event_id
            body, headers = signed(settings, payload)
            assert client.post("/webhooks/qq", content=body, headers=headers).json() == {"op": 12, "d": 0}
        assert len(app.state.store.rows("SELECT * FROM events")) == 1
        assert app.state.store.rows("SELECT * FROM events")[0]["state"] == "pending"
        asyncio.run(app.state.service.tick())
        assert app.state.store.rows("SELECT * FROM events")[0]["state"] == "done"
        assert app.state.store.rows("SELECT * FROM notifications")[0]["state"] == "simulated"


def test_admin_requires_bearer_auth(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/admin/orders").status_code == 401
        assert client.get("/admin/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/admin/status", headers={"Authorization": f"Bearer {settings.admin_token}"}).status_code == 200


def test_bad_event_is_not_acknowledged(settings):
    with TestClient(create_app(settings)) as client:
        payload = {"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"id": "m1"}}
        body, headers = signed(settings, payload)
        assert client.post("/webhooks/qq", content=body, headers=headers).status_code == 400


def test_live_config_requires_real_credentials(settings):
    import pytest
    from pydantic import ValidationError
    from qqbot.config import Settings
    with pytest.raises(ValidationError):
        Settings(_env_file=None, qq_dry_run=False)


def test_csv_reconciliation_preserves_owner_and_rejects_unknown(settings, full_order):
    from qqbot.domain import OrderInput
    app = create_app(settings)
    auth = {"Authorization": f"Bearer {settings.admin_token}"}
    with TestClient(app) as client:
        cid = app.state.store.identity(event())["customer_id"]
        app.state.service.ledger.apply(cid, OrderInput(**full_order), "test")
        csv = f"订单号,店铺,订单状态,退款金额\n{full_order['order_no']},{full_order['shop']},部分退款,20.10\nunknown,无店,已退款,1.00\n"
        response = client.post("/admin/import/csv", headers=auth, files={"file": ("orders.csv", csv.encode("utf-8-sig"), "text/csv")})
        assert response.status_code == 200
        assert response.json()["verified"] == 1
        assert len(response.json()["errors"]) == 1
        assert app.state.store.totals()[0]["net_cents"] == 7980
        assert client.get("/admin/export.xlsx", headers=auth).status_code == 200


def test_short_csv_row_returns_row_error(settings, full_order):
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post("/admin/import/csv", headers={"Authorization": f"Bearer {settings.admin_token}"},
                               files={"file": ("orders.csv", "订单号,店铺,订单状态,退款金额\n123,示例店\n".encode(), "text/csv")})
        assert response.status_code == 200
        assert len(response.json()["errors"]) == 1
