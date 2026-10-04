import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor


def instruction(reference, org="ORG-A", account="ACC1", currency="CNY", day="2026-10-06",
                instrument="IBM", direction="pay", quantity=100, price=10.0, fees=0.0):
    return {"reference": reference, "organization": org, "account": account, "currency": currency,
            "settlement_date": day, "instrument": instrument, "direction": direction,
            "quantity": quantity, "price": price, "fees": fees}


MEMBER = lambda org="ORG-A": Actor("m-1", "member", org)
OFFICER = Actor("s-1", "settlement_officer")
OFFICER2 = Actor("s-2", "settlement_officer")
CUSTODIAN = Actor("c-1", "custodian")
CA_ACTOR = Actor("ca-1", "corporate_actions")


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _two_instructions(self):
        self.service.submit_instruction(MEMBER(), instruction("I1", direction="pay", fees=1.0))
        self.service.submit_instruction(MEMBER(), instruction("I2", direction="receive", quantity=50, price=20.0))

    def test_full_netting_confirm_reconcile_settle(self):
        self._two_instructions()
        batch = self.service.build_batch(OFFICER, "B-1", {"references": ["I1", "I2"]})
        self.assertEqual(batch["state"], "open")
        self.assertEqual({e["reference"]: e["net_amount"] for e in batch["entries"]}, {"I1": -1001.0, "I2": 1000.0})
        self.assertEqual(batch["total_net"], -1.0)

        confirmed = self.service.confirm_batch(OFFICER, "B-1", batch["version"])
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["confirmed_by"], "s-1")

        # 确认后公司行动换版：已确认金额保持冻结
        self.service.publish_ca(CA_ACTOR, {"instrument": "IBM", "factor": 2.0})
        frozen = self.service.get_batch(OFFICER, "B-1")
        self.assertEqual(frozen["total_net"], -1.0)
        self.assertTrue(all(e["ca_version"] in (None, 1) for e in frozen["entries"]))

        # 确认后指令改动：已确认批次仍冻结
        amended = self.service.amend_instruction(
            MEMBER(), "I1", 1, instruction("I1", direction="pay", fees=9.0))
        self.assertEqual(amended["version"], 2)
        self.assertEqual(self.service.get_batch(OFFICER, "B-1")["total_net"], -1.0)

        # 托管回执对账（版本以确认时固定的 v1 为准）
        self.service.record_receipt(CUSTODIAN, {"receipt_no": "R-1", "reference": "I1", "instruction_version": 1, "amount": -1001.0, "batch_no": "B-1"})
        self.service.record_receipt(CUSTODIAN, {"receipt_no": "R-2", "reference": "I2", "instruction_version": 1, "amount": 1000.0, "batch_no": "B-1"})
        report = self.service.reconcile_batch(OFFICER, "B-1")
        self.assertFalse(report["hold"])
        self.assertEqual(report["frozen_total_net"], -1.0)

        settled = self.service.settle_batch(OFFICER2, "B-1")
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(settled["settled_by"], "s-2")

    def test_late_receipt_after_amend_freezes_version_discrepancy(self):
        self.service.submit_instruction(MEMBER(), instruction("I1"))
        batch = self.service.build_batch(OFFICER, "B-9", {"references": ["I1"]})
        self.service.confirm_batch(OFFICER, "B-9", batch["version"])
        self.service.amend_instruction(MEMBER(), "I1", 1, instruction("I1", price=11.0))
        # 托管回执晚到，引用新版本v2，与批次固定v1不符
        self.service.record_receipt(CUSTODIAN, {"receipt_no": "R-9", "reference": "I1", "instruction_version": 2, "amount": -1100.0, "batch_no": "B-9"})
        report = self.service.reconcile_batch(OFFICER, "B-9")
        self.assertTrue(report["hold"])
        self.assertEqual(report["counters"]["version_mismatch"], 1)
        # 差异保留，交收停住
        from src.domain import Conflict
        with self.assertRaises(Conflict):
            self.service.settle_batch(OFFICER, "B-9")

    def test_unconfirmed_batch_invalidated_and_rebuilt_with_same_no(self):
        self._two_instructions()
        batch = self.service.build_batch(OFFICER, "B-2", {"references": ["I1", "I2"]})
        self.service.amend_instruction(MEMBER(), "I2", 1, instruction("I2", direction="receive", quantity=50, price=22.0))
        invalid = self.service.get_batch(OFFICER, "B-2")
        self.assertEqual(invalid["state"], "invalid")
        self.assertTrue(invalid["invalidated_reason"])

        # 按原批次号重算重建
        rebuilt = self.service.build_batch(OFFICER, "B-2", {"references": ["I1", "I2"]})
        self.assertEqual(rebuilt["state"], "open")
        self.assertEqual(rebuilt["total_net"], 99.0)
        self.assertEqual(rebuilt["version"], 2)

    def test_ca_republish_invalidates_open_batches_only(self):
        self._two_instructions()
        self.service.build_batch(OFFICER, "B-3", {"references": ["I1", "I2"]})
        self.service.publish_ca(CA_ACTOR, {"instrument": "IBM", "factor": 1.5})
        self.assertEqual(self.service.get_batch(OFFICER, "B-3")["state"], "invalid")

    def test_duplicate_receipt_recorded_once(self):
        self.service.submit_instruction(MEMBER(), instruction("I1"))
        first = self.service.record_receipt(CUSTODIAN, {"receipt_no": "R-D", "reference": "I1", "instruction_version": 1, "amount": -1000.0})
        second = self.service.record_receipt(CUSTODIAN, {"receipt_no": "R-D", "reference": "I1", "instruction_version": 1, "amount": -1.0})
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["amount"], -1000.0)

    def test_reads_share_one_result(self):
        self._two_instructions()
        batch = self.service.build_batch(OFFICER, "B-4", {"references": ["I1", "I2"]})
        self.service.confirm_batch(OFFICER, "B-4", batch["version"])
        detail = self.service.get_batch(OFFICER, "B-4")
        listed = next(item for item in self.service.list_batches(OFFICER, {}) if item["batch_no"] == "B-4")
        timeline = self.service.batch_timeline(OFFICER, "B-4")
        stats = self.service.stats(OFFICER)
        self.assertEqual(detail["total_net"], listed["total_net"])
        self.assertEqual([e["action"] for e in timeline], ["created", "confirmed"])
        self.assertEqual(stats["batches"]["confirmed"], 1)
        self.assertEqual(detail["open_discrepancy_count"], listed["open_discrepancy_count"])


if __name__ == "__main__":
    unittest.main()
