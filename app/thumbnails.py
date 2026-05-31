"""타임라인 썸네일 필름스트립 — 소스에서 균등 N프레임을 가로 스프라이트 1장으로.

프론트는 이 스프라이트를 비디오 레인 배경으로 깔아 "한눈에" 내용을 보여준다.
waveform.py 와 같은 패턴(ffmpeg 서브프로세스 + 디스크 캐시).
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from . import config
from .silence import probe_duration

THUMB_W = 160          # 썸네일 한 칸 가로(px); 세로는 원본 비율 유지
THUMB_DIR = config.CACHE_DIR / "thumbs"


def _cache_path(src: str, n: int) -> Path:
    key = hashlib.sha1(f"{Path(src).resolve()}::{n}".encode()).hexdigest()[:16]
    return THUMB_DIR / f"{key}.jpg"


def sprite(src: str, n: int = 120) -> str:
    """소스에서 균등 n프레임 → 가로 1행 스프라이트 JPEG. 캐시된 경로 반환."""
    n = max(8, min(400, int(n)))
    out = _cache_path(src, n)
    if out.exists():
        return str(out)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    dur = max(0.1, probe_duration(src))
    fps = n / dur                       # 초당 추출 프레임 수 → 총 ~n장
    vf = f"fps={fps:.6f},scale={THUMB_W}:-2,tile={n}x1"
    cmd = [config.FFMPEG, "-y", "-i", src, "-vf", vf,
           "-frames:v", "1", "-q:v", "4", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"썸네일 생성 실패:\n{proc.stderr[-1200:]}")
    return str(out)
