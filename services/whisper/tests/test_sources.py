"""/source/probe, /source/fetch, /inbox/upload и SSRF-защита.

Сеть наружу не нужна: «интернет» — http.server в потоке. Тестовая политика (подмена
app.source_policy) разрешает http, любой порт и только 127.0.0.1 из блок-листа, а имена
ok.test / evil.test резолвятся подменным резолвером. Строгая политика проверяется отдельно.
Все данные синтетические.
"""

import ipaddress
import json
import logging
import shutil
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import app as app_module
import safe_http
import sources

BLOB = b"\x00fake-audio\x01" * 4000
SECRET_NAME = "secret-lecture-name.mp3"
SECRET_QUERY = "token=abc123secret"
PRIVATE_ID = "private0001"
BIG_ID = "bigfile0001"
OK_ID = "publicfile1"
MISSING_ID = "missing0001"
TEST_TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}

RESOLVE = {
    "ok.test": ["127.0.0.1"],
    "evil.test": ["127.0.0.2"],
    "mixed.test": ["127.0.0.1", "10.0.0.5"],
}


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _send(self, code, body=b"", ctype="application/octet-stream", extra=None, length=True):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if length:
                self.send_header("Content-Length", str(len(data)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _redirect(self, location):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            state["hits"].append(self.path)
            state["hosts"].append(self.headers.get("Host"))
            base = f"http://ok.test:{state['port']}"
            path = url.path
            if path == "/files/yandex-download":  # как downloader.disk: общий тип
                return self._send(200, BLOB, "application/octet-stream")
            if path.startswith("/files/"):
                return self._send(200, BLOB, "audio/mpeg")
            if path == "/blob":  # тип неизвестен, имя без расширения
                return self._send(200, BLOB, "application/octet-stream")
            if path == "/notes.txt":
                return self._send(200, b"text " * 50, "text/plain; charset=utf-8")
            if path == "/html":
                return self._send(200, b"<html>login</html>", "text/html; charset=utf-8")
            if path == "/redir":
                return self._redirect(f"{base}/files/a.mp3")
            if path == "/redir-relative":
                return self._redirect("/files/a.mp3")
            if path.startswith("/chain/"):
                left = int(path.rsplit("/", 1)[-1])
                return self._redirect(f"{base}/chain/{left - 1}" if left > 0 else f"{base}/files/a.mp3")
            if path == "/to-loopback":
                return self._redirect(f"http://evil.test:{state['port']}/files/a.mp3")
            if path == "/to-metadata":
                return self._redirect("http://169.254.169.254/latest/meta-data/")
            if path == "/to-file":
                return self._redirect("file:///etc/passwd")
            if path == "/to-ftp":
                return self._redirect("ftp://ok.test/a.mp3")
            if path == "/to-mixed":
                return self._redirect(f"http://mixed.test:{state['port']}/files/a.mp3")
            if path == "/big":
                return self._send(200, b"x", "audio/mpeg", extra={"Content-Length": str(10**9)}, length=False)
            if path == "/liar":  # Content-Length нет, тело длиннее лимита
                return self._send(200, BLOB * 10, "audio/mpeg", length=False)
            if path == "/short":  # Content-Length больше реального тела
                return self._send(200, BLOB, "audio/mpeg", extra={"Content-Length": str(len(BLOB) + 100)}, length=False)
            if path == "/empty":
                return self._send(200, b"", "audio/mpeg")
            if path == "/forbidden":
                return self._send(403, b"no", "text/plain")
            if path == "/gone":
                return self._send(410, b"gone", "text/plain")
            if path == "/missing-file":
                return self._send(404, b"nf", "text/plain")
            if path == "/playlist.m3u8":
                return self._send(200, b"#EXTM3U\n", "application/vnd.apple.mpegurl")
            if path == "/slow":
                # Мало байт за окно скорости — для теста too_slow.
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", "200000")
                self.end_headers()
                try:
                    for _ in range(40):
                        self.wfile.write(b"x" * 50)
                        self.wfile.flush()
                        time.sleep(0.15)
                except BrokenPipeError:
                    pass
                return
            if path == "/boom":
                return self._send(500, b"no", "text/plain")
            if path == "/uc":  # Google Drive «по ссылке»
                file_id = qs.get("id", [""])[0]
                if file_id == OK_ID:
                    return self._send(
                        200, BLOB, "audio/mpeg", extra={"Content-Disposition": 'attachment; filename="Lecture 01.mp3"'}
                    )
                if file_id == BIG_ID:
                    if qs.get("confirm") == ["t"]:
                        return self._send(
                            200, BLOB, "video/mp4", extra={"Content-Disposition": "attachment; filename*=UTF-8''%D0%BB%D0%B5%D0%BA.mp4"}
                        )
                    return self._send(200, b"<html>virus scan warning</html>", "text/html; charset=utf-8")
                if file_id == PRIVATE_ID:
                    return self._send(200, b"<html>sign in</html>", "text/html; charset=utf-8")
                return self._send(404, b"nf", "text/plain")
            if path == "/v1/disk/public/resources":
                key = qs.get("public_key", [""])[0]
                if "/i/okfile" in key:
                    return self._send(
                        200,
                        {"type": "file", "name": "yadisk.m4a", "size": len(BLOB), "mime_type": "audio/mp4", "media_type": "audio"},
                        "application/json",
                    )
                if "/d/folder1" in key:
                    return self._send(200, {"type": "dir", "name": "folder"}, "application/json")
                return self._send(404, {"error": "DiskNotFoundError"}, "application/json")
            if path == "/v1/disk/public/resources/download":
                return self._send(200, {"href": f"{base}/files/yandex-download"}, "application/json")
            return self._send(404, b"nf", "text/plain")

    return Handler


@pytest.fixture()
def web(monkeypatch):
    state = {"hits": [], "hosts": [], "port": 0}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    state["port"] = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SOURCE_GDRIVE_BASE", f"http://ok.test:{state['port']}")
    monkeypatch.setenv("SOURCE_YANDEX_API_BASE", f"http://ok.test:{state['port']}")
    yield state
    server.shutdown()


@pytest.fixture()
def resolver_calls():
    return []


@pytest.fixture()
def policy(monkeypatch, resolver_calls):
    def resolver(host, _port):
        resolver_calls.append(host)
        return RESOLVE[host]

    relaxed = safe_http.Policy(
        allow_http=True, ports=None, allow_ips=frozenset({"127.0.0.1"}), resolver=resolver
    )
    monkeypatch.setattr(app_module, "source_policy", lambda: relaxed)
    return relaxed


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("DATA_DIR", str(root))
    monkeypatch.setenv("WHISPER_IDLE_EXIT_SEC", "0")
    monkeypatch.setenv("WHISPER_MIN_FREE_MB", "0")
    # Новые ручки требуют WHISPER_TOKEN (require_token_strict).
    monkeypatch.setenv("WHISPER_TOKEN", TEST_TOKEN)
    return root


@pytest.fixture()
def client(data_dir):
    with TestClient(app_module.app) as test_client:
        yield test_client


def direct(web, path):
    return f"http://ok.test:{web['port']}{path}"


def yandex(key="okfile1"):
    return f"https://disk.yandex.ru/i/{key}"


def gdrive(file_id=OK_ID):
    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"


def fetch(client, kind, url, out="inbox/l1.mp3", max_bytes=10**7, headers=None):
    return client.post(
        "/source/fetch",
        json={"kind": kind, "url": url, "output_path": out, "max_bytes": max_bytes},
        headers=headers if headers is not None else AUTH,
    )


def probe(client, kind, url, headers=None):
    return client.post(
        "/source/probe",
        json={"kind": kind, "url": url},
        headers=headers if headers is not None else AUTH,
    )


# --- /source/probe ------------------------------------------------------------------


def test_probe_direct_url(client, web, policy, monkeypatch):
    monkeypatch.setattr(sources, "ffprobe_json", lambda **_k: {"format": {"duration": "12.5"}, "streams": []})
    response = probe(client, "direct_url", direct(web, "/files/l.mp3"))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "l.mp3",
        "size_bytes": len(BLOB),
        "mime": "audio/mpeg",
        "duration_sec": 12.5,
        "resolved_kind": "audio",
    }


def test_probe_duration_is_null_without_ffprobe(client, web, policy, monkeypatch):
    monkeypatch.setattr(sources, "ffprobe_json", lambda **_k: None)
    body = probe(client, "direct_url", direct(web, "/files/l.mp3")).json()
    assert body["duration_sec"] is None
    assert body["resolved_kind"] == "audio"


def test_probe_duration_estimated_from_bitrate(client, web, policy, monkeypatch):
    info = {"format": {"bit_rate": str(len(BLOB) * 8 // 10)}, "streams": []}
    monkeypatch.setattr(sources, "ffprobe_json", lambda **_k: info)
    assert probe(client, "direct_url", direct(web, "/files/l.mp3")).json()["duration_sec"] == 10.0


def test_probe_kind_from_streams_when_type_unknown(client, web, policy, monkeypatch):
    info = {"format": {}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
    monkeypatch.setattr(sources, "ffprobe_json", lambda **_k: info)
    assert probe(client, "direct_url", direct(web, "/blob")).json()["resolved_kind"] == "video"
    monkeypatch.setattr(sources, "ffprobe_json", lambda **_k: None)
    unknown = probe(client, "direct_url", direct(web, "/blob"))
    assert unknown.status_code == 400
    assert unknown.json() == {"error": "unsupported_type"}


def test_probe_text_source(client, web, policy):
    body = probe(client, "direct_url", direct(web, "/notes.txt")).json()
    assert body["resolved_kind"] == "text"
    assert body["duration_sec"] is None


def test_probe_gdrive(client, web, policy, monkeypatch):
    monkeypatch.setattr(sources, "ffprobe_json", lambda **_k: None)
    body = probe(client, "gdrive_public", gdrive()).json()
    assert body["name"] == "Lecture 01.mp3"
    assert body["resolved_kind"] == "audio"
    assert body["size_bytes"] == len(BLOB)


def test_probe_gdrive_big_file_uses_confirm_step_only(client, web, policy, monkeypatch):
    monkeypatch.setattr(sources, "ffprobe_json", lambda **_k: None)
    body = probe(client, "gdrive_public", gdrive(BIG_ID)).json()
    assert body["name"] == "лек.mp4"
    assert body["resolved_kind"] == "video"
    hits = [h for h in web["hits"] if h.startswith("/uc")]
    assert len(hits) == 2 and "confirm=t" in hits[1]


def test_probe_gdrive_not_public_and_missing(client, web, policy):
    private = probe(client, "gdrive_public", gdrive(PRIVATE_ID))
    assert private.status_code == 403
    assert private.json() == {"error": "not_public"}
    missing = probe(client, "gdrive_public", gdrive(MISSING_ID))
    assert missing.json() == {"error": "not_public"}


@pytest.mark.parametrize(
    "url",
    [
        "https://drive.google.com/drive/folders/folderid0001",
        "https://docs.google.com/document/d/docid00000001/edit",
        "https://drive.google.com/file/d/short/view",
        "http://drive.google.com/file/d/publicfile1/view",
        "https://evil.example.com/file/d/publicfile1/view",
    ],
)
def test_probe_gdrive_unsupported_links(client, web, policy, url):
    response = probe(client, "gdrive_public", url)
    assert response.status_code == 400
    assert response.json() == {"error": "unsupported_link"}


def test_probe_yandex(client, web, policy, monkeypatch):
    monkeypatch.setattr(sources, "ffprobe_json", lambda **_k: None)
    body = probe(client, "yandex_public", yandex()).json()
    assert body == {
        "name": "yadisk.m4a",
        "size_bytes": len(BLOB),
        "mime": "audio/mp4",
        "duration_sec": None,
        "resolved_kind": "audio",
    }


def test_probe_yandex_folder_missing_and_foreign_host(client, web, policy):
    folder = probe(client, "yandex_public", "https://disk.yandex.ru/d/folder1")
    assert folder.status_code == 400
    assert folder.json() == {"error": "unsupported_link"}
    missing = probe(client, "yandex_public", yandex("nokeyatall"))
    assert missing.status_code == 403
    assert missing.json() == {"error": "not_public"}
    foreign = probe(client, "yandex_public", "https://evil.example.com/i/okfile1")
    assert foreign.json() == {"error": "unsupported_link"}


def test_probe_html_instead_of_file_is_not_public(client, web, policy):
    response = probe(client, "direct_url", direct(web, "/html"))
    assert response.status_code == 403
    assert response.json() == {"error": "not_public"}


def test_probe_does_not_take_job_lock(client, web, policy):
    app_module.begin_job()
    try:
        response = probe(client, "direct_url", direct(web, "/files/l.mp3"))
        assert response.status_code == 200, response.text
        assert client.get("/health").json()["busy"] is True
    finally:
        app_module.end_job()


def test_probe_local_file(client, data_dir, monkeypatch):
    monkeypatch.setattr(sources, "ffprobe_json", lambda **_k: {"format": {"duration": "3.0"}, "streams": []})
    (data_dir / "inbox").mkdir()
    (data_dir / "inbox" / "a.mp3").write_bytes(BLOB)
    body = client.post("/source/probe", json={"path": "inbox/a.mp3"}, headers=AUTH).json()
    assert body["name"] == "a.mp3"
    assert body["size_bytes"] == len(BLOB)
    assert body["duration_sec"] == 3.0
    assert body["resolved_kind"] == "audio"
    assert client.post("/source/probe", json={"path": "inbox/none.mp3"}, headers=AUTH).status_code == 404
    assert client.post("/source/probe", json={"path": "/etc/passwd"}, headers=AUTH).status_code == 400
    # S1: путь вне inbox/ запрещён (даже если файл есть в /data).
    (data_dir / "outside.mp3").write_bytes(BLOB)
    assert client.post("/source/probe", json={"path": "outside.mp3"}, headers=AUTH).status_code == 400
    assert client.post("/source/probe", json={"path": "audio/x.mp3"}, headers=AUTH).status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"kind": "direct_url"},
        {"url": "https://example.com/a.mp3"},
        {"kind": "ftp", "url": "https://example.com/a.mp3"},
        {"path": "inbox/a.mp3", "kind": "direct_url", "url": "https://example.com/a.mp3"},
    ],
)
def test_probe_rejects_bad_body(client, payload):
    assert client.post("/source/probe", json=payload, headers=AUTH).status_code == 422


# --- /source/fetch ------------------------------------------------------------------


def test_fetch_direct_url(client, web, policy, data_dir):
    response = fetch(client, "direct_url", direct(web, "/files/l.mp3"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["size_bytes"] == len(BLOB)
    assert body["resolved_kind"] == "audio"
    assert body["mime"] == "audio/mpeg"
    assert (data_dir / "inbox" / "l1.mp3").read_bytes() == BLOB
    assert not (data_dir / "inbox" / "l1.mp3.part").exists()


def test_fetch_gdrive_and_big_file(client, web, policy, data_dir):
    assert fetch(client, "gdrive_public", gdrive(), "inbox/g1.mp3").status_code == 200
    big = fetch(client, "gdrive_public", gdrive(BIG_ID), "inbox/g2.mp4")
    assert big.status_code == 200, big.text
    assert big.json()["resolved_kind"] == "video"
    assert (data_dir / "inbox" / "g2.mp4").read_bytes() == BLOB


def test_fetch_gdrive_not_public(client, web, policy, data_dir):
    response = fetch(client, "gdrive_public", gdrive(PRIVATE_ID), "inbox/g.mp3")
    assert response.status_code == 403
    assert response.json() == {"error": "not_public"}
    assert not list((data_dir / "inbox").glob("*")) if (data_dir / "inbox").exists() else True


def test_fetch_yandex(client, web, policy, data_dir):
    response = fetch(client, "yandex_public", yandex(), "inbox/y1.m4a")
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "yadisk.m4a"
    assert (data_dir / "inbox" / "y1.m4a").read_bytes() == BLOB


def test_fetch_html_is_not_public_and_leaves_nothing(client, web, policy, data_dir):
    response = fetch(client, "direct_url", direct(web, "/html"))
    assert response.status_code == 403
    assert response.json() == {"error": "not_public"}
    assert not (data_dir / "inbox" / "l1.mp3").exists()
    assert not (data_dir / "inbox" / "l1.mp3.part").exists()


def test_fetch_follows_redirects_up_to_five(client, web, policy, data_dir):
    assert fetch(client, "direct_url", direct(web, "/redir")).status_code == 200
    assert fetch(client, "direct_url", direct(web, "/redir-relative")).status_code == 200
    # /chain/4 — ровно пять редиректов, и это ещё допустимо
    ok = fetch(client, "direct_url", direct(web, "/chain/4"))
    assert ok.status_code == 200, ok.text


def test_fetch_redirect_chain_over_five_fails(client, web, policy, data_dir):
    for hops in (5, 6, 9):  # /chain/5 — шестой редирект
        response = fetch(client, "direct_url", direct(web, f"/chain/{hops}"))
        assert response.status_code == 502
        assert response.json() == {"error": "download_failed"}
    assert not (data_dir / "inbox" / "l1.mp3").exists()


def test_fetch_redirect_to_private_address_is_blocked(client, web, policy, data_dir):
    response = fetch(client, "direct_url", direct(web, "/to-loopback"))
    assert response.status_code == 400
    assert response.json() == {"error": "unsupported_link"}
    # Соединения с evil.test не было: на сервер пришёл только первый запрос.
    assert web["hits"] == ["/to-loopback"]
    assert not (data_dir / "inbox" / "l1.mp3").exists()


@pytest.mark.parametrize("path", ["/to-metadata", "/to-file", "/to-ftp", "/to-mixed"])
def test_fetch_redirect_to_forbidden_target(client, web, policy, path):
    response = fetch(client, "direct_url", direct(web, path))
    assert response.status_code == 400
    assert response.json() == {"error": "unsupported_link"}
    assert web["hits"] == [path]


def test_fetch_declared_size_over_limit(client, web, policy, data_dir):
    response = fetch(client, "direct_url", direct(web, "/files/l.mp3"), max_bytes=len(BLOB) - 1)
    assert response.status_code == 413
    assert response.json() == {"error": "too_large"}
    assert not (data_dir / "inbox" / "l1.mp3").exists()
    huge = fetch(client, "direct_url", direct(web, "/big"))
    assert huge.status_code == 413


def test_fetch_actual_size_over_limit_without_content_length(client, web, policy, data_dir):
    response = fetch(client, "direct_url", direct(web, "/liar"), max_bytes=len(BLOB) * 2)
    assert response.status_code == 413
    assert response.json() == {"error": "too_large"}
    assert not (data_dir / "inbox" / "l1.mp3").exists()
    assert not (data_dir / "inbox" / "l1.mp3.part").exists()


def test_fetch_env_cap_limits_body_max_bytes(client, web, policy, monkeypatch):
    monkeypatch.setenv("SOURCE_MAX_BYTES", str(len(BLOB) - 1))
    response = fetch(client, "direct_url", direct(web, "/files/l.mp3"), max_bytes=10**9)
    assert response.status_code == 413


def test_fetch_truncated_empty_and_http_errors(client, web, policy, data_dir):
    assert fetch(client, "direct_url", direct(web, "/short")).status_code == 502
    assert fetch(client, "direct_url", direct(web, "/empty")).status_code == 502
    assert fetch(client, "direct_url", direct(web, "/forbidden")).json() == {"error": "not_public"}
    assert fetch(client, "direct_url", direct(web, "/boom")).json() == {"error": "download_failed"}
    assert not (data_dir / "inbox" / "l1.mp3.part").exists()


def test_fetch_checks_free_space(client, web, policy, monkeypatch):
    monkeypatch.setenv("WHISPER_MIN_FREE_MB", str(10**9))
    response = fetch(client, "direct_url", direct(web, "/files/l.mp3"))
    assert response.status_code == 507


def test_fetch_busy_takes_job_lock(client, web, policy):
    app_module.begin_job()
    try:
        response = fetch(client, "direct_url", direct(web, "/files/l.mp3"))
    finally:
        app_module.end_job()
    assert response.status_code == 409
    assert response.json() == {"error": "busy"}
    assert web["hits"] == []
    # замок отпущен после обычного запроса и после ошибки
    assert fetch(client, "direct_url", direct(web, "/html")).status_code == 403
    assert fetch(client, "direct_url", direct(web, "/files/l.mp3")).status_code == 200


@pytest.mark.parametrize("out", ["/etc/x.mp3", "x.mp3", "audio/x.mp3", "inbox", "inbox/../x.mp3", "inbox/a.part"])
def test_fetch_output_must_be_inside_inbox(client, web, policy, out):
    response = fetch(client, "direct_url", direct(web, "/files/l.mp3"), out)
    assert response.status_code == 400
    assert web["hits"] == []


def test_fetch_ip_is_pinned_and_checked_once_per_hop(client, web, policy, resolver_calls):
    assert fetch(client, "direct_url", direct(web, "/redir")).status_code == 200
    # два хопа: /redir → /files/a.mp3; DNS на каждый хоп ровно один раз
    assert resolver_calls == ["ok.test", "ok.test"]
    assert set(web["hosts"]) == {f"ok.test:{web['port']}"}


def test_logs_have_no_urls_or_file_names(client, web, policy, caplog):
    caplog.set_level(logging.DEBUG)
    path = f"/files/{SECRET_NAME}?{SECRET_QUERY}"
    assert probe(client, "direct_url", direct(web, path)).status_code == 200
    assert fetch(client, "direct_url", direct(web, path), "inbox/safe-name.mp3").status_code == 200
    assert fetch(client, "direct_url", direct(web, "/to-loopback?" + SECRET_QUERY)).status_code == 400
    assert probe(client, "gdrive_public", gdrive(PRIVATE_ID)).status_code == 403
    text = caplog.text
    assert "source fetch" in text and "source probe" in text and "source reject" in text
    for secret in (SECRET_NAME, "abc123secret", "ok.test", "safe-name", "evil.test", PRIVATE_ID):
        assert secret not in text


# --- токен --------------------------------------------------------------------------


def test_source_endpoints_require_token(client, web, policy, monkeypatch):
    monkeypatch.setenv("WHISPER_TOKEN", "s3cret")
    url = direct(web, "/files/l.mp3")
    bodies = {
        "/source/probe": {"kind": "direct_url", "url": url},
        "/source/fetch": {"kind": "direct_url", "url": url, "output_path": "inbox/t.mp3", "max_bytes": 10**7},
    }
    for path, body in bodies.items():
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/inbox/upload?path=inbox/t.bin", content=b"x").status_code == 401
    assert web["hits"] == []
    bearer = {"Authorization": "Bearer s3cret"}
    header = {"X-Whisper-Token": "s3cret"}
    assert client.post("/source/probe", json=bodies["/source/probe"], headers=bearer).status_code == 200
    assert client.post("/source/fetch", json=bodies["/source/fetch"], headers=header).status_code == 200
    assert client.post("/inbox/upload?path=inbox/t.bin", content=b"x", headers=bearer).status_code == 200


def test_source_endpoints_reject_when_token_unset(client, web, policy, monkeypatch):
    """B2: require_token_strict — без WHISPER_TOKEN в env новые ручки закрыты."""
    monkeypatch.delenv("WHISPER_TOKEN", raising=False)
    url = direct(web, "/files/l.mp3")
    assert client.post("/source/probe", json={"kind": "direct_url", "url": url}).status_code == 401
    assert (
        client.post(
            "/source/fetch",
            json={"kind": "direct_url", "url": url, "output_path": "inbox/t.mp3", "max_bytes": 10**7},
            headers=AUTH,
        ).status_code
        == 401
    )
    assert client.post("/inbox/upload?path=inbox/t.bin", content=b"x", headers=AUTH).status_code == 401
    # Старые ручки: без токена в env require_token пропускает (не 401).
    old = client.post("/extract-audio", json={"input_path": "inbox/missing.mp3"})
    assert old.status_code != 401


# --- /inbox/upload ------------------------------------------------------------------


def test_upload_happy_path(client, data_dir, monkeypatch):
    monkeypatch.setattr(sources, "reject_unsafe_container", lambda *_a, **_k: None)
    response = client.post("/inbox/upload?path=inbox/voice-1.ogg", content=BLOB, headers=AUTH)
    assert response.status_code == 200, response.text
    assert response.json() == {"path": str(data_dir.resolve() / "inbox" / "voice-1.ogg"), "size_bytes": len(BLOB)}
    assert (data_dir / "inbox" / "voice-1.ogg").read_bytes() == BLOB
    assert not (data_dir / "inbox" / "voice-1.ogg.part").exists()
    again = client.post("/inbox/upload?path=inbox/voice-1.ogg", content=b"new", headers=AUTH)
    assert again.status_code == 200
    assert (data_dir / "inbox" / "voice-1.ogg").read_bytes() == b"new"


def test_upload_over_limit_by_content_length_and_by_stream(client, data_dir, monkeypatch):
    monkeypatch.setenv("INBOX_UPLOAD_MAX_BYTES", "1000")
    monkeypatch.setattr(sources, "reject_unsafe_container", lambda *_a, **_k: None)
    declared = client.post("/inbox/upload?path=inbox/a.bin", content=b"x" * 1001, headers=AUTH)
    assert declared.status_code == 413
    assert declared.json() == {"error": "too_large"}

    def chunks():  # без Content-Length: chunked
        for _ in range(5):
            yield b"y" * 300

    streamed = client.post("/inbox/upload?path=inbox/b.bin", content=chunks(), headers=AUTH)
    assert streamed.status_code == 413
    assert not (data_dir / "inbox" / "a.bin").exists()
    assert not (data_dir / "inbox" / "b.bin").exists()
    assert not (data_dir / "inbox" / "b.bin.part").exists()
    assert client.post("/inbox/upload?path=inbox/c.bin", content=b"z" * 1000, headers=AUTH).status_code == 200


def test_upload_default_limit_is_25_mb(data_dir):
    assert app_module.load_settings().inbox_upload_max_bytes == 25 * 1024 * 1024


@pytest.mark.parametrize(
    "path",
    ["x.bin", "audio/x.bin", "inbox/a/b.bin", "inbox/../x.bin", "/etc/x.bin", "inbox", "inbox/.hidden", "inbox/a.part"],
)
def test_upload_path_must_be_inbox_name(client, data_dir, path):
    response = client.post("/inbox/upload", params={"path": path}, content=b"x", headers=AUTH)
    assert response.status_code == 400
    assert not (data_dir / "x.bin").exists()


def test_upload_empty_body_rejected(client, data_dir):
    response = client.post("/inbox/upload?path=inbox/e.bin", content=b"", headers=AUTH)
    assert response.status_code == 400
    assert not (data_dir / "inbox" / "e.bin").exists()
    assert not (data_dir / "inbox" / "e.bin.part").exists()


def test_upload_does_not_take_job_lock(client, data_dir, monkeypatch):
    monkeypatch.setattr(sources, "reject_unsafe_container", lambda *_a, **_k: None)
    app_module.begin_job()
    try:
        assert client.post("/inbox/upload?path=inbox/l.bin", content=b"x", headers=AUTH).status_code == 200
    finally:
        app_module.end_job()


# --- строгая SSRF-политика ----------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.255.255.254",
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",
        "169.254.0.1",
        "100.64.0.1",
        "100.127.255.255",
        "0.0.0.0",
        "224.0.0.1",
        "255.255.255.255",
        "198.18.0.1",
        "::1",
        "::",
        "fc00::1",
        "fd12:3456::1",
        "fe80::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:169.254.169.254",
        "64:ff9b::7f00:1",
        "2002:7f00:1::1",
        "2002:a9fe:a9fe::",
        "2001:db8::1",
    ],
)
def test_blocked_addresses(address):
    assert safe_http.is_blocked_ip(ipaddress.ip_address(address)) is True


@pytest.mark.parametrize("address", ["8.8.8.8", "93.184.216.34", "172.32.0.1", "100.128.0.1", "2606:4700:4700::1111"])
def test_public_addresses_allowed(address):
    assert safe_http.is_blocked_ip(ipaddress.ip_address(address)) is False


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/a.mp3",
        "ftp://example.com/a.mp3",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:pass@example.com/a.mp3",
        "https://example.com:8443/a.mp3",
        "https://example.com:80/a.mp3",
        "https://localhost/a.mp3",
        "https://foo.localhost/a.mp3",
        "https://printer.local/a.mp3",
        "https://db.internal/a.mp3",
        "https:///a.mp3",
        "https://exa mple.com/a.mp3",
        "https://example.com/a\nb",
        "https://" + "a" * 300 + ".com/x",
        "https://example.com/" + "a" * 3000,
        "",
    ],
)
def test_strict_parse_rejects(url):
    with pytest.raises(safe_http.SourceError) as err:
        safe_http.parse_target(url)
    assert err.value.code == "unsupported_link"


def test_strict_parse_accepts_plain_https():
    target = safe_http.parse_target("https://Example.COM/a%20b/файл.mp3?x=1&y=2")
    assert target.host == "example.com" and target.port == 443 and target.tls
    assert " " not in target.path and target.path.endswith("?x=1&y=2")
    assert safe_http.parse_target("https://example.com:443/a").port == 443


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/a.mp3",
        "https://[::1]/a.mp3",
        "https://169.254.169.254/latest/meta-data/",
        "https://10.0.0.5/a.mp3",
        "https://[::ffff:127.0.0.1]/a.mp3",
        "https://100.64.0.1/a.mp3",
        "https://2130706433/a.mp3",  # десятичная запись 127.0.0.1
    ],
)
def test_strict_policy_blocks_ip_literals_via_api(client, url):
    response = probe(client, "direct_url", url)
    assert response.status_code == 400
    assert response.json() == {"error": "unsupported_link"}
    response = fetch(client, "direct_url", url)
    assert response.status_code == 400


def test_strict_policy_blocks_hosts_resolving_to_private(client, monkeypatch):
    answers = {
        "rebind.test": ["93.184.216.34", "10.0.0.1"],  # хотя бы один внутренний — отказ
        "meta.test": ["169.254.169.254"],
        "v6.test": ["fd00::1"],
        "mapped.test": ["::ffff:192.168.0.1"],
    }
    strict = safe_http.Policy(resolver=lambda host, _port: answers[host])
    monkeypatch.setattr(app_module, "source_policy", lambda: strict)
    for host in answers:
        response = probe(client, "direct_url", f"https://{host}/a.mp3")
        assert response.status_code == 400, host
        assert response.json() == {"error": "unsupported_link"}


