"""ffmpeg 점프컷 MP4 추출 (공유 코어).

보존 구간만 이어붙여 MP4 생성. 컷 지점마다 짧은 오디오 페이드로 클릭/팝 제거하고,
옵션으로 루드니스 정규화(-14 LUFS)를 적용한다. 프레임 정확 컷(trim+setpts).
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import List, Sequence

from . import config
from .silence import Segment


def _build_filtergraph(segments: Sequence[Segment], crossfade: float,
                       normalize: bool) -> str:
    parts: List[str] = []
    for i, seg in enumerate(segments):
        s, e, dur = seg.start, seg.end, seg.dur
        cf = min(crossfade, dur / 2) if dur > 0 else 0.0
        parts.append(
            f"[0:v]trim=start={s:.4f}:end={e:.4f},setpts=PTS-STARTPTS[v{i}];"
        )
        afade = ""
        if cf > 0:
            out_st = max(0.0, dur - cf)
            afade = (f",afade=t=in:st=0:d={cf:.4f}"
                     f",afade=t=out:st={out_st:.4f}:d={cf:.4f}")
        parts.append(
            f"[0:a]atrim=start={s:.4f}:end={e:.4f},asetpts=PTS-STARTPTS{afade}[a{i}];"
        )
    concat_in = "".join(f"[v{i}][a{i}]" for i in range(len(segments)))
    parts.append(f"{concat_in}concat=n={len(segments)}:v=1:a=1[vout][araw];")
    if normalize:
        parts.append("[araw]loudnorm=I=%.1f:TP=-1.5:LRA=11[aout]" % config.TARGET_LUFS)
    else:
        parts.append("[araw]anull[aout]")
    return "".join(parts)


def render_jumpcut(input_path: str, segments: Sequence[Segment], output_path: str,
                   *, crossfade: float | None = None, normalize: bool = False) -> str:
    """보존 구간만 이어붙인 MP4 생성. output_path 반환."""
    if not segments:
        raise ValueError("보존 구간이 없습니다 (전부 무음으로 감지됨).")
    crossfade = config.CROSSFADE if crossfade is None else crossfade
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    graph = _build_filtergraph(segments, crossfade, normalize)

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(graph)
        graph_path = f.name

    cmd = [
        config.FFMPEG, "-y", "-i", input_path,
        "-filter_complex_script", graph_path,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", config.PRESET, "-crf", str(config.CRF),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    Path(graph_path).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 렌더 실패:\n{proc.stderr[-2000:]}")
    return output_path


def total_kept(segments: Sequence[Segment]) -> float:
    return sum(s.dur for s in segments)
