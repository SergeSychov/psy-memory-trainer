#!/usr/bin/env python3
"""Smoke-run a workflow via Public API (inactive workflows still run via API)."""

from __future__ import annotations

import argparse
import json
import sys
import time

from _env import WORKFLOWS, api_request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("slug")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--data", default="{}", help="JSON object passed as workflow input/body")
    args = parser.parse_args()

    id_path = WORKFLOWS / f"{args.slug}.id"
    if not id_path.exists():
        print(f"Missing {id_path}", file=sys.stderr)
        return 1
    workflow_id = id_path.read_text(encoding="utf-8").strip()
    payload = json.loads(args.data)
    # n8n 2.x: POST /api/v1/workflows/{id}/run
    result = api_request(
        "POST",
        f"/api/v1/workflows/{workflow_id}/run",
        {"workflowData": {"id": workflow_id}, "destinationNode": None, "inputs": payload or None},
    )
    # Some versions expect {runData} differently; fall back to executions
    execution_id = result.get("executionId") or result.get("id") or (result.get("data") or {}).get("executionId")
    print(json.dumps({"action": "started", "workflow_id": workflow_id, "raw_keys": list(result.keys()), "execution_id": execution_id, "result": result}, ensure_ascii=False, default=str)[:2000])
    if not args.wait or not execution_id:
        return 0
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        ex = api_request("GET", f"/api/v1/executions/{execution_id}?includeData=false")
        status = ex.get("status")
        if status in {"success", "error", "crashed", "canceled"}:
            print(json.dumps({"action": "finished", "execution_id": execution_id, "status": status}, ensure_ascii=False))
            return 0 if status == "success" else 2
        time.sleep(2)
    print(json.dumps({"action": "timeout", "execution_id": execution_id}, ensure_ascii=False), file=sys.stderr)
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
