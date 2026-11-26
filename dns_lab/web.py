"""本地查询台：HTTP API + 单页控制台。

只用标准库（:mod:`http.server`）。页面放在 ``dns_lab/web_static/index.html``，
所有动态数据走 ``/api/*`` JSON 接口。

能力对应需求：

* 修改受控时钟（冻结/恢复/拨快拨慢/直接设定）；
* 修改权威回复（故障模式、增删改区域记录）；
* 查看回源次数（每台权威分别统计）；
* 查看每条返回记录的出处（缓存类别 / 权威名 / 陈旧兜底）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .authority import AuthoritativeServer, Zone
from .message import NAME_TO_TYPE, Query, RCODE_NAMES, TYPE_NAMES
from .resolver import ORIGIN_LABELS

_STATIC_DIR = Path(__file__).parent / "web_static"


class WebConsole:
    def __init__(self, lab: dict, host: str = "127.0.0.1",
                 port: int = 8080) -> None:
        self.lab = lab
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        lab = self.lab
        handler = _make_handler(lab)
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="dns-web-console",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def address(self) -> str:
        return f"http://{self.host}:{self.port}/"


def _make_handler(lab: dict):
    clock = lab["clock"]
    resolver = lab["resolver"]
    cache = lab["cache"]
    transport = lab["transport"]
    authorities: list[AuthoritativeServer] = lab["authorities"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "DnsLabConsole/1.0"

        def log_message(self, fmt, *args):  # 静默默认访问日志
            pass

        # -------------------------------------------------------------- #
        def _send_json(self, payload, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))

        def _find_authority(self, name: str) -> AuthoritativeServer:
            for auth in authorities:
                if auth.name == name:
                    return auth
            raise KeyError(name)

        # -------------------------------------------------------------- #
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path == "/api/state":
                    return self._send_json(self._state())
                if path == "/api/cache":
                    return self._send_json({"entries": cache.snapshot()})
                if path == "/api/log":
                    return self._send_json({"entries": resolver.query_log()})
                if path == "/api/stats":
                    return self._send_json(resolver.stats_snapshot())
                if path in ("/", "/index.html"):
                    return self._serve_static("index.html", "text/html; charset=utf-8")
                self._send_json({"error": "not found"}, 404)
            except Exception as exc:  # 保证异常也返回 JSON
                self._send_json({"error": str(exc)}, 500)

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            try:
                body = self._read_json()
                if path == "/api/query":
                    return self._handle_query(body)
                if path == "/api/clock/freeze":
                    clock.freeze()
                elif path == "/api/clock/resume":
                    clock.resume()
                elif path == "/api/clock/advance":
                    clock.advance(float(body.get("seconds", 0)))
                elif path == "/api/clock/set":
                    clock.set_time(float(body["epoch"]))
                elif path == "/api/clock/reset":
                    clock.reset()
                elif path == "/api/authority/mode":
                    self._find_authority(body["server"]).set_mode(body["mode"])
                elif path == "/api/authority/record":
                    self._upsert_record(body)
                elif path == "/api/authority/record/delete":
                    self._delete_record(body)
                elif path == "/api/cache/clear":
                    cache.clear()
                elif path == "/api/log/clear":
                    resolver.clear_log()
                elif path == "/api/stats/reset":
                    transport.reset_counters()
                    cache.hits.update(fresh=0, stale=0, negative=0, failure=0)
                    cache.misses = 0
                    resolver._cooldown_until.clear()
                else:
                    return self._send_json({"error": "not found"}, 404)
                self._send_json({"ok": True, "state": self._state()})
            except KeyError as exc:
                self._send_json({"error": f"unknown authority: {exc}"}, 404)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self._send_json({"error": str(exc)}, 500)

        # -------------------------------------------------------------- #
        def _handle_query(self, body: dict) -> None:
            qtype = body.get("qtype", "A")
            if isinstance(qtype, str):
                if qtype.strip().upper() not in NAME_TO_TYPE:
                    raise ValueError(f"unsupported qtype {qtype!r}")
                qtype = NAME_TO_TYPE[qtype.strip().upper()]
            name = (body.get("name") or "").strip()
            if not name:
                raise ValueError("query name is required")
            query = Query(name, qtype)
            result = resolver.resolve(query)
            self._send_json({
                "ok": True,
                "result": {
                    "name": result.query.name,
                    "qtype": result.query.qtype_name,
                    "rcode": result.rcode,
                    "rcode_name": result.rcode_name,
                    "origin": result.origin,
                    "origin_label": ORIGIN_LABELS.get(result.origin, result.origin),
                    "source": result.source,
                    "fetched": result.fetched,
                    "attempts": result.upstream_attempts,
                    "note": result.note,
                    "answers": [
                        {"name": r.name, "rtype": r.rtype_name,
                         "data": r.data, "ttl": r.ttl}
                        for r in result.answers
                    ],
                },
                "stats": resolver.stats_snapshot(),
            })

        def _upsert_record(self, body: dict) -> None:
            auth = self._find_authority(body["server"])
            zone = auth.find_zone(body["zone"])
            if zone is None:
                zone = Zone(body["zone"])
                auth.zones[zone.name] = zone
            from .message import Record
            rtype = body["rtype"]
            if isinstance(rtype, str):
                rtype = NAME_TO_TYPE[rtype.strip().upper()]
            # 同 owner+rtype 先删后插，保持“修改权威回复”的语义直观
            zone.remove_records(body["name"], rtype)
            zone.add_record(Record(body["name"], rtype, body["data"],
                                   int(body.get("ttl", 300))))

        def _delete_record(self, body: dict) -> None:
            auth = self._find_authority(body["server"])
            zone = auth.find_zone(body["zone"])
            if zone is None:
                raise KeyError(body["zone"])
            rtype = body.get("rtype")
            if isinstance(rtype, str):
                rtype = NAME_TO_TYPE[rtype.strip().upper()]
            removed = zone.remove_records(body["name"], rtype)
            if removed == 0:
                raise ValueError("no matching record")

        # -------------------------------------------------------------- #
        def _state(self) -> dict:
            return {
                "clock": clock.snapshot(),
                "authorities": [a.snapshot() for a in authorities],
                "cache": cache.snapshot(),
                "stats": resolver.stats_snapshot(),
                "config": {
                    "failure_ttl": cache.failure_ttl,
                    "stale_window": cache.stale_window,
                    "max_negative_ttl": cache.max_negative_ttl,
                    "health_cooldown": resolver.health_cooldown,
                    "upstream_order": resolver.upstream_order,
                },
                "type_names": TYPE_NAMES,
                "rcode_names": RCODE_NAMES,
            }

        def _serve_static(self, filename: str, content_type: str) -> None:
            target = (_STATIC_DIR / filename).resolve()
            if not str(target).startswith(str(_STATIC_DIR.resolve())):
                return self._send_json({"error": "forbidden"}, 403)
            if not target.exists():
                return self._send_json({"error": "not found"}, 404)
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler
