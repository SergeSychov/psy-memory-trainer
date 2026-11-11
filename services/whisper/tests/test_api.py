import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import (
    DEFAULT_MODEL,
    PROMPT_CHAR_LIMIT,
    begin_job,
    end_job,
    idle_exit_seconds,
    idle_unload_seconds,
    load_settings,
    probe_audio,
    truncate_prompt,
)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    cache = tmp_path / "models"
    root.mkdir()
    cache.mkdir()
    monkeypatch.setenv("DATA_DIR", str(root))
    monkeypatch.setenv("WHISPER_CACHE_DIR", str(cache))
    monkeypatch.setenv("HF_HOME", str(cache))
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    monkeypatch.setenv("WHISPER_MODEL", "tiny")
    monkeypatch.setenv("WHISPER_IDLE_EXIT_SEC", "0")
    monkeypatch.delenv("WHISPER_TOKEN", raising=False)
    return root


@pytest.fixture()
def client(data_dir):
    from app import app

    with TestClient(app) as test_client:
        yield test_client


def synth_tone(path: Path, seconds: float = 1.5) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(path),
        ],
        check=True,
    )


def synth_speech(path: Path) -> None:
    subprocess.run(
        [
            "espeak-ng",
            "-v",
            "ru",
            "-w",
            str(path),
            "Раз, два, три. Проверка сервиса.",
        ],
        check=True,
    )


def test_defaults_are_turbo_int8():
    settings = load_settings({})
    assert settings.model == DEFAULT_MODEL == "large-v3-turbo"
    assert settings.compute_type == "int8"
    assert settings.cpu_threads == 8
    assert settings.vad_filter is True
    assert idle_exit_seconds({}) == 0
    assert idle_unload_seconds({}) == 900
    assert load_settings({"WHISPER_MODEL": "large-v3"}).model == "large-v3"
    assert load_settings({"WHISPER_CPU_THREADS": "32"}).cpu_threads == 8
    with pytest.raises(Exception, match="only cpu"):
        load_settings({"WHISPER_DEVICE": "cuda"})
    with pytest.raises(Exception, match="not allowed"):
        load_settings({"WHISPER_MODEL": "large-v2"})


def test_truncate_prompt_on_comma():
    empty, truncated = truncate_prompt("  ")
    assert empty is None and truncated is False
    short, truncated = truncate_prompt("альфа, бета")
    assert short == "альфа, бета" and truncated is False
    long, truncated = truncate_prompt("термин, " * 200)
    assert truncated is True
    assert long is not None
    assert len(long) <= PROMPT_CHAR_LIMIT
    assert not long.endswith(",")


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["ffmpeg"] is True
    assert body["device"] == "cpu"
    assert body["model"] == "tiny"
    assert body["compute_type"] == "int8"
    assert "text" not in body


def test_cuda_setting_degrades_health(client, monkeypatch):
    monkeypatch.setenv("WHISPER_DEVICE", "cuda")
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["error"] == "only cpu is supported"


def test_invalid_body(client):
    response = client.post("/extract-audio", json={})
    assert response.status_code == 422
    assert response.json() == {"error": "invalid body"}


def test_path_outside_data_dir(client):
    response = client.post("/extract-audio", json={"input_path": "/etc/passwd"})
    assert response.status_code == 400
    assert response.json()["error"] == "path outside DATA_DIR"
    response = client.post("/extract-audio", json={"input_path": "../../etc/passwd"})
    assert response.status_code == 400


