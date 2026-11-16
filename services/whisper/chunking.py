"""Нарезка длинного аудио и фильтр галлюцинаций Whisper.

Чистые функции: без модели, без ffmpeg, без секретов. Сервис декодирует
только одно окно за раз, поэтому в модель не попадает весь файл.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Sequence


# Короткие титры и заставки, которые Whisper дописывает на тишине.
# Это не термины курса. Свои фразы добавляются переменной окружения.
DEFAULT_HALLUCINATION_PHRASES: tuple[str, ...] = (
    "продолжение следует",
    "субтитры сделал",
    "субтитры создал",
    "субтитры добавил",
    "редактор субтитров",
    "спасибо за просмотр",
    "подписывайтесь на канал",
    "dimatorzok",
)

# Кредит в кадре короткий. Длинную фразу лекции, где слова случайно встретились, не режем.
BLOCKLIST_MAX_CHARS = 180
BOUNDARY_SIMILARITY = 0.84
BOUNDARY_OVERLAP_MS = 200

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)")
_SILENCE_END_RE = re.compile(
    r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)\s*\|\s*silence_duration:\s*([0-9]+(?:\.[0-9]+)?)"
)


@dataclass(frozen=True)
class AudioPiece:
    """Окно декодирования и отрезок времени, чьи сегменты оставляем.

    start/end — что отдаём ffmpeg и модели.
    keep_from/keep_to — полуинтервал [keep_from, keep_to) абсолютного времени.
    У последнего окна верхняя граница не отсекает сегмент: хвост файла остаётся.
    cut: silence — разрез в паузе, без перекрытия; hard — разрез по времени, с перекрытием;
    end — хвост файла.
    """

    index: int
    start: float
    end: float
    keep_from: float
    keep_to: float
    cut: str


def _r(value: float) -> float:
    return round(float(value), 3)


def normalize_text(text: str) -> str:
    lowered = text.casefold().replace("ё", "е")
    cleaned = _PUNCT_RE.sub(" ", lowered)
    return " ".join(cleaned.split())


def merge_terms(*parts: str | None) -> str | None:
    """Список через запятую: раньше идущий источник важнее, повторы выкидываются."""
    seen: set[str] = set()
    terms: list[str] = []
    for part in parts:
        if not part:
            continue
        for raw in part.split(","):
            term = raw.strip()
            if not term:
                continue
            key = normalize_text(term)
            if not key or key in seen:
                continue
            seen.add(key)
            terms.append(term)
    if not terms:
        return None
    return ", ".join(terms)


def parse_silencedetect(
    stderr: str,
    *,
    offset: float = 0.0,
    end: float | None = None,
) -> list[tuple[float, float]]:
    """Разбор stderr ffmpeg silencedetect. Время смещается на offset секунд."""
    regions: list[tuple[float, float]] = []
    pending: float | None = None
    for line in stderr.splitlines():
        started = _SILENCE_START_RE.search(line)
        if started:
            pending = float(started.group(1))
            continue
        finished = _SILENCE_END_RE.search(line)
        if finished and pending is not None:
            regions.append((offset + pending, offset + float(finished.group(1))))
            pending = None
    if pending is not None and end is not None and end > offset + pending:
        regions.append((offset + pending, end))
    return regions


def _merge_silences(
    silences: Sequence[tuple[float, float]],
    duration: float,
) -> list[tuple[float, float]]:
    cleaned: list[tuple[float, float]] = []
    for start, stop in silences:
        if stop <= start:
            continue
        lo = max(0.0, start)
        hi = min(duration, stop)
        if hi - lo < 0.05:
            continue
        cleaned.append((lo, hi))
    cleaned.sort()
    merged: list[tuple[float, float]] = []
    for start, stop in cleaned:
        if merged and start <= merged[-1][1] + 0.02:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((start, stop))
    return merged


def _silence_cut(
    ownership: float,
    nominal_end: float,
    duration: float,
    silences: Sequence[tuple[float, float]],
    *,
    search_sec: float,
    lookahead: float,
    min_piece: float,
) -> float | None:
    window_lo = max(ownership + min_piece, nominal_end - search_sec)
    window_hi = min(duration, nominal_end + lookahead)
    if window_hi - window_lo < 0.2:
        return None
    best: tuple[float, float] | None = None
    for start, stop in silences:
        lo = max(start, window_lo)
        hi = min(stop, window_hi)
        if hi - lo <= 0:
            continue
        mid = (start + stop) / 2
        if mid < window_lo or mid > window_hi:
            mid = (lo + hi) / 2
        length = stop - start
        if best is None or length > best[0] + 1e-6:
            best = (length, mid)
        elif abs(length - best[0]) <= 1e-6 and abs(mid - nominal_end) < abs(best[1] - nominal_end):
            best = (length, mid)
    if best is None:
        return None
    cut = best[1]
    if cut <= ownership + min_piece or cut >= duration - 0.5:
        return None
    return cut


def plan_pieces(
    duration: float,
    silences: Sequence[tuple[float, float]] | None = None,
    *,
    chunk_sec: float,
    overlap_sec: float,
    search_sec: float,
) -> list[AudioPiece]:
    """План окон.

    Пауза рядом с границей окна — разрез в её середине, без перекрытия.
    Если паузы нет — жёсткий разрез и перекрытие, чтобы слово на стыке
    попало в оба окна и осталось в одном (по середине сегмента).
    Длина окна не больше chunk_sec + overlap_sec.
    """
    duration = _r(duration)
    if duration <= 0:
        return []
    chunk_sec = max(1.0, float(chunk_sec))
    overlap_sec = max(0.0, min(float(overlap_sec), chunk_sec / 3))
    search_sec = max(0.0, min(float(search_sec), chunk_sec * 0.75))
    lookahead = overlap_sec / 2
    min_piece = min(30.0, chunk_sec * 0.25)
    absorb = overlap_sec
    cap = chunk_sec + overlap_sec + 0.05
    silent = _merge_silences(silences or (), duration)

    pieces: list[AudioPiece] = []
    ownership = 0.0
    decode_start = 0.0
    for _ in range(10000):
        if ownership >= duration - 0.05:
            break
        remaining = duration - ownership
        window = duration - decode_start
        if remaining <= chunk_sec + absorb and window <= cap:
            pieces.append(
                AudioPiece(
                    index=len(pieces),
                    start=_r(decode_start),
                    end=duration,
                    keep_from=_r(ownership),
                    keep_to=duration,
                    cut="end",
                )
            )
            break
        nominal_end = min(duration, ownership + chunk_sec)
        cut = _silence_cut(
            ownership,
            nominal_end,
            duration,
            silent,
            search_sec=search_sec,
            lookahead=lookahead,
            min_piece=min_piece,
        )
        if cut is not None:
            boundary = _r(cut)
            if boundary > ownership + 0.05:
                pieces.append(
                    AudioPiece(
                        index=len(pieces),
                        start=_r(decode_start),
                        end=boundary,
                        keep_from=_r(ownership),
                        keep_to=boundary,
                        cut="silence",
                    )
                )
                decode_start = boundary
                ownership = boundary
                continue
        boundary = _r(nominal_end)
        half = overlap_sec / 2
        following = duration - boundary
        if following > 0:
            half = min(half, following / 2, max(0.0, boundary - decode_start) / 2)
        else:
            half = 0.0
        decode_end = min(duration, _r(boundary + half))
        if decode_end - decode_start > cap:
            decode_end = _r(decode_start + chunk_sec + overlap_sec)
        pieces.append(
            AudioPiece(
                index=len(pieces),
                start=_r(decode_start),
                end=decode_end,
                keep_from=_r(ownership),
                keep_to=boundary,
                cut="hard",
            )
        )
        next_ownership = boundary
        next_start = _r(max(0.0, boundary - half))
        if next_ownership <= ownership + 0.05:
            raise RuntimeError("chunk planner did not advance")
        ownership = next_ownership
        decode_start = next_start
    else:
        raise RuntimeError("chunk planner did not finish")
    _validate_pieces(pieces, duration, cap)
    return pieces


def _validate_pieces(pieces: list[AudioPiece], duration: float, cap: float) -> None:
    if not pieces:
        raise RuntimeError("chunk planner returned no pieces")
    if pieces[0].keep_from > 0.001 or pieces[0].start > 0.001:
        raise RuntimeError("chunk planner did not start at 0")
    if abs(pieces[-1].keep_to - duration) > 0.001 or abs(pieces[-1].end - duration) > 0.001:
        raise RuntimeError("chunk planner did not cover the file")
    previous: AudioPiece | None = None
    for piece in pieces:
        if piece.end - piece.start > cap + 0.02:
            raise RuntimeError("chunk window is too long")
        if piece.start > piece.keep_from + 0.001:
            raise RuntimeError("decode starts after ownership")
        if piece.keep_to > piece.end + 0.001:
            raise RuntimeError("ownership ends after decode")
        if piece.keep_to <= piece.keep_from:
            raise RuntimeError("empty ownership")
        if previous is not None and abs(previous.keep_to - piece.keep_from) > 0.001:
            raise RuntimeError("ownership gap")
        previous = piece


def window_ffmpeg_args(path: str, start: float, end: float) -> list[str]:
    """argv ffmpeg: точный seek коротким добором, в stdout только float32 mono 16 kHz.

    -ss до -i прыгает по ключевому кадру. Второй -ss добирает не больше секунды
    уже декодированным потоком, поэтому стык не уезжает на длину GOP.
    """
    start = max(0.0, float(start))
    end = max(start, float(end))
    duration = max(0.0, end - start)
    preroll = min(1.0, start)
    coarse = start - preroll
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if coarse > 0:
        cmd.extend(["-ss", f"{coarse:.3f}"])
    cmd.extend(["-i", path])
    if preroll > 0:
        cmd.extend(["-ss", f"{preroll:.3f}"])
    cmd.extend(
        [
            "-t",
            f"{duration:.3f}",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "pipe:1",
        ]
    )
    return cmd


def _seg_text(segment: object) -> str:
    if isinstance(segment, dict):
        return str(segment.get("text") or "")
    return str(getattr(segment, "text", "") or "")


def _seg_time(segment: object, name: str) -> float:
    if isinstance(segment, dict):
        return float(segment.get(name) or 0.0)
    return float(getattr(segment, name, 0.0) or 0.0)


def assemble_segments(
    pieces: Sequence[AudioPiece],
    groups: Sequence[Iterable[object]],
) -> tuple[list[dict[str, object]], list[int]]:
    """Склеивает сегменты окон в одну ленту с абсолютными start_ms/end_ms.

    Середина сегмента должна попасть в [keep_from, keep_to). У последнего окна
    верхняя граница не режет хвост. Возвращает (сегменты без ordinal, сколько
    выброшено на каждом окне как чужое перекрытие).
    """
    if len(pieces) != len(groups):
        raise ValueError("pieces and groups differ in length")
    kept: list[dict[str, object]] = []
    discarded: list[int] = []
    last = len(pieces) - 1
    for index, (piece, group) in enumerate(zip(pieces, groups)):
        dropped = 0
        keep_from = int(round(piece.keep_from * 1000))
        keep_to = int(round(piece.keep_to * 1000))
        for segment in group:
            text = _seg_text(segment).strip()
            if not text:
                continue
            rel_start = max(0.0, _seg_time(segment, "start"))
            rel_end = max(rel_start, _seg_time(segment, "end"))
            start_ms = int(round((piece.start + rel_start) * 1000))
            end_ms = int(round((piece.start + rel_end) * 1000))
            if end_ms < start_ms:
                end_ms = start_ms
            mid = (start_ms + end_ms) // 2
            if mid < keep_from or (index != last and mid >= keep_to):
                dropped += 1
                continue
            kept.append({"start_ms": start_ms, "end_ms": end_ms, "text": text})
        discarded.append(dropped)
    kept.sort(key=lambda item: (int(item["start_ms"]), int(item["end_ms"])))
    return kept, discarded


def _view(segment: dict[str, object], reason: str) -> dict[str, object]:
    text = str(segment["text"])
    if len(text) > 500:
        text = text[:500]
    return {
        "start_ms": int(segment["start_ms"]),
        "end_ms": int(segment["end_ms"]),
        "text": text,
        "reason": reason,
    }


def is_blocklisted(text: str, phrases: Sequence[str]) -> bool:
    norm = normalize_text(text)
    if not norm or len(norm) > BLOCKLIST_MAX_CHARS:
        return False
    return any(phrase and phrase in norm for phrase in phrases)


def _similar(left: str, right: str) -> bool:
    if left == right:
        return True
    if not left or not right:
        return False
    return SequenceMatcher(None, left, right).ratio() >= BOUNDARY_SIMILARITY


def _time_overlap_ms(left: dict[str, object], right: dict[str, object]) -> int:
    return min(int(left["end_ms"]), int(right["end_ms"])) - max(
        int(left["start_ms"]), int(right["start_ms"])
    )


def filter_hallucinations(
    segments: Sequence[dict[str, object]],
    phrases: Sequence[str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Убирает известные заставки, одинаковые подряд и дубль на стыке окон.

    Порядок важен: фраза из блоклиста снимается целиком, а не схлопывается в одну.
    """
    dropped: list[dict[str, object]] = []
    kept: list[dict[str, object]] = []
    normalized_phrases = tuple(normalize_text(phrase) for phrase in phrases if phrase.strip())

    pending: list[dict[str, object]] = []
    for segment in segments:
        if is_blocklisted(str(segment["text"]), normalized_phrases):
            dropped.append(_view(segment, "blocklist"))
        else:
            pending.append(
                {
                    "start_ms": int(segment["start_ms"]),
                    "end_ms": int(segment["end_ms"]),
                    "text": str(segment["text"]).strip(),
                }
            )

    boundary: list[dict[str, object]] = []
    for segment in pending:
        if boundary:
            previous = boundary[-1]
            overlap = _time_overlap_ms(previous, segment)
            if overlap >= BOUNDARY_OVERLAP_MS and _similar(
                normalize_text(str(previous["text"])),
                normalize_text(str(segment["text"])),
            ):
                dropped.append(_view(segment, "boundary_duplicate"))
                if int(segment["start_ms"]) - int(previous["end_ms"]) <= 5000:
                    previous["end_ms"] = max(int(previous["end_ms"]), int(segment["end_ms"]))
                continue
        boundary.append(segment)

    for segment in boundary:
        if kept and normalize_text(str(kept[-1]["text"])) == normalize_text(str(segment["text"])):
            dropped.append(_view(segment, "consecutive_duplicate"))
            if int(segment["start_ms"]) - int(kept[-1]["end_ms"]) <= 5000:
                kept[-1]["end_ms"] = max(int(kept[-1]["end_ms"]), int(segment["end_ms"]))
            continue
        kept.append(segment)
    return kept, dropped


def number_segments(segments: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    numbered: list[dict[str, object]] = []
    for ordinal, segment in enumerate(segments, start=1):
        numbered.append(
            {
                "ordinal": ordinal,
                "start_ms": int(segment["start_ms"]),
                "end_ms": int(segment["end_ms"]),
                "text": str(segment["text"]),
            }
        )
    return numbered
