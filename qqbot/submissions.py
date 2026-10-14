"""Customer-facing incomplete submissions and explicit, auditable supplements."""
import json
from decimal import Decimal

from pydantic import ValidationError

from .domain import OrderConflict, OrderInput, SubmissionInput


FIELD_NAMES = {
    "qq_number": "QQ号", "taobao_account": "淘宝账号名", "shop": "店铺",
    "order_no": "订单号", "ordered_on": "下单日期", "order_amount": "下单金额",
    "paid_amount": "实付款", "status": "订单状态", "refund_amount": "累计已到账退款",
}
REQUIRED = ("taobao_account", "shop", "order_no", "ordered_on", "order_amount", "paid_amount", "status")


def friendly_reason(reason):
    for internal, label in {**FIELD_NAMES, "order_cents": "下单金额", "paid_cents": "实付款"}.items():
        reason = reason.replace(internal, label)
    return reason


def normalize(data):
    result = SubmissionInput.model_validate(data).model_dump(mode="json", exclude_none=True, exclude_unset=True)
    for name in ("order_amount", "paid_amount", "refund_amount"):
        if name in result:
            result[name] = f"{Decimal(result[name]):.2f}"
    return result


def format_preview(data):
    lines = []
    for name, label in FIELD_NAMES.items():
        value = data.get(name)
        if value is None or value == "":
            continue
        if name.endswith("amount"):
            try:
                value = f"{float(value):.2f} 元"
            except (ValueError, TypeError):
                pass
        lines.append(f"{label}：{value}")
    return "\n".join(lines) or "目前还没有读到可用的订单信息。"


