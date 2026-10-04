"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


INSTRUCTIONS_RE = re.compile(r"^/api/instructions$")
INSTRUCTION_RE = re.compile(r"^/api/instructions/([^/]+)$")
CA_RE = re.compile(r"^/api/corporate-actions$")
BATCHES_RE = re.compile(r"^/api/batches$")
BATCH_RE = re.compile(r"^/api/batches/([^/]+)$")
BATCH_CONFIRM_RE = re.compile(r"^/api/batches/([^/]+)/confirm$")
BATCH_RECONCILE_RE = re.compile(r"^/api/batches/([^/]+)/reconcile$")
BATCH_SETTLE_RE = re.compile(r"^/api/batches/([^/]+)/settle$")
BATCH_AUDIT_RE = re.compile(r"^/api/batches/([^/]+)/audit$")
RECEIPTS_RE = re.compile(r"^/api/receipts$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "netting-settlement/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", "").strip())

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if content_type.startswith("application/json") else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                self._send(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        @staticmethod
        def _filters(query: dict) -> dict:
            return {key: values[0] for key, values in query.items() if values}

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "netting-settlement", "database": service.repository.health()})
                    return
                if parsed.path == "/":
                    self._send(200, (static_dir / "index.html").read_bytes(), "text/html; charset=utf-8")
                    return
                if parsed.path == "/api/instructions":
                    self._send(200, {"items": service.list_instructions(self._actor(), self._filters(query))})
                    return
                if INSTRUCTION_RE.match(parsed.path):
                    reference = INSTRUCTION_RE.match(parsed.path).group(1)
                    self._send(200, service.get_instruction(self._actor(), reference))
                    return
                if parsed.path == "/api/batches":
                    self._send(200, {"items": service.list_batches(self._actor(), self._filters(query))})
                    return
                match = BATCH_AUDIT_RE.match(parsed.path)
                if match:
                    self._send(200, {"items": service.batch_timeline(self._actor(), match.group(1))})
                    return
                match = BATCH_RECONCILE_RE.match(parsed.path)
                if match:
                    self._send(200, service.reconcile_batch(self._actor(), match.group(1)))
                    return
                match = BATCH_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_batch(self._actor(), match.group(1)))
                    return
                if parsed.path == "/api/receipts":
                    self._send(200, {"items": service.list_receipts(self._actor(), query.get("reference", [None])[0])})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/instructions":
                    self._send(201, service.submit_instruction(self._actor(), body))
                    return
                match = INSTRUCTION_RE.match(parsed.path)
                if match:
                    version = body.pop("expected_version", None)
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    self._send(200, service.amend_instruction(self._actor(), match.group(1), version, body))
                    return
                if parsed.path == "/api/corporate-actions":
                    self._send(201, service.publish_ca(self._actor(), body))
                    return
                if parsed.path == "/api/batches":
                    batch_no = body.get("batch_no")
                    if not isinstance(batch_no, str) or not batch_no.strip():
                        raise ValidationError("batch_no不能为空")
                    self._send(201, service.build_batch(self._actor(), batch_no.strip(), body))
                    return
                match = BATCH_CONFIRM_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if version is not None and not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    self._send(200, service.confirm_batch(self._actor(), match.group(1), version))
                    return
                match = BATCH_SETTLE_RE.match(parsed.path)
                if match:
                    self._send(200, service.settle_batch(self._actor(), match.group(1)))
                    return
                match = BATCH_SETTLE_RE.match(parsed.path)
                if match:
                    self._send(200, service.settle_batch(self._actor(), match.group(1)))
                    return
                match = BATCH_RECONCILE_RE.match(parsed.path)
                if match:
                    self._send(200, service.reconcile_batch(self._actor(), match.group(1)))
                    return
                if parsed.path == "/api/receipts":
                    # 重复回执只记一次：重复号幂等返回原记录（200）。
                    receipt = service.record_receipt(self._actor(), body)
                    self._send(200 if receipt.get("duplicate") else 201, receipt)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
