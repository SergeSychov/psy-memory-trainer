#!/usr/bin/env python3
"""Download workflow JSON from n8n into n8n/workflows/<slug>.json (+ .id)."""

from __future__ import annotations

import json
import sys

from _env import WORKFLOWS, api_request, sanitize_workflow


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: pull_workflow.py <slug>", file=sys.stderr)
        return 1
    slug = sys.argv[1]
    id_path = WORKFLOWS / f"{slug}.id"
    if not id_path.exists():
        print(f"Missing {id_path}", file=sys.stderr)
        return 1
    workflow_id = id_path.read_text(encoding="utf-8").strip()
    remote = api_request("GET", f"/api/v1/workflows/{workflow_id}")
    local = sanitize_workflow(remote)
    # Drop pinData / staticData if API ever returns them
    local.pop("pinData", None)
    local.pop("staticData", None)
    out = WORKFLOWS / f"{slug}.json"
    out.write_text(json.dumps(local, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"action": "pulled", "id": workflow_id, "name": local["name"], "path": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
