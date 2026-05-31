"""오디오 파형(peaks) 추출 — 타임라인에 말/무음을 눈으로 보여주기 위함."""
from __future__ import annotations

import array
import subprocess
from typing import List

from . import config


def peaks(path: str, buckets: int = 900) -> List[float]:
    """모노 PCM을 buckets개 구간의 최대 진폭(0~1)으로 다운샘플."""
    proc = subprocess.run(
        [config.FFMPEG, "-v", "error", "-i", path, "-ac", "1", "-ar", "8000",
         "-f", "s16le", "-"],
        capture_output=True,
    )
    raw = proc.stdout
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    n = len(samples)
    if n == 0:
        return []
    size = max(1, n // buckets)
    out: List[float] = []
    for i in range(0, n, size):
        chunk = samples[i:i + size]
        m = 0
        for x in chunk:
            ax = x if x >= 0 else -x
            if ax > m:
                m = ax
        out.append(round(m / 32768, 3))
    return out
