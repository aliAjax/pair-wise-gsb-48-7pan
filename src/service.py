"""业务用例编排：权限隔离、净额组批、确认冻结、回执对账与交收挂起。"""
from typing import Any, Dict, List, Optional

from . import rules
from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError
from .repository import Repository


# 会员只能提交本公司指令；结算主管负责组批/确认/交收；托管方负责回执；公司行动岗负责发布版本。
ROLE_MEMBER = "member"
ROLE_SETTLEMENT_OFFICER = "settlement_officer"
ROLE_CUSTODIAN = "custodian"
ROLE_CORPORATE_ACTIONS = "corporate_actions"
ROLE_ADMIN = "admin"
KNOWN_ROLES = {ROLE_MEMBER, ROLE_SETTLEMENT_OFFICER, ROLE_CUSTODIAN, ROLE_CORPORATE_ACTIONS}


class Service:
    def __init__(self, repository: Repository, rulebook: Any = None, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rulebook or rules
        self.audit = audit or AuditRecorder(repository)

    # ---------- 身份与权限 ----------

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _require_role(self, actor: Actor, allowed: set) -> Actor:
        if actor.role != ROLE_ADMIN and actor.role not in allowed:
            raise PermissionDenied("角色%s无权执行该操作" % actor.role)
        return actor

    @staticmethod
    def _require_org(actor: Actor, organization: str) -> None:
        """会员视角的越权检查：只能访问本机构（X-Org）数据。"""
        if actor.role == ROLE_MEMBER:
            if not actor.organization:
                raise PermissionDenied("会员身份缺少机构信息")
            if organization != actor.organization:
                raise PermissionDenied("禁止跨机构提交或访问批次")

    @staticmethod
    def _org_scope(actor: Actor) -> Optional[str]:
        return actor.organization if actor.role == ROLE_MEMBER and actor.organization else None

    # ---------- 结算指令 ----------

    def submit_instruction(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, {ROLE_MEMBER})
        data = self.rules.validate_instruction(payload or {})
        self._require_org(actor, data["organization"])
        return self.repository.create_instruction(data, actor.user_id)

    def amend_instruction(self, actor: Actor, reference: str, expected_version: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, {ROLE_MEMBER})
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValidationError("expected_version必须是整数")
        existing = self.repository.get_instruction(reference=reference)
        self._require_org(actor, existing["organization"])
        data = self.rules.validate_instruction(payload or {})
        self._require_org(actor, data["organization"])
        return self.repository.amend_instruction(existing["id"], expected_version, data, actor.user_id)

    def get_instruction(self, actor: Actor, reference: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        if actor.role not in KNOWN_ROLES and actor.role != ROLE_ADMIN:
            raise PermissionDenied("角色无权访问该服务")
        existing = self.repository.get_instruction(reference=reference)
        self._require_org(actor, existing["organization"])
        return existing

    def list_instructions(self, actor: Actor, filters: Dict[str, str]) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        if actor.role not in KNOWN_ROLES and actor.role != ROLE_ADMIN:
            raise PermissionDenied("角色无权访问该服务")
        scope = self._org_scope(actor)
        organization = scope or filters.get("organization")
        return self.repository.list_instructions(
            account=filters.get("account"), currency=filters.get("currency"),
            settlement_date=filters.get("settlement_date"), organization=organization,
            limit=filters.get("limit", 100),
        )

    # ---------- 公司行动版本 ----------

    def publish_ca(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, {ROLE_CORPORATE_ACTIONS})
        data = self.rules.validate_ca_version(payload or {})
        return self.repository.publish_ca(data, actor.user_id)

    # ---------- 净额批次 ----------

    def build_batch(self, actor: Actor, batch_no: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """按账户、币种、交收日组净额批次；组批即固定公司行动版本与每笔净额。"""
        actor = self._actor(actor)
        self._require_role(actor, {ROLE_SETTLEMENT_OFFICER})
        if not isinstance(batch_no, str) or not batch_no.strip():
            raise ValidationError("batch_no不能为空")
        batch_no = batch_no.strip()
        refs = payload.get("references") if isinstance(payload, dict) else None
        if not isinstance(refs, list) or not refs or any(not isinstance(item, str) or not item.strip() for item in refs):
            raise ValidationError("references必须是非空文本列表")
        refs = [item.strip() for item in refs]
        if len(set(refs)) != len(refs):
            raise ValidationError("references存在重复指令")

        instructions = []
        for reference in refs:
            instructions.append(self.repository.get_instruction(reference=reference))

        groups = {rules.group_key(item) for item in instructions}
        if len(groups) != 1:
            raise ValidationError("同一净额批次必须属于相同账户、币种和交收日")
        organizations = {item["organization"] for item in instructions}
        if len(organizations) != 1:
            raise ValidationError("同一净额批次只能包含单一机构指令，禁止跨机构批次")
        account, currency, settlement_date = next(iter(groups))

        ca_versions = self.repository.ca_versions(sorted({item["instrument"] for item in instructions}))
        entries = rules.build_batch_entries(instructions, ca_versions)
        total_net = rules.money(sum(rules.Decimal(str(entry["net_amount"])) for entry in entries))
        fingerprint = rules.batch_fingerprint(entries)
        return self.repository.create_batch(
            batch_no,
            {"account": account, "currency": currency, "settlement_date": settlement_date,
             "organization": next(iter(organizations))},
            entries, total_net, fingerprint, actor.user_id,
        )

    def confirm_batch(self, actor: Actor, batch_no: str, expected_version: Any) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, {ROLE_SETTLEMENT_OFFICER})
        batch = self.repository.get_batch(batch_no)
        self._require_org(actor, batch["organization"])
        if expected_version is not None and (not isinstance(expected_version, int) or isinstance(expected_version, bool)):
            raise ValidationError("expected_version必须是整数")
        return self.repository.confirm_batch(batch_no, expected_version, actor.user_id)

    def get_batch(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        if actor.role not in KNOWN_ROLES and actor.role != ROLE_ADMIN:
            raise PermissionDenied("角色无权访问该服务")
        batch = self.repository.get_batch(batch_no)
        self._require_org(actor, batch["organization"])
        return self._batch_view(batch)

    def list_batches(self, actor: Actor, filters: Dict[str, str]) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        if actor.role not in KNOWN_ROLES and actor.role != ROLE_ADMIN:
            raise PermissionDenied("角色无权访问该服务")
        scope = self._org_scope(actor)
        organization = scope or filters.get("organization")
        batches = self.repository.list_batches(
            state=filters.get("state"), account=filters.get("account"),
            currency=filters.get("currency"), settlement_date=filters.get("settlement_date"),
            organization=organization, limit=filters.get("limit", 100),
        )
        return [self._batch_view(batch) for batch in batches]

    def _batch_view(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        open_discrepancies = self.repository.list_discrepancies(batch["id"])
        view = dict(batch)
        view["open_discrepancy_count"] = len(open_discrepancies)
        return view

    def batch_timeline(self, actor: Actor, batch_no: str) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        if actor.role not in KNOWN_ROLES and actor.role != ROLE_ADMIN:
            raise PermissionDenied("角色无权访问该服务")
        batch = self.repository.get_batch(batch_no)
        self._require_org(actor, batch["organization"])
        return self.audit.timeline("batch", batch["id"])

    # ---------- 托管回执 ----------

    def record_receipt(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, {ROLE_CUSTODIAN})
        data = self.rules.validate_receipt(payload or {})
        if data.get("batch_no"):
            batch = self.repository.get_batch(data["batch_no"])
            if batch["state"] == rules.INVALID:
                raise Conflict("批次已失效，不能再登记回执")
        receipt, duplicate = self.repository.add_receipt(data, actor.user_id)
        receipt["duplicate"] = duplicate
        return receipt

    def list_receipts(self, actor: Actor, reference: str = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        if actor.role not in KNOWN_ROLES and actor.role != ROLE_ADMIN:
            raise PermissionDenied("角色无权访问该服务")
        return self.repository.list_receipts(reference=reference)

    # ---------- 对账与交收 ----------

    def reconcile_batch(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, {ROLE_SETTLEMENT_OFFICER})
        batch = self.repository.get_batch(batch_no)
        self._require_org(actor, batch["organization"])
        if batch["state"] not in (rules.CONFIRMED, rules.SETTLED):
            raise Conflict("只有已确认（金额冻结）的批次才能对账")
        receipts = self.repository.receipts_for_batch(batch)
        discrepancies, counters = self.rules.reconcile(batch["entries"], receipts)
        saved = self.repository.save_reconciliation(batch["id"], discrepancies)
        return {
            "batch_no": batch_no,
            "state": batch["state"],
            "frozen_total_net": batch["total_net"],
            "receipt_count": len(receipts),
            "counters": counters,
            "hold": bool(discrepancies),
            "discrepancies": saved,
        }

    def settle_batch(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._require_role(actor, {ROLE_SETTLEMENT_OFFICER})
        batch = self.repository.get_batch(batch_no)
        self._require_org(actor, batch["organization"])
        # 交收前自动对账：版本对不上等差异保留并停住交收。
        receipts = self.repository.receipts_for_batch(batch)
        discrepancies, _counters = self.rules.reconcile(batch["entries"], receipts)
        self.repository.save_reconciliation(batch["id"], discrepancies)
        if discrepancies:
            raise Conflict("对账存在%s笔差异，交收挂起" % len(discrepancies))
        return self._batch_view(self.repository.settle_batch(batch_no, actor.user_id))

    # ---------- 统计 ----------

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        if actor.role not in KNOWN_ROLES and actor.role != ROLE_ADMIN:
            raise PermissionDenied("角色无权访问该服务")
        return self.repository.stats(organization=self._org_scope(actor))
