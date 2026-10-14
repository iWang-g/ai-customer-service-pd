import json
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .domain import now


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS customers(id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS identities(
 key TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
 scene TEXT NOT NULL, target_id TEXT NOT NULL, subject_id TEXT NOT NULL,
 last_msg_id TEXT NOT NULL, last_event_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS orders(
 id INTEGER PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES customers(id),
 order_no TEXT NOT NULL, shop TEXT NOT NULL, ordered_on TEXT NOT NULL,
 order_cents INTEGER NOT NULL, paid_cents INTEGER NOT NULL, refund_cents INTEGER NOT NULL,
 status TEXT NOT NULL, net_cents INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 source TEXT NOT NULL, updated_at TEXT NOT NULL, verified_at TEXT,
 UNIQUE(shop, order_no));
CREATE INDEX IF NOT EXISTS orders_customer ON orders(customer_id);
CREATE TABLE IF NOT EXISTS events(
 id TEXT PRIMARY KEY, type TEXT NOT NULL, payload TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 available_at REAL NOT NULL DEFAULT 0, error TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(
 id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, item_index INTEGER NOT NULL,
 customer_id TEXT NOT NULL REFERENCES customers(id), draft TEXT NOT NULL,
 reason TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 created_at TEXT NOT NULL, resolved_at TEXT, UNIQUE(event_id, item_index));
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY, action TEXT NOT NULL, customer_id TEXT,
 order_id INTEGER, before_json TEXT, after_json TEXT, source TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(
 id INTEGER PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
 customer_id TEXT NOT NULL REFERENCES customers(id), content TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', identity_key TEXT, msg_id TEXT,
 msg_seq INTEGER, msg_event_at REAL, attempts INTEGER NOT NULL DEFAULT 0, available_at REAL NOT NULL DEFAULT 0,
 error TEXT, created_at TEXT NOT NULL, sent_at TEXT);
CREATE TABLE IF NOT EXISTS reply_sequences(
 identity_key TEXT NOT NULL, msg_id TEXT NOT NULL, seq INTEGER NOT NULL,
 PRIMARY KEY(identity_key, msg_id));
CREATE TABLE IF NOT EXISTS review_checks(
 order_id INTEGER PRIMARY KEY REFERENCES orders(id), reason TEXT NOT NULL, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(event_id,digest));
"""


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


class Store:
    def __init__(self, path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.read() as db:
            db.executescript(SCHEMA)
            # Additive, repeatable migration; copied ledgers retain all existing data.
            for table, name, definition in (
                ("customers", "qq_number", "TEXT"),
                ("orders", "taobao_account", "TEXT"),
                ("reviews", "origin", "TEXT NOT NULL DEFAULT 'ai'"),
            ):
                if name not in {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            db.commit()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def read(self):
        db = self.connect()
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.read() as db:
            return [dict(row) for row in db.execute(sql, args)]

    def identity(self, event: dict) -> dict:
        data = event["d"]
        scene = "c2c" if event["t"] == "C2C_MESSAGE_CREATE" else "group"
        subject = data["author"]["user_openid" if scene == "c2c" else "member_openid"]
        target = subject if scene == "c2c" else data["group_openid"]
        # 群成员 ID 与单聊 ID 不假设相等；由管理员核实后绑定。
        key = f"c2c:{subject}" if scene == "c2c" else f"group:{target}:{subject}"
        try:
            from datetime import datetime
            received = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, TypeError):
            received = time.time()
        with self.transaction() as db:
            found = db.execute("SELECT * FROM identities WHERE key=?", (key,)).fetchone()
            if not found:
                cid = uuid.uuid4().hex
                db.execute("INSERT INTO customers(id,created_at) VALUES (?,?)", (cid, now()))
                db.execute("INSERT INTO identities VALUES (?,?,?,?,?,?,?)",
                           (key, cid, scene, target, subject, data["id"], received))
            elif received >= found["last_event_at"]:
                db.execute("UPDATE identities SET last_msg_id=?,last_event_at=? WHERE key=?",
                           (data["id"], received, key))
            return dict(db.execute("SELECT * FROM identities WHERE key=?", (key,)).fetchone())

    def enqueue_event(self, key, event):
        with self.transaction() as db:
            result = db.execute("INSERT OR IGNORE INTO events(id,type,payload,created_at) VALUES (?,?,?,?)",
                                (key, event["t"], dumps(event), now()))
            return result.rowcount == 1

    def gateway_checkpoint(self, name, checkpoint, event=None, key=None, malformed=False):
        # 同一事务提交业务事件与恢复序号，避免重启后跳过未入队消息。
        with self.transaction() as db:
            if event is not None:
                db.execute("INSERT OR IGNORE INTO events(id,type,payload,state,error,created_at) VALUES (?,?,?,?,?,?)",
                           (key, event["t"], dumps(event), "dead" if malformed else "pending",
                            "invalid_message_envelope" if malformed else None, now()))
            db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (name, dumps(checkpoint)))

    def claim_event(self):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM events WHERE state='pending' AND available_at<=? "
                             "ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
            if row:
                db.execute("UPDATE events SET state='processing',attempts=attempts+1 WHERE id=?", (row["id"],))
                return dict(row)

    def finish_event(self, key, error=None):
        with self.transaction() as db:
            if error is None:
                db.execute("UPDATE events SET state='done',error=NULL WHERE id=?", (key,))
            else:
                attempts = db.execute("SELECT attempts FROM events WHERE id=?", (key,)).fetchone()[0]
                db.execute("UPDATE events SET state=?,error=?,available_at=? WHERE id=?",
                           ("dead" if attempts >= 5 else "pending", error[:500], time.time() + 2 ** attempts, key))

    def recover(self):
        # 仅支持单进程 worker。重启恢复持久任务和未确认发送。
        with self.transaction() as db:
            db.execute("UPDATE events SET state='pending' WHERE state='processing'")
            db.execute("UPDATE notifications SET state='pending' WHERE state='sending'")

    def review(self, event_id, index, cid, draft, reason, origin="ai"):
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO reviews(event_id,item_index,customer_id,draft,reason,created_at,origin) "
                       "VALUES (?,?,?,?,?,?,?)", (event_id, index, cid, dumps(draft), reason[:1000], now(), origin))
            return db.execute("SELECT id FROM reviews WHERE event_id=? AND item_index=?", (event_id, index)).fetchone()[0]

    def register_qq(self, cid, qq_number):
        if not qq_number:
            return
        with self.transaction() as db:
            old = db.execute("SELECT qq_number FROM customers WHERE id=?", (cid,)).fetchone()[0]
            if old and old != qq_number:
                raise ValueError("您提供的 QQ 号与此前登记的不一致，请联系管理员核实修改。")
            if old != qq_number:
                db.execute("UPDATE customers SET qq_number=? WHERE id=?", (qq_number, cid))
                db.execute("INSERT INTO audit(action,customer_id,before_json,after_json,source,at) VALUES (?,?,?,?,?,?)",
                           ("customer_qq", cid, dumps({"qq_number": old}), dumps({"qq_number": qq_number}), "customer_provided", now()))

    def update_review(self, review_id, cid, draft, reason, event_id):
        with self.transaction() as db:
            old = db.execute("SELECT * FROM reviews WHERE id=? AND customer_id=? AND state='pending'", (review_id, cid)).fetchone()
            if not old:
                raise ValueError("这条记录已处理或不属于您，请核对编号。")
            db.execute("UPDATE reviews SET draft=?,reason=? WHERE id=?", (dumps(draft), reason[:1000], review_id))
            db.execute("INSERT OR IGNORE INTO evidence SELECT ?,digest FROM evidence WHERE event_id=?", (old["event_id"], event_id))
            if old["draft"] != dumps(draft) or old["reason"] != reason[:1000]:
                db.execute("INSERT INTO audit(action,customer_id,before_json,after_json,source,at) VALUES (?,?,?,?,?,?)",
                           ("submission_supplement", cid, old["draft"], dumps(draft), f"qq:{event_id}", now()))

    @staticmethod
    def notify_in(db, key, cid, content):
        db.execute("INSERT OR IGNORE INTO notifications(dedupe_key,customer_id,content,created_at) VALUES (?,?,?,?)",
                   (key, cid, content[:1500], now()))

    def notify(self, key, cid, content):
        with self.transaction() as db:
            self.notify_in(db, key, cid, content)

    def totals(self):
        return self.rows("SELECT c.id AS customer_id,c.qq_number,COUNT(o.id) AS order_count,"
                         "COALESCE(SUM(o.paid_cents),0) AS paid_cents,"
                         "COALESCE(SUM(o.refund_cents),0) AS refund_cents,"
                         "COALESCE(SUM(o.net_cents),0) AS net_cents FROM customers c "
                         "LEFT JOIN orders o ON o.customer_id=c.id GROUP BY c.id ORDER BY c.created_at")

    def link_customers(self, source_id, target_id, reason):
        if source_id == target_id or not reason.strip():
            raise ValueError("请选择不同客户并填写身份核验依据")
        with self.transaction() as db:
            if db.execute("SELECT COUNT(*) FROM customers WHERE id IN (?,?)", (source_id, target_id)).fetchone()[0] != 2:
                raise ValueError("客户不存在")
            source_qq = db.execute("SELECT qq_number FROM customers WHERE id=?", (source_id,)).fetchone()[0]
            target_qq = db.execute("SELECT qq_number FROM customers WHERE id=?", (target_id,)).fetchone()[0]
            if source_qq and target_qq and source_qq != target_qq:
                raise ValueError("两位客户登记的 QQ 号不同，请先核实并更正资料")
            db.execute("UPDATE customers SET qq_number=COALESCE(qq_number,?) WHERE id=?", (source_qq, target_id))
            for table in ("identities", "orders", "reviews", "notifications"):
                db.execute(f"UPDATE {table} SET customer_id=? WHERE customer_id=?", (target_id, source_id))
            db.execute("INSERT INTO audit(action,customer_id,before_json,after_json,source,at) VALUES (?,?,?,?,?,?)",
                       ("link_identity", target_id, dumps({"source": source_id}), dumps({"target": target_id, "reason": reason}), "admin", now()))
            db.execute("DELETE FROM customers WHERE id=?", (source_id,))

    def get_meta(self, key):
        rows = self.rows("SELECT value FROM metadata WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key, value):
        with self.transaction() as db:
            db.execute("INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
