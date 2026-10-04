"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, SettlementHeld, ValidationError, text
from .repository import Repository
from .rules import BATCH_OPEN, DEFAULT_ORG, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _org(actor: Actor) -> str:
        return actor.organization.strip() or DEFAULT_ORG

    def _require_batch_role(self, actor: Actor, action: str) -> None:
        if not self.rules.role_can_batch_action(actor.role, action):
            raise PermissionDenied("角色无权执行该批次操作")

    def _get_batch_org_scoped(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        batch = self.repository.get_batch(batch_id)
        if batch is None:
            raise NotFound("批次不存在")
        if actor.role != "admin" and batch["organization"] != self._org(actor):
            # 不暴露其他机构批次的存在
            raise NotFound("批次不存在")
        return batch

    # ------------------------------------------------------------------
    # 结算指令
    # ------------------------------------------------------------------

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, self._org(actor))

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(state=state, limit=limit)
        org = self._org(actor)
        if actor.role != "admin":
            records = [record for record in records if record.get("organization", DEFAULT_ORG) == org]
        return records

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        if actor.role != "admin" and record.get("organization", DEFAULT_ORG) != self._org(actor):
            raise NotFound("记录不存在")
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        if actor.role != "admin" and record.get("organization", DEFAULT_ORG) != self._org(actor):
            raise PermissionDenied("不能操作其他机构的指令")
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        updated = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        # 指令改动后，包含它的未确认批次立即失效；已确认金额保持冻结。
        self.repository.invalidate_open_for_record(
            record_id, actor.user_id, "指令%s被执行%s" % (record["reference"], action)
        )
        return updated

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.get_record(actor, record_id)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ------------------------------------------------------------------
    # 净额批次：组批 / 确认 / 重算
    # ------------------------------------------------------------------

    def _load_records(self, references: List[str]) -> Dict[str, Dict[str, Any]]:
        records: Dict[str, Dict[str, Any]] = {}
        for ref in references:
            record = self.repository.get_by_reference(ref)
            if record is not None:
                records[ref] = record
        return records

    def compose_batch(self, actor: Actor, batch_no: str, references: List[str]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_batch_role(actor, "compose_batch")
        batch_no = text({"batch_no": batch_no}, "batch_no")
        org = self._org(actor)

        existing = self.repository.get_batch_by_no(batch_no)
        if existing is not None:
            if existing["organization"] != org:
                raise PermissionDenied("不能向其他机构的批次号记账")
            # 写入失败后按原批次号重试：幂等返回，不重复记账
            return self.repository.batch_detail(existing["id"])

        refs = self.rules.normalize_references(references)
        records = self._load_records(refs)
        # 会员只提交本公司指令，越权提交跨机构批次会被拒绝
        for ref in refs:
            record = records.get(ref)
            if record is None:
                raise ValidationError("指令不存在：%s" % ref)
            if actor.role != "admin" and record.get("organization", DEFAULT_ORG) != org:
                raise PermissionDenied("不能把其他机构的指令%s纳入批次" % ref)
        items = self.rules.build_items(refs, records)
        account, currency, settlement_day = self.rules.group_key(records[refs[0]]["payload"])
        batch = self.repository.create_batch(batch_no, org, account, currency, settlement_day, items, actor.user_id)
        return self.repository.batch_detail(batch["id"])

    def confirm_batch(self, actor: Actor, batch_id: int, expected_version: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_batch_role(actor, "confirm_batch")
        batch = self._get_batch_org_scoped(actor, batch_id)

        items_row = self.repository.batch_items(batch_id)
        references = [item["reference"] for item in items_row]
        records = self._load_records(references)
        # 重新按当前指令计算净额；若指令已改动，仓储层条件写入会使批次失效并拒绝确认
        fresh_items = self.rules.build_items(references, records)
        frozen_items = [
            {
                "reference": current["reference"],
                "frozen_quantity": current["quantity"],
                "frozen_net_amount": current["net_amount"],
                "frozen_ca_version": current["ca_version"],
            }
            for current in fresh_items
        ]
        frozen_total = self.rules.batch_total(fresh_items)

        updated = self.repository.confirm_batch(batch_id, int(expected_version), actor.user_id, frozen_items, frozen_total)
        # 确认瞬间对挂起的托管回执做一次对账（版本对不上即停住交收）
        self.repository.reconcile_on_confirm(batch_id, actor.user_id)
        return self.repository.batch_detail(updated["id"])

    @staticmethod
    def _ordered_rows(rows: List[Dict[str, Any]], references: List[str]) -> List[Dict[str, Any]]:
        by_ref = {row["reference"]: row for row in rows}
        return [by_ref[ref] for ref in references]

    @staticmethod
    def _ordered_rows(rows: List[Dict[str, Any]], references: List[str]) -> List[Dict[str, Any]]:
        by_ref = {row["reference"]: row for row in rows}
        return [by_ref[ref] for ref in references]

    def recompute_batch(self, actor: Actor, batch_id: int, references: Optional[List[str]] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_batch_role(actor, "recompute_batch")
        batch = self._get_batch_org_scoped(actor, batch_id)
        if references is None:
            references = [item["reference"] for item in self.repository.batch_items(batch_id)]
        refs = self.rules.normalize_references(references)
        records = self._load_records(refs)
        org = self._org(actor)
        for ref in refs:
            record = records.get(ref)
            if record is None:
                raise ValidationError("指令不存在：%s" % ref)
            if actor.role != "admin" and record.get("organization", DEFAULT_ORG) != org:
                raise PermissionDenied("不能把其他机构的指令%s纳入批次" % ref)
        items = self.rules.build_items(refs, records)
        total = self.rules.batch_total(items)
        self.repository.recompute_batch(batch_id, actor.user_id, items, total)
        return self.repository.batch_detail(batch_id)

    # ------------------------------------------------------------------
    # 托管回执对账
    # ------------------------------------------------------------------

    def post_receipt(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_batch_role(actor, "post_receipt")
        org = self._org(actor)
        custodian_receipt_id = text(data or {}, "custodian_receipt_id")
        receipt_input = self.rules.validate_receipt(data or {})

        # 重复回执只记一次（乱序送达也不会重复入账）
        duplicate = self.repository.receipt_by_external_id(custodian_receipt_id)
        if duplicate is not None:
            raise Conflict("回执%s已登记，重复回执只记一次" % custodian_receipt_id)

        record = self.repository.get_by_reference(receipt_input["reference"])
        if record is None:
            # 引用不存在指令：保留为无法挂接的差异回执，供人工核查
            stored = self.repository.insert_receipt({
                "custodian_receipt_id": custodian_receipt_id,
                "batch_id": None,
                "record_id": None,
                **receipt_input,
                "status": "orphan",
                "reasons": ["引用的结算指令不存在"],
            }, actor.user_id)
            return {"receipt": stored, "batch": None, "matched": False, "reasons": stored["reasons"]}

        if actor.role != "admin" and record.get("organization", DEFAULT_ORG) != org:
            raise PermissionDenied("不能向其他机构的指令登记回执")

        # 找到当前包含该指令且已确认/开放的批次（同账户币种交收日只可能有一个活跃批次）
        batch = self._active_batch_for_record(record["id"], org, actor)
        receipt = {
            "custodian_receipt_id": custodian_receipt_id,
            "batch_id": batch["id"] if batch else None,
            "record_id": record["id"],
            **receipt_input,
        }

        if batch is None or batch["state"] == BATCH_OPEN:
            # 交收批次尚未确认（回执早到/晚到）：挂起等待确认时对账；已确认金额不因此改变
            receipt["status"] = "pending"
            receipt["reasons"] = []
            stored = self.repository.insert_receipt(receipt, actor.user_id)
            return {"receipt": stored, "batch": batch, "matched": False, "reasons": []}

        item = self._batch_item(batch["id"], receipt_input["reference"])
        if item is None:
            receipt["status"] = "discrepancy"
            receipt["reasons"] = ["引用的指令不在当前批次中"]
            stored = self.repository.insert_receipt(receipt, actor.user_id)
            self.repository.add_discrepancy(batch["id"], stored["id"], receipt_input["reference"], receipt["reasons"])
            return {"receipt": stored, "batch": batch, "matched": False, "reasons": receipt["reasons"]}

        matched, reasons = self.rules.match_receipt(item, receipt_input)
        if matched:
            receipt["status"] = "matched"
            receipt["reasons"] = []
            stored = self.repository.insert_receipt(receipt, actor.user_id)
            self.repository.mark_receipt_status(stored["id"], "matched", [], matched_item_id=item["id"])
            return {"receipt": stored, "batch": batch, "matched": True, "reasons": []}

        # 版本对不上：保留差异并停住交收
        receipt["status"] = "discrepancy"
        receipt["reasons"] = reasons
        stored = self.repository.insert_receipt(receipt, actor.user_id)
        self.repository.add_discrepancy(batch["id"], stored["id"], receipt_input["reference"], reasons)
        self.repository.mark_receipt_status(stored["id"], "discrepancy", reasons, hold_batch_id=batch["id"])
        self.repository.append_batch_audit(batch["id"], actor.user_id, "receipt_discrepancy", {
            "reference": receipt_input["reference"], "reasons": reasons, "receipt_id": custodian_receipt_id,
        })
        raise SettlementHeld("回执与冻结版本不一致，差异已保留，批次交收停住：%s" % "；".join(reasons))

    def _active_batch_for_record(self, record_id: int, org: str, actor: Actor):
        return self.repository.active_batch_for_record(record_id, org)

    def _batch_item(self, batch_id: int, reference: str) -> Optional[Dict[str, Any]]:
        for item in self.repository.batch_items(batch_id):
            if item["reference"] == reference:
                return item
        return None

    def complete_settlement(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_batch_role(actor, "complete_settlement")
        batch = self._get_batch_org_scoped(actor, batch_id)
        items = self.repository.batch_items(batch_id)
        self.rules.can_settle(batch, items)
        updated = self.repository.settle_batch(batch_id, actor.user_id)
        return self.repository.batch_detail(updated["id"])

    # ------------------------------------------------------------------
    # 读取：列表 / 详情 / 统计 / 审计，同一批次结果
    # ------------------------------------------------------------------

    def list_batches(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        org = None if actor.role == "admin" else self._org(actor)
        return self.repository.list_batches(organization=org, state=state, limit=limit)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._get_batch_org_scoped(actor, batch_id)
        return self.repository.batch_detail(batch_id)

    def batch_timeline(self, actor: Actor, batch_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._get_batch_org_scoped(actor, batch_id)
        return self.repository.batch_audit_timeline(batch_id)

    def batch_stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        org = None if actor.role == "admin" else self._org(actor)
        return self.repository.batch_stats(organization=org)
