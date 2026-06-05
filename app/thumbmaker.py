"""자동 썸네일 — 영상에서 좋은 장면 프레임 + 큰 제목을 얹어 PNG로.

외부 의존 없이 ffmpeg(프레임 추출) + PIL(합성). 90% 완성 비전의 '썸네일까지'.
프레임은 여러 장 샘플 후 색 분산 최대(검정/단조 회피)로 자동 선택.
스타일: band(하단 띠) / bold(중앙 큰글자). 플랫폼별 비율 지원.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import assets, config

DIMS = {"shorts": (1080, 1920), "square": (1080, 1080), "wide": (1280, 720)}


def _font(px: int):
    from PIL import ImageFont
    fp = assets._font_path()
    try:
        return ImageFont.truetype(fp, px) if fp else ImageFont.load_default()
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _extract(video: str, t: float, dest: str) -> bool:
    r = subprocess.run([config.FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", video,
                        "-frames:v", "1", "-q:v", "3", dest], capture_output=True)
    return Path(dest).exists() and Path(dest).stat().st_size > 0


def pick_frame(video: str, dur: float, n: int = 8) -> float:
    """샘플 프레임 중 색 분산 최대(가장 정보 많은) 시각."""
    from PIL import Image
    import numpy as np
    best_t, best_v = dur * 0.4, -1.0
    tmp = str(config.UPLOAD_DIR / "_thumbpick.jpg")
    for i in range(1, n + 1):
        t = dur * i / (n + 1)
        if not _extract(video, t, tmp):
            continue
        try:
            a = np.asarray(Image.open(tmp).convert("RGB").resize((96, 54)), dtype=float)
        except Exception:  # noqa: BLE001
            continue
        v = float(a.var())                       # 분산↑ = 단조롭지 않은 장면
        if v > best_v:
            best_v, best_t = v, t
    return round(best_t, 2)


def _wrap(draw, text, font, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur); cur = w
    if cur:
        lines.append(cur)
    return lines[:3]


def make_thumbnail(video: str, title: str, out_path: str, *, t: float | None = None,
                   style: str = "band", fmt: str = "wide",
                   brand_color: str = "#ff3d8b") -> str:
    from PIL import Image, ImageDraw, ImageFilter
    W, H = DIMS.get(fmt, (1280, 720))
    from .silence import probe_duration
    dur = max(0.1, probe_duration(video))
    ts = t if t is not None else pick_frame(video, dur)
    frm = str(config.UPLOAD_DIR / "_thumbframe.jpg")
    if not _extract(video, min(ts, dur - 0.05), frm):
        _extract(video, dur * 0.3, frm)
    base = Image.open(frm).convert("RGB")
    # cover-crop to WxH
    sr, dr = base.width / base.height, W / H
    if sr > dr:
        nw = int(base.height * dr); base = base.crop(((base.width - nw) // 2, 0, (base.width + nw) // 2, base.height))
    else:
        nh = int(base.width / dr); base = base.crop((0, (base.height - nh) // 2, base.width, (base.height + nh) // 2))
    base = base.resize((W, H), Image.LANCZOS)
    d = ImageDraw.Draw(base, "RGBA")
    bc = brand_color.lstrip("#")
    brand = tuple(int(bc[i:i + 2], 16) for i in (0, 2, 4)) if len(bc) == 6 else (255, 61, 139)
    title = (title or "제목").strip()

    if style == "bold":                          # 중앙 큰 글자 + 어둡게
        d.rectangle([0, 0, W, H], fill=(0, 0, 0, 90))
        fpx = int(H * (0.13 if fmt != "shorts" else 0.09))
        font = _font(fpx); lines = _wrap(d, title, font, int(W * 0.9))
        th = len(lines) * fpx * 1.18; y = (H - th) / 2
        for ln in lines:
            tw = d.textlength(ln, font=font); x = (W - tw) / 2
            for ox in (-4, 4):
                for oy in (-4, 4):
                    d.text((x + ox, y + oy), ln, font=font, fill=(0, 0, 0, 255))
            d.text((x, y), ln, font=font, fill=(255, 255, 255, 255)); y += fpx * 1.18
        d.rectangle([0, H - 14, W, H], fill=(*brand, 255))
    else:                                        # band: 하단 띠 제목
        bh = int(H * (0.30 if fmt != "shorts" else 0.22))
        grad = Image.new("L", (1, bh), 0)
        for i in range(bh):
            grad.putpixel((0, i), int(235 * (i / bh) ** 1.3))
        alpha = grad.resize((W, bh))
        shade = Image.new("RGBA", (W, bh), (10, 8, 20, 0)); shade.putalpha(alpha)
        base.paste(Image.new("RGB", (W, bh), (10, 8, 20)), (0, H - bh), shade)
        d.rectangle([0, H - bh, int(W * 0.34), H - bh + 12], fill=(*brand, 255))
        fpx = int(H * (0.11 if fmt != "shorts" else 0.075))
        font = _font(fpx); lines = _wrap(d, title, font, int(W * 0.92))
        y = H - 28 - len(lines) * fpx * 1.12
        for ln in lines:
            for ox in (-3, 3):
                d.text((40 + ox, y), ln, font=font, fill=(0, 0, 0, 230))
            d.text((40, y), ln, font=font, fill=(255, 255, 255, 255)); y += fpx * 1.12

    base.filter(ImageFilter.SMOOTH_MORE).save(out_path, quality=92)
    return out_path
