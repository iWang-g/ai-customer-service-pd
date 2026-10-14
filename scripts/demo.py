"""Offline demo through the real signed Webhook and worker, using a separate ledger."""
import asyncio
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from qqbot.app import create_app
from qqbot.config import Settings
from qqbot.security import signing_key


def main():
    # Separate directory per run; demonstration data never enters the real database.
    output = ROOT / "outputs" / ("demo-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    settings = Settings(_env_file=None, data_dir=output, worker_enabled=False,
                        qq_app_id="demo", qq_app_secret="offline-demo-secret", admin_token="offline-demo-admin-token-32-chars")
    app = create_app(settings)
    auth = {"Authorization": f"Bearer {settings.admin_token}"}
    messages = [
        "QQ号：123456789\n淘宝账号名：演示账号\n店铺：示例旗舰店\n订单号：00012345678901234567\n下单日期：2026-10-01\n下单金额：129.00\n实付款：99.90\n订单状态：待收货",
        "淘宝账号名：演示账号\n订单号：00012345678901234567\n店铺：示例旗舰店\n下单日期：2026-10-01\n下单金额：129.00\n实付款：99.90\n订单状态：待收货",
        "淘宝账号名：另一个演示账号\n店铺：示例旗舰店\n订单号：00012345678901234568\n下单日期：2026-10-02\n下单金额：88.00\n实付款：80.00\n订单状态：已完成",
        "订单号：00012345678901234567\n店铺：示例旗舰店\n订单状态：部分退款\n退款金额：20.10",
        "订单号：未识别完整\n订单状态：待收货",
        "/消费",
    ]
    with TestClient(app) as client:
        for index, content in enumerate(messages):
            event = {"op": 0, "t": "C2C_MESSAGE_CREATE", "id": f"event-{index}", "d": {
                "id": f"message-{index}", "author": {"user_openid": "demo-customer"}, "content": content,
                "timestamp": datetime.now(timezone.utc).isoformat()}}
            body = json.dumps(event, ensure_ascii=False).encode()
            timestamp = str(int(time.time()))
            signature = signing_key(settings.qq_app_secret).sign(timestamp.encode() + body).hex()
            response = client.post("/webhooks/qq", content=body, headers={"X-Signature-Ed25519": signature, "X-Signature-Timestamp": timestamp})
            assert response.json() == {"op": 12, "d": 0}
            asyncio.run(app.state.service.tick())
        checked = client.post("/admin/reconcile", headers=auth).json()
        # 展示无有效回复窗口时保留待通知消息的情形。
        cid = app.state.store.totals()[0]["customer_id"]
        with app.state.store.transaction() as db:
            db.execute("UPDATE identities SET last_event_at=0")
        app.state.store.notify("demo-waiting", cid, "您好，您的消费数据已更新，最新累计有效消费为 ¥159.80。如有疑问请提供最新订单凭证。")
        asyncio.run(app.state.service.notifier.flush_one())
        asyncio.run(app.state.service.export())
        summary = {"demo_only": True, "expected_total_yuan": "159.80", "actual_total_cents": app.state.store.totals()[0]["net_cents"],
                   "orders": client.get("/admin/orders", headers=auth).json(), "reconcile": checked,
                   "notifications": client.get("/admin/notifications", headers=auth).json()}
        assert summary["actual_total_cents"] == 15980
        (output / "demo-results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"离线演示通过：两个订单，重复发送未重复计入，部分退款后合计 159.80 元。\nExcel：{output / 'reports' / 'orders.xlsx'}")


if __name__ == "__main__":
    main()
