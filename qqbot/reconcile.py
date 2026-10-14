import json
import time
from urllib.parse import quote

from .domain import OrderInput, now


class Reconciler:
    def __init__(self, store, ledger, settings, http):
        self.store, self.ledger, self.settings, self.http = store, ledger, settings, http

    async def run(self):
        orders = self.store.rows("SELECT * FROM orders ORDER BY id")
        summary = {"total": len(orders), "verified": 0, "changed": 0, "needs_review": 0, "errors": []}
        self.store.set_meta("last_reconcile_attempt", time.time())
        for order in orders:
            try:
                if not self.settings.commerce_base_url:
                    raise ValueError("尚无店铺查询接口，请导入最新店铺 CSV 或人工核验")
                response = await self.http.get(
                    self.settings.commerce_base_url.rstrip("/") + "/orders/" + quote(order["order_no"], safe=""),
                    params={"shop": order["shop"]},
                    headers={"Authorization": f"Bearer {self.settings.commerce_api_key}"}, timeout=20)
                response.raise_for_status()
                data = OrderInput.model_validate(response.json())
                if data.order_no != order["order_no"] or data.shop != order["shop"]:
                    raise ValueError("查询响应的店铺或订单号与目标不一致")
                if data.status is None or data.refund_amount is None:
                    raise ValueError("权威查询必须返回最新状态和累计已到账退款额")
                result = self.ledger.apply(order["customer_id"], data, "commerce_api", verified=True)
                summary["verified"] += 1
                summary["changed"] += int(result["changed"])
            except Exception as exc:
                # 不打印第三方响应或敏感 URL。
                reason = str(exc)[:250] if isinstance(exc, ValueError) else type(exc).__name__
                summary["needs_review"] += 1
                summary["errors"].append({"order_id": order["id"], "reason": reason})
                with self.store.transaction() as db:
                    db.execute("INSERT INTO review_checks VALUES (?,?,?) ON CONFLICT(order_id) DO UPDATE SET reason=excluded.reason,checked_at=excluded.checked_at",
                               (order["id"], reason, now()))
        self.store.set_meta("last_reconcile_summary", json.dumps(summary, ensure_ascii=False))
        if not summary["needs_review"]:
            self.store.set_meta("last_full_reconcile_completed", now())
        return summary

    def is_due(self):
        last = float(self.store.get_meta("last_reconcile_attempt") or 0)
        return time.time() - last >= self.settings.reconcile_days * 86400
