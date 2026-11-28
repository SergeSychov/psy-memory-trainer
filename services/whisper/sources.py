"""Источники лекции по ссылке: проба и скачивание в /data.

Виды: yandex_public (публичная ссылка Яндекс Диска), gdrive_public («доступ по ссылке»
в Google Drive, без OAuth), direct_url (прямой https). Всё сетевое идёт через safe_http
(SSRF-защита, пиннинг IP, ручные редиректы). Ни URL, ни имена файлов в лог не пишутся.

Длительность для пробы берётся так: первые байты тела уходят в ffprobe через stdin.
ffprobe сам в сеть не ходит (иначе он обошёл бы пиннинг), поэтому у файла с moov в конце
длительность может остаться неизвестной, это штатно (`duration_sec: null`).
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import safe_http
from safe_http import HTML_TYPES, Policy, SafeResponse, SourceError

log = logging.getLogger("psy.whisper.source")

KINDS = ("yandex_public", "gdrive_public", "direct_url")
GDRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
YANDEX_HOSTS = frozenset({"disk.yandex.ru", "disk.yandex.com", "yadi.sk"})
YANDEX_PATH = re.compile(r"^/[di]/[^/\s]{4,200}(/.*)?$")
GENERIC_MIME = frozenset({"", "application/octet-stream", "binary/octet-stream", "application/force-download"})
TEXT_MIME = frozenset({"text/plain", "text/markdown", "text/x-markdown"})
AUDIO_EXT = frozenset({"mp3", "m4a", "wav", "ogg", "oga", "opus", "flac", "aac", "wma", "amr", "aiff"})
VIDEO_EXT = frozenset({"mp4", "mkv", "webm", "mov", "avi", "m4v", "mpeg", "mpg", "3gp", "wmv", "flv", "ts"})
TEXT_EXT = frozenset({"txt", "md"})
PROBE_HEAD_BYTES = 8 * 1024 * 1024
FFPROBE_TIMEOUT_SEC = 15
CHUNK = 1024 * 1024
DISK_RECHECK_EVERY = 64  # чанков
DEFAULT_FETCH_MAX_SEC = 3300
DEFAULT_MIN_BYTES_PER_MIN = 64 * 1024
SPEED_WINDOW_SEC = 60.0
# MIME/контейнеры плейлистов и «картинка как видео» — не скачиваем как лекцию.
PLAYLIST_MIME_MARKERS = ("mpegurl", "scpls", "asx", "dash+xml")
UNSAFE_FORMAT_NAMES = frozenset({"hls", "concat", "sdp", "dash", "image2"})


@dataclass(frozen=True)
class SourceConfig:
    timeout_sec: float = 30.0
    probe_timeout_sec: float = 30.0
    fetch_max_sec: float = float(DEFAULT_FETCH_MAX_SEC)
    min_bytes_per_min: int = DEFAULT_MIN_BYTES_PER_MIN
    yandex_api_base: str = "https://cloud-api.yandex.net"
    gdrive_base: str = "https://drive.google.com"
    min_free_mb: int = 3072
    head_bytes: int = PROBE_HEAD_BYTES


@dataclass
class Opened:
    resp: SafeResponse
    name: str | None = None
    size: int | None = None
    mime: str | None = None
    kind_hint: str | None = None  # audio|video по метаданным провайдера


def _bad_link(reason: str) -> SourceError:
    log.warning("source reject reason=%s", reason)
    return SourceError("unsupported_link", 400, reason)


def gdrive_file_id(url: str) -> str:
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or host != "drive.google.com":
        raise _bad_link("gdrive_host")
    match = re.match(r"^/file/(?:u/\d+/)?d/([^/?#]+)", parts.path)
    if match:
        file_id = match.group(1)
    elif parts.path in ("/open", "/uc"):
        ids = urllib.parse.parse_qs(parts.query).get("id") or []
        file_id = ids[0] if ids else ""
    else:
        raise _bad_link("gdrive_path")  # папки, docs и прочее
    if not GDRIVE_ID.match(file_id):
        raise _bad_link("gdrive_id")
    return file_id


def yandex_public_key(url: str) -> str:
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if (
        parts.scheme.lower() != "https"
        or host not in YANDEX_HOSTS
        or parts.username
        or parts.port not in (None, 443)
        or not YANDEX_PATH.match(parts.path)
    ):
        raise _bad_link("yandex_link")
    return urllib.parse.urlunsplit(("https", host, parts.path, "", ""))


def classify_media(mime: str | None, name: str | None) -> str | None:
    """audio | video | text по MIME и расширению. None — не удалось понять.

    Плейлистные MIME (mpegurl / scpls / asx / dash+xml) — SourceError unsupported_type.
    """
    mime = (mime or "").split(";", 1)[0].strip().lower()
    if mime and any(marker in mime for marker in PLAYLIST_MIME_MARKERS):
        raise SourceError("unsupported_type", 400, "playlist_mime")
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime in TEXT_MIME:
        return "text"
    if mime in GENERIC_MIME or mime in ("application/ogg", "application/mp4"):
        ext = (name or "").rsplit(".", 1)[-1].lower() if name and "." in name else ""
        if ext in AUDIO_EXT:
            return "audio"
        if ext in VIDEO_EXT:
            return "video"
        if ext in TEXT_EXT:
            return "text"
    return None


def reject_unsafe_container(path: Path | None = None, info: dict | None = None) -> None:
    """Отклоняет hls/concat/sdp/dash/image2 по format_name ffprobe."""
    payload = info if info is not None else (ffprobe_json(path=path) if path is not None else None)
    if not payload:
        return
    raw = ((payload.get("format") or {}).get("format_name") or "")
    names = {part.strip().lower() for part in str(raw).split(",") if part.strip()}
    bad = names & UNSAFE_FORMAT_NAMES
    if bad:
        raise SourceError("unsupported_type", 400, "unsafe_format")


def _name_from_url(url: str) -> str | None:
    last = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    return safe_http.clean_filename(urllib.parse.unquote(last)) if last else None


def _total_size(resp: SafeResponse) -> int | None:
    if resp.status == 206:
        rng = resp.headers.get("Content-Range") or ""
        match = re.search(r"/(\d+)$", rng.strip())
        return int(match.group(1)) if match else None
    return resp.content_length


def _open_media(url: str, cfg: SourceConfig, policy: Policy, deadline: float | None) -> SafeResponse:
    resp = safe_http.open_url(url, policy, timeout=cfg.timeout_sec, deadline=deadline)
    if resp.content_type in HTML_TYPES:
        resp.close()
        raise SourceError("not_public", 403, "html_instead_of_file")
    return resp


def _open_gdrive(url: str, cfg: SourceConfig, policy: Policy, deadline: float | None) -> SafeResponse:
    file_id = gdrive_file_id(url)
    base = f"{cfg.gdrive_base}/uc?export=download&id={urllib.parse.quote(file_id)}"
    resp = safe_http.open_url(base, policy, timeout=cfg.timeout_sec, deadline=deadline)
    if resp.content_type not in HTML_TYPES:
        return resp
    resp.close()
    # Единственный разрешённый шаг по HTML: подтверждение большого файла.
    # Ссылку строим сами с confirm=t, ссылки из страницы не читаем и не открываем.
    resp = safe_http.open_url(base + "&confirm=t", policy, timeout=cfg.timeout_sec, deadline=deadline)
    if resp.content_type in HTML_TYPES:
        resp.close()
        raise SourceError("not_public", 403, "html_instead_of_file")
    return resp


def _open_yandex(url: str, cfg: SourceConfig, policy: Policy, deadline: float | None) -> Opened:
    key = urllib.parse.quote(yandex_public_key(url), safe="")
    api = f"{cfg.yandex_api_base}/v1/disk/public/resources"
    meta = safe_http.read_json(f"{api}?public_key={key}", policy, timeout=cfg.timeout_sec, deadline=deadline)
    if meta.get("type") != "file":
        raise _bad_link("yandex_not_file")  # папка
    href = safe_http.read_json(
        f"{api}/download?public_key={key}", policy, timeout=cfg.timeout_sec, deadline=deadline
    ).get("href")
    if not isinstance(href, str) or not href:
        raise SourceError("download_failed", 502, "yandex_no_href")
    size = meta.get("size")
    media_type = meta.get("media_type")
    mime = meta.get("mime_type") if isinstance(meta.get("mime_type"), str) else None
    resp = _open_media(href, cfg, policy, deadline)
    return Opened(
        resp,
        name=safe_http.clean_filename(meta["name"]) if isinstance(meta.get("name"), str) else None,
        size=size if isinstance(size, int) and size >= 0 else None,
        mime=mime,
        kind_hint=media_type if media_type in ("audio", "video") else None,
    )


def open_source(kind: str, url: str, cfg: SourceConfig, policy: Policy, deadline: float | None) -> Opened:
    if kind == "yandex_public":
        return _open_yandex(url, cfg, policy, deadline)
    if kind == "gdrive_public":
        return Opened(_open_gdrive(url, cfg, policy, deadline))
    if kind == "direct_url":
        try:
            return Opened(_open_media(url, cfg, policy, deadline))
        except SourceError as exc:
            # У прямой ссылки 404/410 — «файл пропал / битая ссылка», не «не публично».
            if exc.code == "not_public" and exc.reason in ("http_404", "http_410"):
                raise SourceError("download_failed", 502, exc.reason) from None
            raise
    raise _bad_link("kind")


def describe(opened: Opened) -> dict[str, object]:
    resp = opened.resp
    name = opened.name or resp.filename() or _name_from_url(resp.url)
    header_mime = resp.content_type
    mime = header_mime if header_mime not in GENERIC_MIME else (opened.mime or header_mime)
    size = opened.size if opened.size is not None else _total_size(resp)
    return {"name": name, "size_bytes": size, "mime": mime or None}


def ffprobe_ready() -> bool:
    return shutil.which("ffprobe") is not None


def ffprobe_json(*, data: bytes | None = None, path: Path | None = None) -> dict | None:
    """Метаданные через ffprobe. Байты идут в stdin, сеть ffprobe закрыта whitelist'ом."""
    if not ffprobe_ready():
        return None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
    ]
    if path is not None:
        cmd += ["-i", str(path)]
    else:
        cmd += ["-i", "pipe:0"]
    try:
        proc = subprocess.run(
            cmd,
            input=data if path is None else None,
            capture_output=True,
            check=False,
            timeout=FFPROBE_TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        payload = json.loads(proc.stdout.decode("utf-8", "replace"))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _kind_from_streams(info: dict | None) -> str | None:
    if not info:
        return None
    kinds = set()
    for stream in info.get("streams") or []:
        if stream.get("codec_type") == "video" and not (stream.get("disposition") or {}).get("attached_pic"):
            kinds.add("video")
        elif stream.get("codec_type") == "audio":
            kinds.add("audio")
    return "video" if "video" in kinds else ("audio" if "audio" in kinds else None)


def _duration(info: dict | None, size: int | None) -> float | None:
    if not info:
        return None
    fmt = info.get("format") or {}
    for raw in (fmt.get("duration"),):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return round(value, 3)
    try:
        bit_rate = float(fmt.get("bit_rate") or 0)
    except (TypeError, ValueError):
        bit_rate = 0.0
    if size and bit_rate > 0:
        return round(size * 8 / bit_rate, 3)
    return None


def _read_head(resp: SafeResponse, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total < limit:
        chunk = resp.read(min(CHUNK, limit - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def probe_url(kind: str, url: str, cfg: SourceConfig, policy: Policy) -> dict[str, object]:
    """Лёгкая проба: заголовки + первые байты в ffprobe. Общий дедлайн cfg.probe_timeout_sec."""
    started = time.monotonic()
    deadline = started + cfg.probe_timeout_sec
    opened = open_source(kind, url, cfg, policy, deadline)
    try:
        meta = describe(opened)
        resolved = classify_media(str(meta["mime"] or ""), meta["name"]) or opened.kind_hint  # type: ignore[arg-type]
        info = None
        if resolved != "text":
            head = _read_head(opened.resp, cfg.head_bytes)
            info = ffprobe_json(data=head)
            if resolved is None:
                resolved = _kind_from_streams(info)
    finally:
        opened.resp.close()
    if resolved is None:
        raise SourceError("unsupported_type", 400, "unknown_media_type")
    duration = None if resolved == "text" else _duration(info, meta["size_bytes"])  # type: ignore[arg-type]
    log.info(
        "source probe tag=%s kind=%s size=%s elapsed=%.3f",
        safe_http.url_tag(url),
        kind,
        meta["size_bytes"],
        time.monotonic() - started,
    )
    return {**meta, "duration_sec": duration, "resolved_kind": resolved}


def probe_file(path: Path) -> dict[str, object]:
    size = path.stat().st_size
    name = path.name
    mime = mimetypes.guess_type(name)[0]
    resolved = classify_media(mime or "", name)
    info = None
    if resolved != "text":
        info = ffprobe_json(path=path)
        if resolved is None:
            resolved = _kind_from_streams(info)
    if resolved is None:
        raise SourceError("unsupported_type", 400, "unknown_media_type")
    duration = None if resolved == "text" else _duration(info, size)
    log.info("source probe-file tag=%s size=%s", safe_http.url_tag(str(path)), size)
    return {
        "name": name,
        "size_bytes": size,
        "mime": mime,
        "duration_sec": duration,
        "resolved_kind": resolved,
    }


def fetch_url(
    kind: str,
    url: str,
    dst: Path,
    max_bytes: int,
    cfg: SourceConfig,
    policy: Policy,
    *,
    disk_free_mb: Callable[[Path], int],
    touch: Callable[[], None],
) -> dict[str, object]:
    """Стрим в <dst>.part, жёсткий max_bytes, общий дедлайн и мин. скорость. Под job-lock."""
    started = time.perf_counter()
    deadline = time.monotonic() + float(cfg.fetch_max_sec)
    opened = open_source(kind, url, cfg, policy, deadline)
    part = dst.with_name(dst.name + ".part")
    try:
        meta = describe(opened)
        # MIME плейлиста отсекаем до записи на диск.
        classify_media(str(meta["mime"] or ""), meta["name"])  # type: ignore[arg-type]
        size = meta["size_bytes"]
        if size is not None and size > max_bytes:  # type: ignore[operator]
            raise SourceError("too_large", 413, "declared_size")
        dst.parent.mkdir(parents=True, exist_ok=True)
        need = (int(size) // (1024 * 1024) if size else 0) + cfg.min_free_mb  # type: ignore[arg-type]
        if disk_free_mb(dst.parent) < need:
            raise SourceError("not enough disk space", 507, "disk_space")
        written = 0
        chunks = 0
        window_start = time.monotonic()
        window_bytes = 0
        with part.open("wb") as out:
            while True:
                if time.monotonic() > deadline:
                    raise SourceError("timeout", 504, "deadline")
                chunk = opened.resp.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                window_bytes += len(chunk)
                if written > max_bytes:
                    raise SourceError("too_large", 413, "actual_size")
                out.write(chunk)
                chunks += 1
                touch()
                now = time.monotonic()
                if now - window_start >= SPEED_WINDOW_SEC:
                    if window_bytes < int(cfg.min_bytes_per_min):
                        raise SourceError("timeout", 504, "too_slow")
                    window_start = now
                    window_bytes = 0
                if chunks % DISK_RECHECK_EVERY == 0 and disk_free_mb(dst.parent) < max(256, cfg.min_free_mb // 4):
                    raise SourceError("not enough disk space", 507, "disk_space_during")
        declared = opened.resp.content_length if opened.resp.status == 200 else None
        if declared is not None and written != declared:
            raise SourceError("download_failed", 502, "size_mismatch")
        if written == 0:
            raise SourceError("download_failed", 502, "empty_body")
        os.replace(part, dst)
        try:
            reject_unsafe_container(dst)
        except SourceError:
            # После rename отказ по format_name не должен оставлять файл в inbox.
            dst.unlink(missing_ok=True)
            raise
    except OSError:
        raise SourceError("download_failed", 502, "write_failed") from None
    finally:
        opened.resp.close()
        part.unlink(missing_ok=True)
    elapsed = round(time.perf_counter() - started, 3)
    resolved = classify_media(str(meta["mime"] or ""), meta["name"]) or opened.kind_hint  # type: ignore[arg-type]
    log.info(
        "source fetch tag=%s kind=%s bytes=%s elapsed=%.3f",
        safe_http.url_tag(str(dst)),
        kind,
        written,
        elapsed,
    )
    return {
        "output_path": str(dst),
        "size_bytes": written,
        "name": meta["name"],
        "mime": meta["mime"],
        "resolved_kind": resolved,
        "elapsed_sec": elapsed,
    }
