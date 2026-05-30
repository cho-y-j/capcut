"""1단 검증용 CLI.

  python -m app.cli INPUT.mp4 [-o OUT.mp4] [--normalize] [--db -30] [--min 0.4]

무음 감지 → 보존 구간 산출 → 점프컷 MP4 추출. 통계를 출력한다.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import config, render, silence


def main() -> None:
    ap = argparse.ArgumentParser(description="캡컷 에이전트 — 점프컷 추출 (1단)")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--db", type=float, default=None, help="무음 임계 dB (기본 -30)")
    ap.add_argument("--min", type=float, default=None, help="최소 무음 길이 s (기본 0.4)")
    ap.add_argument("--normalize", action="store_true", help="루드니스 정규화 -14 LUFS")
    args = ap.parse_args()

    config.ensure_dirs()
    inp = args.input
    out = args.output or str(config.OUTPUT_DIR / (Path(inp).stem + "_cut.mp4"))

    segs, duration = silence.keep_segments(inp, noise_db=args.db, min_silence=args.min)
    kept = render.total_kept(segs)
    print(f"입력 길이   : {duration:7.2f}s")
    print(f"보존 구간   : {len(segs)}개")
    print(f"보존 길이   : {kept:7.2f}s  ({kept/duration*100:4.1f}%)")
    print(f"제거 길이   : {duration-kept:7.2f}s  ({(duration-kept)/duration*100:4.1f}%)")
    for i, s in enumerate(segs):
        print(f"  [{i:02d}] {s.start:7.2f} → {s.end:7.2f}  ({s.dur:5.2f}s)")

    render.render_jumpcut(inp, segs, out, normalize=args.normalize)
    print(f"\n✓ 추출 완료: {out}")


if __name__ == "__main__":
    main()
