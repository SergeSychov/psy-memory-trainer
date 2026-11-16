"""HTTP-сервис faster-whisper и ffmpeg.

n8n ffmpeg не содержит, поэтому и нарезка, и распознавание живут здесь.
Файлы не загружаются через HTTP: вызывающий и контейнер видят один каталог DATA_DIR.
Большие лекции сервис качает из Google Drive сам (/drive/fetch), n8n видео в память не берёт.
Модель в память попадает на первом /transcribe и выгружается после простоя
(WHISPER_IDLE_UNLOAD_SEC). Процесс при этом живёт дальше.
Длинный файл в модель целиком не отдаётся: распознавание идёт окнами
WHISPER_CHUNK_SEC, в RAM один кусок float32.
"""

from __future__ import annotations

import os

# До загрузки CTranslate2. На Intel с AMX int8 через MKL даёт мусорные токены;
# AVX512 этот путь отключает. На AMD (прод, EPYC Rome) флаг не меняет рабочий ISA.
os.environ.setdefault("MKL_ENABLE_INSTRUCTIONS", "AVX512")

import ctypes
import gc
import hashlib
import json
import logging
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from chunking import (
    DEFAULT_HALLUCINATION_PHRASES,
    AudioPiece,
    assemble_segments,
    filter_hallucinations,
    merge_terms,
    normalize_text,
    number_segments,
    parse_silencedetect,
    plan_pieces,
    window_ffmpeg_args,
)
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("psy.whisper")

DEFAULT_MODEL = "large-v3-turbo"
ALLOWED_MODELS = frozenset(
    {
        "tiny",
        "tiny.en",
        "base",
        "small",
        "medium",
        "large-v3",
        "large-v3-turbo",
    }
)
ALLOWED_COMPUTE = frozenset({"int8", "int8_float32", "int16", "float32"})
PROMPT_CHAR_LIMIT = 800
DRIVE_ID_PATTERN = r"^[A-Za-z0-9_-]{5,200}$"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_FILE_FIELDS = (
    "id,name,mimeType,size,md5Checksum,createdTime,modifiedTime,parents,"
    "trashed,videoMediaMetadata(durationMillis)"
)
DOWNLOAD_CHUNK = 1024 * 1024
IDLE_POLL_SEC = 15

_job = threading.Lock()
_model = None
_model_key: tuple[str, str, str] | None = None
_last_activity = time.monotonic()
_idle_started = False


class Settings(BaseModel):
    model_config = {"frozen": True}

    data_dir: Path
    model: str
    compute_type: str
    cpu_threads: int
    cache_dir: Path
    beam_size: int
    vad_filter: bool
    extract_timeout_sec: int
    idle_exit_sec: int
    idle_unload_sec: int
    max_audio_sec: int
    min_free_mb: int
    drive_api_base: str
    drive_timeout_sec: int
    chunk_sec: float
    chunk_overlap_sec: float
    silence_noise_db: float
    silence_min_sec: float
    silence_search_sec: float
    condition_on_previous_text: bool
    hallucination_silence_sec: float | None
    hallucination_phrases: tuple[str, ...]
    prompts_file: Path | None


class ExtractRequest(BaseModel):
    input_path: str = Field(min_length=1, max_length=4096)
    output_path: str | None = Field(default=None, max_length=4096)
    delete_input: bool = False


class DeleteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class DriveListRequest(BaseModel):
    folder_id: str = Field(pattern=DRIVE_ID_PATTERN)
    depth: int = Field(default=1, ge=0, le=3)


class DriveFetchRequest(BaseModel):
    file_id: str = Field(pattern=DRIVE_ID_PATTERN)
    output_path: str = Field(min_length=1, max_length=4096)


class TranscribeRequest(BaseModel):
    audio_path: str = Field(min_length=1, max_length=4096)
    initial_prompt: str | None = Field(default=None, max_length=20000)
    hotwords: str | None = Field(default=None, max_length=20000)
    # Ключ курса (uuid). Текст промпта по нему читается с диска, в запрос его не кладут.
    prompt_key: str | None = Field(default=None, max_length=200)
    language: str = "ru"
    model: str | None = None
    vad_filter: bool | None = None
    beam_size: int | None = Field(default=None, ge=1, le=5)


def _env(name: str, default: str, env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    value = source.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int, env: dict[str, str] | None = None) -> int:
    raw = _env(name, str(default), env)
    try:
        return int(raw)
    except ValueError as exc:
        raise HTTPException(400, name + " must be an integer") from exc


