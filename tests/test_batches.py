import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, SettlementHeld, ValidationError
from src.rules import BATCH_CONFIRMED, BATCH_INVALID, BATCH_OPEN, BATCH_SETTLED


def buy(reference, quantity=100, price=10.0, side="buy", currency="CNY", day=2, account="ACC-1", org=None, instrument=None):
    return {
        "reference": reference, "org": org,
        "data": {"instrument": instrument or reference, "side": side, "quantity": quantity, "price": price, "fees": 0.0,
                 "currency": currency, "settlement_day": day, "corporate_action": "none",
                 "action_ratio": 1.0, "account": account},
    }


OFFICER = lambda org="MEMBER-A": Actor("officer", "settlement_officer", org)
TRADER = lambda org="MEMBER-A": Actor("trader-1", "trader", org)


class BatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.actor = OFFICER()
        self.r1 = self._create(buy("INS-1"))
        self.r2 = self._create(buy("INS-2", quantity=40))
        self.r3 = self._create(buy("INS-3", quantity=60, side="sell"))

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, spec):
        return self.service.create(TRADER(spec.get("org") or "MEMBER-A"), spec["reference"], spec["data"])

    def _compose(self, refs, batch_no="B-1", actor=None):
        return self.service.compose_batch(actor or self.actor, batch_no, refs)

    def test_compose_nets_by_account_currency_day(self):
        batch = self._compose(["INS-1", "INS-2", "INS-3"])
        self.assertEqual(batch["state"], BATCH_OPEN)
        self.assertEqual(batch["account"], "ACC-1")
        self.assertEqual(batch["currency"], "CNY")
        self.assertEqual(batch["settlement_day"], 2)
        self.assertIsNone(batch["frozen_total"])
        self.assertEqual(len(batch["items"]), 3)

    def test_mixed_group_key_rejected(self):
        self._create(buy("INS-USD", currency="USD"))
        with self.assertRaises(ValidationError):
            self._compose(["INS-1", "INS-USD"])
        self._create(buy("INS-DAY", day=3))
        with self.assertRaises(ValidationError):
            self._compose(["INS-1", "INS-DAY"])
        self._create(buy("INS-ACC", account="ACC-2"))
        with self.assertRaises(ValidationError):
            self._compose(["INS-1", "INS-ACC"])

    def test_trader_cannot_compose_and_cross_org_rejected(self):
        with self.assertRaises(PermissionDenied):
            self.service.compose_batch(Actor("t", "trader", "MEMBER-A"), "B-X", ["INS-1"])
        self._create(buy("INS-B", org="MEMBER-B"))
        with self.assertRaises(PermissionDenied):
            self._compose(["INS-1", "INS-B"])

    def test_same_batch_no_is_idempotent(self):
        first = self._compose(["INS-1"], batch_no="B-IDEM")
        again = self._compose(["INS-1", "INS-2"], batch_no="B-IDEM")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(again["items"]), 1)
        batches = self.service.list_batches(self.actor)
        self.assertEqual(len([b for b in batches if b["batch_no"] == "B-IDEM"]), 1)

    def test_confirm_freezes_amounts_and_version(self):
        batch = self._compose(["INS-1", "INS-2", "INS-3"])
        confirmed = self.service.confirm_batch(self.actor, batch["id"], batch["version"])
        self.assertEqual(confirmed["state"], BATCH_CONFIRMED)
        # 100*10 买 + 40*10 买 - 60*10 卖 = 800
        self.assertEqual(confirmed["frozen_total"], 800.0)
        self.assertEqual(confirmed["version"], 2)
        for item in confirmed["items"]:
            self.assertEqual(item["frozen_ca_version"], 1)
            self.assertIsNotNone(item["frozen_net_amount"])
        # 确认后指令改动，已确认金额保持冻结
        self.service.act(Actor("co", "corporate_actions", "MEMBER-A"), self.r1["id"], self.r1["version"],
                         "revise_corporate", {"action_ratio": 2.0})
        detail = self.service.get_batch(self.actor, batch["id"])
        self.assertEqual(detail["frozen_total"], 800.0)
        self.assertEqual(detail["state"], BATCH_CONFIRMED)
        # 列表/详情/统计/审计读取同一批次结果
        listed = self.service.list_batches(self.actor, state=BATCH_CONFIRMED)[0]
        self.assertEqual(listed["frozen_total"], 800.0)
        stats = self.service.batch_stats(self.actor)
        self.assertEqual(stats[BATCH_CONFIRMED]["total"], 1)
        self.assertEqual(stats[BATCH_CONFIRMED]["frozen_total"], 800.0)
        actions = [event["action"] for event in self.service.batch_timeline(self.actor, batch["id"])]
        self.assertIn("batch_confirmed", actions)

    def test_instruction_change_invalidates_open_batch_then_recompute(self):
        batch = self._compose(["INS-1", "INS-2"])
        # 组批后指令再改动 -> 未确认批次立即失效
        self.service.act(Actor("off", "settlement_officer", "MEMBER-A"), self.r1["id"], self.r1["version"],
                         "approve", {})
        detail = self.service.get_batch(self.actor, batch["id"])
        self.assertEqual(detail["state"], BATCH_INVALID)
        with self.assertRaises(Conflict):
            self.service.confirm_batch(self.actor, batch["id"], batch["version"])
        # 待重算后恢复开放
        recomputed = self.service.recompute_batch(self.actor, batch["id"])
        self.assertEqual(recomputed["state"], BATCH_OPEN)
        confirmed = self.service.confirm_batch(self.actor, batch["id"], recomputed["version"])
        self.assertEqual(confirmed["state"], BATCH_CONFIRMED)

    def test_receipts_duplicate_out_of_order_and_missing_instruction(self):
        batch = self._compose(["INS-1", "INS-2", "INS-3"])
        confirmed = self.service.confirm_batch(self.actor, batch["id"], batch["version"])
        receipt = {"custodian_receipt_id": "RCP-1", "reference": "INS-1", "received_version": 1,
                   "delivered_quantity": 100, "net_amount": 1000.0}
        first = self.service.post_receipt(self.actor, receipt)
        self.assertTrue(first["matched"])
        # 重复回执只记一次
        with self.assertRaises(Conflict):
            self.service.post_receipt(self.actor, dict(receipt))
        # 乱序：后两笔先到不影响结果
        self.service.post_receipt(self.actor, {"custodian_receipt_id": "RCP-3", "reference": "INS-3",
                                               "received_version": 1, "delivered_quantity": 60,
                                               "net_amount": -600.0})
        self.service.post_receipt(self.actor, {"custodian_receipt_id": "RCP-2", "reference": "INS-2",
                                               "received_version": 1, "delivered_quantity": 40,
                                               "net_amount": 400.0})
        detail = self.service.get_batch(self.actor, confirmed["id"])
        self.assertEqual(len(detail["receipts"]), 3)
        # 引用不存在指令
        orphan = self.service.post_receipt(self.actor, {"custodian_receipt_id": "RCP-X", "reference": "NOPE",
                                                        "received_version": 1, "delivered_quantity": 1,
                                                        "net_amount": 1.0})
        self.assertIsNone(orphan["batch"])
        self.assertEqual(orphan["receipt"]["status"], "orphan")

    def test_version_mismatch_keeps_discrepancy_and_holds_settlement(self):
        # 带拆股的指令：组批确认固定 v1
        spec = buy("INS-SPLIT", quantity=100, price=10.0)
        spec["data"]["corporate_action"] = "split"
        spec["data"]["action_ratio"] = 2.0
        record = self._create(spec)
        batch = self._compose(["INS-SPLIT"], batch_no="B-SPLIT")
        confirmed = self.service.confirm_batch(self.actor, batch["id"], batch["version"])
        frozen_qty = confirmed["items"][0]["frozen_quantity"]
        self.assertEqual(frozen_qty, 200)
        # 公司行动换版到 v2，已确认金额仍冻结
        self.service.act(Actor("co", "corporate_actions", "MEMBER-A"), record["id"], record["version"],
                         "revise_corporate", {"action_ratio": 3.0})
        # 托管回执按 v3 数量/版本到达 -> 版本对不上，停住
        with self.assertRaises(SettlementHeld):
            self.service.post_receipt(self.actor, {"custodian_receipt_id": "RCP-S", "reference": "INS-SPLIT",
                                                   "received_version": 2, "delivered_quantity": 300,
                                                   "net_amount": 1000.0})
        detail = self.service.get_batch(self.actor, confirmed["id"])
        self.assertTrue(detail["settlement_held"])
        self.assertEqual(len(detail["discrepancies"]), 1)
        with self.assertRaises(Conflict):
            self.service.complete_settlement(self.actor, confirmed["id"])
        # 即使后来补一张与冻结版本一致的正确回执，差异保留、交收仍停住
        self.service.post_receipt(self.actor, {"custodian_receipt_id": "RCP-S-OK", "reference": "INS-SPLIT",
                                               "received_version": 1, "delivered_quantity": 200,
                                               "net_amount": 1000.0})
        with self.assertRaises(Conflict):
            self.service.complete_settlement(self.actor, confirmed["id"])

    def test_late_receipt_after_confirm_matches_frozen_amounts(self):
        batch = self._compose(["INS-1"])
        confirmed = self.service.confirm_batch(self.actor, batch["id"], batch["version"])
        # 回执晚到：按确认时冻结的版本与金额匹配即可
        result = self.service.post_receipt(self.actor, {"custodian_receipt_id": "RCP-LATE", "reference": "INS-1",
                                                        "received_version": 1, "delivered_quantity": 100,
                                                        "net_amount": 1000.0})
        self.assertTrue(result["matched"])
        settled = self.service.complete_settlement(self.actor, confirmed["id"])
        self.assertEqual(settled["state"], BATCH_SETTLED)

    def test_early_receipt_reconciled_on_confirm(self):
        # 回执早到（批次还未确认），确认时统一对账
        batch = self._compose(["INS-1"], batch_no="B-EARLY")
        pending = self.service.post_receipt(self.actor, {"custodian_receipt_id": "RCP-EARLY", "reference": "INS-1",
                                                         "received_version": 1, "delivered_quantity": 100,
                                                         "net_amount": 1000.0})
        self.assertEqual(pending["receipt"]["status"], "pending")
        confirmed = self.service.confirm_batch(self.actor, batch["id"], batch["version"])
        self.assertFalse(confirmed["settlement_held"])
        self.assertEqual(confirmed["receipts"][0]["status"], "matched")

    def test_other_org_cannot_read_batch(self):
        batch = self._compose(["INS-1"], batch_no="B-ORG")
        other = OFFICER("MEMBER-B")
        with self.assertRaises(Exception):
            self.service.get_batch(other, batch["id"])
        self.assertEqual(self.service.list_batches(other), [])
