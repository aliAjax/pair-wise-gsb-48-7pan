"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    organization TEXT NOT NULL DEFAULT 'DEFAULT',
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    organization TEXT NOT NULL,
                    account TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    settlement_day INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    frozen_total REAL,
                    settlement_held INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    confirmed_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    reference TEXT NOT NULL,
                    side TEXT NOT NULL,
                    instruction_version INTEGER NOT NULL,
                    frozen_quantity INTEGER,
                    frozen_net_amount REAL,
                    frozen_ca_version INTEGER,
                    matched_receipt_id INTEGER,
                    UNIQUE(batch_id, reference)
                );
                CREATE TABLE IF NOT EXISTS custodian_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    custodian_receipt_id TEXT NOT NULL UNIQUE,
                    batch_id INTEGER REFERENCES batches(id),
                    record_id INTEGER,
                    reference TEXT NOT NULL,
                    received_version INTEGER NOT NULL,
                    delivered_quantity INTEGER NOT NULL,
                    net_amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    reasons TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipt_discrepancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    receipt_id INTEGER NOT NULL REFERENCES custodian_receipts(id),
                    reference TEXT NOT NULL,
                    reasons TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_batches_state ON batches(state);
                CREATE INDEX IF NOT EXISTS idx_batches_org ON batches(organization, id);
                CREATE INDEX IF NOT EXISTS idx_items_record ON batch_items(record_id);
                CREATE INDEX IF NOT EXISTS idx_items_batch ON batch_items(batch_id, ordinal);
                CREATE INDEX IF NOT EXISTS idx_receipts_batch ON custodian_receipts(batch_id);
                CREATE INDEX IF NOT EXISTS idx_batch_audit ON batch_audit_events(batch_id, id);
                """
            )
            # 既有库补机构列
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(records)").fetchall()}
            if "organization" not in columns:
                connection.execute("ALTER TABLE records ADD COLUMN organization TEXT NOT NULL DEFAULT 'DEFAULT'")

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    # ------------------------------------------------------------------
    # 结算指令
    # ------------------------------------------------------------------

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str, organization: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,organization,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (reference, state, 1, organization, self._dumps(payload), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, self._dumps({"state": state}), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def get_by_reference(self, reference: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE reference=?", (reference,)).fetchone()
        return self._row(row) if row is not None else None

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, self._dumps(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, self._dumps(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), self._dumps(details), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    # ------------------------------------------------------------------
    # 净额批次
    # ------------------------------------------------------------------

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        batch = dict(row)
        batch["settlement_held"] = bool(batch["settlement_held"])
        return batch

    def get_batch(self, batch_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        return self._batch_row(row) if row is not None else None

    def get_batch_by_no(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
        return self._batch_row(row) if row is not None else None

    def list_batches(self, organization: Optional[str] = None, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM batches"
        where, params = [], []
        if organization:
            where.append("organization=?")
            params.append(organization)
        if state:
            where.append("state=?")
            params.append(state)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._batch_row(row) for row in rows]

    def batch_items(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM batch_items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def batch_detail(self, batch_id: int) -> Dict[str, Any]:
        batch = self.get_batch(batch_id)
        if batch is None:
            raise NotFound("批次不存在")
        batch["items"] = self.batch_items(batch_id)
        with self._connect() as connection:
            receipt_rows = connection.execute(
                "SELECT * FROM custodian_receipts WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
            diff_rows = connection.execute(
                "SELECT * FROM receipt_discrepancies WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        receipts, discrepancies = [], []
        for row in receipt_rows:
            item = dict(row)
            item["reasons"] = json.loads(item["reasons"])
            receipts.append(item)
        for row in diff_rows:
            item = dict(row)
            item["reasons"] = json.loads(item["reasons"])
            discrepancies.append(item)
        batch["receipts"] = receipts
        batch["discrepancies"] = discrepancies
        return batch

    def add_batch_audit(self, connection: sqlite3.Connection, batch_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO batch_audit_events(batch_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
            (batch_id, action, actor_id, self._dumps(details), _now()),
        )

    def append_batch_audit(self, batch_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            self.add_batch_audit(connection, batch_id, actor_id, action, details)

    def active_batch_for_record(self, record_id: int, organization: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT b.* FROM batches b JOIN batch_items bi ON bi.batch_id=b.id "
                "WHERE bi.record_id=? AND b.organization=? AND b.state IN ('confirmed','open') "
                "ORDER BY CASE b.state WHEN 'confirmed' THEN 0 ELSE 1 END, b.id DESC LIMIT 1",
                (record_id, organization),
            ).fetchone()
        return self._batch_row(row) if row is not None else None

    def create_batch(self, batch_no: str, organization: str, account: str, currency: str, settlement_day: int, items: List[Dict[str, Any]], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO batches(batch_no,organization,account,currency,settlement_day,state,version,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (batch_no, organization, account, currency, int(settlement_day), "open", 1, actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("批次号已存在，重复记账被拒绝") from exc
            batch_id = int(cursor.lastrowid)
            for ordinal, item in enumerate(items):
                connection.execute(
                    "INSERT INTO batch_items(batch_id,ordinal,record_id,reference,side,instruction_version) VALUES(?,?,?,?,?,?)",
                    (batch_id, ordinal, item["record_id"], item["reference"], item["side"], item["instruction_version"]),
                )
            self.add_batch_audit(connection, batch_id, actor_id, "batch_composed", {
                "batch_no": batch_no, "references": [item["reference"] for item in items],
            })
            # 组批前到达的回执挂接到新批次
            pending = connection.execute(
                "SELECT * FROM custodian_receipts WHERE batch_id IS NULL AND reference IN (%s)" % ",".join("?" * len(items)),
                [item["reference"] for item in items],
            ).fetchall()
            for row in pending:
                connection.execute("UPDATE custodian_receipts SET batch_id=? WHERE id=?", (batch_id, row["id"]))
            row = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            result = self._batch_row(row)
        return result

    def _invalidate_batch_locked(self, connection: sqlite3.Connection, batch_id: int, actor_id: str, reason: str) -> None:
        connection.execute(
            "UPDATE batches SET state='invalid', updated_at=? WHERE id=? AND state='open'",
            (_now(), batch_id),
        )
        self.add_batch_audit(connection, batch_id, actor_id, "batch_invalidated", {"reason": reason})

    def invalidate_open_for_record(self, record_id: int, actor_id: str, reason: str) -> List[int]:
        """指令改动后，包含它的未确认批次立即失效。已确认批次保持冻结。"""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT batch_id FROM batch_items WHERE record_id=?", (record_id,)
            ).fetchall()
            invalidated = []
            for row in rows:
                batch_id = int(row["batch_id"])
                state = connection.execute("SELECT state FROM batches WHERE id=?", (batch_id,)).fetchone()
                if state is not None and state["state"] == "open":
                    self._invalidate_batch_locked(connection, batch_id, actor_id, reason)
                    invalidated.append(batch_id)
            connection.commit()
        return invalidated

    def confirm_batch(self, batch_id: int, expected_version: int, actor_id: str, frozen_items: List[Dict[str, Any]], frozen_total: float) -> Dict[str, Any]:
        """条件写入：只有 open 且版本一致才能确认，两名主管只有先到者成功。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            batch = self._batch_row(row)
            if batch["state"] == "confirmed":
                connection.rollback()
                raise Conflict("批次已确认，先到的确认已生效")
            if batch["state"] == "invalid":
                connection.rollback()
                raise Conflict("批次已失效，请先重算")
            if int(batch["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("批次版本冲突，请刷新后重试")
            # 指令在组批后被改动 -> 未确认批次立即失效，确认被拒
            current = connection.execute(
                "SELECT bi.reference AS reference, bi.instruction_version AS item_version, r.version AS record_version "
                "FROM batch_items bi JOIN records r ON r.id=bi.record_id WHERE bi.batch_id=?",
                (batch_id,),
            ).fetchall()
            if any(int(r["item_version"]) != int(r["record_version"]) for r in current):
                self._invalidate_batch_locked(connection, batch_id, actor_id, "确认时发现指令已改动")
                connection.commit()
                raise Conflict("批次内指令已改动，批次失效待重算")
            cursor = connection.execute(
                "UPDATE batches SET state='confirmed',version=version+1,frozen_total=?,confirmed_by=?,updated_at=? "
                "WHERE id=? AND state='open' AND version=?",
                (frozen_total, actor_id, now, batch_id, expected_version),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise Conflict("批次确认冲突，已有主管先确认")
            for item in frozen_items:
                connection.execute(
                    "UPDATE batch_items SET frozen_quantity=?,frozen_net_amount=?,frozen_ca_version=? WHERE batch_id=? AND reference=?",
                    (item["frozen_quantity"], item["frozen_net_amount"], item["frozen_ca_version"], batch_id, item["reference"]),
                )
            self.add_batch_audit(connection, batch_id, actor_id, "batch_confirmed", {
                "frozen_total": frozen_total,
                "items": [
                    {"reference": item["reference"], "net_amount": item["frozen_net_amount"], "ca_version": item["frozen_ca_version"]}
                    for item in frozen_items
                ],
            })
            result = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def recompute_batch(self, batch_id: int, actor_id: str, fresh_items: List[Dict[str, Any]], fresh_total: float) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            if row["state"] != "invalid":
                connection.rollback()
                raise Conflict("只有失效批次可以重算")
            # 重算按当前指令重建明细（替换成员、重置冻结列与匹配状态）
            connection.execute("DELETE FROM batch_items WHERE batch_id=?", (batch_id,))
            for ordinal, item in enumerate(fresh_items):
                connection.execute(
                    "INSERT INTO batch_items(batch_id,ordinal,record_id,reference,side,instruction_version) VALUES(?,?,?,?,?,?)",
                    (batch_id, ordinal, item["record_id"], item["reference"], item["side"], item["instruction_version"]),
                )
            connection.execute(
                "UPDATE batches SET state='open',version=version+1,settlement_held=0,frozen_total=NULL,updated_at=? WHERE id=?",
                (now, batch_id),
            )
            member_refs = [item["reference"] for item in fresh_items]
            # 重算后仍属于批次成员的挂起回执保留挂接，其余退回待挂接
            placeholders = ",".join("?" * len(member_refs)) if member_refs else "''"
            if member_refs:
                connection.execute(
                    "UPDATE custodian_receipts SET status='pending',reasons='[]' "
                    "WHERE batch_id=? AND status='pending' AND reference IN (%s)" % placeholders,
                    [batch_id, *member_refs],
                )
                connection.execute(
                    "UPDATE custodian_receipts SET batch_id=NULL "
                    "WHERE batch_id=? AND (status='pending' OR status='orphan') AND reference NOT IN (%s)" % placeholders,
                    [batch_id, *member_refs],
                )
            else:
                connection.execute(
                    "UPDATE custodian_receipts SET batch_id=NULL WHERE batch_id=? AND (status='pending' OR status='orphan')",
                    (batch_id,),
                )
            self.add_batch_audit(connection, batch_id, actor_id, "batch_recomputed", {
                "references": [item["reference"] for item in fresh_items], "total": fresh_total,
            })
            result = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def receipt_by_external_id(self, custodian_receipt_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM custodian_receipts WHERE custodian_receipt_id=?", (custodian_receipt_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def pending_receipts_for(self, batch_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM custodian_receipts WHERE batch_id=? AND status='pending' ORDER BY id", (batch_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_receipt(self, receipt: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO custodian_receipts(custodian_receipt_id,batch_id,record_id,reference,received_version,"
                    "delivered_quantity,net_amount,status,reasons,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt["custodian_receipt_id"], receipt.get("batch_id"), receipt.get("record_id"),
                        receipt["reference"], int(receipt["received_version"]), int(receipt["delivered_quantity"]),
                        float(receipt["net_amount"]), receipt["status"], self._dumps(receipt.get("reasons", [])),
                        actor_id, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("回执已存在，重复回执只记一次") from exc
            receipt_id = int(cursor.lastrowid)
            row = connection.execute("SELECT * FROM custodian_receipts WHERE id=?", (receipt_id,)).fetchone()
            result = dict(row)
        return result

    def mark_receipt_status(self, receipt_id: int, status: str, reasons: List[str], matched_item_id: Optional[int] = None, hold_batch_id: Optional[int] = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE custodian_receipts SET status=?,reasons=? WHERE id=?",
                (status, self._dumps(reasons), receipt_id),
            )
            if matched_item_id is not None:
                connection.execute(
                    "UPDATE batch_items SET matched_receipt_id=? WHERE id=?", (receipt_id, matched_item_id)
                )
            if hold_batch_id is not None:
                connection.execute(
                    "UPDATE batches SET settlement_held=1,updated_at=? WHERE id=?", (_now(), hold_batch_id)
                )

    def add_discrepancy(self, batch_id: int, receipt_id: int, reference: str, reasons: List[str]) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO receipt_discrepancies(batch_id,receipt_id,reference,reasons,created_at) VALUES(?,?,?,?,?)",
                (batch_id, receipt_id, reference, self._dumps(reasons), _now()),
            )

    def reconcile_on_confirm(self, batch_id: int, actor_id: str) -> List[Dict[str, Any]]:
        """确认时把挂起回执与冻结净额对账；版本不一致则保留差异并停住交收。返回差异列表。"""
        discrepancies: List[Dict[str, Any]] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            pending = connection.execute(
                "SELECT * FROM custodian_receipts WHERE batch_id=? AND status='pending' ORDER BY id", (batch_id,)
            ).fetchall()
            for prow in pending:
                item = connection.execute(
                    "SELECT * FROM batch_items WHERE batch_id=? AND reference=?", (batch_id, prow["reference"])
                ).fetchone()
                if item is None:
                    # 重算后该指令已不在批次中：保留差异但不停交收（不属于本批次）
                    connection.execute(
                        "UPDATE custodian_receipts SET status='discrepancy',reasons=? WHERE id=?",
                        (self._dumps(["引用的指令不在当前批次中"]), prow["id"]),
                    )
                    continue
                reasons = []
                if int(prow["received_version"]) != int(item["frozen_ca_version"]):
                    reasons.append("公司行动版本不一致：回执v%s/批次v%s" % (prow["received_version"], item["frozen_ca_version"]))
                if int(prow["delivered_quantity"]) != int(item["frozen_quantity"]):
                    reasons.append("数量不一致：回执%s/批次%s" % (prow["delivered_quantity"], item["frozen_quantity"]))
                if round(abs(float(prow["net_amount"])), 2) != round(abs(float(item["frozen_net_amount"])), 2):
                    reasons.append("净额不一致：回执%s/批次%s" % (prow["net_amount"], item["frozen_net_amount"]))
                if reasons:
                    connection.execute(
                        "UPDATE custodian_receipts SET status='discrepancy',reasons=? WHERE id=?",
                        (self._dumps(reasons), prow["id"]),
                    )
                    connection.execute(
                        "INSERT INTO receipt_discrepancies(batch_id,receipt_id,reference,reasons,created_at) VALUES(?,?,?,?,?)",
                        (batch_id, prow["id"], prow["reference"], self._dumps(reasons), _now()),
                    )
                    connection.execute("UPDATE batches SET settlement_held=1,updated_at=? WHERE id=?", (_now(), batch_id))
                    self.add_batch_audit(connection, batch_id, actor_id, "receipt_discrepancy", {
                        "reference": prow["reference"], "reasons": reasons,
                    })
                    discrepancies.append({"reference": prow["reference"], "reasons": reasons})
                else:
                    connection.execute(
                        "UPDATE custodian_receipts SET status='matched',reasons='[]' WHERE id=?", (prow["id"],)
                    )
                    connection.execute(
                        "UPDATE batch_items SET matched_receipt_id=? WHERE id=?", (prow["id"], item["id"])
                    )
                    self.add_batch_audit(connection, batch_id, actor_id, "receipt_matched", {
                        "reference": prow["reference"], "receipt_id": prow["custodian_receipt_id"],
                    })
            connection.commit()
        return discrepancies

    def settle_batch(self, batch_id: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            if row["state"] != "confirmed":
                connection.rollback()
                raise Conflict("只有已确认批次可以交收")
            if row["settlement_held"]:
                connection.rollback()
                raise Conflict("批次存在版本差异，交收已停住，待人工处理")
            unmatched = connection.execute(
                "SELECT reference FROM batch_items WHERE batch_id=? AND matched_receipt_id IS NULL", (batch_id,)
            ).fetchall()
            if unmatched:
                connection.rollback()
                raise Conflict("尚有指令未匹配托管回执：%s" % ",".join(r["reference"] for r in unmatched))
            cursor = connection.execute(
                "UPDATE batches SET state='settled',version=version+1,updated_at=? WHERE id=? AND state='confirmed' AND settlement_held=0",
                (now, batch_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise Conflict("批次交收冲突")
            self.add_batch_audit(connection, batch_id, actor_id, "batch_settled", {"total": row["frozen_total"]})
            result = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def batch_audit_timeline(self, batch_id: int) -> List[Dict[str, Any]]:
        if self.get_batch(batch_id) is None:
            raise NotFound("批次不存在")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM batch_audit_events WHERE batch_id=? ORDER BY id", (batch_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def batch_stats(self, organization: Optional[str] = None) -> Dict[str, Any]:
        sql = "SELECT state, COUNT(*) AS total, COALESCE(SUM(frozen_total),0) AS frozen_total FROM batches"
        params: List[Any] = []
        if organization:
            sql += " WHERE organization=?"
            params.append(organization)
        sql += " GROUP BY state"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return {
            str(row["state"]): {"total": int(row["total"]), "frozen_total": round(float(row["frozen_total"]), 2)}
            for row in rows
        }

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