def _env_float(name: str, default: float, env: dict[str, str] | None = None) -> float:
    raw = _env(name, str(default), env)
    try:
        return float(raw)
    except ValueError as exc:
        raise HTTPException(400, name + " must be a number") from exc


def _env_bool(name: str, default: bool, env: dict[str, str] | None = None) -> bool:
    raw = _env(name, "1" if default else "0", env).lower()
    return raw in {"1", "true", "yes", "on"}


def load_settings(env: dict[str, str] | None = None) -> Settings:
    model = _env("WHISPER_MODEL", DEFAULT_MODEL, env)
    compute = _env("WHISPER_COMPUTE_TYPE", "int8", env)
    if model not in ALLOWED_MODELS:
        raise HTTPException(400, "model is not allowed")
    if compute not in ALLOWED_COMPUTE:
        raise HTTPException(400, "compute_type is not allowed")
    device = _env("WHISPER_DEVICE", "cpu", env)
    if device != "cpu":
        raise HTTPException(400, "only cpu is supported")
    threads = _env_int("WHISPER_CPU_THREADS", 8, env)
    beam = _env_int("WHISPER_BEAM_SIZE", 5, env)
    timeout = _env_int("EXTRACT_TIMEOUT_SEC", 2 * 60 * 60, env)
    idle = _env_int("WHISPER_IDLE_EXIT_SEC", 0, env)
    unload = _env_int("WHISPER_IDLE_UNLOAD_SEC", 900, env)
    min_free = _env_int("WHISPER_MIN_FREE_MB", 3072, env)
    drive_timeout = _env_int("DRIVE_TIMEOUT_SEC", 120, env)
    max_audio = _env_int("WHISPER_MAX_AUDIO_SEC", 4 * 60 * 60, env)
    vad = _env("WHISPER_VAD_FILTER", "1", env) not in {"0", "false", "False"}
    chunk = _env_float("WHISPER_CHUNK_SEC", 600, env)
    overlap = _env_float("WHISPER_CHUNK_OVERLAP_SEC", 20, env)
    noise_db = _env_float("WHISPER_SILENCE_NOISE_DB", -35, env)
    silence_min = _env_float("WHISPER_SILENCE_MIN_SEC", 0.5, env)
    silence_search = _env_float("WHISPER_SILENCE_SEARCH_SEC", 90, env)
    return Settings(
        data_dir=Path(_env("DATA_DIR", "/data", env)),
        model=model,
        compute_type=compute,
        cpu_threads=max(1, min(8, threads)),
        cache_dir=Path(_env("WHISPER_CACHE_DIR", "/models", env)),
        beam_size=max(1, min(5, beam)),
        vad_filter=vad,
        extract_timeout_sec=max(1, timeout),
        idle_exit_sec=max(0, idle),
        idle_unload_sec=max(0, unload),
        max_audio_sec=max(1, max_audio),
        min_free_mb=max(0, min_free),
        drive_api_base=_env(
            "DRIVE_API_BASE", "https://www.googleapis.com/drive/v3", env
        ).rstrip("/"),
        drive_timeout_sec=max(5, drive_timeout),
        chunk_sec=max(1.0, min(900.0, chunk)),
        chunk_overlap_sec=max(0.0, min(120.0, overlap)),
        silence_noise_db=max(-90.0, min(-10.0, noise_db)),
        silence_min_sec=max(0.1, min(5.0, silence_min)),
        silence_search_sec=max(0.0, min(300.0, silence_search)),
        condition_on_previous_text=_env_bool("WHISPER_CONDITION_ON_PREVIOUS_TEXT", False, env),
        hallucination_silence_sec=_silence_threshold(env),
        hallucination_phrases=_hallucination_phrases(env),
        prompts_file=_prompts_path(env),
    )


def _silence_threshold(env: dict[str, str] | None) -> float | None:
    raw = _env("WHISPER_HALLUCINATION_SILENCE_THRESHOLD", "0", env).lower()
    if raw in {"", "0", "off", "none", "false"}:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise HTTPException(
            400, "WHISPER_HALLUCINATION_SILENCE_THRESHOLD must be a number"
        ) from exc
    if value <= 0:
        return None
    return min(value, 30.0)


