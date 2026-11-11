"""Trusted HTTP middleware and stdlib server for spec-declared AXON routes."""

from __future__ import annotations

import copy
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

from . import spec as spec_mod
from .ir import GraphError
from .memsql import MemorySql
from .service import Service

MAX_BODY_BYTES = 16 * 1024 * 1024
_MISSING = object()
_OMIT = object()
_SCOPE_RANK = {"read": 0, "write": 1, "admin": 2}


class HttpHost:
    """Dispatch requests through immutable spec middleware into transactional roots."""

    def __init__(
        self, service: Service, spec: dict, *, test_mode: bool = False, sql_backend=None
    ):
        spec_mod.validate_spec(spec)
        self.service = service
        self.spec = copy.deepcopy(spec)
        self.routes = spec_mod.normalize_routes(spec["name"], spec.get("routes", []))
        self.sessions = spec_mod.normalize_auth(
            spec["name"], spec.get("auth", {}), self.routes
        )
        self.rate = spec_mod.normalize_rate(spec["name"], spec.get("rate"), self.routes)
        self.test_mode = test_mode
        self._writes: list[tuple[str, int]] = []
        self._sql = sql_backend or MemorySql(service.store.table_schemas(), service.store.get)
        self.service._sql_backend = self._sql

    def reset(self, sessions: dict, seed: dict) -> dict:
        """Replace test-only auth, relational state and rate history."""
        if not self.test_mode:
            return _error(404, "not_found")
        try:
            normalized_sessions = spec_mod.normalize_auth(
                self.spec["name"], sessions, self.routes
            )
            normalized_seed = {}
            for table, rows in seed.items():
                schema = self.service.store.table_schema(table)
                if schema is None or not isinstance(rows, list):
                    raise ValueError("unknown table or non-list rows")
                normalized_seed[table] = [
                    self._coerce({"rec": schema["decl"]}, row) for row in rows
                ]
            sql = self._sql
            if isinstance(sql, MemorySql):
                sql = MemorySql(self.service.store.table_schemas(), self.service.store.get)
            sql.load(normalized_seed)
        except (GraphError, TypeError, ValueError, KeyError):
            return _error(422, "validation_error")
        self.sessions = normalized_sessions
        self._writes = []
        self._sql = sql
        self.service._sql_backend = sql
        return {"status": 204, "body": {}}

    def dispatch(
        self,
        method: str,
        target: str,
        headers: dict[str, str] | None = None,
        raw_body: bytes = b"",
    ) -> dict:
        """Apply route -> auth -> rate -> format -> transactional handler."""
        split = urlsplit(target or "/")
        match = self._match(method.upper(), unquote(split.path))
        if match is None:
            return _error(404, "not_found")
        route, params = match
        headers = {key.lower(): value for key, value in (headers or {}).items()}
        session = self._authenticate(route, headers)
        if isinstance(session, dict) and "status" in session:
            return session
        subject = "" if session is None else session["subject"]
        now = self._clock(headers)
        if isinstance(now, dict):
            return now
        limited = self._consume_rate(route, subject, now)
        if limited is not None:
            return limited
        required_scope = route["auth"]
        if (
            required_scope is not None
            and _SCOPE_RANK[session["scope"]] < _SCOPE_RANK[required_scope]
        ):
            return _error(403, "forbidden")
        if not self._params_valid(params):
            return _error(404, "not_found")
        try:
            body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            if not isinstance(body, dict):
                raise ValueError("JSON body must be an object")
            query = dict(parse_qsl(split.query, keep_blank_values=True))
            request = self._request(method.upper(), split.path, params, query, body, subject)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
            return _error(422, "validation_error")
        result = self.service.run(route["handler"], [request], clock=now)
        if not result.get("ok"):
            return _error(500, "handler_error")
        try:
            response = result["result"]
            response_type = {"rec": self.service.store.type_pointer("response")}
            if self._coerce(response_type, response) != response:
                raise ValueError("non-canonical response")
            body = self._decode(response["body"])
            retry = self._decode(response["retry_after_ms"])
            if retry is not _OMIT:
                body["retry_after_ms"] = retry
            return {"status": response["status"], "body": body}
        except (TypeError, ValueError, KeyError):
            return _error(500, "invalid_handler_response")

    def _match(self, method: str, path: str):
        actual = path.split("/")[1:] if path != "/" else []
        for route in self.routes:
            if route["method"] != method:
                continue
            pattern = route["path"].split("/")[1:] if route["path"] != "/" else []
            if len(pattern) != len(actual):
                continue
            params = {}
            for wanted, got in zip(pattern, actual, strict=True):
                if wanted.startswith("{"):
                    params[wanted[1:-1]] = got
                elif wanted != got:
                    break
            else:
                return route, params
        return None

    def _authenticate(self, route, headers):
        if route["auth"] is None:
            return None
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            return _error(401, "unauthorized")
        session = self.sessions.get(authorization[7:])
        return session if session is not None else _error(401, "unauthorized")

    def _clock(self, headers):
        if not self.test_mode:
            return int(time.time() * 1000)
        value = headers.get("x-test-time")
        if value is None:
            return int(time.time() * 1000)
        try:
            return int(value)
        except ValueError:
            return _error(400, "invalid_test_time")

    def _consume_rate(self, route, subject, now):
        if not route["rate"] or self.rate is None:
            return None
        window = self.rate["window_ms"]
        young = [
            stamp
            for owner, stamp in self._writes
            if owner == subject and now - stamp < window
        ]
        if len(young) >= self.rate["limit"]:
            retry = max(0, min(young) + window - now)
            return {
                "status": 429,
                "body": {"error": "rate_limited", "retry_after_ms": retry},
            }
        self._writes.append((subject, now))
        return None

    def _params_valid(self, params) -> bool:
        """A path segment that does not fit its declared type names nothing: 404, not 422."""
        decl = self.service.store.type_pointer("request")
        names = self.service.store.field_names(decl) or []
        fields = self.service.store.get(decl)["fields"]
        try:
            self._coerce(fields[names.index("params")], params)
        except ValueError:
            return False
        return True

    def _request(self, method, path, params, query, body, subject):
        decl = self.service.store.type_pointer("request")
        raw = {
            "method": method,
            "path": path,
            "params": params,
            "query": query,
            "body": body,
            "subject": subject,
        }
        return self._coerce({"rec": decl}, raw)

    def _coerce(self, type_, value):
        if isinstance(type_, str):
            if type_ == "i64":
                if isinstance(value, str):
                    if not value or value.strip() != value:
                        raise ValueError("invalid integer")
                    value = int(value)
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ValueError("expected integer")
            elif type_ == "string" and not isinstance(value, str):
                raise ValueError("expected string")
            elif type_ == "bool" and not isinstance(value, bool):
                raise ValueError("expected boolean")
            elif type_ == "unit" and value is not None:
                raise ValueError("expected null")
            return value
        constructor, payload = next(iter(type_.items()))
        if constructor == "option":
            if value is _MISSING or value is None or value == {"tag": "none"}:
                return {"tag": "none"}
            if isinstance(value, dict) and set(value) == {"tag", "v"} and value["tag"] == "some":
                value = value["v"]
            return {"tag": "some", "v": self._coerce(payload, value)}
        if constructor == "list":
            if not isinstance(value, list):
                raise ValueError("expected list")
            return [self._coerce(payload, item) for item in value]
        if constructor != "rec" or not isinstance(value, dict):
            raise ValueError("expected record")
        declaration = self.service.store.get(payload)
        names = self.service.store.field_names(payload) or []
        fields = declaration["fields"] if declaration else []
        if set(value) - set(names):
            raise ValueError("unexpected record field")
        out = {}
        for name, field_type in zip(names, fields, strict=True):
            current = value.get(name, _MISSING)
            if current is _MISSING and not (
                isinstance(field_type, dict) and set(field_type) == {"option"}
            ):
                raise ValueError("missing record field")
            out[name] = self._coerce(field_type, current)
        return out

    def _decode(self, value):
        if isinstance(value, list):
            return [self._decode(item) for item in value]
        if not isinstance(value, dict):
            return value
        if value == {"tag": "none"}:
            return _OMIT
        if set(value) == {"tag", "v"} and value["tag"] == "some":
            return self._decode(value["v"])
        out = {}
        for key, item in value.items():
            decoded = self._decode(item)
            if decoded is not _OMIT:
                out[key] = decoded
        return out


