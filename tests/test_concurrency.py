import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


def buy(reference, quantity=100, price=10.0):
    return {"instrument": "ACME", "side": "buy", "quantity": quantity, "price": price, "fees": 0.0,
            "currency": "CNY", "settlement_day": 2, "corporate_action": "none",
            "action_ratio": 1.0, "account": "ACC-1"}


class ConcurrentConfirmTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.service.create(Actor("t", "trader", "MEMBER-A"), "INS-1", buy("INS-1"))
        self.batch = self.service.compose_batch(
            Actor("officer-1", "settlement_officer", "MEMBER-A"), "B-CONC", ["INS-1"]
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_two_supervisors_only_first_succeeds(self):
        results = []
        errors = []
        barrier = threading.Barrier(2)

        def confirm(user):
            barrier.wait()
            try:
                batch = self.service.confirm_batch(
                    Actor(user, "settlement_officer", "MEMBER-A"), self.batch["id"], self.batch["version"]
                )
                results.append(batch)
            except Conflict as exc:
                errors.append(str(exc))

        threads = [threading.Thread(target=confirm, args=(u,)) for u in ("officer-1", "officer-2")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(results[0]["state"], "confirmed")
        self.assertIn(results[0]["confirmed_by"], ("officer-1", "officer-2"))
        # 失败方按原批次号重试读取不会重复记账，批次仍只有一个
        self.assertEqual(len(self.service.list_batches(Actor("officer-2", "settlement_officer", "MEMBER-A"))), 1)

    def test_stale_version_rejected(self):
        self.service.confirm_batch(
            Actor("officer-1", "settlement_officer", "MEMBER-A"), self.batch["id"], self.batch["version"]
        )
        with self.assertRaises(Conflict):
            self.service.confirm_batch(
                Actor("officer-1", "settlement_officer", "MEMBER-A"), self.batch["id"], self.batch["version"]
            )