def _hallucination_phrases(env: dict[str, str] | None) -> tuple[str, ...]:
    phrases: list[str] = []
    if _env_bool("WHISPER_HALLUCINATION_USE_DEFAULTS", True, env):
        phrases.extend(DEFAULT_HALLUCINATION_PHRASES)
    extra = _env("WHISPER_HALLUCINATION_PHRASES", "", env)
    for part in extra.split("|"):
        text = normalize_text(part)
        if text and text not in phrases:
            phrases.append(text)
    return tuple(normalize_text(item) for item in phrases)


def _prompts_path(env: dict[str, str] | None) -> Path | None:
    raw = _env("WHISPER_PROMPTS_FILE", "", env)
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(_env("DATA_DIR", "/data", env)) / path
    return path


def idle_exit_seconds(env: dict[str, str] | None = None) -> int:
    return load_settings(env).idle_exit_sec


def idle_unload_seconds(env: dict[str, str] | None = None) -> int:
    return load_settings(env).idle_unload_sec


def truncate_prompt(prompt: str | None) -> tuple[str | None, bool]:
    if prompt is None:
        return None, False
    text = prompt.strip()
    if not text:
        return None, False
    if len(text) <= PROMPT_CHAR_LIMIT:
        return text, False
    cut = text[:PROMPT_CHAR_LIMIT]
    comma = cut.rfind(",")
    if comma >= 200:
        cut = cut[:comma]
    return cut.strip(), True


def _prompt_fields(value: object) -> tuple[str, str]:
    if isinstance(value, str):
        return value.strip()[:20000], ""
    if isinstance(value, dict):
        prompt = value.get("initial_prompt") or ""
        hotwords = value.get("hotwords") or ""
        if not isinstance(prompt, str):
            prompt = ""
        if not isinstance(hotwords, str):
            hotwords = ""
        return prompt.strip()[:20000], hotwords.strip()[:20000]
    return "", ""


def load_prompt_table(path: Path | None) -> dict[str, tuple[str, str]]:
    """course id → (initial_prompt, hotwords). Пустого файла и пустых строк нет в таблице."""
    if path is None or not path.is_file():
        return {}
    try:
        if path.stat().st_size > 256 * 1024:
            log.warning("whisper prompts file is too large")
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        log.warning("whisper prompts file is unreadable")
        return {}
    courses = payload.get("courses") if isinstance(payload, dict) else None
    if not isinstance(courses, dict):
        log.warning("whisper prompts file has no courses object")
        return {}
    table: dict[str, tuple[str, str]] = {}
    for key, value in courses.items():
        if not isinstance(key, str) or not key.strip():
            continue
        prompt, hotwords = _prompt_fields(value)
        if not prompt and not hotwords:
            continue
        table[key.strip()] = (prompt, hotwords)
    return table


def lookup_course_prompt(table: dict[str, tuple[str, str]], key: str | None) -> tuple[str, str]:
    if not key:
        return "", ""
    found = table.get(key.strip())
    if found is not None:
        return found
    folded = key.strip().casefold()
    for name, value in table.items():
        if name.casefold() == folded:
            return value
    return "", ""


def resolve_request_prompts(
    body: TranscribeRequest,
    settings: Settings,
) -> tuple[str | None, str | None, bool]:
    """Файл курса идёт первым: при обрезке до 800 символов фамилии не отваливаются хвостом."""
    table = load_prompt_table(settings.prompts_file)
    file_prompt, file_hotwords = lookup_course_prompt(table, body.prompt_key)
    # Без файла курса строка запроса режется как раньше, включая повторы.
    # Со списком курса повторы выкидываются, и написания из файла остаются в начале.
    if file_prompt and body.initial_prompt:
        merged = merge_terms(file_prompt, body.initial_prompt)
    elif file_prompt:
        merged = merge_terms(file_prompt)
    else:
        merged = body.initial_prompt
    prompt, truncated = truncate_prompt(merged)
    hot_source = body.hotwords.strip() if body.hotwords and body.hotwords.strip() else file_hotwords
    hotwords, hot_truncated = truncate_prompt(hot_source)
    return prompt, hotwords, truncated or hot_truncated


def resolve_data_path(raw: str, data_dir: Path, *, must_exist: bool) -> Path:
    if not raw or "\x00" in raw:
        raise HTTPException(400, "invalid path")
    root = data_dir.resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise HTTPException(400, "invalid path") from exc
    if not resolved.is_relative_to(root):
        raise HTTPException(400, "path outside DATA_DIR")
    if must_exist and not resolved.is_file():
        raise HTTPException(404, "file not found")
    return resolved


def path_tag(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:8]


