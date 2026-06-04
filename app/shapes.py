"""도형·이모지 → 투명 PNG 생성. ONCUT 오버레이로 얹어 위치·시간·키프레임 재사용.

도형은 안티앨리어싱 위해 4배 슈퍼샘플 후 축소. 이모지는 NotoColorEmoji(컬러).
"""
from __future__ import annotations

SS = 4   # supersample


def _rgba(hexs: str, op: float = 1.0):
    s = (hexs or "#ff3d8b").lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        r, g, b = 255, 61, 139
    return (r, g, b, max(0, min(255, int(255 * op))))


def make_shape(kind: str, out_path: str, *, color: str = "#ff3d8b", stroke: str = "",
               stroke_w: float = 0.0, opacity: float = 1.0, radius: float = 0.25,
               w: int = 400, h: int = 400) -> tuple[int, int]:
    """kind: rect/round/circle/line/arrow/triangle/star/heart. 채움색·외곽선·투명도."""
    from PIL import Image, ImageDraw
    W, H = max(8, int(w)) * SS, max(8, int(h)) * SS
    fill = _rgba(color, opacity)
    sw = int(stroke_w * SS)
    outline = _rgba(stroke, 1.0) if (stroke and sw > 0) else None
    pad = max(sw, SS * 2)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    L, T, R, B = pad, pad, W - pad, H - pad
    if kind == "rect":
        d.rectangle([L, T, R, B], fill=fill, outline=outline, width=sw)
    elif kind == "round":
        d.rounded_rectangle([L, T, R, B], radius=int(min(R - L, B - T) * max(0.02, min(0.5, radius))),
                            fill=fill, outline=outline, width=sw)
    elif kind in ("circle", "ellipse"):
        d.ellipse([L, T, R, B], fill=fill, outline=outline, width=sw)
    elif kind == "line":
        cy = H // 2
        d.line([L, cy, R, cy], fill=fill, width=max(SS * 3, sw or SS * 4))
    elif kind == "arrow":
        cy = H // 2
        bw = max(SS * 3, int(min(W, H) * 0.10))
        hx = R - int((R - L) * 0.28)
        d.line([L, cy, hx, cy], fill=fill, width=bw)
        hh = int((B - T) * 0.42)
        d.polygon([(R, cy), (hx, cy - hh), (hx, cy + hh)], fill=fill)
    elif kind == "triangle":
        d.polygon([((L + R) // 2, T), (L, B), (R, B)], fill=fill, outline=outline)
    elif kind == "star":
        import math
        cx, cy = (L + R) / 2, (T + B) / 2
        ro = min(R - L, B - T) / 2
        ri = ro * 0.42
        pts = []
        for i in range(10):
            ang = -math.pi / 2 + i * math.pi / 5
            r = ro if i % 2 == 0 else ri
            pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
        d.polygon(pts, fill=fill, outline=outline)
    elif kind == "heart":
        import math
        cx, cy = (L + R) / 2, (T + B) / 2
        sc = min(R - L, B - T) / 34
        pts = []
        for i in range(0, 360, 6):
            a = math.radians(i)
            x = 16 * math.sin(a) ** 3
            yv = 13 * math.cos(a) - 5 * math.cos(2 * a) - 2 * math.cos(3 * a) - math.cos(4 * a)
            pts.append((cx + x * sc, cy - yv * sc))
        d.polygon(pts, fill=fill)
    else:
        d.rounded_rectangle([L, T, R, B], radius=int((B - T) * 0.2), fill=fill, outline=outline, width=sw)
    img = img.resize((W // SS, H // SS), Image.LANCZOS)
    img.save(out_path)
    return img.size


_EMOJI_FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"


def make_emoji(ch: str, out_path: str, px: int = 200) -> tuple[int, int]:
    """이모지 문자 → 컬러 PNG (NotoColorEmoji)."""
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(_EMOJI_FONT, 109)   # NotoColorEmoji는 109px 비트맵
    im = Image.new("RGBA", (140, 140), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.text((70, 74), ch[:8], font=font, embedded_color=True, anchor="mm")
    px = max(48, min(512, int(px)))
    im = im.resize((px, px), Image.LANCZOS)
    im.save(out_path)
    return im.size
