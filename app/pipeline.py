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
from .silence import (Segment, classify_silence_cuts, detect_silences,
                      keep_segments, probe_video)

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
    silences = await asyncio.to_thread(detect_silences, input_path)
    try:
        vw, vh, vfps = await asyncio.to_thread(probe_video, input_path)
    except Exception:  # noqa: BLE001
        vw, vh, vfps = 1280, 720, 30.0
    await _pause()
    _emit(progress, "silence", "done", f"{len(segs)}개 보존구간")

    _emit(progress, "asr", "run", "대본 추출 중 (whisper)")
    tr = await asr.transcribe(input_path, model)
    await _pause()
    _emit(progress, "asr", "done", f"{len(tr['segments'])}개 세그먼트")

    _emit(progress, "filler", "run", "무음·잔말·반복 탐지 중")
    # 무음 컷(의도적 쉼은 보존) + 잔말/반복 컷을 합쳐 자동정리 후보로.
    sil_cuts = classify_silence_cuts(silences, tr["segments"], duration)
    fil_cuts = await asyncio.to_thread(filler.suggest_cuts, tr["segments"])
    cuts = filler.merge_cuts(sil_cuts + fil_cuts)
    await _pause()
    _emit(progress, "filler", "done",
          f"무음 {len(sil_cuts)} + 잔말 {len(fil_cuts)} → 컷 {len(cuts)}")

    _emit(progress, "draft", "run", "타임라인 구성 중")
    cues = subtitle.build_cues(tr["segments"])
    await _pause()
    _emit(progress, "draft", "done", "완료")
    return {
        "mode": "a",
        "duration": duration,
        "w": vw, "h": vh, "fps": vfps,
        "keep": [{"start": s.start, "end": s.end} for s in segs],
        "cuts": cuts,
        "script": tr["script"],
        "segments": tr["segments"],
        "cues": [{"start": c.start, "end": c.end, "text": c.text} for c in cues],
    }


def _as_clips(items) -> List[dict]:
    """[(s,e), ...] 또는 [{srcIn,srcEnd,transition?}, ...] → 클립 dict 리스트."""
    clips: List[dict] = []
    for it in items or []:
        if isinstance(it, dict):
            clips.append(it)
        else:
            a, b = it
            clips.append({"srcIn": float(a), "srcEnd": float(b)})
    return clips


def export_project(input_path: str, clips, out_path: str, *, subtitles: bool = True,
                   cues: Sequence[dict] | None = None, style: dict | None = None,
                   bgm: str | None = None, bgm_opts: dict | None = None,
                   overlays: Sequence[dict] | None = None,
                   sfx: Sequence[dict] | None = None,
                   texts: Sequence[dict] | None = None,
                   model: str | None = None, normalize: bool = True,
                   progress: Optional[Callable[[float], None]] = None) -> str:
    """클립 타임라인 → MP4 (+ 자막 + 텍스트박스 + 배경음악 + 오버레이 + 효과음).

    cues 시간은 **원본 타임라인** 기준 → 클립 레이아웃(재정렬·트랜지션)으로 remap.
    texts(자유 텍스트박스)·overlays(로고)·sfx(효과음)는 출력 타임라인 기준.
    """
    clips = _as_clips(clips)
    if not clips:
        raise ValueError("클립(보존 구간)이 비었습니다.")
    texts = list(texts or [])
    bo = bgm_opts or {}
    vol = float(bo.get("volume", 0.16))
    fin, fout = float(bo.get("fadeIn", 0.0)), float(bo.get("fadeOut", 0.0))
    layout, total = render.clip_layout(clips)
    has_ov = bool(overlays) or bool(sfx)
    need_burn = subtitles or bool(texts)
    base = out_path if not has_ov else str(Path(out_path).with_suffix(".noov.mp4"))
    span = 0.85 if has_ov else 1.0

    def _scale(lo, hi):
        return (lambda p: progress(lo + p * (hi - lo))) if progress else None

    def _render(dst, prog):
        return render.render_timeline(input_path, clips, dst, normalize=normalize,
                                      bgm=bgm, bgm_volume=vol, bgm_fade_in=fin,
                                      bgm_fade_out=fout, progress=prog)

    if not need_burn:
        _render(base, _scale(0.0, span))
    else:
        tmp = str(Path(out_path).with_suffix(".tmp.mp4"))
        _render(tmp, _scale(0.0, span * 0.6))
        cue_objs: list = []
        if subtitles:
            if cues is not None:
                cue_objs = [subtitle.Cue(float(c["start"]), float(c["end"]), c["text"])
                            for c in cues if c.get("text", "").strip()]
            else:
                tr = asr._transcribe_sync(input_path, model or config.WHISPER_MODEL, "ko")
                cue_objs = subtitle.build_cues(tr["segments"])
            cue_objs = subtitle.remap_cues_clips(cue_objs, layout)
        w, h, _ = probe_video(input_path)
        ass = str(Path(out_path).with_suffix(".ass"))
        subtitle.write_ass(cue_objs, ass, play_w=w, play_h=h, texts=texts, total=total,
                           **subtitle.style_to_kwargs(style))
        render.burn_subtitles(tmp, ass, base, total_sec=total,
                              progress=_scale(span * 0.6, span))
        Path(tmp).unlink(missing_ok=True)

    if has_ov:
        render.composite(base, out_path, overlays=overlays, sfx=sfx,
                         progress=_scale(0.85, 1.0))
        Path(base).unlink(missing_ok=True)
    return out_path