def test_strict_policy_dns_failure_is_download_failed(client, monkeypatch):
    def broken(_host, _port):
        raise OSError("nxdomain")

    monkeypatch.setattr(app_module, "source_policy", lambda: safe_http.Policy(resolver=broken))
    response = probe(client, "direct_url", "https://nope.test/a.mp3")
    assert response.status_code == 502
    assert response.json() == {"error": "download_failed"}


def test_default_policy_is_strict():
    assert app_module.source_policy() is safe_http.STRICT
    assert safe_http.STRICT.allow_http is False
    assert safe_http.STRICT.ports == frozenset({443})
    assert safe_http.STRICT.allow_ips == frozenset()
    assert safe_http.STRICT.max_redirects == 5


# --- TLS с пиннингом: SNI и сертификат проверяются по имени, соединение — на IP -------


@pytest.fixture()
def tls_cert(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    proc = subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
            "-out", str(cert), "-days", "1", "-subj", "/CN=ok.test",
            "-addext", "subjectAltName=DNS:ok.test",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip("openssl could not make a test certificate")
    return key, cert


def test_https_pinned_connection_verifies_certificate_by_name(client, tls_cert, monkeypatch):
    key, cert = tls_cert
    state = {"hits": [], "hosts": [], "port": 0}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(cert), str(key))
    server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
    state["port"] = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client_ctx = ssl.create_default_context(cafile=str(cert))
        names = {"ok.test": ["127.0.0.1"], "other.test": ["127.0.0.1"]}
        relaxed = safe_http.Policy(
            ports=None,
            allow_ips=frozenset({"127.0.0.1"}),
            resolver=lambda host, _port: names[host],
            ssl_context=client_ctx,
        )
        monkeypatch.setattr(app_module, "source_policy", lambda: relaxed)
        good = fetch(client, "direct_url", f"https://ok.test:{state['port']}/files/a.mp3")
        assert good.status_code == 200, good.text
        assert state["hosts"] == [f"ok.test:{state['port']}"]
        # Тот же IP, но имя не совпадает с сертификатом: соединение отвергается.
        bad = fetch(client, "direct_url", f"https://other.test:{state['port']}/files/a.mp3", "inbox/b.mp3")
        assert bad.status_code == 502
        assert bad.json() == {"error": "download_failed"}
        assert len(state["hits"]) == 1
    finally:
        server.shutdown()


