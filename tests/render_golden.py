"""렌더 골든 — 이번 세션 기능이 다시 안 깨지게 하는 자동 검수(브라우저 불필요).

회전(정적/스핀)·비정사각·배경 풀커버·텍스트 스핀·raw 클립·에셋/배경 생성을
실제 ffmpeg 렌더 + 프레임 픽셀로 검증한다. 실패하면 비0 종료.

실행: PYTHONPATH=. .venv/bin/python3 tests/render_golden.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import assets, config, pipeline, render, shapes  # noqa: E402

TMP = Path("/tmp/oncut_golden")
TMP.mkdir(exist_ok=True)
FAILS: list[str] = []


def ok(cond: bool, label: str, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILS.append(label)


def mk_video(path: str, color: str = "navy", dur: float = 4.0, size: str = "320x240") -> None:
    subprocess.run([config.FFMPEG, "-y", "-f", "lavfi", "-i", f"color=c={color}:s={size}:d={dur}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}", "-shortest",
                    "-pix_fmt", "yuv420p", path], capture_output=True)


def frame(path: str, t: float, w: int = 320, h: int = 240):
    r = subprocess.run([config.FFMPEG, "-i", path, "-ss", str(t), "-frames:v", "1",
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    if len(r.stdout) < w * h * 3:
        return None
    return np.frombuffer(r.stdout, np.uint8)[:w * h * 3].reshape(h, w, 3).astype(int)


def pink_bbox(a, sub=None):
    if a is None:
        return None
    reg = a if sub is None else a[sub[2]:sub[3], sub[0]:sub[1]]
    m = (reg[:, :, 0] > 150) & (reg[:, :, 1] < 130) & (reg[:, :, 2] > 70)
    xs = np.where(m.any(0))[0]; ys = np.where(m.any(1))[0]
    if not len(xs) or not len(ys):
        return None
    return int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)


def main() -> int:
    blk = str(TMP / "blk.mp4"); mk_video(blk)
    mk_video(str(TMP / "long.mp4"), dur=20.0)   # e2e 숏폼 추출용(20초)
    rect = str(TMP / "rect.png"); shapes.make_shape("rect", rect, color="#ff3d8b", w=300, h=300)

    print("[1] 정적 회전 + 비정사각 오버레이")
    out = str(TMP / "static.mp4")
    render.composite(blk, out, overlays=[{"path": rect, "x": .5, "y": .5, "scale": .5,
                     "scaleH": .12, "rot": 0, "opacity": 1, "start": 0, "end": 3}],
                     preset="ultrafast", crf="30")
    bb = pink_bbox(frame(out, 1.0))
    ok(bb is not None and bb[0] > bb[1] * 2.5, "비정사각 가로막대(폭≫높이)", str(bb))

    print("[2] 회전 키프레임(스핀) — 모서리 안 잘림")
    out = str(TMP / "spin.mp4")
    render.composite(blk, out, overlays=[{"path": rect, "x": .5, "y": .5, "scale": .4, "opacity": 1,
                     "start": 0, "end": 3, "kf": [{"t": 0, "x": .5, "y": .5, "rot": 0},
                     {"t": 1.5, "x": .5, "y": .5, "rot": 45}, {"t": 3, "x": .5, "y": .5, "rot": 90}]}],
                     preset="ultrafast", crf="30")
    b0 = pink_bbox(frame(out, 0.1)); b45 = pink_bbox(frame(out, 1.4))
    ok(b0 and b45 and b45[0] > b0[0] * 1.2, "45°서 대각 bbox 확장(회전 동작)", f"{b0}->{b45}")

    print("[3] 배경 풀커버(scale·scaleH=1.0)")
    assets.ensure_assets()
    bg = assets.preset_path("bg_sunset")
    ok(bg is not None, "내장 배경 preset_path 해석")
    out = str(TMP / "bg.mp4")
    render.composite(blk, out, overlays=[{"path": bg, "x": .5, "y": .5, "scale": 1.0,
                     "scaleH": 1.0, "opacity": 1, "start": 0, "end": 3}], preset="ultrafast", crf="30")
    a = frame(out, 1.0)
    corner_filled = a is not None and a[8, 8].sum() > 120 and a[230, 310].sum() > 120
    ok(corner_filled, "네 귀퉁이까지 배경이 덮음(레터박스 없음)",
       str(None if a is None else [a[8, 8].tolist(), a[230, 310].tolist()]))

    print("[4] 텍스트 스핀(파이프라인 PNG→오버레이 rotate)")
    out = str(TMP / "txt.mp4")
    pipeline.export_project(blk, [{"src": "0", "srcIn": 0, "srcEnd": 3, "transition": {}}], out,
        subtitles=False, texts=[{"text": "스핀", "x": .5, "y": .5, "fontSize": 80, "color": "#ff3d8b",
        "outlineColor": "#fff", "outlineW": 4, "start": 0, "end": 3,
        "kf": [{"t": 0, "x": .5, "y": .5, "rot": 0}, {"t": 3, "x": .5, "y": .5, "rot": 90}]}],
        preset="ultrafast", crf="30")
    dur = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", out], capture_output=True, text=True).stdout.strip()
    ok(bool(dur) and float(dur) > 1.5, "텍스트 스핀 렌더 성공", f"dur={dur}")

    print("[5] 배경 16종 생성")
    ok(len(assets.backgrounds()) >= 16, "내장 배경 ≥16종", str(len(assets.backgrounds())))

    print("[7] 숏폼 하이라이트 추출(에너지·통째·자막오프셋)")
    from app import shortify  # noqa: E402
    from app.silence import probe_duration  # noqa: E402
    lp = str(TMP / "long.mp4"); ld = probe_duration(lp)
    es, ee = shortify._energy_window(lp, ld, 8.0)
    ok(abs((ee - es) - 8.0) < 1.5 and 0 <= es and ee <= ld + 0.5, "에너지 창 길이≈target & 범위내", f"{es:.1f}-{ee:.1f}/{ld:.1f}")
    hs, he, sg = shortify.pick_highlight(lp, 30.0)
    ok(hs == 0.0 and abs(he - ld) < 0.5 and sg is None, "target>길이면 통째(ASR 생략)", f"{hs}-{he:.1f}")
    wc = shortify.window_cues([{"start": 13.0, "end": 15.0, "text": "하이"}], 12.0, 20.0)
    ok(len(wc) == 1 and abs(wc[0]["start"] - 1.0) < .01, "창 자막 0기준 오프셋", str(wc))
    g = shortify._greedy([(0, 5), (2, 9), (10, 8), (30, 7)], 40.0, 8.0, 3)
    ok(len(g) <= 3 and all(g[i][1] <= g[i + 1][0] for i in range(len(g) - 1)), "멀티: greedy 비겹침 상위N", str(g))
    gg = shortify._greedy(shortify._energy_scores(lp, ld, 8.0), ld, 8.0, 3)
    ok(any(s >= 10 for s, e, sc in gg), "멀티: 에너지 큰소리 구간 포함", str(gg))

    print("[8] 자동 썸네일(band/bold/쇼츠비율 + 제목 글자)")
    from app import thumbmaker  # noqa: E402
    from PIL import Image  # noqa: E402
    for style, fmt, ww, hh in [("band", "wide", 1280, 720), ("bold", "wide", 1280, 720), ("band", "shorts", 1080, 1920)]:
        tp = str(TMP / f"thumb_{style}_{fmt}.jpg")
        thumbmaker.make_thumbnail(str(TMP / "long.mp4"), "제주 여행 브이로그 성산일출봉", tp, style=style, fmt=fmt)
        im = np.asarray(Image.open(tp).convert("RGB"))
        white = int(((im[:, :, 0] > 235) & (im[:, :, 1] > 235) & (im[:, :, 2] > 235)).sum())
        ok(im.shape[1] == ww and im.shape[0] == hh and white > 500,
           f"썸네일 {style}/{fmt} {ww}x{hh}+제목", f"{im.shape[1]}x{im.shape[0]} white={white}")

    print("[6] 도형·이모지 PNG 생성")
    arr = str(TMP / "arrow.png"); shapes.make_shape("arrow", arr, color="#ff3d8b", w=400, h=140)
    ok(Path(arr).exists() and Path(arr).stat().st_size > 0, "화살표 도형 PNG")
    try:
        emo = str(TMP / "emo.png"); shapes.make_emoji("😀", emo)
        ok(Path(emo).exists(), "이모지 PNG(NotoColorEmoji)")
    except Exception as e:  # noqa: BLE001
        ok(False, "이모지 PNG", str(e)[:60])

    print("\n" + ("✗ 실패 %d개: %s" % (len(FAILS), ", ".join(FAILS)) if FAILS
                  else "전체 통과 ✓ — 렌더 골든(회전·비정사각·배경·텍스트스핀·에셋)"))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
