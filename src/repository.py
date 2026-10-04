"""SQLite 表结构与事务访问。

写操作均在单事务内完成联动（如指令改动同时失效未确认批次）；
确认/交收采用 BEGIN IMMEDIATE + 状态比较，保证并发下先到者成功。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import rules
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
                CREATE TABLE IF NOT EXISTS instructions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    account TEXT NOT NULL,
                    organization TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    settlement_date TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL NOT NULL,
                    fees REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS corporate_action_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ca_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    factor REAL NOT NULL,
                    cash_factor REAL NOT NULL DEFAULT 0,
                    note TEXT NOT NULL DEFAULT '',
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    UNIQUE(instrument, version)
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    account TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    settlement_date TEXT NOT NULL,
                    organization TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open',
                    version INTEGER NOT NULL DEFAULT 1,
                    entries TEXT NOT NULL,
                    total_net REAL NOT NULL,
                    fingerprint TEXT NOT NULL,
                    invalidated_reason TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    confirmed_by TEXT NOT NULL DEFAULT '',
                    settled_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT,
                    settled_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_links (
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    instruction_id INTEGER NOT NULL REFERENCES instructions(id),
                    PRIMARY KEY (batch_id, instruction_id)
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_no TEXT NOT NULL UNIQUE,
                    reference TEXT NOT NULL,
                    instruction_version INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    batch_no TEXT,
                    recorded_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discrepancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    type TEXT NOT NULL,
                    receipt_id INTEGER,
                    receipt_no TEXT NOT NULL DEFAULT '',
                    reference TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_instructions_group
                    ON instructions(account, currency, settlement_date, organization);
                CREATE INDEX IF NOT EXISTS idx_batches_group
                    ON batches(account, currency, settlement_date, organization);
                CREATE INDEX IF NOT EXISTS idx_links_instruction ON batch_links(instruction_id, batch_id);
                CREATE INDEX IF NOT EXISTS idx_receipts_reference ON receipts(reference);
                CREATE INDEX IF NOT EXISTS idx_discrepancies_batch ON discrepancies(batch_id, status);
                CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, id);
                """
            )

    # ---------- 审计 ----------

    @staticmethod
    def _audit(connection: sqlite3.Connection, entity_type: str, entity_id: int, action: str, actor_id: str, details: Dict[str, Any], at: str = None) -> None:
        connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, action, actor_id, json.dumps(details, ensure_ascii=False, sort_keys=True), at or _now()),
        )

    def audit_timeline(self, entity_type: str, entity_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE entity_type=? AND entity_id=? ORDER BY id",
                (entity_type, entity_id),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    # ---------- 结算指令 ----------

    @staticmethod
    def _instruction_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["active"] = bool(item["active"])
        return item

    def create_instruction(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """INSERT INTO instructions(reference,account,organization,instrument,currency,settlement_date,
                       direction,quantity,price,fees,version,active,created_by,updated_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,1,1,?,?,?,?)""",
                    (data["reference"], data["account"], data["organization"], data["instrument"], data["currency"],
                     data["settlement_date"], data["direction"], data["quantity"], data["price"], data["fees"],
                     actor_id, actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("指令reference已存在") from exc
            instruction_id = int(cursor.lastrowid)
            self._audit(connection, "instruction", instruction_id, "submitted", actor_id, {"reference": data["reference"], "organization": data["organization"]}, now)
            row = connection.execute("SELECT * FROM instructions WHERE id=?", (instruction_id,)).fetchone()
        return self._instruction_row(row)

    def get_instruction(self, instruction_id: int = None, reference: str = None) -> Dict[str, Any]:
        with self._connect() as connection:
            if instruction_id is not None:
                row = connection.execute("SELECT * FROM instructions WHERE id=?", (instruction_id,)).fetchone()
            else:
                row = connection.execute("SELECT * FROM instructions WHERE reference=?", (reference,)).fetchone()
        if row is None:
            raise NotFound("结算指令不存在")
        return self._instruction_row(row)

    def list_instructions(self, account: str = None, currency: str = None, settlement_date: str = None,
                          organization: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if account:
            clauses.append("account=?"); params.append(account)
        if currency:
            clauses.append("currency=?"); params.append(currency)
        if settlement_date:
            clauses.append("settlement_date=?"); params.append(settlement_date)
        if organization:
            clauses.append("organization=?"); params.append(organization)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM instructions" + where + " ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [self._instruction_row(row) for row in rows]

    def amend_instruction(self, instruction_id: int, expected_version: int, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """改动指令：版本+1，并立即失效包含该指令的未确认批次；已确认批次金额保持冻结。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version,reference FROM instructions WHERE id=?", (instruction_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("结算指令不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("指令版本冲突，请刷新后重试")
            connection.execute(
                """UPDATE instructions SET account=?,organization=?,instrument=?,currency=?,settlement_date=?,
                   direction=?,quantity=?,price=?,fees=?,version=version+1,updated_by=?,updated_at=? WHERE id=?""",
                (data["account"], data["organization"], data["instrument"], data["currency"], data["settlement_date"],
                 data["direction"], data["quantity"], data["price"], data["fees"], actor_id, now, instruction_id),
            )
            self._invalidate_open(
                connection,
                "SELECT batches.id, batches.batch_no FROM batches JOIN batch_links ON batch_links.batch_id=batches.id "
                "WHERE batch_links.instruction_id=? AND batches.state='open'",
                (instruction_id,),
                "指令%s改动，未确认批次失效待重算" % row["reference"], now,
            )
            self._audit(connection, "instruction", instruction_id, "amended", actor_id,
                        {"from_version": int(expected_version), "to_version": int(expected_version) + 1}, now)
            result = connection.execute("SELECT * FROM instructions WHERE id=?", (instruction_id,)).fetchone()
            connection.commit()
        return self._instruction_row(result)

    # ---------- 公司行动版本 ----------

    def publish_ca(self, data: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        """发布公司行动新版本，并立即失效持有该证券的未确认批次。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM corporate_action_versions WHERE instrument=?",
                (data["instrument"],),
            ).fetchone()
            version = int(row["v"]) + 1
            cursor = connection.execute(
                """INSERT INTO corporate_action_versions(ca_id,instrument,version,factor,cash_factor,note,published_by,published_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                ("CA-%s" % data["instrument"], data["instrument"], version, data["factor"],
                 data.get("cash_factor", 0.0), data.get("note", ""), actor_id, now),
            )
            ca_id = int(cursor.lastrowid)
            self._invalidate_open(
                connection,
                "SELECT DISTINCT batches.id, batches.batch_no FROM batches JOIN batch_links ON batch_links.batch_id=batches.id "
                "JOIN instructions ON instructions.id=batch_links.instruction_id "
                "WHERE instructions.instrument=? AND batches.state='open'",
                (data["instrument"],),
                "证券%s公司行动发布新版本，未确认批次失效待重算" % data["instrument"], now,
            )
            self._audit(connection, "corporate_action", ca_id, "published", actor_id,
                        {"instrument": data["instrument"], "version": version, "factor": data["factor"]}, now)
            result = connection.execute("SELECT * FROM corporate_action_versions WHERE id=?", (ca_id,)).fetchone()
            connection.commit()
        return dict(result)

    def ca_versions(self, instruments: List[str], at_iso: str = None) -> Dict[str, List[Dict[str, Any]]]:
        if not instruments:
            return {}
        placeholders = ",".join("?" for _ in instruments)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM corporate_action_versions WHERE instrument IN (%s) ORDER BY instrument, version DESC" % placeholders,
                instruments,
            ).fetchall()
        result: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            if at_iso is not None and item["published_at"] > at_iso:
                continue
            result.setdefault(item["instrument"], []).append(item)
        return result

    # ---------- 净额批次 ----------

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["entries"] = json.loads(item["entries"])
        return item

    def _invalidate_open(self, connection: sqlite3.Connection, query: str, params: tuple, reason: str, at: str) -> int:
        rows = connection.execute(query, params).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE batches SET state='invalid',invalidated_reason=?,updated_at=? WHERE id=? AND state='open'",
                (reason, at, row["id"]),
            )
            self._audit(connection, "batch", int(row["id"]), "invalidated", "system",
                        {"reason": reason, "batch_no": row["batch_no"]}, at)
        return len(rows)

    def create_batch(self, batch_no: str, group: Dict[str, str], entries: List[Dict[str, Any]],
                     total_net: float, fingerprint: str, actor_id: str) -> Dict[str, Any]:
        """按原批次号写入：open重号为幂等重试，invalid重号按新净额重建，跨分组/冻结重号拒绝。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
            if existing is not None:
                same_group = all(existing[key] == group[key] for key in ("account", "currency", "settlement_date", "organization"))
                if not same_group:
                    connection.rollback()
                    raise Conflict("批次号%s已被其他账户/币种/交收日分组占用" % batch_no)
                if existing["state"] in (rules.CONFIRMED, rules.SETTLED):
                    connection.rollback()
                    raise Conflict("批次%s已%s，金额已冻结，禁止重复记账" % (batch_no, "交收" if existing["state"] == rules.SETTLED else "确认"))
                if existing["state"] == rules.OPEN and existing["fingerprint"] == fingerprint:
                    # 写入失败后按原批次号重试：open且内容一致，幂等返回。
                    connection.rollback()
                    return self._batch_row(existing)
                if existing["state"] == rules.OPEN:
                    connection.rollback()
                    raise Conflict("批次号%s已存在未确认批次，内容已变化，请使用新批次号" % batch_no)
                # invalid：按原批次号用最新指令与公司行动版本重建
                connection.execute("DELETE FROM batch_links WHERE batch_id=?", (existing["id"],))
                cursor = connection.execute(
                    """UPDATE batches SET state='open',version=version+1,entries=?,total_net=?,fingerprint=?,
                       invalidated_reason='',updated_at=? WHERE id=?""",
                    (json.dumps(entries, ensure_ascii=False, sort_keys=True), total_net, fingerprint, now, existing["id"]),
                )
                batch_id = int(existing["id"])
                self._replace_links(connection, batch_id, entries)
                self._audit(connection, "batch", batch_id, "rebuilt", actor_id,
                            {"batch_no": batch_no, "entry_count": len(entries), "total_net": total_net}, now)
                result = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
                connection.commit()
                return self._batch_row(result)
            cursor = connection.execute(
                """INSERT INTO batches(batch_no,account,currency,settlement_date,organization,state,version,
                   entries,total_net,fingerprint,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,'open',1,?,?,?,?,?,?)""",
                (batch_no, group["account"], group["currency"], group["settlement_date"], group["organization"],
                 json.dumps(entries, ensure_ascii=False, sort_keys=True), total_net, fingerprint, actor_id, now, now),
            )
            batch_id = int(cursor.lastrowid)
            self._replace_links(connection, batch_id, entries)
            self._audit(connection, "batch", batch_id, "created", actor_id,
                        {"batch_no": batch_no, "entry_count": len(entries), "total_net": total_net}, now)
            result = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    @staticmethod
    def _replace_links(connection: sqlite3.Connection, batch_id: int, entries: List[Dict[str, Any]]) -> None:
        connection.executemany(
            "INSERT OR IGNORE INTO batch_links(batch_id,instruction_id) VALUES(?,?)",
            [(batch_id, entry["instruction_id"]) for entry in entries],
        )

    def get_batch(self, batch_no: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
        if row is None:
            raise NotFound("净额批次不存在")
        return self._batch_row(row)

    def list_batches(self, state: str = None, account: str = None, currency: str = None,
                     settlement_date: str = None, organization: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        clauses, params = [], []
        for column, value in (("state", state), ("account", account), ("currency", currency),
                              ("settlement_date", settlement_date), ("organization", organization)):
            if value:
                clauses.append("%s=?" % column)
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM batches" + where + " ORDER BY id DESC LIMIT ?", params
            ).fetchall()
        return [self._batch_row(row) for row in rows]

    def _cas_state(self, batch_no: str, expected_version: Optional[int], action: str, actor_id: str,
                   details: Dict[str, Any], source: str, target: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM batches WHERE batch_no=?", (batch_no,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("净额批次不存在")
            if row["state"] != source:
                connection.rollback()
                raise Conflict("批次%s当前状态为%s，仅%s批次可执行%s" % (batch_no, row["state"], source, action))
            if expected_version is not None and int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("批次版本冲突，请刷新后重试")
            stamp = ",confirmed_by=?,confirmed_at=?" if target == rules.CONFIRMED else ",settled_by=?,settled_at=?"
            connection.execute(
                "UPDATE batches SET state=?,version=version+1,updated_at=?" + stamp + " WHERE id=?",
                (target, now, actor_id, now, row["id"]),
            )
            self._audit(connection, "batch", int(row["id"]), action, actor_id,
                        dict(details, from_version=int(row["version"])), now)
            result = connection.execute("SELECT * FROM batches WHERE id=?", (row["id"],)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def confirm_batch(self, batch_no: str, expected_version: Optional[int], actor_id: str) -> Dict[str, Any]:
        # 两名主管并发确认：行锁串行化，先到者把状态改为confirmed，后者读到非open即冲突。
        batch = self.get_batch(batch_no)
        return self._cas_state(
            batch_no, expected_version, "confirmed", actor_id,
            {"total_net": batch["total_net"], "fingerprint": batch["fingerprint"],
             "ca_versions": {entry["reference"]: entry["ca_version"] for entry in batch["entries"]}},
            rules.OPEN, rules.CONFIRMED,
        )

    # ---------- 托管回执 ----------

    def add_receipt(self, data: Dict[str, Any], actor_id: str) -> tuple:
        now = _now()
        with self._connect() as connection:
            existing = connection.execute("SELECT * FROM receipts WHERE receipt_no=?", (data["receipt_no"],)).fetchone()
            if existing is not None:
                # 重复回执只记一次：返回原记录，不重复入账。
                return dict(existing), True
            cursor = connection.execute(
                """INSERT INTO receipts(receipt_no,reference,instruction_version,amount,batch_no,recorded_by,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (data["receipt_no"], data["reference"], data["instruction_version"], data["amount"],
                 data.get("batch_no"), actor_id, now),
            )
            receipt_id = int(cursor.lastrowid)
            self._audit(connection, "receipt", receipt_id, "recorded", actor_id,
                        {"receipt_no": data["receipt_no"], "reference": data["reference"]}, now)
            row = connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        return dict(row), False

    def receipts_for_batch(self, batch: Dict[str, Any]) -> List[Dict[str, Any]]:
        refs = [entry["reference"] for entry in batch["entries"]]
        with self._connect() as connection:
            if refs:
                placeholders = ",".join("?" for _ in refs)
                # 纳入：引用批次内指令且未指定其他批次的回执（含晚到回执），
                # 以及明确挂到本批次号但引用不存在指令的幽灵回执。
                rows = connection.execute(
                    """SELECT * FROM receipts WHERE
                       (reference IN (%s) AND (batch_no IS NULL OR batch_no=?))
                       OR (batch_no=? AND reference NOT IN (%s))""" % (placeholders, placeholders),
                    refs + [batch["batch_no"], batch["batch_no"]] + refs,
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM receipts WHERE batch_no=?", (batch["batch_no"],)).fetchall()
        return [dict(row) for row in rows]

    def list_receipts(self, reference: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if reference:
                rows = connection.execute("SELECT * FROM receipts WHERE reference=? ORDER BY id DESC LIMIT ?", (reference, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM receipts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    # ---------- 对账与交收 ----------

    def save_reconciliation(self, batch_id: int, discrepancies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # 重新对账：历史open差异以本轮结果为准（缺失类可自动消除），resolved差异保留。
            connection.execute("UPDATE discrepancies SET status='resolved',resolved_at=? WHERE batch_id=? AND status='open'", (now, batch_id))
            saved = []
            for item in discrepancies:
                cursor = connection.execute(
                    """INSERT INTO discrepancies(batch_id,type,receipt_id,receipt_no,reference,detail,status,created_at)
                       VALUES(?,?,?,?,?,?,'open',?)""",
                    (batch_id, item["type"], item.get("receipt_id"), item.get("receipt_no", ""),
                     item.get("reference", ""), item["detail"], now),
                )
                saved.append(dict(connection.execute("SELECT * FROM discrepancies WHERE id=?", (cursor.lastrowid,)).fetchone()))
            connection.commit()
        return saved

    def list_discrepancies(self, batch_id: int, include_resolved: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM discrepancies WHERE batch_id=?"
        if not include_resolved:
            sql += " AND status='open'"
        sql += " ORDER BY id"
        with self._connect() as connection:
            rows = connection.execute(sql, (batch_id,)).fetchall()
        return [dict(row) for row in rows]

    def settle_batch(self, batch_no: str, actor_id: str) -> Dict[str, Any]:
        return self._cas_state(batch_no, None, "settled", actor_id,
                               {"total_net": self.get_batch(batch_no)["total_net"]},
                               rules.CONFIRMED, rules.SETTLED)

    # ---------- 统计与健康 ----------

    def stats(self, organization: str = None) -> Dict[str, Any]:
        org_filter = " WHERE organization=?" if organization else ""
        params = (organization,) if organization else ()
        with self._connect() as connection:
            state_rows = connection.execute("SELECT state,COUNT(*) AS total FROM batches" + org_filter + " GROUP BY state", params).fetchall()
            instructions = connection.execute("SELECT COUNT(*) AS total FROM instructions" + org_filter, params).fetchone()["total"]
            receipts = connection.execute("SELECT COUNT(*) AS total FROM receipts").fetchone()["total"]
            open_discrepancies = connection.execute(
                """SELECT COUNT(*) AS total FROM discrepancies WHERE status='open' AND batch_id IN
                   (SELECT id FROM batches WHERE 1=1""" + (" AND organization=?" if organization else "") + ")",
                params if organization else (),
            ).fetchone()["total"]
        return {
            "batches": {str(row["state"]): int(row["total"]) for row in state_rows},
            "instructions": int(instructions),
            "receipts": int(receipts),
            "open_discrepancies": int(open_discrepancies),
        }

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
