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


# ----- 클립 타임라인 (재정렬·트랜지션 지원) -----
Clip = dict   # {srcIn, srcEnd, transition?:{type,dur}}  transition = "이 클립으로의" 전환
_XFADE = {"dissolve", "fade", "fadeblack", "fadewhite", "slideleft", "slideright",
          "slideup", "slidedown", "wipeleft", "wiperight", "circleopen", "radial"}


def _norm_clips(clips: Sequence[Clip]) -> List[dict]:
    """입력 클립 → {s,e,dur,ttype,tdur} 정규화. 0길이·역전 클립 제거."""
    out: List[dict] = []
    for i, c in enumerate(clips):
        s, e = float(c["srcIn"]), float(c["srcEnd"])
        if e - s <= 0.001:
            continue
        tr = c.get("transition") or {}
        ttype = (tr.get("type") or "none")
        if len(out) == 0 or ttype not in _XFADE:   # 첫 클립엔 전환 없음
            ttype, tdur = "none", 0.0
        else:
            tdur = max(0.0, float(tr.get("dur", 0.0)))
        out.append({"s": s, "e": e, "dur": e - s, "ttype": ttype, "tdur": tdur})
    return out


def output_layout(clips: Sequence[Clip]) -> tuple[List[float], float, List[float]]:
    """클립 순서·트랜지션 → (각 클립의 출력 시작시각, 총 출력길이, 클램프된 전환길이).

    트랜지션은 인접 클립을 겹치므로 총 길이가 그만큼 줄어든다. 자막 remap과
    렌더가 **동일한** 이 레이아웃을 공유한다(어긋남 방지).
    """
    norm = _norm_clips(clips)
    n = len(norm)
    starts = [0.0] * n
    dvals = [0.0] * n
    if n == 0:
        return starts, 0.0, dvals
    acc = norm[0]["dur"]
    for i in range(1, n):
        d = norm[i]["tdur"] if norm[i]["ttype"] != "none" else 0.0
        d = max(0.0, min(d, norm[i]["dur"] * 0.95, acc * 0.95))   # 양쪽보다 짧게 클램프
        dvals[i] = d
        starts[i] = acc - d
        acc = starts[i] + norm[i]["dur"]
    return starts, acc, dvals


def clip_layout(clips: Sequence[Clip]) -> tuple[List[dict], float]:
    """자막 remap용 — 각 클립의 {s(srcIn), e(srcEnd), outStart} + 총 출력길이."""
    norm = _norm_clips(clips)
    starts, total, _ = output_layout(clips)
    layout = [{"s": norm[i]["s"], "e": norm[i]["e"], "outStart": starts[i]}
              for i in range(len(norm))]
    return layout, total


