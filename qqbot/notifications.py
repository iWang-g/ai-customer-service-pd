import time

import httpx

from .domain import now
from .qq import SendError


class Notifier:
    def __init__(self, store, qq, settings):
        self.store, self.qq, self.settings = store, qq, settings

    def window(self, identity):
        return self.settings.c2c_reply_window_seconds if identity["scene"] == "c2c" else self.settings.reply_window_seconds

    async def flush_one(self):
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM notifications WHERE state='pending' AND available_at<=? ORDER BY id LIMIT 1",
                             (time.time(),)).fetchone()
            if not row:
                return False
            notification = dict(row)
            # 已分配的目的地与序号保持稳定，网络超时重试不分配新序号。
            identity = db.execute("SELECT * FROM identities WHERE key=?", (row["identity_key"],)).fetchone() if row["identity_key"] else None
            if identity and row["msg_id"] and time.time() - (row["msg_event_at"] or 0) > self.window(identity):
                identity = None
                # 旧 msg_id 不应转成主动推送重发，避免结果不明确时重复发送。
                db.execute("UPDATE notifications SET state='waiting',error='原回复窗口已过期，需人工核实发送结果' WHERE id=?", (row["id"],))
                return True
            if not identity:
                identity = db.execute("SELECT * FROM identities WHERE customer_id=? ORDER BY last_event_at DESC LIMIT 1", (row["customer_id"],)).fetchone()
            if not identity:
                db.execute("UPDATE notifications SET state='waiting',error='没有可发送的 QQ 会话' WHERE id=?", (row["id"],))
                return True
            identity = dict(identity)
            recent = time.time() - identity["last_event_at"] < self.window(identity)
            if not recent:
                db.execute("UPDATE notifications SET state='waiting',error='QQ 主动推送已停止，等待客户下次发消息' WHERE id=?", (row["id"],))
                return True
            msg_id = notification["msg_id"] or (identity["last_msg_id"] if recent else None)
            seq = notification["msg_seq"]
            if seq is None:
                if msg_id:
                    previous = db.execute("SELECT seq FROM reply_sequences WHERE identity_key=? AND msg_id=?", (identity["key"], msg_id)).fetchone()
                    if previous and previous[0] >= 5:
                        db.execute("UPDATE notifications SET state='waiting',error='本条 QQ 消息的 5 次回复额度用尽，等待下一条消息' WHERE id=?", (row["id"],))
                        return True
                    db.execute("INSERT INTO reply_sequences VALUES (?,?,1) ON CONFLICT(identity_key,msg_id) DO UPDATE SET seq=seq+1", (identity["key"], msg_id))
                    seq = db.execute("SELECT seq FROM reply_sequences WHERE identity_key=? AND msg_id=?", (identity["key"], msg_id)).fetchone()[0]
                else:
                    seq = 1
            # group 消息加上明确的接收人，防止多个客户的通知无法区分。
            content = notification["content"]
            if identity["scene"] == "group":
                content = f"<@{identity['subject_id']}> " + content
            db.execute("UPDATE notifications SET state='sending',identity_key=?,msg_id=?,msg_seq=?,msg_event_at=?,attempts=attempts+1 WHERE id=?",
                       (identity["key"], msg_id, seq, notification["msg_event_at"] or identity["last_event_at"], row["id"]))
        try:
            state = await self.qq.send(identity, content, msg_id, seq)
            with self.store.transaction() as db:
                db.execute("UPDATE notifications SET state=?,sent_at=?,error=NULL WHERE id=?", (state, now(), row["id"]))
        except (SendError, httpx.HTTPError) as exc:
            # 不记录响应 body、密钥或带 token 的请求 URL。
            retryable = isinstance(exc, httpx.TransportError) or getattr(exc, "retryable", False)
            attempts = row["attempts"] + 1
            state = "pending" if retryable and attempts < 5 else "failed"
            error = str(exc)[:300] if isinstance(exc, SendError) else type(exc).__name__
            with self.store.transaction() as db:
                db.execute("UPDATE notifications SET state=?,error=?,available_at=? WHERE id=?",
                           (state, error, time.time() + min(300, 2 ** attempts * 5), row["id"]))
        return True

    def wake_customer(self, cid):
        with self.store.transaction() as db:
            db.execute("UPDATE notifications SET state='pending',identity_key=NULL,msg_id=NULL,msg_seq=NULL,msg_event_at=NULL,attempts=0,available_at=0 "
                       "WHERE customer_id=? AND state='waiting' AND msg_seq IS NULL", (cid,))
