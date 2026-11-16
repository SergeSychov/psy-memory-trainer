"""Чанки, таймкоды, фильтр галлюцинаций и декодирование окнами.

Аудио только синтетическое. Списков терминов курса здесь нет.
"""

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chunking import (
    assemble_segments,
    filter_hallucinations,
    merge_terms,
    parse_silencedetect,
    plan_pieces,
    window_ffmpeg_args,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PROMPTS = ROOT / "prompts.example.json"


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


def synth_tone(path: Path, seconds: float) -> None:
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


def _cover(duration: float, **kwargs: float) -> list:
    pieces = plan_pieces(duration, kwargs.pop("silences", []), **kwargs)
    assert pieces[0].keep_from == 0
    assert pieces[-1].keep_to == duration
    assert pieces[-1].end == duration
    for left, right in zip(pieces, pieces[1:]):
        assert left.keep_to == right.keep_from
        assert right.start <= left.keep_to + 0.001
    cap = kwargs["chunk_sec"] + kwargs["overlap_sec"] + 0.08
    assert max(piece.end - piece.start for piece in pieces) <= cap
    return pieces


def test_short_file_is_one_piece():
    pieces = _cover(12.5, chunk_sec=600, overlap_sec=20, search_sec=90)
    assert len(pieces) == 1
    assert pieces[0].cut == "end"
    assert pieces[0].start == 0


def test_hard_cuts_overlap_and_cover_four_hours():
    pieces = _cover(14400, chunk_sec=600, overlap_sec=20, search_sec=90)
    assert len(pieces) > 4
    assert pieces[0].cut == "hard"
    assert pieces[0].end > pieces[0].keep_to
    assert pieces[1].start < pieces[1].keep_from
    assert max(piece.end - piece.start for piece in pieces) <= 620.05


def test_silence_cut_has_no_overlap_and_keeps_ownership_closed():
    silences = [(i - 8.0, i + 8.0) for i in range(600, 14400, 600)]
    pieces = plan_pieces(
        14400,
        silences,
        chunk_sec=600,
        overlap_sec=20,
        search_sec=90,
    )
    assert pieces[0].cut == "silence"
    assert pieces[0].end == pieces[0].keep_to == pieces[1].start == pieces[1].keep_from
    assert all(piece.cut != "hard" for piece in pieces)
    assert max(piece.end - piece.start for piece in pieces) <= 620.05
    assert pieces[-1].keep_to == 14400


def test_silence_outside_the_search_window_is_ignored():
    pieces = plan_pieces(
        30,
        [(1.0, 1.8)],
        chunk_sec=10,
        overlap_sec=4,
        search_sec=3,
    )
    assert pieces[0].cut == "hard"
    assert pieces[0].keep_to == 10


def test_timestamp_offset_is_absolute():
    pieces = plan_pieces(900, [(590, 610)], chunk_sec=600, overlap_sec=20, search_sec=90)
    assert pieces[0].cut == "silence"
    groups = [
        [{"start": 1.5, "end": 2.25, "text": "термин"}],
        [{"start": 0.4, "end": 1.0, "text": "дальше"}],
    ]
    # Второе окно начинается в паузе; 0.4 c внутри него — уже после разреза.
    kept, discarded = assemble_segments(pieces[:2], groups)
    assert discarded == [0, 0]
    assert kept[0]["start_ms"] == 1500
    assert kept[0]["end_ms"] == 2250
    assert kept[1]["start_ms"] == int(round((pieces[1].start + 0.4) * 1000))
    assert kept[1]["text"] == "дальше"


def test_boundary_word_is_kept_once_and_both_sides_survive():
    pieces = plan_pieces(20, [], chunk_sec=10, overlap_sec=4, search_sec=3)
    assert len(pieces) == 2
    assert pieces[0].keep_to == 10
    assert pieces[1].keep_from == 10

    def rel(piece, start: float, end: float, text: str) -> dict:
        return {"start": start - piece.start, "end": end - piece.start, "text": text}

    left = pieces[0]
    right = pieces[1]
    groups = [
        [
            rel(left, 1.0, 2.0, "один"),
            rel(left, 8.6, 9.4, "лево"),
            rel(left, 9.2, 10.8, "стык"),
            rel(left, 10.2, 11.0, "право"),
        ],
        [
            rel(right, 8.6, 9.4, "лево"),
            rel(right, 9.2, 10.8, "стык"),
            rel(right, 10.2, 11.0, "право"),
            rel(right, 14.0, 15.0, "два"),
        ],
    ]
    kept, discarded = assemble_segments(pieces, groups)
    texts = [segment["text"] for segment in kept]
    assert texts == ["один", "лево", "стык", "право", "два"]
    assert discarded[0] >= 1
    assert discarded[1] >= 1
    starts = [segment["start_ms"] for segment in kept]
    assert starts == sorted(starts)
    assert kept[0]["start_ms"] == 1000
    assert kept[-1]["end_ms"] == 15000


def test_parse_silencedetect_offsets_and_keeps_open_tail():
    stderr = "\n".join(
        [
            "[silencedetect @ 0x1] silence_start: 1.5",
            "[silencedetect @ 0x1] silence_end: 3.25 | silence_duration: 1.75",
            "[silencedetect @ 0x1] silence_start: 8",
        ]
    )
    assert parse_silencedetect(stderr, offset=10, end=20) == [(11.5, 13.25), (18.0, 20.0)]


def test_merge_terms_keeps_file_order_and_drops_duplicates():
    merged = merge_terms("альфа, бета", "бета, дельта")
    assert merged == "альфа, бета, дельта"
    assert merge_terms("  ", None) is None


def test_blocklist_drops_every_copy_and_reports_the_reason():
    segments = [
        {"start_ms": 0, "end_ms": 400, "text": "Лекция началась."},
        {"start_ms": 5000, "end_ms": 5600, "text": "Продолжение следует..."},
        {"start_ms": 5700, "end_ms": 6300, "text": "Продолжение следует..."},
        {"start_ms": 6400, "end_ms": 7000, "text": "Продолжение следует…"},
        {"start_ms": 8000, "end_ms": 9000, "text": "Субтитры сделал Ivan"},
        {"start_ms": 9100, "end_ms": 9800, "text": "Редактор субтитров А."},
        {"start_ms": 10000, "end_ms": 11000, "text": "Спасибо за просмотр!"},
        {"start_ms": 12000, "end_ms": 13000, "text": "Дальше о внимании."},
    ]
    phrases = (
        "продолжение следует",
        "субтитры сделал",
        "редактор субтитров",
        "спасибо за просмотр",
    )
    kept, dropped = filter_hallucinations(segments, phrases)
    assert [segment["text"] for segment in kept] == ["Лекция началась.", "Дальше о внимании."]
    assert len(dropped) == 6
    assert {item["reason"] for item in dropped} == {"blocklist"}
    assert all("Продолжение следует" not in segment["text"] for segment in kept)


def test_long_sentence_is_not_blocklisted_and_consecutive_copies_collapse():
    lecture = ("слово " * 40) + "продолжение следует в рассказе преподавателя"
    segments = [
        {"start_ms": 0, "end_ms": 4000, "text": lecture},
        {"start_ms": 5000, "end_ms": 5400, "text": "Повтор."},
        {"start_ms": 5450, "end_ms": 5800, "text": "повтор"},
        {"start_ms": 20000, "end_ms": 20400, "text": "повтор"},
    ]
    kept, dropped = filter_hallucinations(segments, ("продолжение следует",))
    assert kept[0]["text"] == lecture
    assert [segment["text"] for segment in kept].count("Повтор.") == 1
    assert any(item["reason"] == "consecutive_duplicate" for item in dropped)
    # Далёкий повтор без текста между ними тоже подряд в ленте, но дыру в 15 с не заливаем.
    assert kept[1]["end_ms"] < 10000


def test_boundary_duplicate_collapses_overlapping_near_copies():
    segments = [
        {"start_ms": 9000, "end_ms": 10800, "text": "альфа бета"},
        {"start_ms": 9600, "end_ms": 11200, "text": "альфа бета."},
        {"start_ms": 14000, "end_ms": 15000, "text": "два"},
    ]
    kept, dropped = filter_hallucinations(segments, ())
    assert [segment["text"] for segment in kept] == ["альфа бета", "два"]
    assert dropped[0]["reason"] == "boundary_duplicate"
    assert kept[0]["end_ms"] == 11200


def test_window_ffmpeg_args_seek_and_limit_the_slice():
    args = window_ffmpeg_args("/data/audio/lecture.16k.opus", 600, 900)
    assert args[args.index("-ss")] == "-ss"
    assert "599.000" in args
    assert "1.000" in args
    assert args[args.index("-t") + 1] == "300.000"
    assert "14400" not in args
    head = window_ffmpeg_args("/data/a.wav", 0, 10)
    assert "-ss" not in head
    assert head[head.index("-t") + 1] == "10.000"


def test_psy03_passes_course_key_and_stays_inactive():
    workflow = json.loads((ROOT.parents[1] / "n8n/workflows/psy_03_transcribe.json").read_text(encoding="utf-8"))
    assert workflow["active"] is False
    prep = next(node for node in workflow["nodes"] if node["name"] == "ASR — Prep Request")
    source = prep["parameters"]["jsCode"]
    assert "prompt_key" in source
    assert "row.course_id" in source
    assert "initial_prompt" in source
    blob = json.dumps(workflow, ensure_ascii=False)
    assert "psy_03_transcribe_v3" in blob


def test_prompt_template_has_no_terms():
    payload = json.loads(EXAMPLE_PROMPTS.read_text(encoding="utf-8"))
    courses = payload["courses"]
    assert isinstance(courses, dict) and courses
    for value in courses.values():
        assert value["initial_prompt"] == ""
        assert value["hotwords"] == ""


def _tone_gap_tone(path: Path) -> None:
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
            "sine=frequency=440:sample_rate=16000:duration=2",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=mono:sample_rate=16000:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000:duration=2",
            "-filter_complex",
            "[0:a][1:a][2:a]concat=n=3:v=0:a=1",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(path),
        ],
        check=True,
    )