def require_token(
    authorization: str | None = Header(default=None),
    x_whisper_token: str | None = Header(default=None),
) -> None:
    expected = os.environ.get("WHISPER_TOKEN", "").strip()
    if not expected:
        return
    if authorization == f"Bearer {expected}" or x_whisper_token == expected:
        return
    raise HTTPException(401, "unauthorized")


def google_token(authorization: str | None = Header(default=None)) -> str:
    """Токен доступа Google приходит в Authorization: его подставляет n8n
    из своего credential Google Drive. Сервис его не хранит и не логирует."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "google access token required")
    token = authorization[len("Bearer ") :].strip()
    if not token or len(token) > 4096 or any(c.isspace() for c in token):
        raise HTTPException(401, "google access token required")
    return token


def begin_job() -> None:
    global _last_activity
    if not _job.acquire(blocking=False):
        raise HTTPException(409, "busy")
    _last_activity = time.monotonic()


def end_job() -> None:
    global _last_activity
    _last_activity = time.monotonic()
    _job.release()


def ffmpeg_ready() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe_audio(path: Path) -> dict[str, object]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            "-select_streams",
            "a:0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise HTTPException(400, "ffprobe failed")
    payload = json.loads(proc.stdout)
    streams = payload.get("streams") or []
    if not streams:
        raise HTTPException(400, "no audio stream")
    stream = streams[0]
    duration_raw = (payload.get("format") or {}).get("duration") or stream.get("duration") or 0
    return {
        "codec": stream.get("codec_name"),
        "sample_rate": int(stream.get("sample_rate") or 0),
        "channels": int(stream.get("channels") or 0),
        "duration_sec": float(duration_raw),
    }


def default_output(src: Path) -> Path:
    return src.with_name(src.stem + ".16k.opus")


def extract_audio(src: Path, dst: Path, timeout_sec: int) -> dict[str, object]:
    if src == dst:
        raise HTTPException(400, "output_path must differ from input_path")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libopus",
        "-b:a",
        "24k",
        "-application",
        "voip",
        str(dst),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "ffmpeg timeout") from exc
    if proc.returncode != 0 or not dst.is_file():
        dst.unlink(missing_ok=True)
        detail = (proc.stderr or "ffmpeg failed").strip()[-400:]
        raise HTTPException(400, detail or "ffmpeg failed")
    info = probe_audio(dst)
    if info["codec"] != "opus" or info["channels"] != 1:
        raise HTTPException(500, "extract produced unexpected audio")
    return info


def disk_free_mb(path: Path) -> int:
    target = path if path.exists() else path.parent
    return int(shutil.disk_usage(target).free // (1024 * 1024))


def _drive_open(url: str, token: str, timeout: int):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 (URL собираем сами)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code == 401:
            # n8n по 401 обновляет OAuth-токен credential и повторяет запрос.
            raise HTTPException(401, "google token rejected") from None
        if code == 403:
            raise HTTPException(403, "drive access denied") from None
        if code == 404:
            raise HTTPException(404, "drive file not found") from None
        if code == 429:
            raise HTTPException(429, "drive rate limited") from None
        raise HTTPException(502, f"drive error {code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HTTPException(502, "drive unreachable") from exc


def _drive_json(url: str, token: str, timeout: int) -> dict:
    with _drive_open(url, token, timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def drive_metadata(file_id: str, token: str, settings: Settings) -> dict:
    query = urllib.parse.urlencode({"fields": DRIVE_FILE_FIELDS, "supportsAllDrives": "true"})
    url = f"{settings.drive_api_base}/files/{urllib.parse.quote(file_id)}?{query}"
    return _drive_json(url, token, settings.drive_timeout_sec)


def _is_media(mime: str | None) -> bool:
    return bool(mime) and (mime.startswith("video/") or mime.startswith("audio/"))


def _file_view(item: dict, folder_path: list[str]) -> dict[str, object]:
    duration_ms = (item.get("videoMediaMetadata") or {}).get("durationMillis")
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "mime_type": item.get("mimeType"),
        "media_kind": "video" if str(item.get("mimeType", "")).startswith("video/") else "audio",
        "size_bytes": int(item["size"]) if item.get("size") else None,
        "md5": item.get("md5Checksum"),
        "created_time": item.get("createdTime"),
        "modified_time": item.get("modifiedTime"),
        "duration_ms": int(duration_ms) if duration_ms else None,
        "parent_id": (item.get("parents") or [None])[0],
        "folder_path": folder_path,
    }


def drive_list_media(folder_id: str, depth: int, token: str, settings: Settings) -> dict[str, object]:
    files: list[dict[str, object]] = []
    queue: list[tuple[str, list[str], int]] = [(folder_id, [], 0)]
    seen: set[str] = set()
    folders = 0
    while queue:
        current, path, level = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        folders += 1
        page_token = None
        while True:
            params = {
                "q": f"'{current}' in parents and trashed = false",
                "fields": f"nextPageToken,files({DRIVE_FILE_FIELDS})",
                "pageSize": "1000",
                "orderBy": "createdTime",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page_token:
                params["pageToken"] = page_token
            url = f"{settings.drive_api_base}/files?{urllib.parse.urlencode(params)}"
            payload = _drive_json(url, token, settings.drive_timeout_sec)
            for item in payload.get("files") or []:
                mime = item.get("mimeType")
                if mime == DRIVE_FOLDER_MIME:
                    if level < depth:
                        queue.append((item["id"], path + [item.get("name") or ""], level + 1))
                elif _is_media(mime):
                    files.append(_file_view(item, path))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break
    return {"folder_id": folder_id, "folders_scanned": folders, "files": files}


def drive_fetch(file_id: str, dst: Path, token: str, settings: Settings) -> dict[str, object]:
    meta = drive_metadata(file_id, token, settings)
    if meta.get("trashed"):
        raise HTTPException(404, "drive file not found")
    if not _is_media(meta.get("mimeType")):
        raise HTTPException(400, "drive file is not audio or video")
    size = int(meta["size"]) if meta.get("size") else None
    dst.parent.mkdir(parents=True, exist_ok=True)
    free = disk_free_mb(dst.parent)
    need = (size or 0) // (1024 * 1024) + settings.min_free_mb
    if free < need:
        raise HTTPException(507, "not enough disk space")
    part = dst.with_name(dst.name + ".part")
    query = urllib.parse.urlencode({"alt": "media", "supportsAllDrives": "true"})
    url = f"{settings.drive_api_base}/files/{urllib.parse.quote(file_id)}?{query}"
    digest = hashlib.md5()  # noqa: S324 (сверка с md5Checksum Drive, не криптография)
    written = 0
    started = time.perf_counter()
    try:
        with _drive_open(url, token, settings.drive_timeout_sec) as resp, part.open("wb") as out:
            while True:
                chunk = resp.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                touch_activity()
    except HTTPException:
        part.unlink(missing_ok=True)
        raise
    except (OSError, TimeoutError) as exc:
        part.unlink(missing_ok=True)
        raise HTTPException(502, "drive download interrupted") from exc
    if size is not None and written != size:
        part.unlink(missing_ok=True)
        raise HTTPException(502, "drive download size mismatch")
    md5 = meta.get("md5Checksum")
    md5_ok = None if not md5 else digest.hexdigest() == md5
    if md5_ok is False:
        part.unlink(missing_ok=True)
        raise HTTPException(502, "drive download checksum mismatch")
    os.replace(part, dst)
    return {
        "output_path": str(dst),
        "size_bytes": written,
        "md5_ok": md5_ok,
        "file": _file_view(meta, []),
        "elapsed_sec": round(time.perf_counter() - started, 3),
    }


def touch_activity() -> None:
    global _last_activity
    _last_activity = time.monotonic()


def release_heap() -> None:
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def unload_model() -> bool:
    """Освобождает веса модели. Вызывать под job-lock."""
    global _model, _model_key
    if _model is None:
        return False
    _model = None
    _model_key = None
    release_heap()
    return True


def load_model(name: str, settings: Settings):
    """Вызывается только под job-lock. В памяти одна модель."""
    global _model, _model_key
    key = (name, settings.compute_type, str(settings.cache_dir))
    if _model is not None and _model_key == key:
        return _model, 0.0
    if _model is not None:
        unload_model()
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HUB_CACHE"] = str(settings.cache_dir)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    started = time.perf_counter()
    from faster_whisper import WhisperModel

    _model = WhisperModel(
        name,
        device="cpu",
        compute_type=settings.compute_type,
        cpu_threads=settings.cpu_threads,
        num_workers=1,
        download_root=str(settings.cache_dir),
    )
    _model_key = key
    return _model, time.perf_counter() - started


def model_loaded() -> bool:
    return _model is not None


def loaded_model_name() -> str | None:
    return None if _model_key is None else _model_key[0]


def check_model_name(name: str) -> str:
    if name not in ALLOWED_MODELS:
        raise HTTPException(400, "model is not allowed")
    return name


def decode_for_model(path: Path, max_seconds: int) -> tuple[object, float]:
    """Полный файл в float32. /transcribe так больше не делает: длинная лекция не влезает в 4 ГБ.

    Оставлено для коротких проверок. Распознавание идёт через decode_window.
    """
    import numpy as np

    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "pipe:1",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or "could not decode audio").strip()[-400:]
        raise HTTPException(400, detail or "could not decode audio")
    audio = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    if audio.size == 0:
        raise HTTPException(400, "no audio")
    duration = float(audio.shape[0]) / 16000.0
    if duration > max_seconds:
        raise HTTPException(400, "audio is longer than WHISPER_MAX_AUDIO_SEC")
    return audio, duration


def decode_window(path: Path, start: float, end: float, timeout_sec: int) -> object:
    """Одно окно в float32 mono 16 kHz. В память процесса попадает только оно."""
    import numpy as np

    cmd = window_ffmpeg_args(str(path), start, end)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            check=False,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "ffmpeg timeout") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"could not decode audio").decode("utf-8", "replace").strip()[-400:]
        raise HTTPException(400, detail or "could not decode audio")
    raw = proc.stdout or b""
    raw = raw[: len(raw) - (len(raw) % 4)]
    audio = np.frombuffer(raw, dtype=np.float32).copy()
    del proc, raw
    return audio


def detect_silences(path: Path, settings: Settings, duration: float) -> list[tuple[float, float]]:
    """Один проход ffmpeg без хранения PCM. Ошибка не валит распознавание: остаётся перекрытие."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-nostdin",
        "-i",
        str(path),
        "-vn",
        "-sn",
        "-dn",
        "-af",
        f"silencedetect=noise={settings.silence_noise_db:.1f}dB:d={settings.silence_min_sec:.3f}",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=settings.extract_timeout_sec,
        )
    except subprocess.TimeoutExpired:
        log.warning("silencedetect timed out, using overlap cuts")
        return []
    if proc.returncode != 0:
        log.warning("silencedetect failed, using overlap cuts")
        return []
    return parse_silencedetect(proc.stderr or "", offset=0.0, end=duration)


