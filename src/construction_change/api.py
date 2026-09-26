"""无第三方依赖的施工变更影响审批 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import ChangeError, ValidationFailed
from .service import ChangeService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: ChangeService) -> None:
        self.service = service
        # 单个 SQLite 连接在请求间串行复用。
        self._lock = threading.Lock()

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        with self._lock:
            return self._handle(method, target, headers, body)

    def _handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/zones":
                return Response(201, self.service.create_zone(actor, payload))
            if len(parts) >= 2 and parts[0] == "zones":
                zone_id = parts[1]
                if method == "POST" and len(parts) == 3 and parts[2] == "service-versions":
                    return Response(201, self.service.publish_service_version(actor, zone_id, payload))
                if method == "GET" and len(parts) == 3 and parts[2] == "service-versions":
                    return Response(200, self.service.list_service_versions(actor, zone_id))
                if method == "GET" and len(parts) == 4 and parts[2:] == ["service-versions", "current"]:
                    return Response(200, self.service.get_current_version(actor, zone_id))
                if method == "POST" and len(parts) == 3 and parts[2] == "buildings":
                    return Response(201, self.service.create_building(actor, zone_id, payload))
                if method == "GET" and len(parts) == 3 and parts[2] == "buildings":
                    return Response(200, self.service.list_buildings(actor, zone_id))
                if method == "POST" and len(parts) == 3 and parts[2] == "applications":
                    return Response(201, self.service.register_application(actor, zone_id, payload))
                if method == "GET" and len(parts) == 3 and parts[2] == "applications":
                    return Response(200, self.service.list_applications(actor, zone_id, query.get("state", [None])[0]))
                if method == "POST" and len(parts) == 3 and parts[2] == "changes":
                    return Response(201, self.service.create_change(actor, zone_id, payload))
            if len(parts) == 2 and parts[0] == "applications":
                if method == "GET":
                    return Response(200, self.service.get_application(actor, parts[1]))
            if len(parts) == 3 and parts[0] == "applications" and parts[2] == "withdraw" and method == "POST":
                return Response(200, self.service.withdraw_application(actor, parts[1], int(payload["expected_revision"])))
            if len(parts) >= 2 and parts[0] == "changes":
                change_id = parts[1]
                if method == "GET" and len(parts) == 2:
                    return Response(200, self.service.get_change(actor, change_id))
                if len(parts) == 3:
                    action = parts[2]
                    if method == "POST" and action == "revisions":
                        return Response(201, self.service.revise_change(actor, change_id, int(payload["expected_revision"]), payload["payload"]))
                    if method == "GET" and action == "revisions":
                        return Response(200, self.service.change_revisions(actor, change_id))
                    if method == "POST" and action == "submit":
                        return Response(200, self.service.submit_change(actor, change_id, int(payload["expected_revision"])))
                    if method == "POST" and action == "approve":
                        return Response(200, self.service.approve_change(actor, change_id, int(payload["expected_revision"])))
                    if method == "POST" and action == "reject":
                        return Response(200, self.service.reject_change(actor, change_id, int(payload["expected_revision"]), payload["reason"]))
                    if method == "POST" and action == "cancel":
                        return Response(200, self.service.cancel_change(actor, change_id, int(payload["expected_revision"])))
                    if method == "POST" and action == "start":
                        return Response(200, self.service.start_execution(actor, change_id))
                    if method == "POST" and action == "receipts":
                        return Response(201, self.service.post_receipt(actor, change_id, payload))
                    if method == "POST" and action == "rollback":
                        return Response(200, self.service.begin_rollback(actor, change_id))
                    if method == "POST" and action == "takeover":
                        return Response(200, self.service.begin_takeover(actor, change_id, payload["note"]))
                    if method == "POST" and action == "close-takeover":
                        return Response(200, self.service.close_takeover(actor, change_id, payload["report"]))
                    if method == "GET" and action == "impact":
                        return Response(200, self.service.change_impact(actor, change_id))
                    if method == "GET" and action == "explanation":
                        return Response(200, self.service.explain_change(actor, change_id))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ChangeError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ConstructionChange/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动安置片区施工变更影响审批服务")
    parser.add_argument("--database", type=Path, default=Path("construction_change.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(ChangeService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