def export_mode_a(input_path: str, kept_ranges: Sequence[Tuple[float, float]],
                  out_path: str, *, subtitles: bool = True,
                  cues: Sequence[dict] | None = None, bgm: str | None = None,
                  model: str | None = None, normalize: bool = True,
                  progress: Optional[Callable[[float], None]] = None) -> str:
    """하위호환 래퍼 — 연속 보존구간(트랜지션 없음)을 export_project로 위임."""
    return export_project(input_path, kept_ranges, out_path, subtitles=subtitles,
                          cues=cues, style=None, bgm=bgm, model=model,
                          normalize=normalize, progress=progress)


def preview_mode_a(input_path: str, clips, out_path: str, *, bgm: str | None = None,
                   bgm_opts: dict | None = None, overlays: Sequence[dict] | None = None,
                   sfx: Sequence[dict] | None = None,
                   progress: Optional[Callable[[float], None]] = None) -> str:
    """클립 타임라인 저화질·고속 프록시 → 컷·트랜지션·오디오·오버레이·효과음 미리보기.

    자막 번인은 생략(속도). 480p·ultrafast·crf30.
    """
    clips = _as_clips(clips)
    if not clips:
        raise ValueError("클립(보존 구간)이 비었습니다.")
    bo = bgm_opts or {}
    has_ov = bool(overlays) or bool(sfx)
    base = out_path if not has_ov else str(Path(out_path).with_suffix(".noov.mp4"))
    render.render_timeline(input_path, clips, base, normalize=True, bgm=bgm,
                           bgm_volume=float(bo.get("volume", 0.16)),
                           bgm_fade_in=float(bo.get("fadeIn", 0.0)),
                           bgm_fade_out=float(bo.get("fadeOut", 0.0)),
                           scale_h=480, preset="ultrafast", crf="30",
                           progress=(lambda p: progress(p * (0.8 if has_ov else 1.0))) if progress else None)
    if has_ov:
        render.composite(base, out_path, overlays=overlays, sfx=sfx,
                         preset="ultrafast", crf="30",
                         progress=(lambda p: progress(0.8 + p * 0.2)) if progress else None)
        Path(base).unlink(missing_ok=True)
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

    _emit(progress, "draft", "run", "성우+영상 합성 중")
    audio = str(tmpdir / "voice.m4a")
    await asyncio.to_thread(_concat_audio, audio_paths, audio)
    # 자막은 번인하지 않는다 — 편집기에서 스타일·수정 가능하도록 큐로만 넘긴다.
    await asyncio.to_thread(slideshow.compose, silent, audio, out_path, ass=None)
    cues = subtitle.build_cues(seg_for_cues)
    await _pause()
    _emit(progress, "draft", "done", "편집기 준비 완료")
    # 모드 A와 동일한 편집기로 들어가도록 클립(씬 경계)·큐·해상도를 함께 반환.
    return {
        "mode": "b",
        "duration": t,
        "w": w, "h": h, "fps": fps,
        "path": out_path,                          # 편집 가능한 소스(자막 미번인)
        "clips": [{"srcIn": seg["start"], "srcEnd": seg["end"]} for seg in seg_for_cues],
        "cuts": [],
        "script": "\n".join(sc.text for sc in scenes),
        "segments": seg_for_cues,
        "cues": [{"start": c.start, "end": c.end, "text": c.text} for c in cues],
    }
