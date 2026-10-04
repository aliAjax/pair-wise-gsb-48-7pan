"""证券结算与企业行动处理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Tuple

from .domain import Conflict, SettlementHeld, ValidationError, choice, integer, number, text


INITIAL_STATE = "captured"
CREATE_ROLES = {'trader'}
ACTION_ROLES = {
    'apply_corporate': {'corporate_actions'},
    'approve': {'settlement_officer'},
    'settle': {'settlement_officer'},
    'fail': {'settlement_officer'},
    'reverse': {'corporate_actions', 'settlement_officer'},
    'revise_corporate': {'corporate_actions'},
}
TRANSITIONS = {
    'apply_corporate': {'captured': 'adjusted'},
    'approve': {'captured': 'approved', 'adjusted': 'approved'},
    'settle': {'approved': 'settled'},
    'fail': {'approved': 'failed'},
    'reverse': {'settled': 'reversed', 'failed': 'reversed'},
    'revise_corporate': {'captured': 'captured', 'adjusted': 'adjusted'},
}

# 批次生命周期
BATCH_OPEN = "open"            # 未确认：可重算，指令改动后立即失效
BATCH_CONFIRMED = "confirmed"  # 已确认：公司行动版本与每笔净额全部冻结
BATCH_INVALID = "invalid"      # 未确认批次因指令改动而失效，待重算
BATCH_SETTLED = "settled"
BATCH_STATES = {BATCH_OPEN, BATCH_CONFIRMED, BATCH_INVALID, BATCH_SETTLED}

BATCH_ACTIONS_ROLES = {
    'compose_batch': {'settlement_officer'},
    'confirm_batch': {'settlement_officer'},
    'recompute_batch': {'settlement_officer'},
    'post_receipt': {'settlement_officer', 'custodian'},
    'complete_settlement': {'settlement_officer'},
}

CURRENCIES = ["CNY", "USD", "HKD"]
DEFAULT_ORG = "DEFAULT"


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        for roles in BATCH_ACTIONS_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_batch_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in BATCH_ACTIONS_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "instrument")
        choice(p, "side", ["buy", "sell"])
        integer(p, "quantity", 1)
        number(p, "price", 0.01)
        number(p, "fees", 0)
        choice(p, "currency", CURRENCIES)
        integer(p, "settlement_day", 0)
        choice(p, "corporate_action", ["none", "split", "dividend", "merger"])
        number(p, "action_ratio", 0.01)
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        gross = float(p["quantity"]) * float(p["price"])
        fee = float(p["fees"])
        p["gross_amount"] = round(gross, 2)
        p["net_amount"] = round(gross + fee if p["side"] == "buy" else gross - fee, 2)
        p["adjusted_quantity"] = p["quantity"]
        p["adjusted_price"] = p["price"]
        p["ca_version"] = 1
        p["account"] = (p.get("account") or "").strip() if isinstance(p.get("account"), str) else ""
        if p["corporate_action"] == "split":
            p["adjusted_quantity"] = int(float(p["quantity"]) * float(p["action_ratio"]))
            p["adjusted_price"] = round(float(p["price"]) / float(p["action_ratio"]), 4)
        elif p["corporate_action"] == "dividend":
            p["cash_entitlement"] = round(float(p["quantity"]) * float(p["action_ratio"]), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] not in {"settled", "reversed"} and item["payload"].get("instrument") == payload.get("instrument") and item["payload"].get("settlement_day") == payload.get("settlement_day"):
                if item["payload"].get("side") == payload.get("side") and item["payload"].get("quantity") == payload.get("quantity") and item["payload"].get("price") == payload.get("price"):
                    raise Conflict("疑似重复结算指令")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "apply_corporate":
            if p["corporate_action"] == "none":
                raise ValidationError("没有待处理的公司行动")
            changes["corporate_applied"] = True
            changes["effective_quantity"] = p["adjusted_quantity"]
            changes["effective_price"] = p["adjusted_price"]
            summary = "公司行动已应用"
        elif action == "approve":
            changes["approved_amount"] = p["net_amount"]
            summary = "结算指令复核通过"
        elif action == "settle":
            delivered = integer(data, "delivered_quantity", 0)
            paid = number(data, "cash_paid", 0)
            required_quantity = int(p.get("effective_quantity", p["quantity"]))
            if delivered != required_quantity:
                raise ValidationError("交收证券数量不匹配")
            if paid < float(p["net_amount"]):
                raise ValidationError("交收资金不足")
            changes["delivered_quantity"] = delivered
            changes["cash_paid"] = paid
            summary = "交收完成"
        elif action == "fail":
            changes["fail_reason"] = text(data, "fail_reason")
            summary = "交收失败"
        elif action == "reverse":
            changes["reverse_reason"] = text(data, "reverse_reason")
            summary = "交收冲正"
        elif action == "revise_corporate":
            # 公司行动换版：版本号推进，按新比率重算。已确认批次不受影响。
            ratio = number(data, "action_ratio", 0.01)
            changes["action_ratio"] = ratio
            changes["ca_version"] = int(p.get("ca_version", 1)) + 1
            changes["adjusted_quantity"] = p["quantity"]
            changes["adjusted_price"] = p["price"]
            if p["corporate_action"] == "split":
                changes["adjusted_quantity"] = int(float(p["quantity"]) * ratio)
                changes["adjusted_price"] = round(float(p["price"]) / ratio, 4)
            elif p["corporate_action"] == "dividend":
                changes["cash_entitlement"] = round(float(p["quantity"]) * ratio, 2)
            if p.get("corporate_applied"):
                changes["effective_quantity"] = changes["adjusted_quantity"]
                changes["effective_price"] = changes["adjusted_price"]
            summary = "公司行动换版至v%s" % changes["ca_version"]
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ------------------------------------------------------------------
    # 净额批次
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_references(refs: List[str]) -> List[str]:
        seen = set()
        ordered: List[str] = []
        for ref in refs:
            ref = ref.strip()
            if not ref:
                raise ValidationError("指令引用不能为空")
            if ref in seen:
                raise ValidationError("批次内指令重复：%s" % ref)
            seen.add(ref)
            ordered.append(ref)
        return ordered

    @staticmethod
    def signed_net(payload: Dict[str, Any]) -> float:
        # 买入为应付（正），卖出为应收（负）
        value = float(payload["net_amount"])
        return round(abs(value) if payload["side"] == "buy" else -abs(value), 2)

    def group_key(self, payload: Dict[str, Any]) -> Tuple[str, str, int]:
        return (payload["account"], payload["currency"], int(payload["settlement_day"]))

    def build_items(self, references: List[str], records: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按账户、币种、交收日校验同质性，生成每笔净额（未固定）。"""
        refs = self.normalize_references(references)
        if not refs:
            raise ValidationError("批次至少包含一笔指令")
        key = None
        items: List[Dict[str, Any]] = []
        for ref in refs:
            record = records.get(ref)
            if record is None:
                raise ValidationError("指令不存在：%s" % ref)
            if record["state"] in {"settled", "reversed"}:
                raise Conflict("指令已终结，不能纳入净额批次：%s" % ref)
            p = record["payload"]
            if not p.get("account"):
                raise ValidationError("指令缺少账户，不能组批：%s" % ref)
            current_key = self.group_key(p)
            if key is None:
                key = current_key
            elif current_key != key:
                raise ValidationError("批次必须同账户、同币种、同交收日")
            items.append({
                "reference": ref,
                "record_id": record["id"],
                "instrument": p["instrument"],
                "side": p["side"],
                "quantity": int(p.get("effective_quantity", p["adjusted_quantity"])),
                "net_amount": self.signed_net(p),
                "ca_version": int(p.get("ca_version", 1)),
                "instruction_version": int(record["version"]),
            })
        return items

    @staticmethod
    def batch_total(items: List[Dict[str, Any]]) -> float:
        return round(sum(float(item["net_amount"]) for item in items), 2)

    @staticmethod
    def validate_receipt(data: Dict[str, Any]) -> Dict[str, Any]:
        result = {
            "reference": text(data, "reference"),
            "received_version": integer(data, "received_version", 1),
            "delivered_quantity": integer(data, "delivered_quantity", 0),
            "net_amount": round(number(data, "net_amount"), 2),
        }
        return result

    def match_receipt(self, item: Dict[str, Any], receipt: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """版本优先：版本对不上即保留差异并停住交收。返回(是否匹配, 差异说明)。"""
        diffs: List[str] = []
        if int(receipt["received_version"]) != int(item["frozen_ca_version"]):
            diffs.append("公司行动版本不一致：回执v%s/批次v%s" % (receipt["received_version"], item["frozen_ca_version"]))
        if int(receipt["delivered_quantity"]) != int(item["frozen_quantity"]):
            diffs.append("数量不一致：回执%s/批次%s" % (receipt["delivered_quantity"], item["frozen_quantity"]))
        if round(abs(float(receipt["net_amount"])), 2) != round(abs(float(item["frozen_net_amount"])), 2):
            diffs.append("净额不一致：回执%s/批次%s" % (receipt["net_amount"], item["frozen_net_amount"]))
        return (not diffs), diffs

    def can_settle(self, batch: Dict[str, Any], items: List[Dict[str, Any]]) -> None:
        if batch["state"] != BATCH_CONFIRMED:
            raise Conflict("只有已确认批次可以交收")
        if batch["settlement_held"]:
            raise SettlementHeld("批次存在版本差异，交收已停住，待人工处理")
        unmatched = [item["reference"] for item in items if not item["matched_receipt_id"]]
        if unmatched:
            raise Conflict("尚有指令未收到托管回执：%s" % ",".join(unmatched))