def _piece_view(piece: AudioPiece, discarded: int) -> dict[str, object]:
    return {
        "index": piece.index,
        "start_sec": piece.start,
        "end_sec": piece.end,
        "keep_from_sec": piece.keep_from,
        "keep_to_sec": piece.keep_to,
        "cut": piece.cut,
        "discarded_overlap": discarded,
    }


def transcribe_file(path: Path, body: TranscribeRequest, settings: Settings) -> dict[str, object]:
    language = body.language.strip().lower()
    if len(language) < 2 or len(language) > 3 or not language.isalpha():
        raise HTTPException(400, "language must be a short code")
    name = check_model_name(body.model or settings.model)
    prompt, hotwords, truncated = resolve_request_prompts(body, settings)
    beam = body.beam_size if body.beam_size is not None else settings.beam_size
    vad = settings.vad_filter if body.vad_filter is None else body.vad_filter
    probed = probe_audio(path)
    duration = float(probed["duration_sec"])
    if duration <= 0:
        raise HTTPException(400, "no audio")
    if duration > settings.max_audio_sec:
        raise HTTPException(400, "audio is longer than WHISPER_MAX_AUDIO_SEC")
    pieces = plan_pieces(
        duration,
        detect_silences(path, settings, duration),
        chunk_sec=settings.chunk_sec,
        overlap_sec=settings.chunk_overlap_sec,
        search_sec=settings.silence_search_sec,
    )
    if not pieces:
        raise HTTPException(400, "no audio")
    cap = settings.chunk_sec + settings.chunk_overlap_sec + 1.0
    for piece in pieces:
        if piece.end - piece.start > cap:
            raise HTTPException(500, "audio window exceeds chunk limit")
    model, load_sec = load_model(name, settings)
    kwargs: dict[str, object] = {
        "language": language,
        "initial_prompt": prompt,
        "beam_size": beam,
        "vad_filter": vad,
        "condition_on_previous_text": settings.condition_on_previous_text,
        "without_timestamps": False,
    }
    if hotwords:
        kwargs["hotwords"] = hotwords
    if settings.hallucination_silence_sec is not None:
        kwargs["word_timestamps"] = True
        kwargs["hallucination_silence_threshold"] = settings.hallucination_silence_sec
    groups: list[list[object]] = []
    model_duration = 0.0
    transcribe_sec = 0.0
    piece_started = time.perf_counter()
    for piece in pieces:
        touch_activity()
        audio = decode_window(path, piece.start, piece.end, settings.extract_timeout_sec)
        if getattr(audio, "size", 0) == 0:
            if len(pieces) == 1:
                raise HTTPException(400, "no audio")
            log.warning("empty audio window index=%s", piece.index)
            del audio
            groups.append([])
            continue
        try:
            started = time.perf_counter()
            segments_iter, info = model.transcribe(audio, **kwargs)
            groups.append(list(segments_iter))
            transcribe_sec += time.perf_counter() - started
            if len(pieces) == 1:
                model_duration = float(getattr(info, "duration", 0.0) or 0.0)
        finally:
            del audio
            release_heap()
    loop_sec = time.perf_counter() - piece_started
    merged, discarded = assemble_segments(pieces, groups)
    kept, dropped = filter_hallucinations(merged, settings.hallucination_phrases)
    segments = number_segments(kept)
    if len(pieces) == 1 and model_duration > 0:
        reported_duration = model_duration
    else:
        reported_duration = duration
    listed = dropped[:100]
    return {
        "text": " ".join(str(segment["text"]) for segment in segments),
        "segments": segments,
        "model": name,
        "compute_type": settings.compute_type,
        "language": language,
        "duration_sec": round(reported_duration, 3),
        "elapsed_sec": round(load_sec + loop_sec, 3),
        "model_load_sec": round(load_sec, 3),
        "transcribe_sec": round(transcribe_sec, 3),
        "initial_prompt_truncated": truncated,
        "audio_pieces": [_piece_view(piece, count) for piece, count in zip(pieces, discarded)],
        "hallucination_filter": {
            "dropped_count": len(dropped),
            "dropped_omitted": max(0, len(dropped) - len(listed)),
            "dropped": listed,
            "condition_on_previous_text": settings.condition_on_previous_text,
            "silence_threshold_sec": settings.hallucination_silence_sec,
        },
    }


