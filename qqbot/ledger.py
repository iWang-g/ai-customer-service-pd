from .domain import OrderConflict, OrderInput, Status, cents, contribution, now
from .storage import Store, dumps


CHANGE_TEMPLATE = ("已帮您{action}这笔订单啦！\n\n{shop}\n订单号：{order_no}\n当前状态：{status}"
                   "\n\n本笔计入消费：{net:.2f} 元\n累计有效消费：{total:.2f} 元"
                   "\n本次消费变动：{delta:+.2f} 元\n\n后续如有退款或状态变化，发来最新凭证就可以更新。")


class Ledger:
    def __init__(self, store: Store):
        self.store = store

    def apply(self, cid: str, data: OrderInput, source: str, verified=False, review_id=None):
        with self.store.transaction() as db:
            if not db.execute("SELECT 1 FROM customers WHERE id=?", (cid,)).fetchone():
                raise OrderConflict("客户不存在")
            if review_id:
                review = db.execute("SELECT * FROM reviews WHERE id=? AND customer_id=?", (review_id, cid)).fetchone()
                if not review:
                    raise OrderConflict("待确认记录不存在")
                if review["state"] != "pending":
                    raise OrderConflict("该记录已处理")
            if data.shop:
                matches = db.execute("SELECT * FROM orders WHERE shop=? AND order_no=?", (data.shop, data.order_no)).fetchall()
            else:
                matches = db.execute("SELECT * FROM orders WHERE customer_id=? AND order_no=?", (cid, data.order_no)).fetchall()
            if len(matches) > 1:
                raise OrderConflict("订单号在多个店铺出现，请补充店铺")
            old = dict(matches[0]) if matches else None
            if old and old["customer_id"] != cid:
                raise OrderConflict("此订单已属于其他客户，请管理员核验归属")
            base = dict(old) if old else {"customer_id": cid, "order_no": data.order_no, "shop": None,
                "taobao_account": None,
                "ordered_on": None, "order_cents": None, "paid_cents": None,
                "refund_cents": 0, "status": None, "version": 0, "verified_at": None}
            for name, value in data.model_dump(exclude_none=True).items():
                if name in {"order_amount", "paid_amount", "refund_amount"}:
                    base[{"order_amount": "order_cents", "paid_amount": "paid_cents", "refund_amount": "refund_cents"}[name]] = cents(value)
                elif name in {"shop", "taobao_account", "ordered_on", "status"}:
                    base[name] = str(value)
            missing = [k for k in ("shop", "ordered_on", "order_cents", "paid_cents", "status") if base[k] is None]
            if missing:
                raise OrderConflict("新订单缺少字段：" + ", ".join(missing))
            # 退款额必须为累计已到账额。全额退款状态可以明确推出全额退款。
            if base["status"] == Status.REFUNDED:
                if data.refund_amount is not None and cents(data.refund_amount) != base["paid_cents"]:
                    raise OrderConflict("已退款状态与退款金额不一致，部分退款请标记部分退款")
                base["refund_cents"] = base["paid_cents"]
            if base["refund_cents"] > base["paid_cents"]:
                raise OrderConflict("退款额不能大于实付款")
            if old and base["refund_cents"] < old["refund_cents"] and not verified:
                raise OrderConflict("累计已到账退款额减少须由管理员核验，请勿用旧截图覆盖退款记录")
            if base["status"] == Status.PARTIAL_REFUND and not 0 < base["refund_cents"] < base["paid_cents"]:
                raise OrderConflict("部分退款必须提供小于实付款的累计退款金额")
            if base["status"] in {Status.UNPAID, Status.CANCELLED} and base["paid_cents"] != base["refund_cents"]:
                raise OrderConflict("待付款或已取消订单仍有未退实付款，请核验状态和退款额")
            base["net_cents"] = contribution(base)
            business = ("shop", "taobao_account", "ordered_on", "order_cents", "paid_cents", "refund_cents", "status", "net_cents")
            changed = not old or any(base[k] != old[k] for k in business)
            total_before = db.execute("SELECT COALESCE(SUM(net_cents),0) FROM orders WHERE customer_id=?", (cid,)).fetchone()[0]
            if changed:
                base.update(source=source, updated_at=now(), version=base["version"] + 1)
                # 客户补图只说明收到新陈述，不能冒充店铺/管理员权威核验。
                base["verified_at"] = now() if verified else None
                fields = ("customer_id", "order_no", *business, "version", "source", "updated_at", "verified_at")
                if old:
                    assignments = ",".join(f"{k}=?" for k in fields)
                    db.execute(f"UPDATE orders SET {assignments} WHERE id=?", (*[base[k] for k in fields], old["id"]))
                    order_id = old["id"]
                else:
                    order_id = db.execute(f"INSERT INTO orders({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                                          tuple(base[k] for k in fields)).lastrowid
                base["id"] = order_id
                db.execute("INSERT INTO audit(action,customer_id,order_id,before_json,after_json,source,at) VALUES (?,?,?,?,?,?,?)",
                           ("order_update" if old else "order_create", cid, order_id, dumps(old), dumps(base), source, now()))
                total_after = total_before + base["net_cents"] - (old["net_cents"] if old else 0)
                message = CHANGE_TEMPLATE.format(action="更新" if old else "记好", shop=base["shop"], order_no=data.order_no, status=base["status"], net=base["net_cents"] / 100,
                                                  total=total_after / 100, delta=(total_after - total_before) / 100)
                self.store.notify_in(db, f"order:{order_id}:v{base['version']}", cid, message)
            elif verified:
                db.execute("UPDATE orders SET verified_at=? WHERE id=?", (now(), old["id"]))
                db.execute("INSERT INTO audit(action,customer_id,order_id,after_json,source,at) VALUES (?,?,?,?,?,?)",
                           ("order_verify", cid, old["id"], dumps(old), source, now()))
            if verified:
                db.execute("DELETE FROM review_checks WHERE order_id=?", ((old or base)["id"],))
            if review_id:
                db.execute("UPDATE reviews SET state='accepted',resolved_at=? WHERE id=?", (now(), review_id))
            return {"changed": changed, "order_no": data.order_no, "order_id": (old or base)["id"],
                    "net_cents": base["net_cents"]}