def test_decode_window_is_the_slice_not_the_whole_file(data_dir):
    from app import decode_window, detect_silences, load_settings

    wav = data_dir / "gap.wav"
    _tone_gap_tone(wav)
    settings = load_settings({})
    regions = detect_silences(wav, settings, 6.0)
    assert any(2.0 <= start <= 3.2 and 3.5 <= stop <= 4.6 for start, stop in regions)
    quiet = decode_window(wav, 2.0, 4.0, 30)
    loud = decode_window(wav, 4.0, 6.0, 30)
    assert len(quiet) < 16000 * 3
    assert len(loud) < 16000 * 3
    assert float(abs(quiet).mean()) < 0.02
    assert float(abs(loud).mean()) > 0.05


def test_transcribe_rejects_over_max_without_full_decode(client, data_dir, monkeypatch):
    called = {"model": 0, "full": 0}

    def _full(*_args, **_kwargs):
        called["full"] += 1
        raise AssertionError("full-file decode")

    def _load(*_args, **_kwargs):
        called["model"] += 1
        raise AssertionError("model loaded")

    monkeypatch.setattr("app.decode_for_model", _full)
    monkeypatch.setattr("app.load_model", _load)
    monkeypatch.setenv("WHISPER_MAX_AUDIO_SEC", "1")
    wav = data_dir / "long.wav"
    synth_tone(wav, 2.0)
    response = client.post("/transcribe", json={"audio_path": str(wav), "model": "tiny"})
    assert response.status_code == 400
    assert response.json()["error"] == "audio is longer than WHISPER_MAX_AUDIO_SEC"
    assert called == {"model": 0, "full": 0}