def _error(status: int, code: str) -> dict:
    return {"status": status, "body": {"error": code}}


def serve_http(
    db: str,
    spec_path: str,
    port: int,
    *,
    host: str = "127.0.0.1",
    test_mode: bool = False,
    backend: str = "memory",
    pg_dsn: str | None = None,
) -> None:
    """Load one immutable spec and serve it until interrupted."""
    from .store import Store

    with open(spec_path, encoding="utf-8") as source:
        body = json.load(source)
    service = Service(Store(db))
    registered = service.register_spec(body)
    if not registered.get("ok"):
        raise ValueError(registered["error"]["detail"])
    sql_backend = _sql_backend(service, backend, pg_dsn)
    app = HttpHost(service, body, test_mode=test_mode, sql_backend=sql_backend)

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = MAX_BODY_BYTES + 1
            if length < 0 or length > MAX_BODY_BYTES:
                response = _error(413, "payload_too_large")
            else:
                raw = self.rfile.read(length) if length else b""
                is_reset = self.command == "POST" and urlsplit(self.path).path == "/__test/reset"
                if test_mode and is_reset:
                    try:
                        reset = json.loads(raw.decode("utf-8"))
                        response = app.reset(reset["sessions"], reset["seed"])
                    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, KeyError):
                        response = _error(422, "validation_error")
                else:
                    response = app.dispatch(self.command, self.path, dict(self.headers), raw)
            payload = json.dumps(response["body"], ensure_ascii=False).encode("utf-8")
            self.send_response(response["status"])
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD" and response["status"] != 204:
                self.wfile.write(payload)

        do_DELETE = _handle
        do_GET = _handle
        do_PATCH = _handle
        do_POST = _handle
        do_PUT = _handle

        def log_message(self, format, *args):  # noqa: A002
            return

    HTTPServer((host, port), Handler).serve_forever()


def _sql_backend(service: Service, backend: str, pg_dsn: str | None):
    if backend == "memory":
        return MemorySql(service.store.table_schemas(), service.store.get)
    if backend == "postgres":
        from .pgsql import PostgresSql

        return PostgresSql(service.store.table_schemas(), service.store.get, pg_dsn or "")
    raise ValueError(f"unknown SQL backend {backend!r}")
