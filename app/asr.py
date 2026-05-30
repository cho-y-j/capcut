"""ASR — faster-whisper (CPU int8). 전체 대본 + 세그먼트 + 단어 타임스탬프.

- numba/ctranslate2 동시 실행 불안정 → asyncio.Lock 으로 직렬화.
- 캐시 키는 파일 **내용 해시**(업로드 임시파일 mtime 은 매번 바뀜).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Optional

from . import config

_model = None
_model_name: Optional[str] = None
_lock = asyncio.Lock()


def content_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _get_model(name: str):
    global _model, _model_name
    if _model is None or _model_name != name:
        from faster_whisper import WhisperModel
        _model = WhisperModel(name, device="cpu", compute_type="int8")
        _model_name = name
    return _model


def _transcribe_sync(path: str, model_name: str, language: str) -> dict:
    config.ensure_dirs()
    key = content_hash(path)
    cache = config.ASR_CACHE_DIR / f"{key}_{model_name}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))

    model = _get_model(model_name)
    seg_iter, info = model.transcribe(
        path, language=language, word_timestamps=True,
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 400},
    )
    segments = []
    for s in seg_iter:
        words = [{"start": w.start, "end": w.end, "word": w.word}
                 for w in (s.words or [])]
        segments.append({"start": s.start, "end": s.end,
                         "text": s.text, "words": words})
    result = {
        "language": info.language,
        "duration": info.duration,
        "script": "".join(s["text"] for s in segments).strip(),
        "segments": segments,
    }
    cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


async def transcribe(path: str, model_name: str | None = None,
                     language: str = "ko") -> dict:
    """직렬화된 비동기 전사. {script, segments[{start,end,text,words}], ...}."""
    model_name = model_name or config.WHISPER_MODEL
    async with _lock:
        return await asyncio.to_thread(_transcribe_sync, path, model_name, language)
