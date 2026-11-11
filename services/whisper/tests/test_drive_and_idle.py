"""Drive-эндпоинты против локального поддельного Drive и сторож простоя.

Сеть наружу не нужна: DRIVE_API_BASE указывает на http.server в потоке.
"""

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import app as app_module

GOOD = "good-token"
BLOB = b"\x00fake-media\x01" * 5000
FILES = {
    "root0001": {"id": "root0001", "name": "course", "mimeType": "application/vnd.google-apps.folder"},
    "sub00001": {
        "id": "sub00001",
        "name": "Лекция X",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": ["root0001"],
    },
    "vid00001": {
        "id": "vid00001",
        "name": "lecture.mp4",
        "mimeType": "video/mp4",
        "size": str(len(BLOB)),
        "md5Checksum": hashlib.md5(BLOB).hexdigest(),
        "parents": ["sub00001"],
        "videoMediaMetadata": {"durationMillis": "1500"},
    },
    "doc00001": {"id": "doc00001", "name": "notes.md", "mimeType": "text/markdown", "parents": ["sub00001"]},
    "aud00001": {
        "id": "aud00001",
        "name": "a.ogg",
        "mimeType": "audio/ogg",
        "size": str(len(BLOB)),
        "parents": ["root0001"],
    },
}


class FakeDrive(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization") != f"Bearer {GOOD}":
            return self._send(401, {"error": "auth"})
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if url.path == "/files":
            parent = qs["q"][0].split("'")[1]
            items = [f for f in FILES.values() if parent in f.get("parents", [])]
            return self._send(200, {"files": items})
        file_id = url.path.rsplit("/", 1)[-1]
        if file_id not in FILES:
            return self._send(404, {"error": "nf"})
        if qs.get("alt") == ["media"]:
            return self._send(200, BLOB, "application/octet-stream")
        return self._send(200, FILES[file_id])


@pytest.fixture()
def drive(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDrive)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("DRIVE_API_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    yield
    server.shutdown()


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("DATA_DIR", str(root))
    monkeypatch.setenv("WHISPER_IDLE_EXIT_SEC", "0")
    monkeypatch.setenv("WHISPER_MIN_FREE_MB", "0")
    monkeypatch.delenv("WHISPER_TOKEN", raising=False)
    return root


@pytest.fixture()
def client(data_dir):
    with TestClient(app_module.app) as test_client:
        yield test_client


def auth(token=GOOD):
    return {"Authorization": f"Bearer {token}"}


def test_list_media_descends_one_level(client, drive):
    response = client.post("/drive/list-media", json={"folder_id": "root0001"}, headers=auth())
    assert response.status_code == 200, response.text
    body = response.json()
    ids = {f["id"]: f for f in body["files"]}
    assert set(ids) == {"vid00001", "aud00001"}
    assert ids["vid00001"]["folder_path"] == ["Лекция X"]
    assert ids["vid00001"]["media_kind"] == "video"
    assert ids["vid00001"]["duration_ms"] == 1500
    assert ids["aud00001"]["media_kind"] == "audio"
    flat = client.post("/drive/list-media", json={"folder_id": "root0001", "depth": 0}, headers=auth())
    assert {f["id"] for f in flat.json()["files"]} == {"aud00001"}


def test_drive_requires_google_token(client, drive):
    assert client.post("/drive/list-media", json={"folder_id": "root0001"}).status_code == 401
    bad = client.post("/drive/list-media", json={"folder_id": "root0001"}, headers=auth("expired"))
    assert bad.status_code == 401
    assert bad.json()["error"] == "google token rejected"


def test_drive_id_validated(client, drive):
    response = client.post("/drive/list-media", json={"folder_id": "x' or '1"}, headers=auth())
    assert response.status_code == 422


def test_fetch_streams_to_data_dir(client, drive, data_dir):
    response = client.post(
        "/drive/fetch",
        json={"file_id": "vid00001", "output_path": "inbox/l1.mp4"},
        headers=auth(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["size_bytes"] == len(BLOB)
    assert body["md5_ok"] is True
    assert (data_dir / "inbox" / "l1.mp4").read_bytes() == BLOB
    assert not (data_dir / "inbox" / "l1.mp4.part").exists()


def test_fetch_rejects_non_media_missing_and_outside(client, drive):
    doc = client.post("/drive/fetch", json={"file_id": "doc00001", "output_path": "x.md"}, headers=auth())
    assert doc.status_code == 400
    missing = client.post("/drive/fetch", json={"file_id": "nope00001", "output_path": "x"}, headers=auth())
    assert missing.status_code == 404
    outside = client.post(
        "/drive/fetch", json={"file_id": "vid00001", "output_path": "/etc/x.mp4"}, headers=auth()
    )
    assert outside.status_code == 400


def test_fetch_checks_free_space(client, drive, monkeypatch):
    monkeypatch.setenv("WHISPER_MIN_FREE_MB", str(10**9))
    response = client.post("/drive/fetch", json={"file_id": "vid00001", "output_path": "a.mp4"}, headers=auth())
    assert response.status_code == 507


def test_fetch_busy(client, drive):
    app_module.begin_job()
    try:
        response = client.post("/drive/fetch", json={"file_id": "vid00001", "output_path": "a.mp4"}, headers=auth())
    finally:
        app_module.end_job()
    assert response.status_code == 409


def test_delete_file(client, data_dir, monkeypatch):
    target = data_dir / "audio" / "x.opus"
    target.parent.mkdir()
    target.write_bytes(b"1")
    monkeypatch.setenv("WHISPER_TOKEN", "secret")
    assert client.post("/files/delete", json={"path": "audio/x.opus"}).status_code == 401
    response = client.post(
        "/files/delete", json={"path": "audio/x.opus"}, headers={"X-Whisper-Token": "secret"}
    )
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert not target.exists()
    again = client.post("/files/delete", json={"path": "audio/x.opus"}, headers=auth("secret"))
    assert again.json()["deleted"] is False
    assert client.post("/files/delete", json={"path": "/etc/passwd"}, headers=auth("secret")).status_code == 400


def test_health_reports_idle_settings(client):
    body = client.get("/health").json()
    assert body["idle_exit_sec"] == 0
    assert body["idle_unload_sec"] == 900
    assert body["busy"] is False
    assert isinstance(body["disk_free_mb"], int)


def test_idle_tick_unloads_instead_of_exiting(monkeypatch, data_dir):
    settings = app_module.load_settings({"WHISPER_IDLE_UNLOAD_SEC": "60", "WHISPER_IDLE_EXIT_SEC": "0"})
    monkeypatch.setattr(app_module, "_model", object())
    monkeypatch.setattr(app_module, "_model_key", ("tiny", "int8", "/models"))
    app_module.touch_activity()
    assert app_module.idle_tick(settings) is None
    assert app_module.idle_tick(settings, now=time.monotonic() + 61) == "unload"
    assert app_module._model is None
    assert app_module.loaded_model_name() is None
    assert app_module.idle_tick(settings, now=time.monotonic() + 120) is None


def test_idle_tick_skips_while_busy(monkeypatch, data_dir):
    settings = app_module.load_settings({"WHISPER_IDLE_UNLOAD_SEC": "1", "WHISPER_IDLE_EXIT_SEC": "1"})
    monkeypatch.setattr(app_module, "_model", object())
    app_module.begin_job()
    try:
        assert app_module.idle_tick(settings, now=time.monotonic() + 100) is None
    finally:
        app_module.end_job()
    assert app_module.idle_tick(settings, now=time.monotonic() + 100) in {"unload", "exit"}
    monkeypatch.setattr(app_module, "_model", None)
    assert app_module.idle_tick(settings, now=time.monotonic() + 100) == "exit"
