"""실편집 스모크 테스트 — 업로드된 영상의 편집→추출→프리뷰→영속성 루프 검증.

pytest 불필요. venv 파이썬으로 직접 실행:

    .venv/bin/python -m tests.smoke

whisper(대본) 단계는 모델 다운로드가 필요해 여기선 건너뛴다. 대신 무음감지로
보존구간을 만들어 실제 ffmpeg 렌더(점프컷·오디오 페이드·루드니스·자막 번인·
프록시 다운스케일)와 JOBS 디스크 영속화를 끝까지 검증하고, 산출 MP4를 ffprobe로
디코딩 가능 여부까지 확인한다. 하나라도 실패하면 비0 종료코드로 끝난다.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from app import config, pipeline, render, silence

SAMPLE = config.BASE_DIR / "samples" / "kr_talk.mp4"
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILURES.append(name)


def probe(path: str) -> dict:
    """ffprobe → {duration, vcodec, acodec, height}. 디코딩 불가면 예외."""
    out = subprocess.run(
        [config.FFPROBE, "-v", "error",
         "-show_entries", "stream=codec_type,codec_name,height",
         "-show_entries", "format=duration", "-of", "default=nw=1", path],
        capture_output=True, text=True, check=True).stdout
    info: dict = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k == "codec_type":
            info.setdefault("types", []).append(v)
        elif k == "codec_name":
            info.setdefault("codecs", []).append(v)
        elif k == "height" and v.isdigit():
            info["height"] = int(v)
        elif k == "duration":
            info["duration"] = float(v)
    # 디코딩 가능 여부 (에러 출력 없어야 함)
    dec = subprocess.run([config.FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"],
                         capture_output=True, text=True)
    info["decodes"] = dec.returncode == 0 and not dec.stderr.strip()
    return info


def main() -> int:
    config.ensure_dirs()
    if not SAMPLE.exists():
        print(f"샘플 없음: {SAMPLE}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="smoke_"))
    print(f"샘플: {SAMPLE.name}  작업폴더: {tmp}")

    # --- 1) 무음감지로 보존구간 산출 ---
    print("\n[1] 무음감지 → 보존구간")
    segs, duration = silence.keep_segments(str(SAMPLE))
    ranges = [(s.start, s.end) for s in segs]
    total_kept = render.total_kept(segs)
    check("보존구간 1개 이상", len(segs) >= 1, f"{len(segs)}개")
    check("보존 길이 < 원본", 0 < total_kept <= duration + 0.1,
          f"{total_kept:.2f}/{duration:.2f}s")

    # --- 2) 점프컷 추출 (자막 없이) ---
    print("\n[2] 점프컷 MP4 추출 (자막 OFF)")
    out1 = str(tmp / "cut.mp4")
    pct = []
    pipeline.export_mode_a(str(SAMPLE), ranges, out1, subtitles=False,
                           progress=lambda p: pct.append(p))
    p1 = probe(out1)
    check("MP4 생성", Path(out1).exists())
    check("디코딩 OK", p1.get("decodes", False))
    check("비디오+오디오 스트림", set(p1.get("types", [])) >= {"video", "audio"},
          ",".join(p1.get("types", [])))
    check("길이 ≈ 보존 합", abs(p1.get("duration", 0) - total_kept) < 0.6,
          f"{p1.get('duration', 0):.2f} vs {total_kept:.2f}")
    check("진행률 1.0 도달", pct and abs(pct[-1] - 1.0) < 1e-6)

    # --- 3) 점프컷 + 자막 번인 (수정된 cue 경로) ---
    print("\n[3] 점프컷 + 자막 번인 (cue remap)")
    out2 = str(tmp / "cut_sub.mp4")
    cues = [{"start": ranges[0][0], "end": ranges[0][0] + 0.8, "text": "테스트 자막"}]
    pipeline.export_mode_a(str(SAMPLE), ranges, out2, subtitles=True, cues=cues)
    p2 = probe(out2)
    check("자막 MP4 디코딩 OK", p2.get("decodes", False))
    check("길이 유지", abs(p2.get("duration", 0) - total_kept) < 0.6,
          f"{p2.get('duration', 0):.2f}")

    # --- 4) 480p 프록시 프리뷰 ---
    print("\n[4] 프리뷰 프록시 (480p)")
    out3 = str(tmp / "preview.mp4")
    pipeline.preview_mode_a(str(SAMPLE), ranges, out3)
    p3 = probe(out3)
    check("프록시 디코딩 OK", p3.get("decodes", False))
    check("세로 해상도 480", p3.get("height") == 480, str(p3.get("height")))

    # --- 5) JOBS 디스크 영속성 라운드트립 ---
    print("\n[5] 상태 영속성 (save→load 라운드트립)")
    from app import main as webmain
    saved = dict(webmain.JOBS)            # 기존 보존
    try:
        webmain.JOBS.clear()
        webmain.JOBS["job_a"] = {"mode": "a", "path": str(SAMPLE),
                                 "filename": "kr_talk.mp4", "result": {"duration": duration}}
        webmain.JOBS["job_b"] = {"mode": "b",
                                 "scenes": [pipeline.Scene(text="안녕", image="x.png")],
                                 "out": "y.mp4"}
        webmain.JOBS["job_gone"] = {"mode": "a", "path": "/nope/missing.mp4"}
        webmain.save_jobs()
        webmain.JOBS.clear()
        webmain.load_jobs()
        check("모드A 작업 복원", webmain.JOBS.get("job_a", {}).get("path") == str(SAMPLE))
        check("결과(result) 복원", webmain.JOBS.get("job_a", {}).get("result", {}).get("duration") == duration)
        b = webmain.JOBS.get("job_b", {})
        check("모드B Scene 복원", b.get("scenes") and isinstance(b["scenes"][0], pipeline.Scene)
              and b["scenes"][0].text == "안녕")
        check("원본 사라진 작업 제외", "job_gone" not in webmain.JOBS)
    finally:
        webmain.JOBS.clear()
        webmain.JOBS.update(saved)
        webmain.save_jobs()               # 실제 상태 원복

    # --- 결과 ---
    print("\n" + "=" * 48)
    if FAILURES:
        print(f"실패 {len(FAILURES)}건: {', '.join(FAILURES)}")
        return 1
    print("전체 통과 ✓ — 편집→추출→프리뷰→영속성 루프 정상")
    return 0


if __name__ == "__main__":
    sys.exit(main())
