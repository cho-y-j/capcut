"""자막 생성 — 한국어 의존명사 청크 줄바꿈 + .srt / .ass(번인).

핵심: 어절 단위로만 끊으면 "수 있다 / 것 같다" 처럼 의존명사가 줄 맨앞에 떨어져
어색하다. 의존명사는 앞 어절과 같은 줄에 유지한다.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from . import config


def _font_file(family: str, bold: bool = False) -> str | None:
    """폰트 패밀리명 → TTF 파일 경로 (fc-match). 키프레임 텍스트 PNG 렌더용."""
    q = f"{family}:bold" if bold else family
    try:
        r = subprocess.run(["fc-match", "-f", "%{file}", q], capture_output=True, text=True)
        p = r.stdout.strip()
        if p and Path(p).exists():
            return p
    except Exception:  # noqa: BLE001
        pass
    import glob
    g = (glob.glob("/usr/share/fonts/**/NanumGothic*.ttf", recursive=True)
         or glob.glob("/usr/share/fonts/**/*.ttf", recursive=True))
    return g[0] if g else None


def _hex_rgb(s: str):
    s = (s or "#ffffff").lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return (255, 255, 255)


def text_to_png(t: dict, font_px: float, stroke_px: float, out_path: str) -> tuple[int, int]:
    """자유 텍스트박스 1개를 투명 PNG로 렌더(키프레임 텍스트 → 오버레이 파이프라인).

    웹 미리보기(HTML 텍스트)와 동일 룩: 폰트·크기·색·외곽선·굵기·줄바꿈. 반환=(w,h).
    """
    from PIL import Image, ImageDraw, ImageFont
    text = (t.get("text", "") or "텍스트")
    fp = _font_file(t.get("font", "Noto Sans CJK KR"), bool(t.get("bold", True)))
    fpx = max(8, int(round(font_px)))
    font = ImageFont.truetype(fp, fpx) if fp else ImageFont.load_default()
    fill = _hex_rgb(t.get("color", "#ffffff"))
    oc = _hex_rgb(t.get("outlineColor", "#000000"))
    sw = max(0, int(round(stroke_px)))
    lines = text.split("\n")
    tmp = Image.new("RGBA", (4, 4))
    d0 = ImageDraw.Draw(tmp)
    sizes = [d0.textbbox((0, 0), ln or " ", font=font, stroke_width=sw) for ln in lines]
    lw = [b[2] - b[0] for b in sizes]
    lh = [b[3] - b[1] for b in sizes]
    pad = sw + max(4, fpx // 6)
    line_gap = int(fpx * 0.18)
    W = max(lw) + pad * 2
    H = sum(lh) + line_gap * (len(lines) - 1) + pad * 2
    img = Image.new("RGBA", (max(2, W), max(2, H)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = pad
    for i, ln in enumerate(lines):
        x = (W - lw[i]) // 2 - sizes[i][0]
        d.text((x, y - sizes[i][1]), ln, font=font, fill=fill,
               stroke_width=sw, stroke_fill=oc)
        y += lh[i] + line_gap
    rot = float(t.get("rot", 0) or 0)
    if abs(rot) > 0.1:                          # CSS는 시계방향(+), PIL은 반시계(+) → 부호 반전
        img = img.rotate(-rot, expand=True, resample=Image.BICUBIC)
    img.save(out_path)
    return img.size

# 줄 맨앞에 오면 어색한 의존명사 (앞말과 붙임)
DEP_NOUNS = {
    "것", "수", "때", "줄", "뿐", "데", "바", "등", "채", "척", "만큼",
    "대로", "듯", "적", "겸", "지", "리", "나름", "터", "셈", "통",
}
_JOSA = "은는이가을를의에도만과와로으로께서부터까지에서한테보다처럼"


def _strip_josa(word: str) -> str:
    if len(word) > 1 and word[-1] in _JOSA:
        return word[:-1]
    return word


def is_dep_noun(word: str) -> bool:
    return word in DEP_NOUNS or _strip_josa(word) in DEP_NOUNS


def chunk_lines(text: str, max_chars: int = 18) -> List[str]:
    """의존명사 규칙을 지키며 max_chars 기준으로 줄 나눔."""
    words = text.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        if cur and is_dep_noun(w):           # 의존명사 → 절대 줄 앞으로 안 보냄
            cur = f"{cur} {w}"
        elif not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= max_chars:
            cur = f"{cur} {w}"
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


@dataclass
class Cue:
    start: float
    end: float
    text: str          # 줄바꿈은 \n


def build_cues(segments: List[dict], max_chars: int = 18, max_lines: int = 2) -> List[Cue]:
    """whisper 세그먼트 → 자막 큐. 줄이 max_lines 초과면 시간 비례로 분할."""
    cues: List[Cue] = []
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        lines = chunk_lines(text, max_chars)
        s, e = float(seg["start"]), float(seg["end"])
        if len(lines) <= max_lines:
            cues.append(Cue(s, e, "\n".join(lines)))
            continue
        # 너무 길면 max_lines 묶음으로 쪼개고 글자수 비례로 시간 배분
        groups = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
        total = sum(len("".join(g)) for g in groups) or 1
        t = s
        for g in groups:
            frac = len("".join(g)) / total
            dur = (e - s) * frac
            cues.append(Cue(t, t + dur, "\n".join(g)))
            t += dur
    return cues


def remap_cues(cues: List[Cue], kept_segments) -> List[Cue]:
    """원본 타임라인 큐 → 점프컷 후 압축된 타임라인으로 재매핑.

    컷으로 잘린 구간을 건너뛰며, 한 큐가 컷을 가로지르면 조각으로 나뉜다.
    """
    offs = []
    acc = 0.0
    for s in kept_segments:
        offs.append((s.start, s.end, acc))
        acc += s.dur
    out: List[Cue] = []
    for c in cues:
        for ss, se, off in offs:
            a = max(c.start, ss)
            b = min(c.end, se)
            if b > a:
                out.append(Cue(off + (a - ss), off + (b - ss), c.text))
    return out


def remap_cues_clips(cues: List[Cue], layout: List[dict]) -> List[Cue]:
    """원본 큐 → 클립 순서·트랜지션 반영 출력 타임라인으로 재매핑.

    layout = [{"s": srcIn, "e": srcEnd, "outStart": 출력시작초}, ...] (render.clip_layout).
    재정렬되면 큐도 따라 움직이고, 트림으로 잘린 부분은 빠진다.
    """
    out: List[Cue] = []
    for c in cues:
        for clip in layout:
            a = max(c.start, clip["s"])
            b = min(c.end, clip["e"])
            if b > a:
                base = clip["outStart"]
                out.append(Cue(base + (a - clip["s"]), base + (b - clip["s"]), c.text))
    out.sort(key=lambda x: x.start)
    return out


def _ts_srt(t: float) -> str:
    h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: List[Cue], path: str) -> str:
    lines = []
    for i, c in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{_ts_srt(c.start)} --> {_ts_srt(c.end)}")
        lines.append(c.text)
        lines.append("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def _ts_ass(t: float) -> str:
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


_ALIGN = {"bottom": 2, "top": 8, "center": 5}


def _ass_color(hexrgb: str, alpha: str = "00") -> str:
    """'#RRGGBB' → ASS '&HAABBGGRR' (BGR 역순)."""
    h = (hexrgb or "FFFFFF").lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    h = (h + "FFFFFF")[:6]
    rr, gg, bb = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha}{bb}{gg}{rr}".upper()


def style_to_kwargs(style: dict | None) -> dict:
    """UI 스타일 dict → write_ass 키워드. 미지정 키는 기본값 유지."""
    if not style:
        return {}
    keymap = {"font": "font", "fontSize": "font_size", "color": "color",
              "outlineW": "outline_w", "outlineColor": "outline_color",
              "align": "align", "bold": "bold", "box": "box",
              "marginV": "margin_v_ratio", "shadow": "shadow"}
    out = {dst: style[src] for src, dst in keymap.items() if style.get(src) is not None}
    if style.get("posX") is not None and style.get("posY") is not None:
        out["pos"] = (float(style["posX"]), float(style["posY"]))   # 정규화 0~1
    return out


def _text_dialogue(t: dict, play_w: int, play_h: int, total: float) -> str:
    """자유 텍스트박스 1개 → ASS Dialogue (위치·글꼴·색·크기 애니메이션)."""
    s = float(t["start"]) if t.get("start") is not None else 0.0
    e = float(t["end"]) if t.get("end") is not None else (total or s + 3)
    if e <= s:
        e = s + 0.1
    x, y = int(float(t.get("x", 0.5)) * play_w), int(float(t.get("y", 0.5)) * play_h)
    fs = int(t.get("fontSize", 60))
    font = t.get("font", "Noto Sans CJK KR")
    prim, outl = _ass_color(t.get("color", "FFFFFF")), _ass_color(t.get("outlineColor", "000000"))
    ow = t.get("outlineW", 3)
    b = 1 if t.get("bold", True) else 0
    dur = int((e - s) * 1000)
    anim = t.get("anim", "none")
    a = ""
    if anim == "pop":
        a = "\\fscx70\\fscy70\\t(0,250,\\fscx106\\fscy106)\\t(250,420,\\fscx100\\fscy100)"
    elif anim == "grow":
        a = f"\\fscx100\\fscy100\\t(0,{dur},\\fscx145\\fscy145)"
    elif anim == "shrink":
        a = f"\\fscx150\\fscy150\\t(0,{dur},\\fscx100\\fscy100)"
    txt = (t.get("text", "") or "").replace("\n", "\\N")
    rot = float(t.get("rot", 0) or 0)
    frz = f"\\frz{-rot:.1f}" if abs(rot) > 0.1 else ""   # ASS는 반시계(+) → CSS시계(+)와 반대
    ov = (f"{{\\an5\\pos({x},{y})\\fn{font}\\fs{fs}\\c{prim}\\3c{outl}"
          f"\\bord{ow}\\b{b}{frz}\\fad(150,150){a}}}")
    return f"Dialogue: 0,{_ts_ass(s)},{_ts_ass(e)},Default,,0,0,0,,{ov}{txt}"


def write_ass(cues: List[Cue], path: str, *, play_w: int = 1920, play_h: int = 1080,
              font: str = "Noto Sans CJK KR", font_size: int = 56,
              color: str = "FFFFFF", outline_w: float = 3, outline_color: str = "000000",
              align: str = "bottom", bold: bool = True, box: bool = False,
              margin_v_ratio: float = 0.08, shadow: float = 1.0,
              pos: tuple | None = None, texts: List[dict] | None = None,
              total: float = 0.0) -> str:
    """번인용 ASS. 글꼴·색·외곽선·그림자·위치(상/중/하 또는 자유좌표)·박스·굵기 지원.

    pos=(x,y) 정규화 좌표가 주어지면 \\an5\\pos로 자유 배치(드래그 위치). 없으면
    align(상/중/하) + 하단 안전영역 margin.
    """
    margin_v = int(play_h * float(margin_v_ratio))
    al = 5 if pos else _ALIGN.get(align, 2)
    primary = _ass_color(color)
    outline = _ass_color(outline_color)
    border_style = 3 if box else 1                 # 3 = 불투명 박스, 1 = 외곽선+그림자
    back = _ass_color(outline_color, alpha="20") if box else "&H64000000"
    sh = 0 if box else float(shadow)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{int(font_size)},{primary},{outline},{back},{1 if bold else 0},{border_style},{outline_w},{sh},{al},60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    pre = ""
    if pos:
        x, y = int(pos[0] * play_w), int(pos[1] * play_h)
        pre = f"{{\\an5\\pos({x},{y})}}"
    rows = []
    for c in cues:
        txt = c.text.replace("\n", "\\N")
        rows.append(f"Dialogue: 0,{_ts_ass(c.start)},{_ts_ass(c.end)},Default,,0,0,0,,{pre}{txt}")
    for t in (texts or []):
        if (t.get("text", "") or "").strip():
            rows.append(_text_dialogue(t, play_w, play_h, total))
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(rows) + "\n")
    return path
