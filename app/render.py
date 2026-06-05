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


def _grade_filter(g: dict | None) -> str:
    """색보정 dict → ffmpeg 필터(footage용). neutral이면 빈 문자열.

    brightness/contrast/saturation은 CSS filter와 동일 모델(1.0=중립)이라 미리보기와
    일치. warmth(-1~1)는 colorbalance로 따뜻/차갑게(미리보기는 틴트로 근사).
    """
    if not g:
        return ""
    b = float(g.get("brightness", 1)); c = float(g.get("contrast", 1))
    s = float(g.get("saturation", 1)); w = float(g.get("warmth", 0))
    parts = []
    if abs(b - 1) > 1e-3 or abs(c - 1) > 1e-3 or abs(s - 1) > 1e-3:
        parts.append(f"eq=brightness={(b - 1):.3f}:contrast={c:.3f}:saturation={s:.3f}")
    if abs(w) > 1e-3:                       # 따뜻=R↑·B↓, 차갑=반대 (CSS 틴트로 근사)
        parts.append(f"colorchannelmixer=rr={1 + 0.2 * w:.3f}:bb={1 - 0.2 * w:.3f}")
    return ",".join(parts)


def _chroma_str(pp: dict) -> str:
    """PIP 크로마키 필터 조각 — chromaKey(hex)·chromaSim. 없으면 빈 문자열."""
    c = pp.get("chromaKey")
    if not c:
        return ""
    h = str(c).lstrip("#")
    if len(h) != 6:
        return ""
    sim = max(0.01, min(0.9, float(pp.get("chromaSim", 0.3))))
    return f",chromakey=0x{h.upper()}:{sim:.3f}:0.10"


def _mask_expr(m: str | None) -> str:
    """마스크 모양 → geq 알파 곱 인자(1=보임/0=투명). 원형/둥근사각. 없으면 빈 문자열."""
    if m == "circle":
        return ("if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2),"
                "(min(W,H)/2)*(min(W,H)/2)),1,0)")
    if m == "round":
        r = "(min(W,H)*0.18)"
        cx = f"clip(X,{r},W-{r})"
        cy = f"clip(Y,{r},H-{r})"
        return (f"if(lte((X-{cx})*(X-{cx})+(Y-{cy})*(Y-{cy}),{r}*{r}),1,0)")
    return ""


