"""캡컷 드래프트 어댑터 (6단, 보너스) — pycapcut 로 draft_content.json 생성.

모드 A 편집결과(보존구간 + 자막)를 캡컷 드래프트로 출력 → Win/Mac 핸드오프.
주의(§7): 시간 단위는 µs (pycapcut trange/tim 헬퍼가 처리). 영상 절대경로가
대상 머신에서 유효해야 함(영상도 함께 옮길 것).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Sequence, Tuple

from . import config, subtitle
from .silence import Segment, probe_video


def default_projects_dir() -> str:
    """OS별 캡컷 Projects 폴더 자동 탐지. 없으면 OUTPUT_DIR."""
    home = Path.home()
    if sys.platform == "darwin":
        p = home / "Movies/CapCut/User Data/Projects/com.lveditor.draft"
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", str(home))
        p = Path(local) / "CapCut/User Data/Projects/com.lveditor.draft"
    else:
        p = config.OUTPUT_DIR
    return str(p if p.exists() else config.OUTPUT_DIR)


def build_capcut(input_path: str, kept_ranges: Sequence[Tuple[float, float]],
                 draft_name: str, *, cues: List[subtitle.Cue] | None = None,
                 out_root: str | None = None) -> str:
    """캡컷 드래프트 생성. 드래프트 폴더 경로 반환."""
    import pycapcut as pc

    w, h, fps = probe_video(input_path)
    root = out_root or default_projects_dir()
    Path(root).mkdir(parents=True, exist_ok=True)

    folder = pc.DraftFolder(root)
    script = folder.create_draft(draft_name, w, h, int(round(fps)), allow_replace=True)

    script.add_track(pc.TrackType.video, "main")
    material = pc.VideoMaterial(os.path.abspath(input_path))
    script.add_material(material)

    t = 0.0
    for s, e in kept_ranges:
        dur = e - s
        if dur <= 0:
            continue
        seg = pc.VideoSegment(material, pc.trange(t, dur),
                              source_timerange=pc.trange(s, dur))
        script.add_segment(seg, "main")
        t += dur

    if cues:
        ass_cues = cues
        srt_path = str(Path(root) / draft_name / "subs.srt")
        Path(srt_path).parent.mkdir(parents=True, exist_ok=True)
        subtitle.write_srt(ass_cues, srt_path)
        script.add_track(pc.TrackType.text, "subs")
        script.import_srt(srt_path, track_name="subs")

    script.save()
    return str(Path(root) / draft_name)