# --- B1/B3/S2/S3 --------------------------------------------------------------------


def test_fetch_aborts_slow_server_and_releases_lock(client, web, policy, data_dir, monkeypatch):
    """B1: <64 КБ за окно → timeout; job-lock освобождается."""
    monkeypatch.setattr(sources, "SPEED_WINDOW_SEC", 0.4)
    monkeypatch.setenv("SOURCE_MIN_BYTES_PER_MIN", "8000")
    monkeypatch.setenv("SOURCE_FETCH_MAX_SEC", "30")
    response = fetch(client, "direct_url", direct(web, "/slow"), out="inbox/slow.mp3")
    assert response.status_code == 504
    assert response.json() == {"error": "timeout"}
    assert not (data_dir / "inbox" / "slow.mp3").exists()
    assert not (data_dir / "inbox" / "slow.mp3.part").exists()
    assert client.get("/health").json()["busy"] is False


def test_fetch_respects_overall_deadline(client, web, policy, data_dir, monkeypatch):
    monkeypatch.setenv("SOURCE_FETCH_MAX_SEC", "1")
    monkeypatch.setattr(sources, "SPEED_WINDOW_SEC", 3600.0)
    monkeypatch.setenv("SOURCE_MIN_BYTES_PER_MIN", "1")
    real_read = safe_http.SafeResponse.read

    def delayed_read(self, size):  # type: ignore[no-untyped-def]
        # Первый же кусок тела «зависает» дольше дедлайна fetch.
        time.sleep(1.2)
        return real_read(self, size)

    monkeypatch.setattr(safe_http.SafeResponse, "read", delayed_read)
    response = fetch(client, "direct_url", direct(web, "/files/l.mp3"), out="inbox/deadline.mp3")
    assert response.status_code == 504
    assert response.json() == {"error": "timeout"}
    assert not (data_dir / "inbox" / "deadline.mp3.part").exists()
    assert client.get("/health").json()["busy"] is False