def idle_tick(settings: Settings, now: float | None = None) -> str | None:
    """Один шаг сторожа простоя. Возвращает "unload", "exit" или None."""
    if _job.locked():
        return None
    now = time.monotonic() if now is None else now
    idle_for = now - _last_activity
    if settings.idle_unload_sec > 0 and _model is not None and idle_for >= settings.idle_unload_sec:
        if _job.acquire(blocking=False):
            try:
                if unload_model():
                    log.info("idle for %ss, model unloaded", settings.idle_unload_sec)
                    return "unload"
            finally:
                _job.release()
    if settings.idle_exit_sec > 0 and idle_for >= settings.idle_exit_sec:
        return "exit"
    return None


def _idle_loop() -> None:
    while True:
        time.sleep(IDLE_POLL_SEC)
        try:
            settings = load_settings()
        except HTTPException:
            continue
        if idle_tick(settings) == "exit":
            log.info("idle for %ss, shutting down", settings.idle_exit_sec)
            os.kill(os.getpid(), signal.SIGTERM)
            return


def start_idle_watch() -> None:
    global _idle_started, _last_activity
    if _idle_started:
        return
    _last_activity = time.monotonic()
    try:
        settings = load_settings()
    except HTTPException:
        return
    if settings.idle_exit_sec <= 0 and settings.idle_unload_sec <= 0:
        return
    _idle_started = True
    threading.Thread(target=_idle_loop, name="whisper-idle", daemon=True).start()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start_idle_watch()
    yield


