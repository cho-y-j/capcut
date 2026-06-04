"""켄번스 슬라이드쇼 (모드 B). 이미지 + 구간길이 → 자연스러운 줌/팬 영상.

각 이미지를 zoompan 으로 천천히 줌인/줌아웃(번갈아) 시켜 정적 사진을 동영상처럼
만든다. 이후 성우 음성 + 자막(ass)을 합성한다.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import List, Sequence

from . import config


def _kenburns_clip(image: str, duration: float, out_path: str,
                   w: int, h: int, fps: int, zoom_in: bool) -> None:
    frames = max(1, int(round(duration * fps)))
    # 부드러운 줌을 위해 먼저 크게 스케일 → zoompan
    if zoom_in:
        z = "min(zoom+0.0009,1.18)"
    else:
        z = "if(eq(on,1),1.18,max(1.001,zoom-0.0009))"
    vf = (
        f"scale={w*2}:{h*2}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={w*2}:{h*2},"
        f"zoompan=z='{z}':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={w}x{h}:fps={fps},format=yuv420p"
    )
    cmd = [config.FFMPEG, "-y", "-loop", "1", "-i", image, "-t", f"{duration:.3f}",
           "-vf", vf, "-c:v", "libx264", "-preset", config.PRESET,
           "-crf", str(config.CRF), "-r", str(fps), out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"켄번스 클립 실패:\n{proc.stderr[-1500:]}")


def build_video(images: Sequence[str], durations: Sequence[float], out_path: str,
                *, w: int = 1920, h: int = 1080, fps: int = 30) -> str:
    """이미지들 → 켄번스 무음 영상."""
    if len(images) != len(durations):
        raise ValueError("이미지 수와 구간길이 수가 다릅니다.")
    tmpdir = Path(tempfile.mkdtemp(prefix="kb_"))
    clips: List[Path] = []
    for i, (img, dur) in enumerate(zip(images, durations)):
        c = tmpdir / f"clip_{i:03d}.mp4"
        _kenburns_clip(img, dur, str(c), w, h, fps, zoom_in=(i % 2 == 0))
        clips.append(c)
    listfile = tmpdir / "list.txt"
    listfile.write_text("".join(f"file '{c}'\n" for c in clips), encoding="utf-8")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [config.FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
           "-c", "copy", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"슬라이드쇼 concat 실패:\n{proc.stderr[-1500:]}")
    return out_path


def compose(video: str, audio: str, out_path: str, *, ass: str | None = None,
            normalize: bool = True) -> str:
    """무음 영상 + 성우 음성 (+ 자막 번인) 합성."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    vfilter = []
    if ass:
        vfilter.append(f"subtitles='{ass}'")
    afilter = []
    if normalize:
        afilter.append(f"loudnorm=I={config.TARGET_LUFS}:TP=-1.5:LRA=11")
    cmd = [config.FFMPEG, "-y", "-i", video, "-i", audio]
    if vfilter:
        cmd += ["-vf", ",".join(vfilter)]
    if afilter:
        cmd += ["-af", ",".join(afilter)]
    cmd += ["-c:v", "libx264", "-preset", config.PRESET, "-crf", str(config.CRF),
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"모드B 합성 실패:\n{proc.stderr[-1800:]}")
    return out_path