def test_playlist_mime_rejected(client, web, policy):
    """B3: playlist MIME → unsupported_type."""
    for kind_url in (
        ("direct_url", direct(web, "/playlist.m3u8")),
    ):
        response = probe(client, kind_url[0], kind_url[1])
        assert response.status_code == 400
        assert response.json() == {"error": "unsupported_type"}
        response = fetch(client, kind_url[0], kind_url[1], out="inbox/pl.m3u8")
        assert response.status_code == 400
        assert response.json() == {"error": "unsupported_type"}


@pytest.mark.parametrize(
    "mime",
    [
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "audio/mpegurl",
        "audio/x-scpls",
        "application/asx",
        "video/x-ms-asx",
        "application/dash+xml",
    ],
)
def test_classify_media_rejects_playlist_mime(mime):
    with pytest.raises(safe_http.SourceError) as err:
        sources.classify_media(mime, "x")
    assert err.value.code == "unsupported_type"


def test_reject_unsafe_container_by_format_name():
    with pytest.raises(safe_http.SourceError) as err:
        sources.reject_unsafe_container(info={"format": {"format_name": "hls,applehttp"}})
    assert err.value.code == "unsupported_type"
    with pytest.raises(safe_http.SourceError):
        sources.reject_unsafe_container(info={"format": {"format_name": "dash"}})
    with pytest.raises(safe_http.SourceError):
        sources.reject_unsafe_container(info={"format": {"format_name": "concat"}})
    with pytest.raises(safe_http.SourceError):
        sources.reject_unsafe_container(info={"format": {"format_name": "sdp"}})
    with pytest.raises(safe_http.SourceError):
        sources.reject_unsafe_container(info={"format": {"format_name": "image2"}})
    sources.reject_unsafe_container(info={"format": {"format_name": "mp3"}})
    sources.reject_unsafe_container(info=None)


