"""HTTP-сервис faster-whisper и ffmpeg.

n8n ffmpeg не содержит, поэтому и нарезка, и распознавание живут здесь.
Файлы не загружаются через HTTP: вызывающий и контейнер видят один каталог DATA_DIR.
Модель в память попадает на первом /transcribe и выгружается вместе с процессом.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

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
    max_audio_sec: int


class ExtractRequest(BaseModel):
    input_path: str = Field(min_length=1, max_length=4096)
    output_path: str | None = Field(default=None, max_length=4096)


class TranscribeRequest(BaseModel):
    audio_path: str = Field(min_length=1, max_length=4096)
    initial_prompt: str | None = Field(default=None, max_length=20000)
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
    idle = _env_int("WHISPER_IDLE_EXIT_SEC", 900, env)
    max_audio = _env_int("WHISPER_MAX_AUDIO_SEC", 4 * 60 * 60, env)
    vad = _env("WHISPER_VAD_FILTER", "1", env) not in {"0", "false", "False"}
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
        max_audio_sec=max(1, max_audio),
    )


def idle_exit_seconds(env: dict[str, str] | None = None) -> int:
    return load_settings(env).idle_exit_sec


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


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("WHISPER_TOKEN", "").strip()
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(401, "unauthorized")


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


def load_model(name: str, settings: Settings):
    """Вызывается только под job-lock. В памяти одна модель."""
    global _model, _model_key
    key = (name, settings.compute_type, str(settings.cache_dir))
    if _model is not None and _model_key == key:
        return _model, 0.0
    if _model is not None:
        _model = None
        _model_key = None
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
    """ffmpeg -> float32 mono 16 kHz. Так мы не зависим от PyAV и сами задаём частоту."""
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


def transcribe_file(path: Path, body: TranscribeRequest, settings: Settings) -> dict[str, object]:
    language = body.language.strip().lower()
    if len(language) < 2 or len(language) > 3 or not language.isalpha():
        raise HTTPException(400, "language must be a short code")
    name = check_model_name(body.model or settings.model)
    prompt, truncated = truncate_prompt(body.initial_prompt)
    beam = body.beam_size if body.beam_size is not None else settings.beam_size
    vad = settings.vad_filter if body.vad_filter is None else body.vad_filter
    audio, decoded_duration = decode_for_model(path, settings.max_audio_sec)
    model, load_sec = load_model(name, settings)
    started = time.perf_counter()
    segments_iter, info = model.transcribe(
        audio,
        language=language,
        initial_prompt=prompt,
        beam_size=beam,
        vad_filter=vad,
        condition_on_previous_text=True,
        without_timestamps=False,
    )
    segments: list[dict[str, object]] = []
    parts: list[str] = []
    for segment in segments_iter:
        text = (segment.text or "").strip()
        if not text:
            continue
        start_ms = int(round(float(segment.start) * 1000))
        end_ms = int(round(float(segment.end) * 1000))
        if end_ms < start_ms:
            end_ms = start_ms
        parts.append(text)
        segments.append(
            {
                "ordinal": len(segments) + 1,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
            }
        )
    transcribe_sec = time.perf_counter() - started
    duration = float(getattr(info, "duration", 0.0) or 0.0)
    if duration <= 0:
        duration = decoded_duration
    return {
        "text": " ".join(parts),
        "segments": segments,
        "model": name,
        "compute_type": settings.compute_type,
        "language": language,
        "duration_sec": round(duration, 3),
        "elapsed_sec": round(load_sec + transcribe_sec, 3),
        "model_load_sec": round(load_sec, 3),
        "transcribe_sec": round(transcribe_sec, 3),
        "initial_prompt_truncated": truncated,
    }


def _idle_loop() -> None:
    while True:
        time.sleep(15)
        try:
            idle = load_settings().idle_exit_sec
        except HTTPException:
            continue
        if idle <= 0:
            return
        if _job.locked():
            continue
        if time.monotonic() - _last_activity >= idle:
            log.info("idle for %ss, shutting down", idle)
            os.kill(os.getpid(), signal.SIGTERM)
            return


def start_idle_watch() -> None:
    global _idle_started, _last_activity
    if _idle_started:
        return
    _last_activity = time.monotonic()
    try:
        if load_settings().idle_exit_sec <= 0:
            return
    except HTTPException:
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
        "transcribe tag=%s model=%s duration=%.3f elapsed=%.3f segments=%s",
        path_tag(src),
        result["model"],
        float(result["duration_sec"]),
        float(result["elapsed_sec"]),
        len(result["segments"]),
    )
    return result