def test_missing_file(client, data_dir):
    response = client.post(
        "/transcribe",
        json={"audio_path": str(data_dir / "missing.wav"), "model": "tiny"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "file not found"


def test_unknown_model_rejected_before_transcribe(client):
    response = client.post(
        "/transcribe",
        json={"audio_path": "nope.wav", "model": "large-v2"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "model is not allowed"


def test_token_required_when_set(client, monkeypatch):
    monkeypatch.setenv("WHISPER_TOKEN", "secret")
    denied = client.post("/extract-audio", json={"input_path": "missing.wav"})
    assert denied.status_code == 401
    allowed = client.post(
        "/extract-audio",
        json={"input_path": "missing.wav"},
        headers={"Authorization": "Bearer secret"},
    )
    assert allowed.status_code == 404


def test_busy(client, data_dir):
    wav = data_dir / "tone.wav"
    synth_tone(wav, 0.4)
    begin_job()
    try:
        response = client.post("/extract-audio", json={"input_path": str(wav)})
    finally:
        end_job()
    assert response.status_code == 409
    assert response.json()["error"] == "busy"


def test_extract_rejects_non_media(client, data_dir):
    note = data_dir / "note.txt"
    note.write_text("это не лекция и не аудио", encoding="utf-8")
    response = client.post("/extract-audio", json={"input_path": str(note)})
    assert response.status_code == 400
    assert "error" in response.json()


def test_decode_for_model_is_16k_mono(data_dir):
    wav = data_dir / "tone.wav"
    synth_tone(wav, 1.0)
    from app import decode_for_model

    audio, duration = decode_for_model(wav, 3600)
    assert abs(duration - 1.0) < 0.2
    assert abs(len(audio) - 16000) < 4000


def test_extract_tone_to_mono_opus(client, data_dir):
    wav = data_dir / "tone.wav"
    synth_tone(wav, 1.5)
    response = client.post("/extract-audio", json={"input_path": "tone.wav"})
    assert response.status_code == 200, response.text
    body = response.json()
    opus = Path(body["output_path"])
    assert opus.is_file()
    assert opus.parent == data_dir
    assert body["codec"] == "opus"
    assert body["channels"] == 1
    assert body["sample_rate"] == 16000
    assert body["container_sample_rate"] in (16000, 48000)
    assert 1.0 < body["duration_sec"] < 2.2
    decoded = data_dir / "decoded.wav"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(opus),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(decoded),
        ],
        check=True,
    )
    info = probe_audio(decoded)
    assert info["sample_rate"] == 16000
    assert info["channels"] == 1


def test_transcribe_contract_without_loading_weights(client, data_dir, monkeypatch):
    wav = data_dir / "clip.wav"
    synth_tone(wav, 0.5)
    seen = {}

    class _Segment:
        def __init__(self, start, end, text):
            self.start = start
            self.end = end
            self.text = text

    class _Info:
        duration = 1.25
        language = "ru"

    class _Model:
        def transcribe(self, path, **kwargs):
            seen["path"] = path
            seen["kwargs"] = kwargs
            return [_Segment(0.0, 0.4, "  "), _Segment(0.4, 1.25, "  раз два  ")], _Info()

    def _load(name, _settings):
        assert name == "tiny"
        return _Model(), 0.2

    monkeypatch.setattr("app.load_model", _load)
    long_prompt = "термин, " * 200
    response = client.post(
        "/transcribe",
        json={
            "audio_path": str(wav),
            "initial_prompt": long_prompt,
            "language": "RU",
            "model": "tiny",
            "vad_filter": False,
            "beam_size": 1,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "раз два"
    assert body["segments"] == [
        {"ordinal": 1, "start_ms": 400, "end_ms": 1250, "text": "раз два"}
    ]
    assert body["model"] == "tiny"
    assert body["compute_type"] == "int8"
    assert body["language"] == "ru"
    assert body["duration_sec"] == 1.25
    assert body["model_load_sec"] == 0.2
    assert body["elapsed_sec"] >= body["transcribe_sec"]
    assert body["initial_prompt_truncated"] is True
    assert seen["kwargs"]["language"] == "ru"
    assert seen["kwargs"]["vad_filter"] is False
    assert seen["kwargs"]["beam_size"] == 1
    assert len(seen["kwargs"]["initial_prompt"]) <= PROMPT_CHAR_LIMIT


def test_synthetic_speech_roundtrip(client, data_dir):
    if shutil.which("espeak-ng") is None:
        pytest.skip("espeak-ng нужен, чтобы синтезировать короткую фразу")
    wav = data_dir / "check.wav"
    synth_speech(wav)
    extracted = client.post("/extract-audio", json={"input_path": str(wav)})
    assert extracted.status_code == 200, extracted.text
    opus = extracted.json()["output_path"]
    response = client.post(
        "/transcribe",
        json={
            "audio_path": opus,
            "language": "ru",
            "model": "tiny",
            "initial_prompt": "проверка, сервиса",
            "vad_filter": False,
            "beam_size": 1,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model"] == "tiny"
    assert body["language"] == "ru"
    assert body["duration_sec"] > 0.5
    assert body["elapsed_sec"] >= 0
    assert body["initial_prompt_truncated"] is False
    assert isinstance(body["text"], str) and body["text"].strip()
    assert body["segments"]
    for segment in body["segments"]:
        assert segment["end_ms"] >= segment["start_ms"]
        assert segment["text"].strip()
        assert segment["ordinal"] >= 1
