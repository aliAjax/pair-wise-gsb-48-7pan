import unittest

from src import rules


def instr(reference="I1", direction="pay", quantity=100, price=10.0, fees=1.0, instrument="IBM", version=1):
    return {"id": 1, "reference": reference, "account": "ACC1", "organization": "ORG-A",
            "instrument": instrument, "currency": "CNY", "settlement_date": "2026-10-06",
            "direction": direction, "quantity": quantity, "price": price, "fees": fees, "version": version}


def ca(version=1, factor=1.0, cash_factor=0.0, at="2026-10-04T10:00:00+00:00"):
    return {"ca_id": "CA-IBM", "instrument": "IBM", "version": version, "factor": factor,
            "cash_factor": cash_factor, "published_at": at}


class NettingRulesTest(unittest.TestCase):
    def test_pay_and_receive_are_signed(self):
        pay = rules.build_entry(instr("I1", "pay", fees=1.0), rules.ca_factor_for(instr(), {}))
        receive = rules.build_entry(instr("I2", "receive", 100, 10.0, 1.0), rules.ca_factor_for(instr(), {}))
        # 付款支出=成交金额+费用；收款实收=成交金额-费用
        self.assertEqual(pay["net_amount"], -1001.0)
        self.assertEqual(receive["net_amount"], 999.0)
        self.assertIsNone(pay["ca_version"])

    def test_ca_factor_adjusts_quantity_gross_and_pins_version(self):
        versions = {"IBM": [ca(version=2, factor=2.0)]}
        selected = rules.ca_factor_for(instr(), versions)
        entry = rules.build_entry(instr(), selected)
        self.assertEqual(entry["ca_version"], 2)
        self.assertEqual(entry["effective_quantity"], 200)
        self.assertEqual(entry["adjusted_gross"], 2000.0)
        self.assertEqual(entry["net_amount"], -2001.0)

    def test_only_versions_published_before_batch_time_apply(self):
        versions = {"IBM": [ca(version=2, at="2026-10-05T00:00:00+00:00"), ca(version=1, at="2026-10-01T00:00:00+00:00")]}
        selected = rules.ca_factor_for(instr(), versions, at_iso="2026-10-04T00:00:00+00:00")
        self.assertEqual(selected["ca_version"], 1)

    def test_instruction_version_change_changes_fingerprint(self):
        e1 = rules.build_entry(instr(version=1), rules.ca_factor_for(instr(version=1), {}))
        e2 = rules.build_entry(instr(version=2), rules.ca_factor_for(instr(version=2), {}))
        self.assertNotEqual(e1["fingerprint"], e2["fingerprint"])

    def test_batch_total_nets_opposite_directions(self):
        entries = rules.build_batch_entries(
            [instr("I1", "pay", 100, 10.0, 1.0), instr("I2", "receive", 50, 20.0, 0.0)], {})
        self.assertEqual([e["reference"] for e in entries], ["I1", "I2"])
        total = rules.money(sum(rules.Decimal(str(e["net_amount"])) for e in entries))
        self.assertEqual(total, -1.0)

    def test_validate_instruction_rejects_bad_payload(self):
        from src.domain import ValidationError
        with self.assertRaises(ValidationError):
            rules.validate_instruction({"reference": "X"})
        with self.assertRaises(ValidationError):
            rules.validate_instruction({**instr(), "settlement_date": "10/06/2026"})


class ReconciliationRulesTest(unittest.TestCase):
    ENTRIES = rules.build_batch_entries([instr("I1", "pay", 100, 10.0, 0.0), instr("I2", "receive", 50, 20.0, 0.0)], {})

    def receipt(self, rid, no, ref, version=1, amount=-1000.0):
        return {"id": rid, "receipt_no": no, "reference": ref, "instruction_version": version, "amount": amount}

    def test_all_correct_passes(self):
        receipts = [self.receipt(1, "R1", "I1", 1, -1000.0), self.receipt(2, "R2", "I2", 1, 1000.0)]
        discrepancies, counters = rules.reconcile(self.ENTRIES, receipts)
        self.assertEqual(discrepancies, [])
        self.assertEqual(sum(counters.values()), 0)

    def test_missing_receipt_holds(self):
        discrepancies, counters = rules.reconcile(self.ENTRIES, [self.receipt(1, "R1", "I1")])
        self.assertEqual(counters["missing_receipt"], 1)
        self.assertEqual(discrepancies[0]["reference"], "I2")

    def test_unknown_reference_is_kept(self):
        receipts = [self.receipt(1, "R1", "I1"), self.receipt(2, "R2", "I2", amount=1000.0),
                    self.receipt(3, "RX", "GHOST")]
        _d, counters = rules.reconcile(self.ENTRIES, receipts)
        self.assertEqual(counters["unknown_reference"], 1)

    def test_version_mismatch_holds(self):
        receipts = [self.receipt(1, "R1", "I1", version=2), self.receipt(2, "R2", "I2", amount=1000.0)]
        discrepancies, counters = rules.reconcile(self.ENTRIES, receipts)
        self.assertEqual(counters["version_mismatch"], 1)
        self.assertIn("版本", discrepancies[0]["detail"])

    def test_amount_mismatch_holds(self):
        receipts = [self.receipt(1, "R1", "I1", amount=-999.0), self.receipt(2, "R2", "I2", amount=1000.0)]
        _d, counters = rules.reconcile(self.ENTRIES, receipts)
        self.assertEqual(counters["amount_mismatch"], 1)

    def test_duplicate_receipt_counted_once_and_flagged(self):
        receipts = [self.receipt(1, "DUP", "I1"), self.receipt(2, "DUP", "I2", amount=1000.0),
                    self.receipt(3, "R2", "I2", amount=1000.0)]
        discrepancies, counters = rules.reconcile(self.ENTRIES, receipts)
        self.assertEqual(counters["duplicate_receipt"], 1)
        self.assertEqual(counters["missing_receipt"], 0)
        # 最早一笔参与对账，I1 已匹配，不因第二笔把 I1 算缺失
        self.assertTrue(all(d["type"] != "missing_receipt" for d in discrepancies))

    def test_out_of_order_delivery_still_matches(self):
        # 乱序送达：先到I2后到I1
        receipts = [self.receipt(2, "R2", "I2", amount=1000.0), self.receipt(1, "R1", "I1")]
        _d, counters = rules.reconcile(self.ENTRIES, receipts)
        self.assertEqual(sum(counters.values()), 0)


if __name__ == "__main__":
    unittest.main()
