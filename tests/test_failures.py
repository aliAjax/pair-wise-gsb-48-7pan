import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from tests.test_workflow import instruction, MEMBER, OFFICER, CUSTODIAN


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)
        self.service.submit_instruction(MEMBER(), instruction("I1"))

    def tearDown(self):
        self.temp.cleanup()

    def test_member_cannot_submit_other_org(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_instruction(Actor("m-2", "member", "ORG-B"), instruction("X1", org="ORG-A"))

    def test_member_without_org_denied(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_instruction(Actor("m-3", "member", ""), instruction("X1"))

    def test_member_cannot_build_batch_and_is_scoped(self):
        with self.assertRaises(PermissionDenied):
            self.service.build_batch(MEMBER(), "B-X", {"references": ["I1"]})
        # 会员看不到其他机构批次
        self.service.submit_instruction(Actor("m-2", "member", "ORG-B"), instruction("J1", org="ORG-B", account="B-ACC"))
        self.service.build_batch(OFFICER, "B-OWN", {"references": ["J1"]})
        self.service.build_batch(OFFICER, "B-A", {"references": ["I1"]})
        self.assertEqual([b["batch_no"] for b in self.service.list_batches(MEMBER(), {})], ["B-A"])
        with self.assertRaises(PermissionDenied):
            self.service.get_batch(MEMBER(), "B-OWN")

    def test_cross_org_grouping_rejected(self):
        self.service.submit_instruction(Actor("m-2", "member", "ORG-B"), instruction("J1", org="ORG-B"))
        with self.assertRaises(ValidationError):
            self.service.build_batch(OFFICER, "B-MIX", {"references": ["I1", "J1"]})

    def test_different_account_currency_day_cannot_share_batch(self):
        self.service.submit_instruction(MEMBER(), instruction("I3", account="ACC2"))
        with self.assertRaises(ValidationError):
            self.service.build_batch(OFFICER, "B-G", {"references": ["I1", "I3"]})

    def test_unknown_instruction_reference(self):
        with self.assertRaises(Exception):
            self.service.build_batch(OFFICER, "B-404", {"references": ["NOPE"]})

    def test_duplicate_reference_rejected(self):
        with self.assertRaises(Conflict):
            self.service.submit_instruction(MEMBER(), instruction("I1"))

    def test_stale_instruction_version_rejected(self):
        with self.assertRaises(Conflict):
            self.service.amend_instruction(MEMBER(), "I1", 99, instruction("I1", price=11.0))

    def test_confirm_invalid_batch_rejected(self):
        batch = self.service.build_batch(OFFICER, "B-INV", {"references": ["I1"]})
        self.service.amend_instruction(MEMBER(), "I1", 1, instruction("I1", price=11.0))
        with self.assertRaises(Conflict):
            self.service.confirm_batch(OFFICER, "B-INV", batch["version"])

    def test_concurrent_confirm_first_writer_wins(self):
        self.service.build_batch(OFFICER, "B-RACE", {"references": ["I1"]})
        outcomes = []
        barrier = threading.Barrier(2)

        def confirm(user):
            service = build_service(self.db_path)
            barrier.wait()
            try:
                result = service.confirm_batch(Actor(user, "settlement_officer"), "B-RACE", None)
                outcomes.append(("ok", result["confirmed_by"]))
            except Conflict:
                outcomes.append(("conflict", user))

        threads = [threading.Thread(target=confirm, args=(user,)) for user in ("s-1", "s-2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(outcomes), 2)
        winners = [item for item in outcomes if item[0] == "ok"]
        losers = [item for item in outcomes if item[0] == "conflict"]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 1)
        # 两个主管都可能先到；最终写入的confirmed_by与获胜者一致
        final = self.service.get_batch(OFFICER, "B-RACE")
        self.assertEqual(final["confirmed_by"], winners[0][1])

    def test_double_confirm_after_the_fact_rejected(self):
        batch = self.service.build_batch(OFFICER, "B-2X", {"references": ["I1"]})
        self.service.confirm_batch(OFFICER, "B-2X", batch["version"])
        with self.assertRaises(Conflict):
            self.service.confirm_batch(OFFICER, "B-2X", batch["version"] + 1)

    def test_confirmed_batch_cannot_rebook(self):
        batch = self.service.build_batch(OFFICER, "B-FRZ", {"references": ["I1"]})
        self.service.confirm_batch(OFFICER, "B-FRZ", batch["version"])
        with self.assertRaises(Conflict):
            self.service.build_batch(OFFICER, "B-FRZ", {"references": ["I1"]})

    def test_settle_before_confirm_rejected(self):
        self.service.build_batch(OFFICER, "B-NS", {"references": ["I1"]})
        with self.assertRaises(Conflict):
            self.service.settle_batch(OFFICER, "B-NS")

    def test_settle_holds_on_discrepancy(self):
        batch = self.service.build_batch(OFFICER, "B-H", {"references": ["I1"]})
        self.service.confirm_batch(OFFICER, "B-H", batch["version"])
        # 回执引用不存在的指令
        self.service.record_receipt(CUSTODIAN, {"receipt_no": "R-G", "reference": "GHOST", "instruction_version": 1, "amount": -1000.0, "batch_no": "B-H"})
        with self.assertRaises(Conflict):
            self.service.settle_batch(OFFICER, "B-H")

    def test_reconcile_requires_confirmed(self):
        self.service.build_batch(OFFICER, "B-OC", {"references": ["I1"]})
        with self.assertRaises(Conflict):
            self.service.reconcile_batch(OFFICER, "B-OC")

    def test_custodian_cannot_confirm(self):
        self.service.build_batch(OFFICER, "B-R", {"references": ["I1"]})
        with self.assertRaises(PermissionDenied):
            self.service.confirm_batch(CUSTODIAN, "B-R", None)

    def test_officer_cannot_post_receipt(self):
        with self.assertRaises(PermissionDenied):
            self.service.record_receipt(OFFICER, {"receipt_no": "R1", "reference": "I1", "instruction_version": 1, "amount": -1000.0})


if __name__ == "__main__":
    unittest.main()