def test_direct_url_404_410_are_download_failed(client, web, policy):
    """S3: у direct_url 404/410 → download_failed (не not_public)."""
    missing = probe(client, "direct_url", direct(web, "/missing-file"))
    assert missing.status_code == 502
    assert missing.json() == {"error": "download_failed"}
    gone = fetch(client, "direct_url", direct(web, "/gone"), out="inbox/gone.mp3")
    assert gone.status_code == 502
    assert gone.json() == {"error": "download_failed"}


def test_probe_semaphore_limits_concurrency(client, web, policy):
    """S2: при трёх занятых слотах четвёртая проба → 409 busy; слоты освобождаются."""
    assert app_module.SOURCE_PROBE_MAX_CONCURRENT == 3
    held = 0
    try:
        while app_module._probe_slots.acquire(blocking=False):
            held += 1
        assert held == 3
        fourth = probe(client, "direct_url", direct(web, "/files/l.mp3"))
        assert fourth.status_code == 409
        assert fourth.json() == {"error": "busy"}
        assert web["hits"] == []
    finally:
        for _ in range(held):
            app_module._probe_slots.release()
    ok = probe(client, "direct_url", direct(web, "/files/l.mp3"))
    assert ok.status_code == 200, ok.text


def test_protocol_whitelist_in_ffmpeg_helpers():
    import inspect

    from chunking import window_ffmpeg_args

    win = window_ffmpeg_args("/data/a.wav", 0, 10)
    assert win[win.index("-protocol_whitelist") + 1] == "file,pipe"
    assert win.index("-protocol_whitelist") < win.index("-i")
    assert "file,pipe" in inspect.getsource(app_module.extract_audio)
    assert "file,pipe" in inspect.getsource(app_module.probe_audio)


# --- обратная совместимость ---------------------------------------------------------


def test_existing_routes_still_registered():
    paths = {route.path for route in app_module.app.routes}
    assert {
        "/health",
        "/extract-audio",
        "/transcribe",
        "/files/delete",
        "/drive/list-media",
        "/drive/fetch",
        "/source/probe",
        "/source/fetch",
        "/inbox/upload",
    } <= paths
