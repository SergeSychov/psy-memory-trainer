#!/usr/bin/env python3
"""Upload n8n/workflows/<slug>.json to n8n (keeps remote credential bindings by node name)."""

from __future__ import annotations

import json
import sys

from _env import WORKFLOWS, api_request


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: push_workflow.py <slug>", file=sys.stderr)
        return 1
    slug = sys.argv[1]
    id_path = WORKFLOWS / f"{slug}.id"
    json_path = WORKFLOWS / f"{slug}.json"
    if not id_path.exists() or not json_path.exists():
        print(f"Missing files for slug: {slug}", file=sys.stderr)
        return 1
    workflow_id = id_path.read_text(encoding="utf-8").strip()
    local = json.loads(json_path.read_text(encoding="utf-8"))
    remote = api_request("GET", f"/api/v1/workflows/{workflow_id}")
    remote_by_name = {node["name"]: node for node in remote.get("nodes", [])}
    for node in local["nodes"]:
        remote_node = remote_by_name.get(node["name"])
        if remote_node and remote_node.get("credentials"):
            node["credentials"] = remote_node["credentials"]
    payload = {
        "name": local["name"],
        "nodes": local["nodes"],
        "connections": local["connections"],
        "settings": {"executionOrder": (local.get("settings") or {}).get("executionOrder", "v1")},
    }
    result = api_request("PUT", f"/api/v1/workflows/{workflow_id}", payload)
    # Ensure inactive
    if result.get("active"):
        api_request("POST", f"/api/v1/workflows/{workflow_id}/deactivate")
    print(json.dumps({"action": "pushed", "id": result.get("id", workflow_id), "name": result.get("name"), "active": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
