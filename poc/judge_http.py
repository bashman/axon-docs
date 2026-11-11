#!/usr/bin/env python3
"""Replay approved HTTP cases against a running AXON server."""

from __future__ import annotations

import argparse
import copy
import json
from json import dumps, loads
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

TIMEOUT = 10.0
RESET_PATH = "/__test/reset"
MAX_FAILURES = 8


def validate_cases_document(document: object) -> dict:
    if not isinstance(document, dict):
        raise ValueError("HTTP cases must be a JSON object")
    if not isinstance(document.get("name"), str) or not document["name"]:
        raise ValueError("HTTP cases require a non-empty name")
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError("HTTP cases require a cases list")
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError(f"HTTP case {index} requires a string id")
        requests, responses = case.get("requests"), case.get("responses")
        if not isinstance(requests, list) or not isinstance(responses, list):
            raise ValueError(f"HTTP case {case['id']!r} requires requests and responses lists")
        if len(requests) != len(responses):
            raise ValueError(f"HTTP case {case['id']!r} has mismatched requests and responses")
    return copy.deepcopy(document)


def matches(expected, actual) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and matches(value, actual[key]) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(matches(want, got) for want, got in zip(expected, actual, strict=True))
        )
    return type(expected) is type(actual) and expected == actual


def http(url: str, method: str, path: str, headers: dict, body: dict, time: int | None = None):
    request_headers = dict(headers or {})
    request_headers.setdefault("Content-Type", "application/json")
    if time is not None:
        request_headers["X-Test-Time"] = str(int(time))
    request = Request(
        url.rstrip("/") + (path or "/"),
        data=dumps(body or {}).encode("utf-8"),
        headers=request_headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            status, raw = response.status, response.read(16 * 1024 * 1024)
    except HTTPError as error:
        status, raw = error.code, error.read(16 * 1024 * 1024)
    text = raw.decode("utf-8", "replace")
    try:
        payload = loads(text) if text else {}
    except ValueError:
        payload = None
    return {"status": status, "body": payload}


def run_case(url: str, case: dict) -> list[dict]:
    reset = http(
        url,
        "POST",
        RESET_PATH,
        {},
        {"sessions": case.get("sessions") or {}, "seed": case.get("seed") or {}},
    )
    if reset["status"] != 204:
        return [
            {
                "request": None,
                "method": "POST",
                "path": RESET_PATH,
                "expected": {"status": 204},
                "actual": reset,
            }
        ]
    failures = []
    for index, request in enumerate(case["requests"]):
        actual = http(
            url,
            request["method"],
            request["path"],
            request.get("headers") or {},
            request.get("body") or {},
            request.get("time"),
        )
        expected = case["responses"][index]
        if expected["status"] != actual["status"] or (
            "body" in expected and not matches(expected["body"], actual["body"])
        ):
            failures.append(
                {
                    "request": index,
                    "method": request["method"],
                    "path": request["path"],
                    "expected": expected,
                    "actual": actual,
                }
            )
    return failures


def run_report(url: str, cases: list[dict]) -> dict:
    failures, more = [], 0
    for case in cases:
        wrong = run_case(url, case)
        if not wrong:
            continue
        if len(failures) < MAX_FAILURES:
            failures.append({"id": case["id"], **wrong[0]})
        else:
            more += 1
    failed = len(failures) + more
    report = {"ok": failed == 0, "passed": len(cases) - failed, "failed": failed}
    if failures:
        report["failures"] = failures
        report["more"] = more
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay HTTP cases and print an English verdict.")
    parser.add_argument("url", help="base URL of the running server")
    parser.add_argument("cases", type=Path, help="compiled HTTP cases JSON")
    args = parser.parse_args()
    document = validate_cases_document(json.loads(args.cases.read_text(encoding="utf-8")))
    report = run_report(args.url, document["cases"])
    total = report["passed"] + report["failed"]
    print(f"{report['passed']}/{total} cases pass over real HTTP")
    for failure in report.get("failures", []):
        print(f"  FAIL {failure['id']} {failure['method']} {failure['path']}")
        print(f"    expected: {json.dumps(failure['expected'])}")
        print(f"    actual:   {json.dumps(failure['actual'])}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
