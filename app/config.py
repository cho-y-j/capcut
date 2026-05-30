"""경로·기본 설정. 환경변수로 오버라이드 가능."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = BASE_DIR / "bin"
CACHE_DIR = Path(os.environ.get("CACHE_DIR", BASE_DIR / "cache"))
ASR_CACHE_DIR = CACHE_DIR / "asr"
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", BASE_DIR / "out"))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", BASE_DIR / "uploads"))


def _resolve_bin(name: str) -> str:
    """bin/ 의 static 빌드 우선, 없으면 PATH 의 시스템 바이너리."""
    local = BIN_DIR / name
    if local.exists():
        return str(local)
    found = shutil.which(name)
    if found:
        return found
    # 마지막 폴백 — 존재하지 않아도 경로 반환 (호출 시 에러로 드러남)
    return str(local)


FFMPEG = _resolve_bin("ffmpeg")
FFPROBE = _resolve_bin("ffprobe")

# --- 무음 감지 기본값 ---
SILENCE_DB = float(os.environ.get("SILENCE_DB", -30.0))     # dB, 이 이하를 무음으로
SILENCE_MIN = float(os.environ.get("SILENCE_MIN", 0.4))      # s, 이 이상 지속해야 무음
KEEP_PAD = float(os.environ.get("KEEP_PAD", 0.06))           # s, 말 구간 앞뒤 여유
MIN_KEEP = float(os.environ.get("MIN_KEEP", 0.20))           # s, 이보다 짧은 보존구간 버림

# --- 렌더 기본값 ---
CROSSFADE = float(os.environ.get("CROSSFADE", 0.012))        # s, 컷 지점 오디오 페이드(팝 제거)
TARGET_LUFS = float(os.environ.get("TARGET_LUFS", -14.0))    # 루드니스 정규화 목표
CRF = os.environ.get("CRF", "18")                            # x264 품질(낮을수록 고화질)
PRESET = os.environ.get("PRESET", "veryfast")

# --- ASR / TTS ---
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")
TTS_VOICE = os.environ.get("TTS_VOICE", "ko-KR-SunHiNeural")


def ensure_dirs() -> None:
    for d in (CACHE_DIR, ASR_CACHE_DIR, OUTPUT_DIR, UPLOAD_DIR):
        d.mkdir(parents=True, exist_ok=True)
