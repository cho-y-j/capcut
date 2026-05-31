"""ffmpeg 점프컷 MP4 추출 (공유 코어) — 진행률·BGM 지원.

보존 구간만 이어붙여 MP4 생성. 컷 지점 오디오 페이드(팝 제거), 루드니스 정규화,
배경음악 믹스(amix), 진행률 콜백(-progress 파싱)을 지원한다.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from . import config
from .silence import Segment

ProgressCB = Optional[Callable[[float], None]]   # 0.0~1.0


def _run_with_progress(cmd: List[str], total_sec: float, cb: ProgressCB) -> None:
    """ffmpeg 실행 + -progress 파싱. 실패 시 stderr 포함 예외."""
    full = cmd[:1] + ["-progress", "pipe:1", "-nostats"] + cmd[1:]
    proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        if cb and total_sec > 0 and line.startswith("out_time_us="):
            try:
                us = int(line.strip().split("=", 1)[1])
                cb(min(1.0, us / 1e6 / total_sec))
            except ValueError:
                pass
    err = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg 실패:\n{err[-1800:]}")
    if cb:
        cb(1.0)


def _build_filtergraph(segments: Sequence[Segment], crossfade: float,
                       normalize: bool) -> str:
    parts: List[str] = []
    for i, seg in enumerate(segments):
        s, e, dur = seg.start, seg.end, seg.dur
        cf = min(crossfade, dur / 2) if dur > 0 else 0.0
        parts.append(f"[0:v]trim=start={s:.4f}:end={e:.4f},setpts=PTS-STARTPTS[v{i}];")
        afade = ""
        if cf > 0:
            afade = (f",afade=t=in:st=0:d={cf:.4f}"
                     f",afade=t=out:st={max(0.0, dur-cf):.4f}:d={cf:.4f}")
        parts.append(f"[0:a]atrim=start={s:.4f}:end={e:.4f},asetpts=PTS-STARTPTS{afade}[a{i}];")
    concat_in = "".join(f"[v{i}][a{i}]" for i in range(len(segments)))
    parts.append(f"{concat_in}concat=n={len(segments)}:v=1:a=1[vout][acat];")
    if normalize:
        parts.append("[acat]loudnorm=I=%.1f:TP=-1.5:LRA=11[spk]" % config.TARGET_LUFS)
    else:
        parts.append("[acat]anull[spk]")
    return "".join(parts)


def render_jumpcut(input_path: str, segments: Sequence[Segment], output_path: str,
                   *, crossfade: float | None = None, normalize: bool = False,
                   bgm: str | None = None, bgm_volume: float = 0.16,
                   scale_h: int | None = None, preset: str | None = None,
                   crf: str | None = None, progress: ProgressCB = None) -> str:
    """보존 구간만 이어붙인 MP4 생성. output_path 반환.

    scale_h 를 주면 세로 해상도를 그 값으로 다운스케일(저화질 프리뷰 프록시용).
    preset/crf 로 인코딩 속도·품질을 오버라이드(프리뷰는 ultrafast/30 권장).
    """
    if not segments:
        raise ValueError("보존 구간이 없습니다 (전부 무음으로 감지됨).")
    crossfade = config.CROSSFADE if crossfade is None else crossfade
    preset = config.PRESET if preset is None else preset
    crf = config.CRF if crf is None else crf
    total = total_kept(segments)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    graph = _build_filtergraph(segments, crossfade, normalize)

    vmap = "[vout]"
    if scale_h:
        graph += f";[vout]scale=-2:{int(scale_h)}:flags=fast_bilinear[vsc]"
        vmap = "[vsc]"

    if bgm:
        graph += (f";[1:a]volume={bgm_volume},atrim=0:{total:.3f},"
                  f"asetpts=PTS-STARTPTS[bg];[spk][bg]"
                  f"amix=inputs=2:duration=first:dropout_transition=0[aout]")
        audio_label = "[aout]"
    else:
        graph += ";[spk]anull[aout]"
        audio_label = "[aout]"

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(graph)
        graph_path = f.name

    cmd = [config.FFMPEG, "-y", "-i", input_path]
    if bgm:
        cmd += ["-stream_loop", "-1", "-i", bgm]
    cmd += ["-filter_complex_script", graph_path,
            "-map", vmap, "-map", audio_label,
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", output_path]
    try:
        _run_with_progress(cmd, total, progress)
    finally:
        Path(graph_path).unlink(missing_ok=True)
    return output_path


def total_kept(segments: Sequence[Segment]) -> float:
    return sum(s.dur for s in segments)


def burn_subtitles(video: str, ass_path: str, out_path: str,
                   *, total_sec: float = 0.0, progress: ProgressCB = None) -> str:
    """ass 자막을 영상에 번인."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [config.FFMPEG, "-y", "-i", video,
           "-vf", f"subtitles='{ass_path}'",
           "-c:v", "libx264", "-preset", config.PRESET, "-crf", str(config.CRF),
           "-pix_fmt", "yuv420p", "-c:a", "copy",
           "-movflags", "+faststart", out_path]
    _run_with_progress(cmd, total_sec, progress)
    return out_path
