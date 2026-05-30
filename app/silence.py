"""무음 감지 → 보존(말) 구간 산출.

ffmpeg `silencedetect` 필터로 무음 구간을 찾고, 그 여집합을 "보존 구간"으로 만든다.
보존 구간 앞뒤에 약간의 패딩을 줘서 말 끝이 잘리지 않게 한다.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import List, Tuple

from . import config


@dataclass
class Segment:
    start: float          # 초
    end: float

    @property
    def dur(self) -> float:
        return max(0.0, self.end - self.start)


def probe_duration(path: str) -> float:
    """영상/오디오 총 길이(초)."""
    out = subprocess.run(
        [config.FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def detect_silences(path: str, noise_db: float | None = None,
                    min_silence: float | None = None) -> List[Tuple[float, float]]:
    """무음 구간 [(start, end), ...] 반환."""
    noise_db = config.SILENCE_DB if noise_db is None else noise_db
    min_silence = config.SILENCE_MIN if min_silence is None else min_silence
    proc = subprocess.run(
        [config.FFMPEG, "-i", path,
         "-af", f"silencedetect=noise={noise_db}dB:d={min_silence}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    log = proc.stderr
    starts = [float(m) for m in re.findall(r"silence_start:\s*([0-9.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([0-9.]+)", log)]
    silences: List[Tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        silences.append((s, e if e is not None else float("inf")))
    return silences


def keep_segments(path: str, *, noise_db: float | None = None,
                  min_silence: float | None = None,
                  pad: float | None = None,
                  min_keep: float | None = None) -> Tuple[List[Segment], float]:
    """말(보존) 구간 리스트와 총 길이 반환.

    무음의 여집합 → 패딩 → 인접 구간 병합 → 짧은 구간 제거.
    """
    pad = config.KEEP_PAD if pad is None else pad
    min_keep = config.MIN_KEEP if min_keep is None else min_keep
    duration = probe_duration(path)
    silences = detect_silences(path, noise_db, min_silence)

    # 무음의 여집합 = 말 구간
    raw: List[Segment] = []
    cursor = 0.0
    for s, e in silences:
        s = min(s, duration)
        e = min(e, duration)
        if s > cursor:
            raw.append(Segment(cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        raw.append(Segment(cursor, duration))

    # 패딩 적용 (이웃·경계로 클램프)
    padded: List[Segment] = []
    for seg in raw:
        padded.append(Segment(max(0.0, seg.start - pad),
                              min(duration, seg.end + pad)))

    # 겹치거나 맞닿는 구간 병합
    merged: List[Segment] = []
    for seg in padded:
        if merged and seg.start <= merged[-1].end:
            merged[-1].end = max(merged[-1].end, seg.end)
        else:
            merged.append(Segment(seg.start, seg.end))

    # 너무 짧은 구간 제거
    kept = [s for s in merged if s.dur >= min_keep]
    return kept, duration