app = FastAPI(title="psy-whisper", docs_url="/docs", redoc_url=None, lifespan=_lifespan)


@app.exception_handler(HTTPException)
def _http_error(_request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "error"
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.exception_handler(RequestValidationError)
def _invalid_body(_request, _exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "invalid body"})


@app.get("/health")
def health() -> JSONResponse:
    ready = ffmpeg_ready()
    try:
        settings = load_settings()
        model = settings.model
        compute = settings.compute_type
    except HTTPException as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "ffmpeg": ready,
                "model": None,
                "compute_type": None,
                "device": "cpu",
                "model_loaded": model_loaded(),
                "loaded_model": loaded_model_name(),
                "error": str(exc.detail),
            },
        )
    body = {
        "status": "ok" if ready else "degraded",
        "ffmpeg": ready,
        "model": model,
        "compute_type": compute,
        "device": "cpu",
        "model_loaded": model_loaded(),
        "loaded_model": loaded_model_name(),
        "busy": _job.locked(),
        "idle_unload_sec": settings.idle_unload_sec,
        "idle_exit_sec": settings.idle_exit_sec,
        "disk_free_mb": disk_free_mb(settings.data_dir),
        "chunk_sec": settings.chunk_sec,
        "prompts_file_set": settings.prompts_file is not None,
        "prompts_loaded": bool(load_prompt_table(settings.prompts_file)),
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)


