"""성우(TTS) — edge-tts (무료·무키·한국어 뉴럴). 엔진 추상화.

synth() 는 음성 파일 + 단어 경계 타임스탬프를 반환한다. 단어 경계로 자막을
TTS 발화에 정확히 맞춘다. 나중에 다른 엔진(ElevenLabs 등)으로 교체 가능.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import List, Tuple

from . import config


@dataclass
class WordTS:
    start: float
    end: float
    text: str


async def _synth_edge(text: str, voice: str, out_path: str,
                      rate: str, pitch: str) -> List[WordTS]:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
    words: List[WordTS] = []
    with open(out_path, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7          # 100ns → s
                dur = chunk["duration"] / 1e7
                words.append(WordTS(start, start + dur, chunk["text"]))
    return words


def _probe_audio_dur(path: str) -> float:
    import subprocess
    o = subprocess.run(
        [config.FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True)
    try:
        return float(o.stdout.strip())
    except ValueError:
        return 0.0


async def synth(text: str, out_path: str, *, voice: str | None = None,
                rate: str = "+0%", pitch: str = "+0Hz") -> Tuple[float, List[WordTS]]:
    """텍스트 → 음성(mp3) + 단어 타임스탬프. (총길이초, [WordTS]) 반환.

    길이는 실제 오디오 파일에서 측정(워드바운더리 미제공 보이스 대비)."""
    voice = voice or config.TTS_VOICE
    if not (text or "").strip():        # 빈 자막 → 짧은 무음(장면은 유지). 파이프라인 크래시 방지
        import asyncio, subprocess
        await asyncio.to_thread(subprocess.run,
            [config.FFMPEG, "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
             "-t", "1.5", "-q:a", "9", out_path], capture_output=True)
        return 1.5, []
    words = await _synth_edge(text, voice, out_path, rate, pitch)
    duration = _probe_audio_dur(out_path)
    if duration <= 0 and words:
        duration = words[-1].end
    if duration <= 0:
        raise RuntimeError(f"TTS 합성 실패(길이 0): voice={voice}")
    return duration, words


async def list_korean_voices() -> List[dict]:
    import edge_tts
    voices = await edge_tts.list_voices()
    return [{"name": v["ShortName"], "gender": v["Gender"]}
            for v in voices if v["Locale"].startswith("ko-")]