class Submissions:
    def __init__(self, store, ledger, settings):
        self.store, self.ledger, self.settings = store, ledger, settings

    def customer_qq(self, cid):
        return self.store.rows("SELECT qq_number FROM customers WHERE id=?", (cid,))[0]["qq_number"]

    def existing(self, cid, data):
        if not isinstance(data.get("order_no"), str) or not data["order_no"]:
            return None
        sql = "SELECT * FROM orders WHERE customer_id=? AND order_no=?"
        args = [cid, data["order_no"]]
        if isinstance(data.get("shop"), str) and data["shop"]:
            sql += " AND shop=?"
            args.append(data["shop"])
        rows = self.store.rows(sql, args)
        return rows[0] if len(rows) == 1 else None

    def effective(self, cid, data):
        old = self.existing(cid, data)
        result = {}
        if old:
            result = {k: old[k] for k in ("order_no", "shop", "taobao_account", "ordered_on", "status") if old[k] is not None}
            result.update(order_amount=f"{old['order_cents']/100:.2f}", paid_amount=f"{old['paid_cents']/100:.2f}",
                          refund_amount=f"{old['refund_cents']/100:.2f}")
        result.update({k: v for k, v in data.items() if v is not None and v != ""})
        # QQ belongs to the customer, never infer an identity from an order/screenshot.
        if self.customer_qq(cid):
            result["qq_number"] = self.customer_qq(cid)
        return result

    def missing(self, cid, data):
        result = self.effective(cid, data)
        missing = [k for k in ("qq_number", *REQUIRED) if result.get(k) is None or result.get(k) == ""]
        if result.get("status") in ("部分退款", "退款中") and result.get("refund_amount") is None:
            missing.append("refund_amount")
        return missing

    def reason(self, cid, data):
        try:
            normalize(data)
        except ValidationError as exc:
            names = dict.fromkeys(FIELD_NAMES.get(str(e["loc"][0]), "订单信息") for e in exc.errors())
            return "请检查这些信息的格式：" + "、".join(names)
        missing = self.missing(cid, data)
        if missing:
            return "待补充：" + "、".join(FIELD_NAMES[k] for k in missing)
        return "请核对后确认入账"

    def preview(self, cid, number, data, reason):
        details = self.effective(cid, data)
        missing = self.missing(cid, data)
        text = "收到啦，已帮您整理好这笔资料：\n\n" + format_preview(details)
        if missing:
            text += "\n\n还差下面几项，补充一下就可以继续啦：\n" + "\n".join(FIELD_NAMES[k] + "：" for k in missing)
            if reason.startswith(("请检查", "需要核对")):
                text += "\n\n" + friendly_reason(reason)
            text += f"\n\n资料已保存，编号：{number}，暂未计入累计消费。\n只有一条待处理记录时可直接补充；有多条时，请在首行写 /补充 {number}。"
        else:
            text += "\n\n" + friendly_reason(reason)
            text += f"\n这笔资料暂未入账。\n信息正确，请发送：/确认 {number}\n需要修改，请在首行写 /更正 {number}，下一行填写要修改的信息。"
        text += f"\n不需要这条记录，可发送：/拒绝 {number}"
        return text

    def select(self, cid, data, target):
        rows = self.store.rows("SELECT * FROM reviews WHERE customer_id=? AND state='pending' ORDER BY id", (cid,))
        if target is not None:
            return next((r for r in rows if r["id"] == target), None), False
        matches = []
        for row in rows:
            saved = json.loads(row["draft"])
            if data.get("order_no") and saved.get("order_no") and data["order_no"] != saved["order_no"]:
                continue
            if data.get("shop") and saved.get("shop") and data["shop"] != saved["shop"]:
                continue
            matches.append(row)
        if len(matches) == 1:
            return matches[0], False
        # A fresh identified order may be saved independently; unlabelled follow-ups need a target.
        return None, len(matches) > 1

    def receive(self, event_id, cid, drafts, target=None, correct=False):
        if self.store.rows("SELECT id FROM audit WHERE action='submission_supplement' AND source=? LIMIT 1", (f"qq:{event_id}",)):
            return ""  # Worker retry after a committed supplement must not create another draft.
        if target is not None and len(drafts) != 1:
            return "请一次补充一条记录，在 /补充 编号 或 /更正 编号 的下一行填写资料。"
        messages = []
        for index, draft in enumerate(drafts):
            data = {k: v for k, v in draft.data.items() if v is not None and v != ""}
            if target is not None and self.select(cid, data, target)[0] is None:
                messages.append("没有找到属于您的待处理记录，请核对编号；已确认或拒绝的记录不能再补充。")
                continue
            try:
                data = normalize(data)
                # Initial customer-provided QQ is reused, but never merges customers.
                self.store.register_qq(cid, data.get("qq_number"))
            except ValidationError:
                pass  # Keep invalid values in the draft so they can be explicitly corrected.
            except ValueError as exc:
                messages.append(str(exc))
                continue
            row, ambiguous = self.select(cid, data, target) if len(drafts) == 1 else (None, False)
            if target is not None and row is None:
                messages.append("没有找到属于您的待处理记录，请核对编号；已确认或拒绝的记录不能再补充。")
                continue
            if ambiguous:
                messages.append("您有多条待处理资料，请在首行写 /补充 编号，下一行填写本次要补充的信息。")
                continue
            if row:
                saved = json.loads(row["draft"])
                conflicts = [k for k, v in data.items() if k != "currency" and saved.get(k) not in (None, "", v)]
                if conflicts and not correct:
                    messages.append("这次提供的" + "、".join(FIELD_NAMES.get(k, "信息") for k in conflicts)
                                    + f"与记录 {row['id']} 不一致。\n\n如果要修改，请在首行写 /更正 {row['id']}，下一行填写正确的信息。")
                    continue
                merged = {**saved, **data}
                reason = self.reason(cid, merged)
                if draft.reason or (row["reason"].startswith("需要核对：") and not correct):
                    reason = "需要核对：" + friendly_reason(draft.reason or row["reason"].removeprefix("需要核对："))
                self.store.update_review(row["id"], cid, merged, reason, event_id)
                messages.append(self.preview(cid, row["id"], merged, reason))
                continue
            if data and set(data) <= {"qq_number", "currency"}:
                reason = self.reason(cid, data)
                if reason.startswith("请检查"):
                    messages.append(reason)
                else:
                    messages.append("QQ号已登记好啦！\n接下来发来订单截图或填写订单模板就可以了。")
                continue
            reason = self.reason(cid, data)
            if draft.reason:
                reason = "需要核对：" + friendly_reason(draft.reason)
            # Complete text templates retain the original automatic entry workflow.
            auto_apply = draft.origin == "text" or (self.settings.auto_apply_ai and draft.confidence >= self.settings.ai_confidence_threshold)
            if not draft.reason and reason == "请核对后确认入账" and auto_apply:
                try:
                    result = self.ledger.apply(cid, OrderInput.model_validate({k: v for k, v in data.items() if k != "qq_number"}), "qq_" + draft.origin)
                    if not result["changed"]:
                        messages.append("这笔订单已经记录过啦，本次没有重复计入消费。")
                    continue
                except (ValidationError, OrderConflict) as exc:
                    reason = friendly_reason(str(exc)) if isinstance(exc, OrderConflict) else "请检查订单信息的格式"
            number = self.store.review(event_id, index, cid, data, reason, draft.origin)
            messages.append(self.preview(cid, number, data, reason))
        return "\n\n".join(messages)

    def confirm(self, cid, review):
        data = json.loads(review["draft"])
        reason = self.reason(cid, data)
        if reason != "请核对后确认入账":
            return self.preview(cid, review["id"], data, reason)
        if review["reason"].startswith("需要核对："):
            return self.preview(cid, review["id"], data, review["reason"])
        try:
            result = self.ledger.apply(cid, OrderInput.model_validate({k: v for k, v in data.items() if k != "qq_number"}),
                                       "customer_confirmation", review_id=review["id"])
            return "已确认，这笔订单与已记录的信息一致，没有重复计入消费。" if not result["changed"] else ""
        except (ValidationError, OrderConflict) as exc:
            reason = friendly_reason(str(exc)) if isinstance(exc, OrderConflict) else "订单信息的格式还需要核对"
            return f"这笔资料暂时还不能入账：{reason}。\n请在首行写 /更正 {review['id']}，下一行填写正确的信息。"