@app.post("/extract-audio", dependencies=[Depends(require_token)])
def extract_audio_route(body: ExtractRequest) -> dict[str, object]:
    settings = load_settings()
    if not ffmpeg_ready():
        raise HTTPException(503, "ffmpeg is not installed")
    src = resolve_data_path(body.input_path, settings.data_dir, must_exist=True)
    if body.output_path:
        dst = resolve_data_path(body.output_path, settings.data_dir, must_exist=False)
    else:
        dst = default_output(src)
        if not dst.is_relative_to(settings.data_dir.resolve()):
            raise HTTPException(400, "path outside DATA_DIR")
    begin_job()
    started = time.perf_counter()
    try:
        info = extract_audio(src, dst, settings.extract_timeout_sec)
        if body.delete_input:
            src.unlink(missing_ok=True)
    finally:
        end_job()
    elapsed = round(time.perf_counter() - started, 3)
    log.info(
        "extract tag=%s duration=%.3f elapsed=%.3f",
        path_tag(dst),
        float(info["duration_sec"]),
        elapsed,
    )
    return {
        "output_path": str(dst),
        "duration_sec": round(float(info["duration_sec"]), 3),
        "channels": 1,
        "codec": "opus",
        "sample_rate": 16000,
        "container_sample_rate": info["sample_rate"],
        "elapsed_sec": elapsed,
        "input_deleted": bool(body.delete_input),
    }


@app.post("/transcribe", dependencies=[Depends(require_token)])
def transcribe_route(body: TranscribeRequest) -> dict[str, object]:
    settings = load_settings()
    check_model_name(body.model or settings.model)
    src = resolve_data_path(body.audio_path, settings.data_dir, must_exist=True)
    begin_job()
    try:
        try:
            result = transcribe_file(src, body, settings)
        except HTTPException:
            raise
        except Exception as exc:
            log.error("transcribe failed: %s", type(exc).__name__)
            raise HTTPException(500, "transcribe failed") from exc
    finally:
        end_job()
    log.info(
        "transcribe tag=%s model=%s duration=%.3f elapsed=%.3f segments=%s pieces=%s dropped=%s",
        path_tag(src),
        result["model"],
        float(result["duration_sec"]),
        float(result["elapsed_sec"]),
        len(result["segments"]),
        len(result["audio_pieces"]),
        result["hallucination_filter"]["dropped_count"],
    )
    return result


@app.post("/files/delete", dependencies=[Depends(require_token)])
def delete_file_route(body: DeleteRequest) -> dict[str, object]:
    settings = load_settings()
    target = resolve_data_path(body.path, settings.data_dir, must_exist=False)
    if target == settings.data_dir.resolve():
        raise HTTPException(400, "invalid path")
    existed = target.is_file()
    if existed:
        target.unlink()
    target.with_name(target.name + ".part").unlink(missing_ok=True)
    log.info("delete tag=%s existed=%s", path_tag(target), existed)
    return {"path": str(target), "deleted": existed}


# Эндпоинты Drive защищены токеном Google в Authorization (его подставляет
# credential n8n). WHISPER_TOKEN здесь не проверяется: заголовок Authorization занят.
@app.post("/drive/list-media")
def drive_list_route(body: DriveListRequest, token: str = Depends(google_token)) -> dict[str, object]:
    settings = load_settings()
    result = drive_list_media(body.folder_id, body.depth, token, settings)
    log.info("drive list folders=%s files=%s", result["folders_scanned"], len(result["files"]))
    return result


@app.post("/drive/fetch")
def drive_fetch_route(body: DriveFetchRequest, token: str = Depends(google_token)) -> dict[str, object]:
    settings = load_settings()
    dst = resolve_data_path(body.output_path, settings.data_dir, must_exist=False)
    begin_job()
    try:
        result = drive_fetch(body.file_id, dst, token, settings)
    finally:
        end_job()
    log.info(
        "drive fetch tag=%s bytes=%s elapsed=%.3f",
        path_tag(dst),
        result["size_bytes"],
        float(result["elapsed_sec"]),
    )
    return result
