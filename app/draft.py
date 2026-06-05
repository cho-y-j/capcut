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


def _rgb01(hexs: str):
    s = (hexs or "#ffffff").lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return (int(s[0:2], 16) / 255, int(s[2:4], 16) / 255, int(s[4:6], 16) / 255)
    except Exception:  # noqa: BLE001
        return (1.0, 1.0, 1.0)


def build_from_project(input_path: str, clips, srcpaths: dict, *, w: int, h: int, fps: float,
                       cues=None, texts=None, overlays=None, audios=None, sfx=None,
                       bgm: str | None = None, bgm_volume: float = 0.16,
                       draft_name: str = "ONCUT", out_root: str | None = None) -> dict:
    """편집기 전체 프로젝트(멀티소스 클립·자막·텍스트·오버레이·오디오) → 캡컷 드래프트.

    각 부가요소는 best-effort(실패해도 핵심 영상·자막은 유지). 반환: {dir, segments, skipped}.
    """
    import pycapcut as pc

    root = out_root or default_projects_dir()
    Path(root).mkdir(parents=True, exist_ok=True)
    folder = pc.DraftFolder(root)
    script = folder.create_draft(draft_name, int(w), int(h), int(round(fps or 30)), allow_replace=True)
    skipped: List[str] = []
    nseg = 0

    # --- 영상 클립(멀티소스·트림·재정렬) ---
    script.add_track(pc.TrackType.video, "main")
    t = 0.0
    for c in clips:
        s = float(c.get("srcIn", 0)); e = float(c.get("srcEnd", 0)); dur = e - s
        if dur <= 0:
            continue
        path = srcpaths.get(str(c.get("src", "0"))) or input_path
        try:
            mat = pc.VideoMaterial(os.path.abspath(path))
            script.add_material(mat)
            script.add_segment(pc.VideoSegment(mat, pc.trange(t, dur),
                               source_timerange=pc.trange(s, dur)), "main")
            nseg += 1
        except Exception as ex:  # noqa: BLE001 — 이미지/probe 실패 등
            skipped.append(f"clip:{Path(path).name}:{ex}")
        t += dur
    total = max(0.1, t)

    # --- 이미지/로고 오버레이 ---
    if overlays:
        try:
            script.add_track(pc.TrackType.video, "overlay")
            for ov in overlays:
                p = ov.get("path")
                if not p or not Path(p).exists():
                    continue
                st = float(ov.get("start") or 0.0)
                en = float(ov["end"]) if ov.get("end") is not None else total
                if en - st <= 0:
                    en = total
                try:
                    mat = pc.VideoMaterial(os.path.abspath(p)); script.add_material(mat)
                    cs = pc.ClipSettings(alpha=float(ov.get("opacity", 1.0)),
                                         rotation=float(ov.get("rot", 0) or 0),
                                         scale_x=max(0.05, float(ov.get("scale", 0.3))),
                                         scale_y=max(0.05, float(ov.get("scaleH") or ov.get("scale", 0.3))),
                                         transform_x=(float(ov.get("x", 0.5)) - 0.5) * 2,
                                         transform_y=(float(ov.get("y", 0.5)) - 0.5) * 2)
                    script.add_segment(pc.VideoSegment(mat, pc.trange(st, en - st), clip_settings=cs), "overlay")
                    nseg += 1
                except Exception as ex:  # noqa: BLE001
                    skipped.append(f"overlay:{ex}")
        except Exception as ex:  # noqa: BLE001
            skipped.append(f"overlay-track:{ex}")

    # --- 자막(SRT) ---
    if cues:
        try:
            srt_path = str(Path(root) / draft_name / "subs.srt")
            Path(srt_path).parent.mkdir(parents=True, exist_ok=True)
            subtitle.write_srt(cues, srt_path)
            script.add_track(pc.TrackType.text, "subs")
            script.import_srt(srt_path, track_name="subs")
            nseg += len(cues)
        except Exception as ex:  # noqa: BLE001
            skipped.append(f"subs:{ex}")

    # --- 자유 텍스트박스 ---
    if texts:
        try:
            script.add_track(pc.TrackType.text, "texts")
            for tx in texts:
                txt = (tx.get("text") or "").strip()
                if not txt:
                    continue
                st = float(tx.get("start") or 0.0)
                en = float(tx["end"]) if tx.get("end") is not None else st + 3
                try:
                    style = pc.TextStyle(size=max(4.0, float(tx.get("fontSize", 48)) / 8.0),
                                         bold=bool(tx.get("bold")), color=_rgb01(tx.get("color", "#ffffff")),
                                         align=1)
                    bw = float(tx.get("outlineW", 0) or 0)
                    border = pc.TextBorder(color=_rgb01(tx.get("outlineColor", "#000000")),
                                           width=40.0) if bw > 0 else None
                    cs = pc.ClipSettings(rotation=float(tx.get("rot", 0) or 0),
                                         transform_x=(float(tx.get("x", 0.5)) - 0.5) * 2,
                                         transform_y=(float(tx.get("y", 0.5)) - 0.5) * 2)
                    script.add_segment(pc.TextSegment(txt, pc.trange(st, max(0.3, en - st)),
                                       style=style, border=border, clip_settings=cs), "texts")
                    nseg += 1
                except Exception as ex:  # noqa: BLE001
                    skipped.append(f"text:{ex}")
        except Exception as ex:  # noqa: BLE001
            skipped.append(f"text-track:{ex}")

    # --- 오디오(BGM + mp3 클립 + 효과음) ---
    auds = list(audios or []) + list(sfx or [])
    if bgm and Path(bgm).exists():
        auds.append({"path": bgm, "at": 0.0, "dur": total, "volume": float(bgm_volume)})
    if auds:
        try:
            script.add_track(pc.TrackType.audio, "audio")
            for au in auds:
                p = au.get("path")
                if not p or not Path(p).exists():
                    continue
                at = float(au.get("at", 0.0))
                dur = float(au.get("dur") or 0) or 5.0
                try:
                    mat = pc.AudioMaterial(os.path.abspath(p)); script.add_material(mat)
                    seg = pc.AudioSegment(mat, pc.trange(at, dur), volume=float(au.get("volume", 1.0)))
                    script.add_segment(seg, "audio")
                    nseg += 1
                except Exception as ex:  # noqa: BLE001
                    skipped.append(f"audio:{ex}")
        except Exception as ex:  # noqa: BLE001
            skipped.append(f"audio-track:{ex}")

    script.save()
    return {"dir": str(Path(root) / draft_name), "segments": nseg, "skipped": skipped}
