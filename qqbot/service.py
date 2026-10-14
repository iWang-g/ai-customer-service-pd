import asyncio
import json
import logging
import re

from .extraction import ExtractionError
from .submissions import Submissions


log = logging.getLogger(__name__)
HELP = """您好，发来订单截图，或填写下面的模板，我就能帮您整理订单和累计消费啦！

QQ号：
淘宝账号名：
店铺：示例旗舰店
订单号：001234567890
下单日期：2026-10-13
下单金额：129.00
实付款：99.00
订单状态：待收货

有些信息暂时没有也没关系，先发已有的，我会提醒您补充。
QQ号登记一次即可；淘宝账号名请填写这笔订单实际使用的账号。
更新退款时请提供订单号、店铺、订单状态和累计已到账退款金额。

/消费：查看累计消费
/补充 编号：下一行填写缺少的信息
/更正 编号：下一行填写要修改的信息
/确认 编号：核对后入账
/拒绝 编号：取消这条待处理资料
群聊请先 @我。"""


class Service:
    def __init__(self, store, ledger, extractor, notifier, reconciler, reports, settings):
        self.store, self.ledger, self.extractor = store, ledger, extractor
        self.notifier, self.reconciler, self.reports, self.settings = notifier, reconciler, reports, settings
        self.lock = asyncio.Lock()
        self.submissions = Submissions(store, ledger, settings)

    def reply(self, event_id, cid, text):
        if not text:
            return
        # Split on line boundaries; preserve all previews and instructions instead of truncating.
        chunks, current = [], ""
        for line in text.splitlines(keepends=True):
            while len(line) > 1200:
                if current:
                    chunks.append(current.rstrip())
                    current = ""
                chunks.append(line[:1200])
                line = line[1200:]
            if len(current) + len(line) > 1200:
                chunks.append(current.rstrip())
                current = ""
            current += line
        if current:
            chunks.append(current.rstrip())
        for index, chunk in enumerate(chunks):
            key = f"reply:{event_id}" + (f":part{index}" if index else "")
            self.store.notify(key, cid, chunk)

    async def process(self, event_id, event):
        identity = self.store.identity(event)
        cid = identity["customer_id"]
        self.notifier.wake_customer(cid)
        text = re.sub(r"<@!?[^>]+>", "", event["d"].get("content", "")).strip()
        if text in {"帮助", "/帮助", "/help", "help"}:
            self.reply(event_id, cid, HELP)
            return
        if text in {"消费", "/消费", "我的消费"}:
            total = next(r for r in self.store.totals() if r["customer_id"] == cid)
            self.reply(event_id, cid, f"帮您查好啦！\n\n已记录订单：{total['order_count']} 笔\n累计有效消费：{total['net_cents']/100:.2f} 元\n\n待补充或未确认的资料暂时不计入消费。")
            return
        command = re.fullmatch(r"/?(确认|拒绝)\s+(\d+)", text)
        if command:
            rows = self.store.rows("SELECT * FROM reviews WHERE id=? AND customer_id=?", (int(command[2]), cid))
            if not rows or rows[0]["state"] != "pending":
                self.reply(event_id, cid, "没有找到属于您的待处理记录，可能已经确认或取消啦，请核对一下编号。")
                return
            review = rows[0]
            if command[1] == "拒绝":
                from .domain import now
                with self.store.transaction() as db:
                    db.execute("UPDATE reviews SET state='rejected',resolved_at=? WHERE id=?", (now(), review["id"]))
                self.reply(event_id, cid, "这条待处理资料已取消，没有计入消费。\n需要重新登记时，发来截图或订单文字就好。")
            else:
                self.reply(event_id, cid, self.submissions.confirm(cid, review))
            return
        supplement = re.match(r"^/?(补充|更正)\s+(\d+)(?:\s+|$)", text)
        target = int(supplement[2]) if supplement else None
        correct = bool(supplement and supplement[1] == "更正")
        if supplement:
            text = text[supplement.end():].strip()
        try:
            drafts = await self.extractor.extract(text, event["d"].get("attachments", []))
        except ExtractionError as exc:
            if target is not None:
                self.reply(event_id, cid, "收到图片啦，不过这次暂时没能读取成功，原资料已保留。\n请稍后补发清晰图片，或用文字填写缺少的信息。")
            else:
                number = self.store.review(event_id, 0, cid, {}, str(exc))
                self.reply(event_id, cid, f"收到图片啦，不过这次暂时没能读取成功，订单还没有入账。\n\n已保存待核验记录，编号：{number}。\n您可以发送 /补充 {number}，下一行填写已有的订单信息，也可以补发截图。")
            return
        finally:
            with self.store.transaction() as db:
                for digest in self.extractor.evidence:
                    db.execute("INSERT OR IGNORE INTO evidence VALUES (?,?)", (event_id, digest))
        if not drafts:
            if target is not None:
                self.reply(event_id, cid, f"请在 /补充 {target} 或 /更正 {target} 的下一行填写资料，例如：\n淘宝账号名：您的账号\n下单金额：120.00")
                return
            if self.settings.chat_enabled and text:
                try:
                    answer = await self.extractor.chat(text)
                except ExtractionError:
                    answer = "这会儿暂时没能连接上聊天服务。\n您可以先发订单资料，或发送‘帮助’查看用法。"
                self.reply(event_id, cid, answer)
            else:
                self.reply(event_id, cid, "您好，发来订单截图或订单文字，我就能帮您整理啦。\n发送‘帮助’可以查看填写模板。")
            return
        self.reply(event_id, cid, self.submissions.receive(event_id, cid, drafts, target, correct))

    async def export(self):
        try:
            await asyncio.to_thread(self.reports.export)
            self.store.set_meta("report_error", "")
        except Exception as exc:
            self.store.set_meta("report_error", type(exc).__name__)
            log.error("Excel export failed: %s", type(exc).__name__)

    async def tick(self):
        async with self.lock:
            row = self.store.claim_event()
            if row:
                try:
                    await self.process(row["id"], json.loads(row["payload"]))
                    self.store.finish_event(row["id"])
                except Exception as exc:
                    self.store.finish_event(row["id"], type(exc).__name__)
                    log.error("Event processing failed: %s", type(exc).__name__)
                    if row["attempts"] >= 4:
                        event = json.loads(row["payload"])
                        cid = self.store.identity(event)["customer_id"]
                        self.store.review(row["id"], 999, cid, {}, "事件处理失败：" + type(exc).__name__)
                        self.reply(row["id"], cid, "处理暂时失败，已转人工核验，请稍后重试。")
            reconciled = self.reconciler.is_due()
            if reconciled:
                await self.reconciler.run()
            sent = False
            # 每轮限制消息数量，避免超量重试和长时间占用。
            for _ in range(4):
                if not await self.notifier.flush_one():
                    break
                sent = True
            if row or sent or reconciled or self.store.get_meta("report_error") or not self.reports.path.exists():
                await self.export()

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Background worker failed: %s", type(exc).__name__)
            await asyncio.sleep(1)
