"""내장 에셋 — 버튼 그래픽(PNG) + 효과음(SFX). 없으면 시작 시 1회 생성.

사용자가 직접 올린 이미지/사운드와 **함께** 쓸 수 있는 기본 제공물.
저장소엔 비추적(런타임 생성). 버튼은 PIL, 효과음은 ffmpeg lavfi로 만든다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import config

ASSETS_DIR = Path(__file__).parent / "static" / "assets"

# 내장 프리셋 (id, kind, file, label). url은 /static/assets/{file}.
BUTTONS = [
    {"id": "btn_subscribe", "label": "구독", "text": "구독", "bg": (229, 9, 20), "fg": (255, 255, 255)},
    {"id": "btn_like", "label": "좋아요", "text": "좋아요", "bg": (37, 99, 235), "fg": (255, 255, 255)},
    {"id": "btn_click", "label": "여기 클릭", "text": "여기 클릭", "bg": (34, 197, 94), "fg": (4, 19, 10)},
]
# 효과음: ffmpeg lavfi 필터로 합성 (frequency, duration, fade-out 시작)
SOUNDS = [
    {"id": "sfx_click", "label": "클릭", "freq": 1600, "dur": 0.05, "fade_st": 0.008},
    {"id": "sfx_ding", "label": "딩", "freq": 988, "dur": 0.55, "fade_st": 0.05},
    {"id": "sfx_pop", "label": "팝", "freq": 420, "dur": 0.14, "fade_st": 0.02},
]
# 내장 배경 — 단색 + 그라데이션(키 없이 즉시 사용). 타이틀카드·색블록·세로띠 배경용.
BACKGROUNDS = [
    {"id": "bg_white", "label": "화이트", "colors": ["#ffffff"]},
    {"id": "bg_black", "label": "블랙", "colors": ["#0a0a0a"]},
    {"id": "bg_magenta", "label": "마젠타", "colors": ["#ff3d8b"]},
    {"id": "bg_lilac", "label": "라일락", "colors": ["#c5b0f4"]},
    {"id": "bg_mint", "label": "민트", "colors": ["#c8e6cd"]},
    {"id": "bg_cream", "label": "크림", "colors": ["#f4ecd6"]},
    {"id": "bg_navy", "label": "네이비", "colors": ["#1f1d3d"]},
    {"id": "bg_lime", "label": "라임", "colors": ["#dceeb1"]},
    {"id": "bg_sunset", "label": "선셋", "colors": ["#ff6a88", "#ffb199"]},
    {"id": "bg_ocean", "label": "오션", "colors": ["#2193b0", "#6dd5ed"]},
    {"id": "bg_grape", "label": "그레이프", "colors": ["#834d9b", "#d04ed6"]},
    {"id": "bg_forest", "label": "포레스트", "colors": ["#5a9367", "#a8e063"]},
    {"id": "bg_peach", "label": "피치", "colors": ["#ffecd2", "#fcb69f"]},
    {"id": "bg_sky", "label": "스카이", "colors": ["#89f7fe", "#66a6ff"]},
    {"id": "bg_dusk", "label": "더스크", "colors": ["#355c7d", "#c06c84"]},
    {"id": "bg_dark", "label": "다크", "colors": ["#232526", "#414345"]},
]


def _font_path() -> str:
    for fam in ("Black Han Sans", "NanumGothicBold", "NanumGothic:bold", "Noto Sans CJK KR:bold"):
        try:
            p = subprocess.run(["fc-match", "-f", "%{file}", fam],
                               capture_output=True, text=True).stdout.strip()
            if p and Path(p).exists():
                return p
        except Exception:  # noqa: BLE001
            pass
    return ""


def _make_button(spec: dict, path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont
    W, H, pad, r = 460, 150, 6, 30
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([pad, pad, W - pad, H - pad], radius=r, fill=(*spec["bg"], 255))
    fp = _font_path()
    font = ImageFont.truetype(fp, 66) if fp else ImageFont.load_default()
    text = spec["text"]
    box = d.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    d.text(((W - tw) / 2 - box[0], (H - th) / 2 - box[1]), text, font=font, fill=(*spec["fg"], 255))
    im.save(path)


def _hex(c: str):
    s = c.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _make_background(spec: dict, path: Path, w: int = 1280, h: int = 720) -> None:
    """단색 1색 → 채움, 2색 → 대각선 그라데이션."""
    from PIL import Image
    cols = [_hex(c) for c in spec["colors"]]
    if len(cols) == 1:
        Image.new("RGB", (w, h), cols[0]).save(path)
        return
    c0, c1 = cols[0], cols[1]
    im = Image.new("RGB", (w, h))
    px = im.load()
    denom = float(w + h - 2) or 1.0
    for y in range(h):
        for x in range(w):
            t = (x + y) / denom            # 대각선 진행도 0→1
            px[x, y] = (int(c0[0] + (c1[0] - c0[0]) * t),
                        int(c0[1] + (c1[1] - c0[1]) * t),
                        int(c0[2] + (c1[2] - c0[2]) * t))
    im.save(path)


def _make_sound(spec: dict, path: Path) -> None:
    dur, st = spec["dur"], spec["fade_st"]
    af = f"afade=t=out:st={st:.3f}:d={max(0.01, dur - st):.3f},volume=0.9"
    cmd = [config.FFMPEG, "-y", "-f", "lavfi",
           "-i", f"sine=frequency={spec['freq']}:duration={dur:.3f}",
           "-af", af, "-c:a", "libmp3lame", "-q:a", "5", str(path)]
    subprocess.run(cmd, capture_output=True, text=True)


def ensure_assets() -> None:
    """누락된 내장 에셋 생성. 실패해도 앱 기동은 막지 않는다."""
    try:
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        for b in BUTTONS:
            p = ASSETS_DIR / f"{b['id']}.png"
            if not p.exists():
                _make_button(b, p)
        for s in SOUNDS:
            p = ASSETS_DIR / f"{s['id']}.mp3"
            if not p.exists():
                _make_sound(s, p)
        for bg in BACKGROUNDS:
            p = ASSETS_DIR / f"{bg['id']}.png"
            if not p.exists():
                _make_background(bg, p)
    except Exception:  # noqa: BLE001
        pass


def presets() -> dict:
    """프론트용 프리셋 목록 (존재하는 것만)."""
    btns = [{"id": b["id"], "label": b["label"], "url": f"/static/assets/{b['id']}.png"}
            for b in BUTTONS if (ASSETS_DIR / f"{b['id']}.png").exists()]
    sfx = [{"id": s["id"], "label": s["label"], "url": f"/static/assets/{s['id']}.mp3"}
           for s in SOUNDS if (ASSETS_DIR / f"{s['id']}.mp3").exists()]
    return {"buttons": btns, "sounds": sfx}


def preset_path(pid: str) -> str | None:
    """프리셋 id → 실제 파일 경로 (ffmpeg 입력용). 모르면 None."""
    for b in BUTTONS:
        if b["id"] == pid:
            p = ASSETS_DIR / f"{pid}.png"
            return str(p) if p.exists() else None
    for s in SOUNDS:
        if s["id"] == pid:
            p = ASSETS_DIR / f"{pid}.mp3"
            return str(p) if p.exists() else None
    return background_path(pid)


def backgrounds() -> list:
    """내장 배경 목록(존재하는 것만)."""
    return [{"id": b["id"], "label": b["label"], "url": f"/static/assets/{b['id']}.png"}
            for b in BACKGROUNDS if (ASSETS_DIR / f"{b['id']}.png").exists()]


def background_path(pid: str) -> str | None:
    for b in BACKGROUNDS:
        if b["id"] == pid:
            p = ASSETS_DIR / f"{pid}.png"
            return str(p) if p.exists() else None
    return None