def render_timeline(input_path: str, clips: Sequence[Clip], output_path: str,
                    *, crossfade: float | None = None, normalize: bool = False,
                    bgm: str | None = None, bgm_volume: float = 0.16,
                    bgm_fade_in: float = 0.0, bgm_fade_out: float = 0.0,
                    scale_h: int | None = None, preset: str | None = None,
                    crf: str | None = None, progress: ProgressCB = None) -> str:
    """클립을 순서대로(+경계 트랜지션) 이어붙여 MP4 생성. output_path 반환.

    하드컷 경계는 concat(+미세 afade로 팝 제거), 트랜지션 경계는 xfade/acrossfade.
    scale_h: 프리뷰 프록시 다운스케일. preset/crf: 인코딩 속도·품질 오버라이드.
    """
    norm = _norm_clips(clips)
    if not norm:
        raise ValueError("보존(클립) 구간이 없습니다.")
    crossfade = config.CROSSFADE if crossfade is None else crossfade
    preset = config.PRESET if preset is None else preset
    crf = config.CRF if crf is None else crf
    starts, total, dvals = output_layout(clips)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    has_trans = any(d > 0 for d in dvals)
    lnorm = f"loudnorm=I={config.TARGET_LUFS:.1f}:TP=-1.5:LRA=11"

    def _afade(dur: float) -> str:
        cf = min(crossfade, dur / 2) if dur > 0 else 0.0
        if cf <= 0:
            return ""
        return (f",afade=t=in:st=0:d={cf:.4f}"
                f",afade=t=out:st={max(0.0, dur-cf):.4f}:d={cf:.4f}")

    def _bg(idx: int) -> str:
        """BGM 입력 idx → 볼륨·길이맞춤·인/아웃 페이드 → [bg]."""
        f = ""
        if bgm_fade_in > 0:
            f += f",afade=t=in:st=0:d={bgm_fade_in:.3f}"
        if bgm_fade_out > 0:
            f += f",afade=t=out:st={max(0.0, total-bgm_fade_out):.3f}:d={bgm_fade_out:.3f}"
        return (f"[{idx}:a]volume={bgm_volume},atrim=0:{total:.3f},"
                f"asetpts=PTS-STARTPTS{f}[bg]")

    def _enc(inputs: List[str], graph: str, maps: List[str], out: str, *,
             total_sec: float, cb: ProgressCB, copy_video: bool = False,
             video: bool = True, audio: bool = True) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(graph)
            gp = f.name
        cmd = [config.FFMPEG, "-y", *inputs, "-filter_complex_script", gp, *maps]
        if video:
            cmd += (["-c:v", "copy"] if copy_video else
                    ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"])
        if audio:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += ["-movflags", "+faststart", out]
        try:
            _run_with_progress(cmd, total_sec, cb)
        finally:
            Path(gp).unlink(missing_ok=True)

    # ---- 하드컷 전용: 단일 패스 interleaved concat (검증된 견고 경로) ----
    if not has_trans:
        P: List[str] = []
        for i, c in enumerate(norm):
            P.append(f"[0:v]trim=start={c['s']:.4f}:end={c['e']:.4f},setpts=PTS-STARTPTS[v{i}]")
            P.append(f"[0:a]atrim=start={c['s']:.4f}:end={c['e']:.4f},"
                     f"asetpts=PTS-STARTPTS{_afade(c['dur'])}[a{i}]")
        inter = "".join(f"[v{i}][a{i}]" for i in range(len(norm)))
        P.append(f"{inter}concat=n={len(norm)}:v=1:a=1[vc][ac]")
        vmap = "[vc]"
        if scale_h:
            P.append(f"[vc]scale=-2:{int(scale_h)}:flags=fast_bilinear[vsc]")
            vmap = "[vsc]"
        P.append(f"[ac]{lnorm}[spk]" if normalize else "[ac]anull[spk]")
        inputs = ["-i", input_path]
        if bgm:
            inputs += ["-stream_loop", "-1", "-i", bgm]
            P.append(_bg(1))
            P.append("[spk][bg]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        else:
            P.append("[spk]anull[aout]")
        _enc(inputs, ";".join(P), ["-map", vmap, "-map", "[aout]"], output_path,
             total_sec=total, cb=progress)
        return output_path

    # ---- 트랜지션 포함: 2-패스(영상→오디오→먹스) ----
    # xfade(영상)와 acrossfade(오디오)를 한 그래프에 두면 필터 스케줄 deadlock 발생.
    # 분리 렌더 후 먹스한다. xfade는 CFR 필요 → 영상 trim에 소스 fps 강제.
    from .silence import probe_video
    try:
        _, _, src_fps = probe_video(input_path)
    except Exception:  # noqa: BLE001
        src_fps = 30.0
    vfps = f",fps={src_fps:.5f}"
    tmp_v = str(Path(output_path).with_suffix(".v.mp4"))
    tmp_a = str(Path(output_path).with_suffix(".a.m4a"))

    # 패스1: 영상 (xfade/concat 폴드)
    Pv = [f"[0:v]trim=start={c['s']:.4f}:end={c['e']:.4f},setpts=PTS-STARTPTS{vfps}[v{i}]"
          for i, c in enumerate(norm)]
    cv, acc = "v0", norm[0]["dur"]
    for i in range(1, len(norm)):
        nv, d = f"vt{i}", dvals[i]
        if d > 0:
            off = acc - d
            Pv.append(f"[{cv}][v{i}]xfade=transition={norm[i]['ttype']}:"
                      f"duration={d:.4f}:offset={off:.4f}[{nv}]")
            acc = off + norm[i]["dur"]
        else:
            Pv.append(f"[{cv}][v{i}]concat=n=2:v=1:a=0[{nv}]")
            acc += norm[i]["dur"]
        cv = nv
    vmap = f"[{cv}]"
    if scale_h:
        Pv.append(f"[{cv}]scale=-2:{int(scale_h)}:flags=fast_bilinear[vsc]")
        vmap = "[vsc]"
    _enc(["-i", input_path], ";".join(Pv), ["-map", vmap, "-an"], tmp_v,
         total_sec=total, cb=(lambda p: progress(p * 0.75)) if progress else None,
         audio=False)

    # 패스2: 오디오 — 각 클립을 출력 시작시각(starts)에 adelay로 놓고 amix.
    # 겹침 구간(트랜지션)은 양쪽 페이드가 합쳐져 크로스페이드가 된다. 하드컷 경계는
    # 미세 페이드(팝 제거)만. acrossfade 체이닝이 빈 출력을 내는 버그를 회피한다.
    Pa: List[str] = []
    n = len(norm)
    for i, c in enumerate(norm):
        dur = c["dur"]
        d_in = dvals[i] if dvals[i] > 0 else min(crossfade, dur / 2)
        d_out = (dvals[i + 1] if i + 1 < n and dvals[i + 1] > 0
                 else min(crossfade, dur / 2))
        fades = (f",afade=t=in:st=0:d={d_in:.4f}"
                 f",afade=t=out:st={max(0.0, dur-d_out):.4f}:d={d_out:.4f}")
        delay = int(round(starts[i] * 1000))
        Pa.append(f"[0:a]atrim=start={c['s']:.4f}:end={c['e']:.4f},"
                  f"asetpts=PTS-STARTPTS{fades},adelay={delay}:all=1[a{i}]")
    mixin = "".join(f"[a{i}]" for i in range(n))
    Pa.append(f"{mixin}amix=inputs={n}:duration=longest:normalize=0:dropout_transition=0[amx]")
    Pa.append(f"[amx]{lnorm}[aout]" if normalize else f"[amx]anull[aout]")
    _enc(["-i", input_path], ";".join(Pa), ["-map", "[aout]", "-vn"], tmp_a,
         total_sec=total, cb=(lambda p: progress(0.75 + p * 0.10)) if progress else None,
         video=False)

    # 패스3: 먹스 (+ BGM 믹스). 영상은 재인코딩 없이 copy.
    try:
        mux_in = ["-i", tmp_v, "-i", tmp_a]
        if bgm:
            mux_in += ["-stream_loop", "-1", "-i", bgm]
            g = (f"{_bg(2)};"
                 f"[1:a][bg]amix=inputs=2:duration=first:dropout_transition=0[aout]")
            _enc(mux_in, g, ["-map", "0:v", "-map", "[aout]"], output_path,
                 total_sec=total, cb=(lambda p: progress(0.85 + p * 0.15)) if progress else None,
                 copy_video=True)
        else:
            cmd = [config.FFMPEG, "-y", *mux_in, "-map", "0:v", "-map", "1:a",
                   "-c", "copy", "-movflags", "+faststart", output_path]
            _run_with_progress(cmd, total,
                               (lambda p: progress(0.85 + p * 0.15)) if progress else None)
    finally:
        Path(tmp_v).unlink(missing_ok=True)
        Path(tmp_a).unlink(missing_ok=True)
    return output_path


def render_jumpcut(input_path: str, segments: Sequence[Segment], output_path: str,
                   *, crossfade: float | None = None, normalize: bool = False,
                   bgm: str | None = None, bgm_volume: float = 0.16,
                   scale_h: int | None = None, preset: str | None = None,
                   crf: str | None = None, progress: ProgressCB = None) -> str:
    """보존 구간을 이어붙인 MP4 (트랜지션 없는 render_timeline 특수형). 하위호환."""
    if not segments:
        raise ValueError("보존 구간이 없습니다 (전부 무음으로 감지됨).")
    clips = [{"srcIn": s.start, "srcEnd": s.end} for s in segments]
    return render_timeline(input_path, clips, output_path, crossfade=crossfade,
                           normalize=normalize, bgm=bgm, bgm_volume=bgm_volume,
                           scale_h=scale_h, preset=preset, crf=crf, progress=progress)


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
