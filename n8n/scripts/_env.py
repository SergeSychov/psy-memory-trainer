"""Load n8n API settings from ~/.config/psy-memory-trainer/n8n.env (never commit)."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path.home() / ".config" / "psy-memory-trainer" / "n8n.env"
ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "workflows"


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def api_request(method: str, path: str, payload: dict | None = None) -> dict:
    env = load_env()
    base_url = (env.get("N8N_API_URL") or env.get("N8N_URL") or "").rstrip("/")
    api_key = env["N8N_API_KEY"]
    url = f"{base_url}{path}"
    headers = {"X-N8N-API-KEY": api_key, "Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(request, timeout=180, context=context) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed ({error.code}): {detail}") from error


def sanitize_workflow(workflow: dict) -> dict:
    settings = workflow.get("settings") or {}
    return {
        "name": workflow["name"],
        "nodes": workflow["nodes"],
        "connections": workflow["connections"],
        "settings": {"executionOrder": settings.get("executionOrder", "v1")},
        "active": False,
    }
