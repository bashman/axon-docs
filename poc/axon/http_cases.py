"""Isolated replay of human-registered HTTP cases against the current graph."""

from __future__ import annotations

import copy
import json
from typing import Any

from .http_host import HttpHost

MAX_COMPACT_FAILURES = 8


def validate_cases_document(document: object) -> dict:
    """Return a defensive copy of one trusted HTTP-cases document."""
    if not isinstance(document, dict):
        raise ValueError("HTTP cases must be a JSON object")
    name = document.get("name")
    cases = document.get("cases")
    if not isinstance(name, str) or not name:
        raise ValueError("HTTP cases require a non-empty name")
    if not isinstance(cases, list):
        raise ValueError("HTTP cases require a cases list")
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError(f"HTTP case {index} requires a string id")
        requests = case.get("requests")
        responses = case.get("responses")
        if not isinstance(requests, list) or not isinstance(responses, list):
            raise ValueError(f"HTTP case {case['id']!r} requires requests and responses lists")
        if len(requests) != len(responses):
            raise ValueError(f"HTTP case {case['id']!r} has mismatched requests and responses")
    return copy.deepcopy(document)


def run_registered_cases(
    service,
    document: dict | None,
    *,
    step: int | None = None,
    limit: int = MAX_COMPACT_FAILURES,
) -> dict:
    """Replay visible cases through an in-memory ``HttpHost`` and return compact failures."""
    if document is None:
        return _error(
            "E_NO_HTTP_CASES",
            "no HTTP cases are registered; restart the server with --http-cases PATH",
        )
    if step is not None and (not isinstance(step, int) or isinstance(step, bool)):
        return _error("E_BAD_HTTP_STEP", "step must be an integer or null")

    cases = [case for case in document["cases"] if step is None or case.get("step") == step]
    if step is not None and not cases:
        available = sorted(
            {case.get("step") for case in document["cases"] if isinstance(case.get("step"), int)}
        )
        return _error(
            "E_UNKNOWN_HTTP_STEP",
            f"no registered HTTP cases for step {step}; available: {available}",
        )

    specs = service.store.specs()
    if len(specs) != 1:
        return _error(
            "E_HTTP_SPEC",
            f"HTTP case replay requires exactly one registered spec; available: {specs}",
        )

    previous_backend = service._sql_backend
    failures: list[dict[str, Any]] = []
    failed = 0
    try:
        host = HttpHost(service, service.store.get_spec(specs[0]), test_mode=True)
        for case in cases:
            failure = _run_case(host, case)
            if failure is None:
                continue
            failed += 1
            if len(failures) < limit:
                failures.append({"id": case["id"], **failure})
    finally:
        service._sql_backend = previous_backend

    report = {
        "ok": failed == 0,
        "cases": document["name"],
        "total": len(cases),
        "passed": len(cases) - failed,
        "failed": failed,
    }
    if failures:
        report["failures"] = failures
        report["more"] = failed - len(failures)
    return report


def _run_case(host: HttpHost, case: dict) -> dict | None:
    reset = host.reset(case.get("sessions") or {}, case.get("seed") or {})
    if reset.get("status") != 204:
        return {
            "request": {"method": "POST", "path": "/__test/reset"},
            "expected": {"status": 204},
            "actual": reset,
        }

    first = None
    for index, (request, expected) in enumerate(
        zip(case["requests"], case["responses"], strict=True)
    ):
        headers = dict(request.get("headers") or {})
        if request.get("time") is not None:
            headers["X-Test-Time"] = str(int(request["time"]))
        raw_body = json.dumps(request.get("body") or {}, ensure_ascii=False).encode("utf-8")
        actual = host.dispatch(request["method"], request["path"], headers, raw_body)
        if first is None and not _response_matches(expected, actual):
            first = {
                "request": {
                    "index": index,
                    "method": request["method"],
                    "path": request["path"],
                    "headers": request.get("headers") or {},
                    "body": request.get("body") or {},
                    "time": request.get("time"),
                },
                "expected": expected,
                "actual": actual,
            }
    return first


def _response_matches(expected: dict, actual: dict) -> bool:
    return expected.get("status") == actual.get("status") and (
        "body" not in expected or partial_match(expected["body"], actual.get("body"))
    )


def partial_match(expected, actual) -> bool:
    """Match the frozen T6 partial-body contract without importing benchmark code."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and partial_match(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            partial_match(want, got) for want, got in zip(expected, actual, strict=True)
        )
    return type(expected) is type(actual) and expected == actual


def _error(code: str, detail: str) -> dict:
    return {
        "ok": False,
        "error": {"node": "http_cases", "code": code, "detail": detail},
    }