def test_transcribe_keeps_arrays_inside_one_chunk(client, data_dir, monkeypatch):
    wav = data_dir / "eight.wav"
    synth_tone(wav, 8.0)
    seen: list[int] = []

    class _Segment:
        def __init__(self, start, end, text):
            self.start = start
            self.end = end
            self.text = text

    class _Info:
        def __init__(self, duration):
            self.duration = duration

    class _Model:
        def __init__(self):
            self.n = 0

        def transcribe(self, audio, **_kwargs):
            seen.append(int(audio.shape[0]))
            self.n += 1
            dur = float(audio.shape[0]) / 16000.0
            mid = dur / 2
            segment = _Segment(max(0.0, mid - 0.1), min(dur, mid + 0.1), f"кусок {self.n}")
            return [segment], _Info(dur)

    def _full_decode(*_args, **_kwargs):
        raise AssertionError("full-file decode")

    monkeypatch.setattr("app.decode_for_model", _full_decode)
    monkeypatch.setattr("app.detect_silences", lambda *_a, **_k: [])
    monkeypatch.setattr("app.load_model", lambda *_a, **_k: (_Model(), 0.0))
    monkeypatch.setenv("WHISPER_CHUNK_SEC", "2")
    monkeypatch.setenv("WHISPER_CHUNK_OVERLAP_SEC", "0.5")
    monkeypatch.setenv("WHISPER_SILENCE_SEARCH_SEC", "0.4")

    response = client.post(
        "/transcribe",
        json={"audio_path": str(wav), "model": "tiny", "vad_filter": False, "beam_size": 1},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(seen) >= 3
    assert max(seen) < 8 * 16000 * 0.6
    texts = [segment["text"] for segment in body["segments"]]
    assert texts == [f"кусок {i}" for i in range(1, len(texts) + 1)]
    starts = [segment["start_ms"] for segment in body["segments"]]
    assert starts == sorted(starts)
    assert starts[0] < 2000
    assert body["segments"][-1]["end_ms"] > 6000
    assert body["hallucination_filter"]["dropped_count"] == 0
    assert len(body["audio_pieces"]) == len(seen)
    assert {segment["ordinal"] for segment in body["segments"]} == set(range(1, len(texts) + 1))


def test_transcribe_reports_dropped_hallucinations(client, data_dir, monkeypatch):
    wav = data_dir / "pause.wav"
    synth_tone(wav, 1.0)

    class _Segment:
        def __init__(self, start, end, text):
            self.start = start
            self.end = end
            self.text = text

    class _Info:
        duration = 1.0

    class _Model:
        def transcribe(self, _audio, **kwargs):
            assert kwargs["condition_on_previous_text"] is False
            assert "hallucination_silence_threshold" not in kwargs
            return [
                _Segment(0.0, 0.4, "Лекция началась"),
                _Segment(0.4, 0.6, "Продолжение следует..."),
                _Segment(0.6, 0.8, "Продолжение следует..."),
                _Segment(0.8, 1.0, "Продолжение следует..."),
            ], _Info()

    monkeypatch.setattr("app.load_model", lambda *_a, **_k: (_Model(), 0.0))
    monkeypatch.setattr("app.detect_silences", lambda *_a, **_k: [])
    response = client.post(
        "/transcribe",
        json={"audio_path": str(wav), "model": "tiny", "vad_filter": True},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "Лекция началась"
    assert "Продолжение следует" not in body["text"]
    assert [segment["text"] for segment in body["segments"]] == ["Лекция началась"]
    dropped = body["hallucination_filter"]["dropped"]
    assert body["hallucination_filter"]["dropped_count"] == 3
    assert len(dropped) == 3
    assert {item["reason"] for item in dropped} == {"blocklist"}


def test_prompt_key_loads_course_file_not_the_other_course(client, data_dir, monkeypatch, tmp_path):
    prompts = tmp_path / "courses.json"
    prompts.write_text(
        json.dumps(
            {
                "courses": {
                    "course-a": {"initial_prompt": "альфа, бета", "hotwords": "гамма"},
                    "course-b": {"initial_prompt": "дельта", "hotwords": ""},
                    "PASTE_COURSE_UUID": {"initial_prompt": "", "hotwords": ""},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("WHISPER_PROMPTS_FILE", str(prompts))
    wav = data_dir / "clip.wav"
    synth_tone(wav, 0.4)
    seen = {}

    class _Segment:
        start = 0.0
        end = 0.2
        text = "речь"

    class _Info:
        duration = 0.4

    class _Model:
        def transcribe(self, _audio, **kwargs):
            seen.update(kwargs)
            return [_Segment()], _Info()

    monkeypatch.setattr("app.load_model", lambda *_a, **_k: (_Model(), 0.0))
    monkeypatch.setattr("app.detect_silences", lambda *_a, **_k: [])
    response = client.post(
        "/transcribe",
        json={
            "audio_path": str(wav),
            "model": "tiny",
            "prompt_key": "course-a",
            "initial_prompt": "бета, эпсилон",
        },
    )
    assert response.status_code == 200, response.text
    assert seen["initial_prompt"] == "альфа, бета, эпсилон"
    assert seen["hotwords"] == "гамма"
    assert "дельта" not in seen["initial_prompt"]
    health = client.get("/health").json()
    assert health["prompts_loaded"] is True

    seen.clear()
    missing = client.post(
        "/transcribe",
        json={"audio_path": str(wav), "model": "tiny", "prompt_key": "course-missing", "initial_prompt": "только запрос"},
    )
    assert missing.status_code == 200, missing.text
    assert seen["initial_prompt"] == "только запрос"
    assert "hotwords" not in seen


def test_silence_threshold_enables_word_timestamps(client, data_dir, monkeypatch):
    wav = data_dir / "clip.wav"
    synth_tone(wav, 0.4)
    seen = {}

    class _Segment:
        start = 0.0
        end = 0.2
        text = "речь"

    class _Info:
        duration = 0.4

    class _Model:
        def transcribe(self, _audio, **kwargs):
            seen.update(kwargs)
            return [_Segment()], _Info()

    monkeypatch.setenv("WHISPER_HALLUCINATION_SILENCE_THRESHOLD", "2")
    monkeypatch.setenv("WHISPER_CONDITION_ON_PREVIOUS_TEXT", "1")
    monkeypatch.setattr("app.load_model", lambda *_a, **_k: (_Model(), 0.0))
    monkeypatch.setattr("app.detect_silences", lambda *_a, **_k: [])
    response = client.post("/transcribe", json={"audio_path": str(wav), "model": "tiny"})
    assert response.status_code == 200, response.text
    assert seen["condition_on_previous_text"] is True
    assert seen["word_timestamps"] is True
    assert seen["hallucination_silence_threshold"] == 2.0
    meta = response.json()["hallucination_filter"]
    assert meta["silence_threshold_sec"] == 2.0
    assert meta["condition_on_previous_text"] is True


def test_defaults_keep_chunks_and_skip_word_timestamps():
    from app import load_settings

    settings = load_settings({})
    assert settings.chunk_sec == 600
    assert settings.chunk_overlap_sec == 20
    assert settings.condition_on_previous_text is False
    assert settings.hallucination_silence_sec is None
    assert "продолжение следует" in settings.hallucination_phrases
    assert settings.prompts_file is None
    custom = load_settings(
        {
            "WHISPER_HALLUCINATION_USE_DEFAULTS": "0",
            "WHISPER_HALLUCINATION_PHRASES": "тестовая вставка",
        }
    )
    assert custom.hallucination_phrases == ("тестовая вставка",)
