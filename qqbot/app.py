import asyncio
import csv
import hmac
import io
import json
import re
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .domain import OrderConflict, OrderInput, now
from .extraction import Extractor
from .events import message_key
from .gateway import Gateway
from .ledger import Ledger
from .notifications import Notifier
from .qq import QQClient
from .reconcile import Reconciler
from .reports import Reports
from .security import challenge, verify_callback
from .service import Service
from .storage import Store, dumps


class ManualOrder(BaseModel):
    customer_id: str
    order: OrderInput
    verification_note: str = Field(min_length=3, max_length=1000)


class ReviewDecision(BaseModel):
    accept: bool
    corrected_order: OrderInput | None = None
    verification_note: str = Field(min_length=3, max_length=1000)


class IdentityLink(BaseModel):
    source_customer_id: str
    target_customer_id: str
    verification_note: str = Field(min_length=5, max_length=1000)


class CustomerProfile(BaseModel):
    qq_number: str = Field(pattern=r"^[1-9][0-9]{4,11}$")
    verification_note: str = Field(min_length=3, max_length=1000)


def create_app(settings: Settings | None = None, transport=None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        http = httpx.AsyncClient(transport=transport, follow_redirects=False)
        store = Store(settings.db_path)
        ledger = Ledger(store)
        extractor = Extractor(settings, http)
        qq = QQClient(settings, http)
        notifier = Notifier(store, qq, settings)
        reconciler = Reconciler(store, ledger, settings, http)
        reports = Reports(store, settings)
        service = Service(store, ledger, extractor, notifier, reconciler, reports, settings)
        gateway = Gateway(store, qq, settings)
        app.state.store, app.state.service = store, service
        app.state.gateway = gateway
        store.recover()
        tasks = []
        if settings.worker_enabled:
            tasks.append(asyncio.create_task(service.run()))
            if settings.qq_transport == "websocket" and settings.qq_gateway_enabled:
                tasks.append(asyncio.create_task(gateway.run()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await http.aclose()

    app = FastAPI(title="QQ 订单机器人管理 API", version="0.1.0", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def admin(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if not settings.admin_token:
            raise HTTPException(503, "请先在 .env 配置 ADMIN_TOKEN")
        if not credentials or not hmac.compare_digest(credentials.credentials, settings.admin_token):
            raise HTTPException(401, "管理员身份验证失败", headers={"WWW-Authenticate": "Bearer"})

    def store() -> Store:
        return app.state.store

    def service() -> Service:
        return app.state.service

    @app.exception_handler(OrderConflict)
    async def order_conflict(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/docs")

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "mode": "dry_run" if settings.qq_dry_run else "live"}

    @app.post("/webhooks/qq")
    async def callback(request: Request):
        # 验签使用原始字节，不能先 JSON 重排再验。
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                raise HTTPException(413, "回调包过大")
        body = bytes(body)
        timestamp = request.headers.get("X-Signature-Timestamp", "")
        signature = request.headers.get("X-Signature-Ed25519", "")
        if not verify_callback(settings.qq_app_secret, timestamp, signature, body):
            raise HTTPException(401, "QQ 回调签名无效或时间戳过期")
        app_id = request.headers.get("X-Bot-Appid")
        if app_id and app_id != settings.qq_app_id:
            raise HTTPException(401, "AppID 不匹配")
        try:
            event = json.loads(body)
            if not isinstance(event, dict):
                raise ValueError("回调不是对象")
            if event.get("op") == 13:
                return challenge(settings.qq_app_secret, event["d"])
            if event.get("op") == 1:
                return {"op": 11, "d": event.get("d", 0)}
            key = message_key(event)
            if key is None:
                return {"op": 12, "d": 0}
            store().enqueue_event(key, event)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise HTTPException(400, "回调数据不完整") from None
        # 事件持久化后立刻 ACK；模型和 Excel 工作由后台消费。
        return {"op": 12, "d": 0}

    @app.get("/admin/status", dependencies=[Depends(admin)])
    async def status():
        return {"dry_run": settings.qq_dry_run,
                "vision_configured": bool(settings.model_base_url and settings.model_api_key and settings.vision_model),
                "commerce_configured": bool(settings.commerce_base_url),
                "chat_enabled": settings.chat_enabled, "worker_enabled": settings.worker_enabled,
                "qq_transport": settings.qq_transport, "gateway": app.state.gateway.snapshot(),
                "report_error": store().get_meta("report_error"),
                "last_reconcile_attempt": store().get_meta("last_reconcile_attempt"),
                "last_full_reconcile_completed": store().get_meta("last_full_reconcile_completed"),
                "events": store().rows("SELECT state,COUNT(*) count FROM events GROUP BY state"),
                "notifications": store().rows("SELECT state,COUNT(*) count FROM notifications GROUP BY state")}

    @app.get("/admin/customers", dependencies=[Depends(admin)])
    async def customers():
        return {"customers": store().totals(), "identities": store().rows("SELECT * FROM identities")}

    @app.patch("/admin/customers/{customer_id}", dependencies=[Depends(admin)])
    async def customer_profile(customer_id: str, body: CustomerProfile):
        async with service().lock:
            with store().transaction() as db:
                old = db.execute("SELECT qq_number FROM customers WHERE id=?", (customer_id,)).fetchone()
                if not old:
                    raise HTTPException(404, "客户不存在")
                db.execute("UPDATE customers SET qq_number=? WHERE id=?", (body.qq_number, customer_id))
                db.execute("INSERT INTO audit(action,customer_id,before_json,after_json,source,at) VALUES (?,?,?,?,?,?)",
                           ("customer_qq", customer_id, dumps({"qq_number": old[0]}), dumps({"qq_number": body.qq_number}), "admin:" + body.verification_note, now()))
            await service().export()
        return {"customer_id": customer_id, "qq_number": body.qq_number}

    @app.get("/admin/orders", dependencies=[Depends(admin)])
    async def orders(customer_id: str | None = None, limit: int = 500, offset: int = 0):
        limit, offset = max(1, min(limit, 2000)), max(0, offset)
        return store().rows("SELECT * FROM orders " + ("WHERE customer_id=? " if customer_id else "") + "ORDER BY id LIMIT ? OFFSET ?",
                            ((customer_id,) if customer_id else ()) + (limit, offset))

    @app.post("/admin/orders", dependencies=[Depends(admin)])
    async def manual_order(body: ManualOrder):
        async with service().lock:
            result = service().ledger.apply(body.customer_id, body.order, "admin:" + body.verification_note, verified=True)
            await service().export()
            return result

    @app.get("/admin/reviews", dependencies=[Depends(admin)])
    async def reviews():
        return store().rows("SELECT * FROM reviews WHERE state='pending' ORDER BY id")

    @app.post("/admin/reviews/{review_id}", dependencies=[Depends(admin)])
    async def review(review_id: int, body: ReviewDecision):
        async with service().lock:
            rows = store().rows("SELECT * FROM reviews WHERE id=?", (review_id,))
            if not rows:
                raise HTTPException(404, "记录不存在")
            item = rows[0]
            if item["state"] != "pending":
                raise HTTPException(409, "该记录已处理")
            if body.accept:
                try:
                    raw = body.corrected_order.model_dump(mode="json", exclude_none=True) if body.corrected_order else json.loads(item["draft"])
                    if service().submissions.reason(item["customer_id"], raw) != "请核对后确认入账":
                        raise HTTPException(422, service().submissions.reason(item["customer_id"], raw))
                    draft = OrderInput.model_validate({k: v for k, v in raw.items() if k != "qq_number"})
                except ValidationError:
                    raise HTTPException(422, "识别数据不完整，请提供 corrected_order") from None
                result = service().ledger.apply(item["customer_id"], draft, "admin_review:" + body.verification_note,
                                                verified=True, review_id=review_id)
            else:
                with store().transaction() as db:
                    db.execute("UPDATE reviews SET state='rejected',resolved_at=? WHERE id=?", (now(), review_id))
                    db.execute("INSERT INTO audit(action,customer_id,after_json,source,at) VALUES (?,?,?,?,?)",
                               ("review_reject", item["customer_id"], dumps({"review_id": review_id}), "admin:" + body.verification_note, now()))
                result = {"state": "rejected"}
            await service().export()
            return result

    @app.post("/admin/identities/link", dependencies=[Depends(admin)])
    async def link(body: IdentityLink):
        async with service().lock:
            try:
                store().link_customers(body.source_customer_id, body.target_customer_id, body.verification_note)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            await service().export()
            return {"customer_id": body.target_customer_id}

    @app.get("/admin/reconcile", dependencies=[Depends(admin)])
    async def checks():
        return store().rows("SELECT o.*,c.reason,c.checked_at FROM review_checks c JOIN orders o ON o.id=c.order_id")

    @app.post("/admin/reconcile", dependencies=[Depends(admin)])
    async def reconcile():
        async with service().lock:
            result = await service().reconciler.run()
            await service().export()
            return result

    @app.post("/admin/import/csv", dependencies=[Depends(admin)])
    async def import_csv(file: UploadFile = File(...)):
        content = await file.read(5 * 1024 * 1024 + 1)
        if len(content) > 5 * 1024 * 1024:
            raise HTTPException(413, "CSV 最大 5MB")
        try:
            reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
            records = list(reader)
        except (UnicodeDecodeError, csv.Error):
            raise HTTPException(400, "请上传 UTF-8 编码的 CSV") from None
        required = {"订单号", "店铺", "订单状态", "退款金额"}
        if not required.issubset(reader.fieldnames or []):
            raise HTTPException(422, "CSV 必须包含：订单号、店铺、订单状态、退款金额")
        if len(records) > 5000:
            raise HTTPException(413, "每次最多导入 5000 行")
        mapping = {"订单号": "order_no", "店铺": "shop", "下单日期": "ordered_on", "下单金额": "order_amount",
                   "实付款": "paid_amount", "订单状态": "status", "退款金额": "refund_amount"}
        result = {"verified": 0, "changed": 0, "errors": []}
        async with service().lock:
            for number, record in enumerate(records, 2):
                try:
                    data = OrderInput.model_validate({target: record[name].strip() for name, target in mapping.items() if (record.get(name) or "").strip()})
                    if data.status is None or data.refund_amount is None or not data.shop:
                        raise ValueError("核对行必须有店铺、订单状态和累计退款金额（未退款填 0）")
                    matches = store().rows("SELECT * FROM orders WHERE shop=? AND order_no=?", (data.shop, data.order_no))
                    if not matches:
                        raise ValueError("账本中无此订单，无法安全判定客户归属；请先由客户提交")
                    applied = service().ledger.apply(matches[0]["customer_id"], data, "store_csv", verified=True)
                    result["verified"] += 1
                    result["changed"] += int(applied["changed"])
                except (ValidationError, ValueError) as exc:
                    reason = "字段格式错误" if isinstance(exc, ValidationError) else str(exc)
                    result["errors"].append({"row": number, "reason": reason})
            await service().export()
        return result

    @app.get("/admin/notifications", dependencies=[Depends(admin)])
    async def notifications(limit: int = 200):
        return store().rows("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),))

    @app.post("/admin/notifications/{notice_id}/retry", dependencies=[Depends(admin)])
    async def retry_notice(notice_id: int):
        with store().transaction() as db:
            row = db.execute("SELECT * FROM notifications WHERE id=?", (notice_id,)).fetchone()
            if not row:
                raise HTTPException(404, "通知不存在")
            if row["state"] not in {"failed", "waiting"}:
                raise HTTPException(409, "只有失败或等待中的通知可重试")
            db.execute("UPDATE notifications SET state='pending',attempts=0,available_at=0 WHERE id=?", (notice_id,))
        return {"state": "pending"}

    @app.get("/admin/events", dependencies=[Depends(admin)])
    async def events(limit: int = 100, include_payload: bool = False):
        columns = "id,type,state,attempts,error,created_at" + (",payload" if include_payload else "")
        return {"events": store().rows(f"SELECT {columns} FROM events ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 500)),)),
                "evidence": store().rows("SELECT * FROM evidence")}

    @app.post("/admin/events/{event_id:path}/retry", dependencies=[Depends(admin)])
    async def retry_event(event_id: str):
        with store().transaction() as db:
            updated = db.execute("UPDATE events SET state='pending',attempts=0,available_at=0 WHERE id=? AND state='dead'", (event_id,))
            if not updated.rowcount:
                raise HTTPException(409, "事件不存在或不处于失败状态")
        return {"state": "pending"}

    @app.get("/admin/audit", dependencies=[Depends(admin)])
    async def audit(limit: int = 200):
        return store().rows("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),))

    @app.get("/admin/export.xlsx", dependencies=[Depends(admin)])
    async def export():
        async with service().lock:
            try:
                path = await asyncio.to_thread(service().reports.export)
            except PermissionError:
                raise HTTPException(409, "Excel 文件被占用，请关闭本地 Excel 后重试") from None
            return FileResponse(path, filename="orders.xlsx", media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.get("/admin/evidence/{digest}", dependencies=[Depends(admin)])
    async def evidence(digest: str):
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise HTTPException(404)
        path = settings.data_dir / "evidence" / digest
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="application/octet-stream", filename=f"{digest}.image")

    return app


app = create_app()
