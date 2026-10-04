import json
import tempfile
import threading
import unittest
import urllib.request
from http.client import HTTPResponse
from pathlib import Path

from app import build_service
from src.http_api import create_server


def instruction(reference, org="ORG-A"):
    return {"reference": reference, "organization": org, "account": "ACC1", "currency": "CNY",
            "settlement_date": "2026-10-06", "instrument": "IBM", "direction": "pay",
            "quantity": 100, "price": 10.0, "fees": 0.0}


class HttpApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        service = build_service(str(Path(self.temp.name) / "http.db"))
        self.server = create_server("127.0.0.1", 0, service, Path("static"))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _request(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request("http://127.0.0.1:%s%s" % (self.port, path), data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            response: HTTPResponse = urllib.request.urlopen(request)
            return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health_and_auth(self):
        status, body = self._request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["database"])
        status, body = self._request("GET", "/api/batches")
        self.assertEqual(status, 403)

    def test_end_to_end_over_http(self):
        member = {"X-User-Id": "m1", "X-Role": "member", "X-Org": "ORG-A"}
        officer = {"X-User-Id": "s1", "X-Role": "settlement_officer"}
        custodian = {"X-User-Id": "c1", "X-Role": "custodian"}

        status, body = self._request("POST", "/api/instructions", instruction("I1"), member)
        self.assertEqual(status, 201)

        status, body = self._request("POST", "/api/batches", {"batch_no": "NB-1", "references": ["I1"]}, officer)
        self.assertEqual(status, 201)
        self.assertEqual(body["total_net"], -1000.0)
        version = body["version"]

        status, body = self._request("POST", "/api/batches/NB-1/confirm", {"expected_version": version}, officer)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "confirmed")

        receipt = {"receipt_no": "RC-1", "reference": "I1", "instruction_version": 1, "amount": -1000.0, "batch_no": "NB-1"}
        status, first = self._request("POST", "/api/receipts", receipt, custodian)
        self.assertEqual(status, 201)
        status, second = self._request("POST", "/api/receipts", receipt, custodian)
        self.assertEqual(status, 200)
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["id"], second["id"])

        status, body = self._request("POST", "/api/batches/NB-1/reconcile", {}, officer)
        self.assertEqual(status, 200)
        self.assertFalse(body["hold"])

        status, body = self._request("POST", "/api/batches/NB-1/settle", {}, officer)
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "settled")

        status, body = self._request("GET", "/api/batches/NB-1", None, officer)
        self.assertEqual(status, 200)
        status, body = self._request("GET", "/api/batches/NB-1/audit", None, officer)
        self.assertEqual([e["action"] for e in body["items"]], ["created", "confirmed", "settled"])
        status, body = self._request("GET", "/api/stats", None, officer)
        self.assertEqual(body["batches"]["settled"], 1)

    def test_cross_org_member_scoped_over_http(self):
        member_a = {"X-User-Id": "m1", "X-Role": "member", "X-Org": "ORG-A"}
        member_b = {"X-User-Id": "m2", "X-Role": "member", "X-Org": "ORG-B"}
        officer = {"X-User-Id": "s1", "X-Role": "settlement_officer"}
        self._request("POST", "/api/instructions", instruction("I1", "ORG-A"), member_a)
        self._request("POST", "/api/instructions", instruction("J1", "ORG-B"), member_b)
        self._request("POST", "/api/batches", {"batch_no": "BA", "references": ["I1"]}, officer)
        self._request("POST", "/api/batches", {"batch_no": "BB", "references": ["J1"]}, officer)
        status, body = self._request("GET", "/api/batches", None, member_a)
        self.assertEqual([item["batch_no"] for item in body["items"]], ["BA"])
        status, _ = self._request("GET", "/api/batches/BB", None, member_a)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
