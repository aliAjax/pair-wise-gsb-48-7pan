"""日终净额结算领域规则：组批净额、公司行动版本固定、回执对账。

本模块只做纯计算，不接触数据库；金额以两位小数表示，采用银行家舍入。
"""
import hashlib
import json
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Dict, List, Optional, Tuple

from .domain import ValidationError, choice, date_text, integer, number, text


CURRENCIES = ["CNY", "USD", "HKD"]
DIRECTIONS = ["pay", "receive"]
BATCH_STATES = ["open", "confirmed", "settled", "invalid"]
DISCREPANCY_TYPES = ["duplicate_receipt", "unknown_reference", "version_mismatch", "amount_mismatch", "missing_receipt"]

# 批次状态：仅 open 可确认；confirmed 冻结、待对账交收；settled 完成；invalid 立即失效待重算。
OPEN = "open"
CONFIRMED = "confirmed"
SETTLED = "settled"
INVALID = "invalid"


def money(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def group_key(instruction: Dict[str, Any]) -> Tuple[str, str, str]:
    """净额批次分组：账户、币种、交收日。"""
    return instruction["account"], instruction["currency"], instruction["settlement_date"]


def ca_factor_for(instruction: Dict[str, Any], ca_versions: Dict[str, List[Dict[str, Any]]], at_iso: Optional[str] = None) -> Dict[str, Any]:
    """取组批时点适用的公司行动版本；无公司行动时返回因子1。

    ca_versions 为按证券代码排列、按版本号降序的版本列表。at_iso 为组批时点，
    取 published_at 不晚于该时点的最新版本；不传则取当前最新版本。
    """
    versions = ca_versions.get(instruction["instrument"], [])
    selected = None
    for version in versions:
        if at_iso is None or version["published_at"] <= at_iso:
            selected = version
            break
    if selected is None:
        return {"ca_id": None, "ca_version": None, "factor": 1.0, "cash_factor": 0.0}
    return {"ca_id": selected["ca_id"], "ca_version": selected["version"], "factor": float(selected["factor"]), "cash_factor": float(selected.get("cash_factor", 0.0))}


def build_entry(instruction: Dict[str, Any], ca: Dict[str, Any]) -> Dict[str, Any]:
    """计算单笔净额并固定公司行动版本。净额带方向符号：pay为负、receive为正。"""
    gross = Decimal(str(instruction["quantity"])) * Decimal(str(instruction["price"]))
    factor = Decimal(str(ca["factor"]))
    effective_quantity = int(Decimal(instruction["quantity"]) * factor)
    adjusted_gross = money(float(gross * factor))
    cash_entitlement = money(float(gross) * float(ca.get("cash_factor", 0.0)))
    fees = money(instruction["fees"])
    # 付款方：支出=成交金额+费用（净额为负）；收款方：收入=成交金额-费用（净额为正）。
    signed = adjusted_gross + cash_entitlement + (fees if instruction["direction"] == "pay" else -fees)
    net = -signed if instruction["direction"] == "pay" else signed
    entry = {
        "instruction_id": instruction["id"],
        "reference": instruction["reference"],
        "instrument": instruction["instrument"],
        "direction": instruction["direction"],
        "quantity": int(instruction["quantity"]),
        "price": float(instruction["price"]),
        "fees": fees,
        "instruction_version": int(instruction["version"]),
        "ca_id": ca["ca_id"],
        "ca_version": ca["ca_version"],
        "effective_quantity": effective_quantity,
        "adjusted_gross": adjusted_gross,
        "cash_entitlement": cash_entitlement,
        "net_amount": money(net),
    }
    entry["fingerprint"] = entry_fingerprint(entry)
    return entry


def entry_fingerprint(entry: Dict[str, Any]) -> str:
    """单笔净额快照指纹：指令/公司行动版本或任何金额变动都会改变指纹。"""
    basis = {key: entry[key] for key in (
        "reference", "instrument", "direction", "quantity", "price", "fees",
        "instruction_version", "ca_id", "ca_version",
    )}
    raw = json.dumps(basis, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def batch_fingerprint(entries: List[Dict[str, Any]]) -> str:
    raw = json.dumps([entry["fingerprint"] for entry in sorted(entries, key=lambda item: item["reference"])], sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_batch_entries(instructions: List[Dict[str, Any]], ca_versions: Dict[str, List[Dict[str, Any]]], at_iso: Optional[str] = None) -> List[Dict[str, Any]]:
    entries = [build_entry(instruction, ca_factor_for(instruction, ca_versions, at_iso)) for instruction in instructions]
    return sorted(entries, key=lambda item: item["reference"])


def validate_instruction(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload or {})
    data = {
        "reference": text(p, "reference"),
        "account": text(p, "account"),
        "organization": text(p, "organization"),
        "instrument": text(p, "instrument"),
        "currency": choice(p, "currency", CURRENCIES),
        "settlement_date": date_text(p, "settlement_date"),
        "direction": choice(p, "direction", DIRECTIONS),
        "quantity": integer(p, "quantity", 1),
        "price": number(p, "price", 0),
        "fees": number(p, "fees", 0),
    }
    return data


def validate_ca_version(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload or {})
    return {
        "instrument": text(p, "instrument"),
        "factor": number(p, "factor", 0.000001),
        "cash_factor": number(p, "cash_factor", 0) if "cash_factor" in p else 0.0,
        "note": p.get("note", "") if isinstance(p.get("note", ""), str) else "",
    }


def validate_receipt(payload: Dict[str, Any]) -> Dict[str, Any]:
    p = dict(payload or {})
    data = {
        "receipt_no": text(p, "receipt_no"),
        "reference": text(p, "reference"),
        "instruction_version": integer(p, "instruction_version", 1),
        "amount": number(p, "amount"),
    }
    data["batch_no"] = p.get("batch_no") or None
    if data["batch_no"] is not None and not isinstance(data["batch_no"], str):
        raise ValidationError("batch_no必须是文本")
    return data


def reconcile(entries: List[Dict[str, Any]], receipts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """纯对账：重复回执只记一次，版本不符或金额不符保留差异，缺失回执也算差异。

    差异优先级：重复回执 > 引用不存在 > 版本不符 > 金额不符 > 缺失回执。
    返回(差异列表, 各类型计数)；无差异时计数全为0，交收可继续。
    """
    discrepancies: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    duplicates: Dict[str, List[str]] = {}
    for receipt in sorted(receipts, key=lambda item: item["id"]):
        if receipt["receipt_no"] in seen:
            duplicates.setdefault(receipt["receipt_no"], [seen[receipt["receipt_no"]]["reference"]]).append(receipt["reference"])
            continue
        seen[receipt["receipt_no"]] = receipt

    used: Dict[int, str] = {}
    by_reference = {entry["reference"]: entry for entry in entries}
    for receipt in seen.values():
        entry = by_reference.get(receipt["reference"])
        if entry is None:
            discrepancies.append({
                "type": "unknown_reference",
                "receipt_id": receipt["id"],
                "receipt_no": receipt["receipt_no"],
                "reference": receipt["reference"],
                "detail": "回执引用的指令不在批次内",
            })
            continue
        used[receipt["id"]] = receipt["reference"]
        if int(receipt["instruction_version"]) != int(entry["instruction_version"]):
            discrepancies.append({
                "type": "version_mismatch",
                "receipt_id": receipt["id"],
                "receipt_no": receipt["receipt_no"],
                "reference": entry["reference"],
                "detail": "回执指令版本%s与批次固定版本%s不符" % (receipt["instruction_version"], entry["instruction_version"]),
            })
            continue
        if money(receipt["amount"]) != money(entry["net_amount"]):
            discrepancies.append({
                "type": "amount_mismatch",
                "receipt_id": receipt["id"],
                "receipt_no": receipt["receipt_no"],
                "reference": entry["reference"],
                "detail": "回执金额%s与已确认净额%s不符" % (money(receipt["amount"]), entry["net_amount"]),
            })

    for receipt_no, references in duplicates.items():
        discrepancies.append({
            "type": "duplicate_receipt",
            "receipt_no": receipt_no,
            "reference": references[-1],
            "detail": "回执号重复，仅最早一笔参与对账",
        })

    matched_refs = set(used.values())
    for entry in entries:
        if entry["reference"] not in matched_refs:
            discrepancies.append({
                "type": "missing_receipt",
                "reference": entry["reference"],
                "detail": "缺少托管回执",
            })

    counters = {kind: 0 for kind in DISCREPANCY_TYPES}
    for item in discrepancies:
        counters[item["type"]] += 1
    return discrepancies, counters
