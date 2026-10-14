import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Status(StrEnum):
    UNPAID = "待付款"
    UNSHIPPED = "待发货"
    SHIPPED = "待收货"
    COMPLETED = "已完成"
    REFUNDING = "退款中"
    PARTIAL_REFUND = "部分退款"
    REFUNDED = "已退款"
    CANCELLED = "已取消"


STATUS_ALIASES = {"交易成功": "已完成", "已收货": "已完成", "交易关闭": "已取消",
                  "退款成功": "已退款", "已发货": "待收货", "待评价": "已完成"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cents(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("金额不能是布尔值")
    try:
        amount = Decimal(str(value).replace(",", ""))
        if not amount.is_finite() or amount < 0 or amount > Decimal("100000000"):
            raise ValueError("金额超出允许范围")
        if amount != amount.quantize(Decimal("0.01")):
            raise ValueError("金额最多两位小数")
        return int(amount * 100)
    except InvalidOperation as exc:
        raise ValueError("金额格式错误") from exc


class OrderInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    order_no: str = Field(min_length=3, max_length=80)
    shop: str | None = Field(default=None, max_length=120)
    taobao_account: str | None = Field(default=None, max_length=120)
    ordered_on: date | None = None
    order_amount: Decimal | None = None
    paid_amount: Decimal | None = None
    refund_amount: Decimal | None = None
    status: Status | None = None
    currency: str = "CNY"

    @field_validator("order_no")
    @classmethod
    def validate_no(cls, value):
        if value is None:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,80}", value):
            raise ValueError("订单号须为字母、数字、下划线或横线")
        return value

    @field_validator("shop", "taobao_account")
    @classmethod
    def empty_shop(cls, value):
        return value or None

    @field_validator("order_amount", "paid_amount", "refund_amount", mode="before")
    @classmethod
    def validate_money(cls, value):
        result = cents(value)
        return None if result is None else Decimal(result) / 100

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value):
        return STATUS_ALIASES.get(value, value)

    @field_validator("currency")
    @classmethod
    def rmb_only(cls, value):
        if value != "CNY":
            raise ValueError("当前只支持人民币，其他币种须人工处理")
        return value


def contribution(order: dict) -> int:
    if order["status"] in {Status.UNPAID, Status.CANCELLED, Status.REFUNDED}:
        return 0
    return order["paid_cents"] - order["refund_cents"]


class OrderConflict(ValueError):
    pass


class SubmissionInput(OrderInput):
    """A saved submission may be incomplete, including lacking an order number."""
    order_no: str | None = Field(default=None, min_length=3, max_length=80)
    qq_number: str | None = Field(default=None, pattern=r"^[1-9][0-9]{4,11}$")
