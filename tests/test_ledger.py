import pytest
from pydantic import ValidationError

from qqbot.domain import OrderConflict, OrderInput, cents
from conftest import event


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1.001", True, "100000001"])
def test_invalid_money(value):
    with pytest.raises(ValueError):
        cents(value)


def test_decimal_money_dedup_and_refund(ledger, cid, full_order):
    order = OrderInput(**full_order)
    first = ledger.apply(cid, order, "test")
    assert first["net_cents"] == 9990
    assert not ledger.apply(cid, order, "test")["changed"]
    assert len(ledger.store.rows("SELECT * FROM notifications")) == 1
    partial = OrderInput(order_no=order.order_no, status="部分退款", refund_amount="20.10")
    assert ledger.apply(cid, partial, "test")["net_cents"] == 7980
    assert not ledger.apply(cid, partial, "test")["changed"]
    assert ledger.store.totals()[0]["net_cents"] == 7980
    assert ledger.apply(cid, OrderInput(order_no=order.order_no, status="已退款"), "test")["net_cents"] == 0
    stored = ledger.store.rows("SELECT * FROM orders")[0]
    assert stored["refund_cents"] == 9990
    assert stored["version"] == 3


def test_status_only_update_preserves_amounts(ledger, cid, full_order):
    ledger.apply(cid, OrderInput(**full_order), "test")
    ledger.apply(cid, OrderInput(order_no=full_order["order_no"], status="交易成功"), "test")
    result = ledger.store.rows("SELECT * FROM orders")[0]
    assert (result["paid_cents"], result["status"]) == (9990, "已完成")


def test_no_cross_customer_order_claim(ledger, cid, full_order):
    ledger.apply(cid, OrderInput(**full_order), "test")
    cid2 = ledger.store.identity(event("m2", "another"))["customer_id"]
    with pytest.raises(OrderConflict, match="其他客户"):
        ledger.apply(cid2, OrderInput(**full_order), "test")
    assert len(ledger.store.rows("SELECT * FROM orders")) == 1


def test_missing_fields_and_ambiguous_order(ledger, cid, full_order):
    with pytest.raises(OrderConflict, match="缺少"):
        ledger.apply(cid, OrderInput(order_no="123"), "test")
    ledger.apply(cid, OrderInput(**full_order), "test")
    ledger.apply(cid, OrderInput(**{**full_order, "shop": "另一店"}), "test")
    with pytest.raises(OrderConflict, match="多个店铺"):
        ledger.apply(cid, OrderInput(order_no=full_order["order_no"], status="已完成"), "test")


def test_refund_invariants_roll_back(ledger, cid, full_order):
    ledger.apply(cid, OrderInput(**full_order), "test")
    for update in ({"refund_amount": "100"}, {"status": "部分退款"}, {"status": "已退款", "refund_amount": "10"}, {"status": "已取消"}):
        with pytest.raises(OrderConflict):
            ledger.apply(cid, OrderInput(order_no=full_order["order_no"], **update), "test")
    assert ledger.store.totals()[0]["net_cents"] == 9990
    assert len(ledger.store.rows("SELECT * FROM audit")) == 1


def test_unpaid_zero_counted(ledger, cid, full_order):
    order = {**full_order, "paid_amount": "0", "status": "待付款"}
    assert ledger.apply(cid, OrderInput(**order), "test")["net_cents"] == 0


def test_group_and_c2c_link_only_after_verification(ledger, cid, full_order):
    group_cid = ledger.store.identity(event("m2", "u1", group="g1"))["customer_id"]
    assert group_cid != cid
    ledger.apply(cid, OrderInput(**full_order), "test")
    ledger.apply(group_cid, OrderInput(**{**full_order, "order_no": "second"}), "test")
    ledger.store.link_customers(group_cid, cid, "管理员已核实 QQ 身份")
    assert ledger.store.totals()[0]["net_cents"] == 19980
    assert len(ledger.store.totals()) == 1
    assert len(ledger.store.rows("SELECT * FROM identities WHERE customer_id=?", (cid,))) == 2


def test_verification_is_distinct_from_customer_claim(ledger, cid, full_order):
    ledger.apply(cid, OrderInput(**full_order), "qq_text")
    assert ledger.store.rows("SELECT verified_at FROM orders")[0]["verified_at"] is None
    ledger.apply(cid, OrderInput(**full_order), "admin", verified=True)
    assert ledger.store.rows("SELECT verified_at FROM orders")[0]["verified_at"] is not None
    ledger.apply(cid, OrderInput(order_no=full_order["order_no"], status="已完成"), "qq_text")
    assert ledger.store.rows("SELECT verified_at FROM orders")[0]["verified_at"] is None


def test_review_confirmation_is_atomic_and_once(ledger, cid, full_order):
    number = ledger.store.review("event", 0, cid, full_order, "test")
    ledger.apply(cid, OrderInput(**full_order), "confirm", review_id=number)
    with pytest.raises(OrderConflict, match="已处理"):
        ledger.apply(cid, OrderInput(**full_order), "confirm", review_id=number)
    assert len(ledger.store.rows("SELECT * FROM orders")) == 1


def test_old_screenshot_cannot_reduce_refunds_without_admin(ledger, cid, full_order):
    ledger.apply(cid, OrderInput(**full_order), "test")
    ledger.apply(cid, OrderInput(order_no=full_order["order_no"], status="部分退款", refund_amount="20.10"), "test")
    with pytest.raises(OrderConflict, match="减少"):
        ledger.apply(cid, OrderInput(order_no=full_order["order_no"], status="待收货", refund_amount="0"), "qq_text")
    assert ledger.store.totals()[0]["net_cents"] == 7980
    ledger.apply(cid, OrderInput(order_no=full_order["order_no"], status="待收货", refund_amount="0"), "admin", verified=True)
    assert ledger.store.totals()[0]["net_cents"] == 9990
