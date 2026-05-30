"""오케스트레이션 — 모드 A(토킹 편집) / 모드 B(이미지→내레이션). CLI·웹 공용.

진행상황은 progress 콜백(step, status, detail)으로 흘려보낸다 → 웹에서 SSE.
"""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import asr, config, filler, render, slideshow, subtitle, tts
from .silence import Segment, keep_segments

Progress = Optional[Callable[[str, str, str], None]]
MIN_STEP = 0.5   # 단계당 최소 지연(애니메이션 가시화)


def _emit(cb: Progress, step: str, status: str, detail: str = "") -> None:
    if cb:
        cb(step, status, detail)


async def _pause():
    await asyncio.sleep(MIN_STEP)


# ---------------- 모드 A: 토킹 영상 편집 ----------------
async def process_mode_a(input_path: str, *, model: str | None = None,
                         progress: Progress = None) -> dict:
    _emit(progress, "silence", "run", "무음 감지 중")
    segs, duration = await asyncio.to_thread(keep_segments, input_path)
    await _pause()
    _emit(progress, "silence", "done", f"{len(segs)}개 보존구간")

    _emit(progress, "asr", "run", "대본 추출 중 (whisper)")
    tr = await asr.transcribe(input_path, model)
    await _pause()
    _emit(progress, "asr", "done", f"{len(tr['segments'])}개 세그먼트")

    _emit(progress, "filler", "run", "잔말/반복 탐지 중")
    cuts = await asyncio.to_thread(filler.suggest_cuts, tr["segments"])
    await _pause()
    _emit(progress, "filler", "done", f"{len(cuts)}개 컷 후보")

    _emit(progress, "draft", "run", "타임라인 구성 중")
    cues = subtitle.build_cues(tr["segments"])
    await _pause()
    _emit(progress, "draft", "done", "완료")
    return {
        "mode": "a",
        "duration": duration,
        "keep": [{"start": s.start, "end": s.end} for s in segs],
        "cuts": cuts,
        "script": tr["script"],
        "segments": tr["segments"],
        "cues": [{"start": c.start, "end": c.end, "text": c.text} for c in cues],
    }


def export_mode_a(input_path: str, kept_ranges: Sequence[Tuple[float, float]],
                  out_path: str, *, subtitles: bool = True,
                  model: str | None = None, normalize: bool = True) -> str:
    """사용자 확정 보존구간으로 점프컷 추출 (+ 자막 번인)."""
    segs = [Segment(float(a), float(b)) for a, b in kept_ranges if b > a]
    if not segs:
        raise ValueError("보존 구간이 비었습니다.")
    if not subtitles:
        return render.render_jumpcut(input_path, segs, out_path, normalize=normalize)

    tmp = str(Path(out_path).with_suffix(".tmp.mp4"))
    render.render_jumpcut(input_path, segs, tmp, normalize=normalize)
    tr = asr._transcribe_sync(input_path, model or config.WHISPER_MODEL, "ko")
    cues = subtitle.build_cues(tr["segments"])
    cues = subtitle.remap_cues(cues, segs)
    ass = str(Path(out_path).with_suffix(".ass"))
    subtitle.write_ass(cues, ass)
    render.burn_subtitles(tmp, ass, out_path)
    Path(tmp).unlink(missing_ok=True)
    return out_path


# ---------------- 모드 B: 이미지 → 내레이션 영상 ----------------
@dataclass
class Scene:
    text: str
    image: str


def _concat_audio(paths: Sequence[str], out_path: str) -> None:
    inputs = []
    for p in paths:
        inputs += ["-i", p]
    n = len(paths)
    fc = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"
    cmd = [config.FFMPEG, "-y", *inputs, "-filter_complex", fc,
           "-map", "[a]", "-c:a", "aac", "-b:a", "192k", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"오디오 concat 실패:\n{proc.stderr[-1500:]}")


async def process_mode_b(scenes: List[Scene], out_path: str, *,
                         voice: str | None = None, w: int = 1920, h: int = 1080,
                         fps: int = 30, progress: Progress = None) -> dict:
    tmpdir = Path(tempfile.mkdtemp(prefix="modeb_"))
    _emit(progress, "tts", "run", f"성우 합성 중 ({len(scenes)}장면)")
    durations: List[float] = []
    audio_paths: List[str] = []
    seg_for_cues: List[dict] = []
    t = 0.0
    for i, sc in enumerate(scenes):
        ap = str(tmpdir / f"voice_{i:03d}.mp3")
        dur, _words = await tts.synth(sc.text, ap, voice=voice)
        durations.append(dur)
        audio_paths.append(ap)
        seg_for_cues.append({"start": t, "end": t + dur, "text": sc.text})
        t += dur
    await _pause()
    _emit(progress, "tts", "done", f"총 {t:.1f}s")

    _emit(progress, "slideshow", "run", "켄번스 슬라이드쇼 생성 중")
    silent = str(tmpdir / "silent.mp4")
    await asyncio.to_thread(slideshow.build_video,
                            [s.image for s in scenes], durations, silent,
                            w=w, h=h, fps=fps)
    await _pause()
    _emit(progress, "slideshow", "done", "")

    _emit(progress, "draft", "run", "성우+자막 합성 중")
    audio = str(tmpdir / "voice.m4a")
    await asyncio.to_thread(_concat_audio, audio_paths, audio)
    cues = subtitle.build_cues(seg_for_cues)
    ass = str(Path(out_path).with_suffix(".ass"))
    subtitle.write_ass(cues, ass, play_w=w, play_h=h)
    await asyncio.to_thread(slideshow.compose, silent, audio, out_path, ass=ass)
    await _pause()
    _emit(progress, "draft", "done", out_path)
    return {"mode": "b", "duration": t, "output": out_path}