def _pw_expr(points, var: str) -> str:
    """키프레임 점들 → 구간별 선형보간 ffmpeg 식. points=[(t, 값식문자열)].

    값식은 W/H/w/h 같은 ffmpeg 변수를 포함할 수 있어 문자열로 받는다.
    범위 밖은 양끝 값으로 클램프(constant). var = 't'(overlay) 또는 'T'(geq).
    """
    pts = sorted(points, key=lambda p: p[0])
    if len(pts) == 1:
        return pts[0][1]
    expr = f"({pts[-1][1]})"                                   # 마지막 이후
    for i in range(len(pts) - 1, 0, -1):
        t0, v0 = pts[i - 1]
        t1, v1 = pts[i]
        if t1 - t0 <= 1e-6:
            seg = f"({v1})"
        else:
            seg = f"(({v0})+(({v1})-({v0}))*({var}-{t0:.4f})/{(t1 - t0):.4f})"
        expr = f"if(lt({var},{t1:.4f}),{seg},{expr})"
    return f"if(lt({var},{pts[0][0]:.4f}),({pts[0][1]}),{expr})"


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
        out.append({"s": s, "e": e, "dur": e - s, "ttype": ttype, "tdur": tdur,
                    "src": str(c.get("src", "0"))})
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
                    canvas: tuple | None = None, sources: dict | None = None,
                    grade: dict | None = None, layout: dict | None = None,
                    scale_h: int | None = None, preset: str | None = None,
                    crf: str | None = None, focus: tuple | None = None,
                    progress: ProgressCB = None) -> str:
    """클립(여러 소스: 영상/이미지)을 순서대로(+트랜지션) 이어붙여 MP4 생성.

    sources={token:{path,kind(video|image)}}, "0"=메인. 멀티소스·이미지·캔버스·
    트랜지션이면 클립별로 공통 캔버스로 scale+crop+fps 정규화(concat/xfade 안전).
    이미지 클립 오디오는 무음(aevalsrc). scale_h=프리뷰 다운스케일.
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
    sources = dict(sources or {})

    from .silence import probe_video
    try:
        mw, mh, mfps = probe_video(input_path)
    except Exception:  # noqa: BLE001
        mw, mh, mfps = 1280, 720, 30.0
    tw, th = (int(canvas[0]), int(canvas[1])) if canvas else (mw, mh)
    F = mfps if mfps and mfps > 0 else 30.0

    def srcinfo(tok):
        if tok in (None, "0"):
            return {"kind": "video", "path": input_path}
        return sources.get(tok, {"kind": "video", "path": input_path})

    has_image = any(srcinfo(c["src"]).get("kind") == "image" for c in norm)
    distinct = {srcinfo(c["src"]).get("path", input_path) for c in norm
                if srcinfo(c["src"]).get("kind") != "image"}
    multi = has_image or len(distinct) > 1
    need_vnorm = has_trans or multi or bool(canvas) or bool(layout)
    ars = ",aresample=44100" if multi else ""
    if layout:
        # 레이아웃 틀: 영상을 중앙 박스에 넣고 상하(좌우) 띠를 배경색으로 채움
        vY = max(0.0, min(0.9, float(layout.get("videoY", 0.0))))
        vH = max(0.1, min(1.0, float(layout.get("videoH", 1.0))))
        bg = str(layout.get("bg", "#000000")).lstrip("#")
        bg = "0x" + bg if len(bg) == 6 else "0x000000"
        boxH = max(2, (int(th * vH)) // 2 * 2)
        padY = max(0, (int(th * vY)) // 2 * 2)
        vsc = (f",scale={tw}:{boxH}:force_original_aspect_ratio=increase,"
               f"crop={tw}:{boxH},pad={tw}:{th}:0:{padY}:{bg},setsar=1,fps={F:.5f}")
    else:
        # cover-crop. focus=(fx,fy) 0~1로 크롭 위치 이동(말하는 사람 추적 리프레이밍).
        if focus and need_vnorm:
            fx = max(0.0, min(1.0, float(focus[0]))); fy = max(0.0, min(1.0, float(focus[1])))
            cx = f"(in_w-{tw})*{fx:.4f}"; cy = f"(in_h-{th})*{fy:.4f}"
            vsc = (f",scale={tw}:{th}:force_original_aspect_ratio=increase,"
                   f"crop={tw}:{th}:'{cx}':'{cy}',setsar=1,fps={F:.5f}")
        else:
            vsc = (f",scale={tw}:{th}:force_original_aspect_ratio=increase,"
                   f"crop={tw}:{th},setsar=1,fps={F:.5f}") if need_vnorm else ""

    inputs: List[str] = ["-i", input_path]
    path2idx = {input_path: 0}
    clip_idx: List[int] = []
    clip_kind: List[str] = []
    nin = 1
    for c in norm:
        info = srcinfo(c["src"])
        if info.get("kind") == "image":
            inputs += ["-loop", "1", "-t", f"{c['dur'] + 0.1:.3f}", "-i", info["path"]]
            clip_idx.append(nin); clip_kind.append("image"); nin += 1
        else:
            p = info.get("path", input_path)
            if p in path2idx:
                clip_idx.append(path2idx[p])
            else:
                inputs += ["-i", p]; path2idx[p] = nin; clip_idx.append(nin); nin += 1
            clip_kind.append("video")

    def vlabel(i: int) -> str:
        idx, c = clip_idx[i], norm[i]
        t = (f"[{idx}:v]trim=0:{c['dur']:.4f}" if clip_kind[i] == "image"
             else f"[{idx}:v]trim=start={c['s']:.4f}:end={c['e']:.4f}")
        return f"{t},setpts=PTS-STARTPTS{vsc}[v{i}]"

    def alabel(i: int, fades: str, delay_ms: int | None = None) -> str:
        idx, c = clip_idx[i], norm[i]
        dl = f",adelay={delay_ms}:all=1" if delay_ms is not None else ""
        if clip_kind[i] == "image":
            return f"aevalsrc=0:d={c['dur']:.4f}:s=44100{fades}{dl}[a{i}]"
        return (f"[{idx}:a]atrim=start={c['s']:.4f}:end={c['e']:.4f},"
                f"asetpts=PTS-STARTPTS{ars}{fades}{dl}[a{i}]")

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

    # ---- 하드컷 전용: 단일 패스 interleaved concat ----
    if not has_trans:
        P: List[str] = []
        for i in range(len(norm)):
            P.append(vlabel(i))
            P.append(alabel(i, _afade(norm[i]["dur"])))
        inter = "".join(f"[v{i}][a{i}]" for i in range(len(norm)))
        P.append(f"{inter}concat=n={len(norm)}:v=1:a=1[vc][ac]")
        cur = "vc"
        gf = _grade_filter(grade)
        if gf:
            P.append(f"[{cur}]{gf}[vgrade]"); cur = "vgrade"
        if scale_h:
            P.append(f"[vc]scale=-2:{int(scale_h)}:flags=fast_bilinear[vsc]")
            cur = "vsc"
        vmap = f"[{cur}]"
        P.append(f"[ac]{lnorm}[spk]" if normalize else "[ac]anull[spk]")
        ins = list(inputs)
        if bgm:
            ins += ["-stream_loop", "-1", "-i", bgm]
            P.append(_bg(nin))
            P.append("[spk][bg]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        else:
            P.append("[spk]anull[aout]")
        _enc(ins, ";".join(P), ["-map", vmap, "-map", "[aout]"], output_path,
             total_sec=total, cb=progress)
        return output_path

    # ---- 트랜지션 포함: 2-패스(영상→오디오→먹스). xfade+acrossfade 한 그래프 deadlock 회피 ----
    tmp_v = str(Path(output_path).with_suffix(".v.mp4"))
    tmp_a = str(Path(output_path).with_suffix(".a.m4a"))

    Pv = [vlabel(i) for i in range(len(norm))]
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
    cur = cv
    gf = _grade_filter(grade)
    if gf:
        Pv.append(f"[{cur}]{gf}[vgrade]"); cur = "vgrade"
    if scale_h:
        Pv.append(f"[{cur}]scale=-2:{int(scale_h)}:flags=fast_bilinear[vsc]")
        cur = "vsc"
    _enc(inputs, ";".join(Pv), ["-map", f"[{cur}]", "-an"], tmp_v,
         total_sec=total, cb=(lambda p: progress(p * 0.75)) if progress else None,
         audio=False)

    # 패스2: 오디오 — 각 클립을 출력 시작시각(starts)에 adelay로 놓고 amix.
    Pa: List[str] = []
    n = len(norm)
    for i in range(n):
        dur = norm[i]["dur"]
        d_in = dvals[i] if dvals[i] > 0 else min(crossfade, dur / 2)
        d_out = (dvals[i + 1] if i + 1 < n and dvals[i + 1] > 0
                 else min(crossfade, dur / 2))
        fades = (f",afade=t=in:st=0:d={d_in:.4f}"
                 f",afade=t=out:st={max(0.0, dur-d_out):.4f}:d={d_out:.4f}")
        Pa.append(alabel(i, fades, int(round(starts[i] * 1000))))
    mixin = "".join(f"[a{i}]" for i in range(n))
    Pa.append(f"{mixin}amix=inputs={n}:duration=longest:normalize=0:dropout_transition=0[amx]")
    Pa.append(f"[amx]{lnorm}[aout]" if normalize else f"[amx]anull[aout]")
    _enc(inputs, ";".join(Pa), ["-map", "[aout]", "-vn"], tmp_a,
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
                   *, total_sec: float = 0.0, preset: str | None = None,
                   crf: str | None = None, progress: ProgressCB = None) -> str:
    """ass 자막을 영상에 번인."""
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    preset = config.PRESET if preset is None else preset
    crf = config.CRF if crf is None else crf
    cmd = [config.FFMPEG, "-y", "-i", video,
           "-vf", f"subtitles='{ass_path}'",
           "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
           "-pix_fmt", "yuv420p", "-c:a", "copy",
           "-movflags", "+faststart", out_path]
    _run_with_progress(cmd, total_sec, progress)
    return out_path


def composite(video: str, out_path: str, *, overlays: Sequence[dict] | None = None,
              sfx: Sequence[dict] | None = None, audios: Sequence[dict] | None = None,
              pips: Sequence[dict] | None = None,
              preset: str | None = None,
              crf: str | None = None, progress: ProgressCB = None) -> str:
    """최종 합성 패스 — 이미지 오버레이(로고/버튼) + 효과음(SFX) + 오디오 클립을 한 번에.

    overlay = {path, x, y(중심 0~1), scale(가로비), opacity, start?, end?, fade?}.
      fade>0 이면 표시구간 경계에서 알파 페이드 인/아웃(부드럽게 등장/퇴장).
    sfx     = {path, at(초), volume?}. 해당 시각에 1회 믹스(반복 안 함).
    audios  = {path, at(출력시작초), in(소스내 시작), dur(재생길이), volume?,
               fadeIn?, fadeOut?}. 특정 시간대에 mp3 등을 깔고 길이 조절(자유 배치).
    위치·시각은 출력 타임라인 기준. 모두 없으면 video 그대로 반환.
    """
    from .silence import probe_duration, probe_video
    overlays = list(overlays or [])
    sfx = list(sfx or [])
    audios = list(audios or [])
    pips = list(pips or [])
    if not overlays and not sfx and not audios and not pips:
        return video
    preset = config.PRESET if preset is None else preset
    crf = config.CRF if crf is None else crf
    W, H, _ = probe_video(video)
    total = probe_duration(video)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    inputs: List[str] = ["-i", video]
    P: List[str] = []
    nin = 0   # 입력 인덱스 (video = 0)

    # --- 비디오: 오버레이 ---
    vmap = "0:v"
    cur = "0:v"
    for i, ov in enumerate(overlays):
        inputs += ["-loop", "1", "-t", f"{total:.3f}", "-i", ov["path"]]
        nin += 1
        idx = nin
        ow = max(2, int(W * float(ov.get("scale", 0.2))))
        op = max(0.0, min(1.0, float(ov.get("opacity", 1.0))))
        px, py = float(ov.get("x", 0.5)), float(ov.get("y", 0.1))
        s, e = ov.get("start"), ov.get("end")
        fd = float(ov.get("fade", 0.0) or 0.0)
        kf = ov.get("kf") or []
        nb = f"b{i}"
        if len(kf) >= 2:
            # 키프레임: 위치(overlay x/y 시간식) + 투명도(geq 알파 시간식)로 정확 모션
            kf = sorted(kf, key=lambda k: float(k["t"]))
            bscale = float(ov.get("scale", 0.2))
            xpts = [(float(k["t"]), f"(W*{float(k.get('x', px)):.4f}-w/2)") for k in kf]
            ypts = [(float(k["t"]), f"(H*{float(k.get('y', py)):.4f}-h/2)") for k in kf]
            opts_ = [(float(k["t"]), f"{max(0.0, min(1.0, float(k.get('opacity', op)))):.4f}") for k in kf]
            spts = [(float(k["t"]), f"{max(2, int(W * float(k.get('scale', bscale)))):d}") for k in kf]
            brot = float(ov.get("rot", 0) or 0)
            rpts = [(float(k["t"]), f"{float(k.get('rot', brot)):.3f}") for k in kf]
            any_rot = any(abs(float(k.get("rot", brot))) > 0.1 for k in kf)
            ks, ke = xpts[0][0], xpts[-1][0]
            ws = float(s) if s is not None else ks
            we = float(e) if e is not None else ke
            # 비정사각(세로 따로)은 회전 없을 때만(회전 키프레임과 동시엔 ffmpeg rotate가
            # eval=frame 스케일과 충돌 → 스핀은 비율 유지). 정적 회전은 static 분기에서 처리.
            hexpr = f"{max(2, int(H * float(ov['scaleH'])))}" if (ov.get("scaleH") and not any_rot) else "-2"
            chain = (f"[{idx}:v]format=rgba,"
                     f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({_pw_expr(opts_, 'T')})',"
                     f"scale=w='{_pw_expr(spts, 't')}':h={hexpr}:eval=frame")
            if any_rot:                       # 회전 키프레임(스핀): 각도 시간식(콤마 보호 위해 따옴표)
                ra = f"(({_pw_expr(rpts, 't')})*PI/180)"
                # ow/oh는 각도와 무관한 상수 대각선(hypot)으로 — rotw/roth는 init에서 a=0로 고정
                # 평가돼 스핀 중 모서리가 잘림(팔각형). hypot는 어느 각도든 안 잘림.
                chain += f",rotate=a='{ra}':ow='hypot(iw,ih)':oh='hypot(iw,ih)':c=none"
            P.append(f"{chain}[ov{i}]")
            en = f":enable='between(t,{ws:.3f},{we:.3f})'"
            P.append(f"[{cur}][ov{i}]overlay=x='{_pw_expr(xpts, 't')}':y='{_pw_expr(ypts, 't')}'{en}[{nb}]")
            cur = nb
            continue
        oh = int(H * float(ov["scaleH"])) if ov.get("scaleH") else -1   # 비정사각(세로 따로)
        chain = f"[{idx}:v]scale={ow}:{oh},format=rgba,colorchannelmixer=aa={op:.3f}"
        en = ""
        if s is not None and e is not None:
            s, e = float(s), float(e)
            if fd > 0:                       # 알파 페이드 → enable 불필요(알파가 가시성 처리)
                fd = min(fd, (e - s) / 2)
                chain += (f",fade=t=in:st={s:.3f}:d={fd:.3f}:alpha=1"
                          f",fade=t=out:st={max(s, e - fd):.3f}:d={fd:.3f}:alpha=1")
            else:
                en = f":enable='between(t,{s:.3f},{e:.3f})'"
        rot = float(ov.get("rot", 0) or 0)
        if abs(rot) > 0.1:                   # 회전(투명 유지, 박스 확장)
            a = f"{rot * 3.14159265 / 180:.5f}"
            chain += f",rotate={a}:ow=rotw({a}):oh=roth({a}):c=none"
        P.append(f"{chain}[ov{i}]")
        P.append(f"[{cur}][ov{i}]overlay=x=W*{px}-w/2:y=H*{py}-h/2{en}[{nb}]")
        cur = nb

    # --- 비디오: PIP(영상 위 영상) — 소스 구간 트림 + 출력시각 이동 + 위치·크기 ---
    pip_aud = []   # (input_idx, pip) — 오디오 있는 PIP만
    for k, pp in enumerate(pips):
        inputs += ["-i", pp["path"]]
        nin += 1
        idx = nin
        pw = max(2, int(W * float(pp.get("scale", 0.4))))
        op = max(0.0, min(1.0, float(pp.get("opacity", 1.0))))
        px, py = float(pp.get("x", 0.5)), float(pp.get("y", 0.5))
        s = float(pp.get("start", 0.0))
        e = float(pp.get("end", s))
        pin = max(0.0, float(pp.get("in", 0.0)))
        dur = max(0.05, e - s)
        kf = pp.get("kf") or []
        nb = f"pb{k}"
        ck = _chroma_str(pp)
        mexpr = _mask_expr(pp.get("mask"))
        trim = f"trim=start={pin:.4f}:end={pin + dur:.4f},setpts=PTS-STARTPTS+{s:.4f}/TB"
        if len(kf) >= 2:   # PIP 키프레임: 위치·크기·투명도 시간식 (+크로마키·마스크)
            kf = sorted(kf, key=lambda kk: float(kk["t"]))
            bscale = float(pp.get("scale", 0.4))
            xpts = [(float(kk["t"]), f"(W*{float(kk.get('x', px)):.4f}-w/2)") for kk in kf]
            ypts = [(float(kk["t"]), f"(H*{float(kk.get('y', py)):.4f}-h/2)") for kk in kf]
            spts = [(float(kk["t"]), f"{max(2, int(W * float(kk.get('scale', bscale)))):d}") for kk in kf]
            opts_ = [(float(kk["t"]), f"{max(0.0, min(1.0, float(kk.get('opacity', op)))):.4f}") for kk in kf]
            amul = f"*({mexpr})" if mexpr else ""
            P.append(f"[{idx}:v]{trim}{ck},format=rgba,"
                     f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({_pw_expr(opts_, 'T')}){amul}',"
                     f"scale=w='{_pw_expr(spts, 't')}':h=-2:eval=frame[pv{k}]")
            P.append(f"[{cur}][pv{k}]overlay=x='{_pw_expr(xpts, 't')}':y='{_pw_expr(ypts, 't')}':"
                     f"enable='between(t,{s:.3f},{e:.3f})':eof_action=pass[{nb}]")
        else:
            mgeq = (f",geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='alpha(X,Y)*({mexpr})'") if mexpr else ""
            P.append(f"[{idx}:v]{trim}{ck},scale={pw}:-1,format=rgba,colorchannelmixer=aa={op:.3f}{mgeq}[pv{k}]")
            P.append(f"[{cur}][pv{k}]overlay=x=W*{px}-w/2:y=H*{py}-h/2:"
                     f"enable='between(t,{s:.3f},{e:.3f})':eof_action=pass[{nb}]")
        cur = nb
        if pp.get("hasAudio") and float(pp.get("volume", 1.0)) > 0:
            pip_aud.append((idx, pp))
    if overlays or pips:
        vmap = f"[{cur}]"

    # --- 오디오: 효과음(1회) + 자유 오디오 클립 + PIP 영상 사운드 믹스 ---
    amap = "0:a"
    if sfx or audios or pip_aud:
        labels = ["[0:a]"]
        for j, sx in enumerate(sfx):
            inputs += ["-i", sx["path"]]
            nin += 1
            idx = nin
            at = max(0.0, float(sx.get("at", 0.0)))
            vol = float(sx.get("volume", 1.0))
            P.append(f"[{idx}:a]adelay={int(at*1000)}:all=1,volume={vol:.3f}[sf{j}]")
            labels.append(f"[sf{j}]")
        for k, au in enumerate(audios):
            inputs += ["-i", au["path"]]
            nin += 1
            idx = nin
            at = max(0.0, float(au.get("at", 0.0)))
            ain = max(0.0, float(au.get("in", 0.0)))
            dur = max(0.05, float(au.get("dur", 0.0))) if au.get("dur") else None
            vol = float(au.get("volume", 1.0))
            fin = max(0.0, float(au.get("fadeIn", 0.0)))
            fout = max(0.0, float(au.get("fadeOut", 0.0)))
            trim = f"atrim=start={ain:.4f}" + (f":end={ain+dur:.4f}" if dur else "")
            fades = ""
            if dur and fin > 0:
                fades += f",afade=t=in:st=0:d={min(fin, dur):.4f}"
            if dur and fout > 0:
                fades += f",afade=t=out:st={max(0.0, dur-min(fout, dur)):.4f}:d={min(fout, dur):.4f}"
            P.append(f"[{idx}:a]{trim},asetpts=PTS-STARTPTS,volume={vol:.3f}"
                     f"{fades},adelay={int(at*1000)}:all=1[au{k}]")
            labels.append(f"[au{k}]")
        for m, (idx, pp) in enumerate(pip_aud):
            s = float(pp.get("start", 0.0)); e = float(pp.get("end", s))
            pin = max(0.0, float(pp.get("in", 0.0))); dur = max(0.05, e - s)
            vol = float(pp.get("volume", 1.0))
            P.append(f"[{idx}:a]atrim=start={pin:.4f}:end={pin+dur:.4f},asetpts=PTS-STARTPTS,"
                     f"volume={vol:.3f},adelay={int(s*1000)}:all=1[pa{m}]")
            labels.append(f"[pa{m}]")
        P.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:"
                 f"normalize=0:dropout_transition=0[aout]")
        amap = "[aout]"

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(";".join(P))
        gp = f.name
    cmd = [config.FFMPEG, "-y", *inputs, "-filter_complex_script", gp,
           "-map", vmap, "-map", amap]
    cmd += (["-c:v", "copy"] if vmap == "0:v" else
            ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"])
    cmd += (["-c:a", "copy"] if amap == "0:a" else ["-c:a", "aac", "-b:a", "192k"])
    cmd += ["-movflags", "+faststart", out_path]
    try:
        _run_with_progress(cmd, total, progress)
    finally:
        Path(gp).unlink(missing_ok=True)
    return out_path
