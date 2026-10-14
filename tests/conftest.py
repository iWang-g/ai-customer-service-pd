from datetime import datetime, timezone

import pytest

from qqbot.config import Settings
from qqbot.ledger import Ledger
from qqbot.storage import Store


def event(message_id="m1", user="u1", content="", group=None, attachments=None):
    data = {"id": message_id, "content": content, "timestamp": datetime.now(timezone.utc).isoformat(),
            "author": {"member_openid" if group else "user_openid": user}, "attachments": attachments or []}
    if group:
        data["group_openid"] = group
    return {"op": 0, "t": "GROUP_AT_MESSAGE_CREATE" if group else "C2C_MESSAGE_CREATE", "id": "e" + message_id, "d": data}


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path / "data", qq_app_id="test-app", qq_app_secret="test-secret",
                    admin_token="unit-test-admin-token-at-least-24", worker_enabled=False)


@pytest.fixture
def ledger(settings):
    store = Store(settings.db_path)
    return Ledger(store)


@pytest.fixture
def cid(ledger):
    return ledger.store.identity(event())["customer_id"]


@pytest.fixture
def full_order():
    return {"order_no": "00012345678901234567", "shop": "测试旗舰店", "ordered_on": "2026-10-01",
            "order_amount": "120.00", "paid_amount": "99.90", "status": "待收货"}
