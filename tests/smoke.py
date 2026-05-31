"""실편집 스모크 테스트 — 업로드된 영상의 편집→추출→프리뷰→영속성 루프 검증.

pytest 불필요. venv 파이썬으로 직접 실행:

    .venv/bin/python -m tests.smoke

whisper(대본) 단계는 모델 다운로드가 필요해 여기선 건너뛴다. 대신 무음감지로
보존구간을 만들어 실제 ffmpeg 렌더(점프컷·오디오 페이드·루드니스·자막 번인·
프록시 다운스케일)와 JOBS 디스크 영속화를 끝까지 검증하고, 산출 MP4를 ffprobe로
디코딩 가능 여부까지 확인한다. 하나라도 실패하면 비0 종료코드로 끝난다.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from app import config, pipeline, render, silence, subtitle, thumbnails

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
         "-show_entries", "stream=codec_type,codec_name,width,height",
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
        elif k == "width" and v.isdigit():
            info["width"] = int(v)
        elif k == "height" and v.isdigit():
            info["height"] = int(v)
        elif k == "duration":
            info["duration"] = float(v)
    # 디코딩 가능 여부 (에러 출력 없어야 함)
    dec = subprocess.run([config.FFMPEG, "-v", "error", "-i", path, "-f", "null", "-"],
                         capture_output=True, text=True)
    info["decodes"] = dec.returncode == 0 and not dec.stderr.strip()
    return info


def mean_volume(path: str) -> float:
    """평균 볼륨(dB) — 오디오가 무음이 아닌지 확인용. 실패 시 -999."""
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-i", path, "-af",
                        "volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    m = re.search(r"mean_volume:\s*([-0-9.]+) dB", r.stderr)
    return float(m.group(1)) if m else -999.0


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

    # --- 5) 클립 재정렬(역순) 렌더 ---
    print("\n[5] 클립 재정렬 (역순)")
    rev = [{"srcIn": s.start, "srcEnd": s.end} for s in reversed(segs)]
    out_rev = str(tmp / "reorder.mp4")
    pipeline.export_project(str(SAMPLE), rev, out_rev, subtitles=False)
    pr = probe(out_rev)
    check("재정렬 디코딩 OK", pr.get("decodes", False))
    check("재정렬 길이 = 보존합", abs(pr.get("duration", 0) - total_kept) < 0.6,
          f"{pr.get('duration', 0):.2f}")
    check("재정렬 오디오 비무음", mean_volume(out_rev) > -40, f"{mean_volume(out_rev):.1f}dB")

    # --- 6) 트랜지션(dissolve) 렌더 ---
    print("\n[6] 트랜지션 (dissolve 0.5s)")
    trans = [{"srcIn": s.start, "srcEnd": s.end} for s in segs]
    for c in trans[1:]:
        c["transition"] = {"type": "dissolve", "dur": 0.5}
    _, tl_total = render.clip_layout(trans)
    out_tr = str(tmp / "trans.mp4")
    pipeline.export_project(str(SAMPLE), trans, out_tr, subtitles=False)
    pt = probe(out_tr)
    check("트랜지션 디코딩 OK", pt.get("decodes", False))
    check("트랜지션 길이 = 겹침반영", abs(pt.get("duration", 0) - tl_total) < 0.6,
          f"{pt.get('duration', 0):.2f} vs {tl_total:.2f}")
    check("트랜지션 < 하드컷", tl_total < total_kept - 0.5,
          f"{tl_total:.2f} < {total_kept:.2f}")
    check("트랜지션 오디오 비무음", mean_volume(out_tr) > -40, f"{mean_volume(out_tr):.1f}dB")

    # --- 7) 자막 스타일 + 클립-aware remap ---
    print("\n[7] 자막 스타일 (상단/박스/노랑) + 트랜지션")
    style = {"fontSize": 60, "color": "#FFEE00", "align": "top", "box": True}
    cues_t = [{"start": segs[0].start, "end": segs[0].start + 0.8, "text": "스타일 자막"}]
    out_st = str(tmp / "styled.mp4")
    pipeline.export_project(str(SAMPLE), trans, out_st, subtitles=True,
                            cues=cues_t, style=style)
    ps = probe(out_st)
    check("스타일 자막 디코딩 OK", ps.get("decodes", False))
    ass_txt = Path(out_st).with_suffix(".ass").read_text(encoding="utf-8")
    sline = next((l for l in ass_txt.splitlines() if l.startswith("Style:")), "")
    check("ASS 노랑(&H0000EEFF)", "&H0000EEFF" in sline, sline[:60])
    check("ASS 상단정렬(Align=8)", ",8,60,60," in sline)
    # 클립-aware remap 단위검증 (트랜지션 겹침 → 출력 시각)
    lay, _ = render.clip_layout([{"srcIn": 0, "srcEnd": 2},
                                 {"srcIn": 5, "srcEnd": 8,
                                  "transition": {"type": "dissolve", "dur": 0.5}}])
    rm = subtitle.remap_cues_clips([subtitle.Cue(5.2, 5.8, "x")], lay)
    check("remap 트랜지션 겹침 시각", rm and abs(rm[0].start - 1.7) < 0.01,
          f"{rm[0].start:.2f}" if rm else "none")

    # --- 8) 썸네일 스프라이트 ---
    print("\n[8] 썸네일 스프라이트")
    sp = thumbnails.sprite(str(SAMPLE), 60)
    psp = probe(sp)
    check("스프라이트 생성", Path(sp).exists())
    check("스프라이트 폭=60칸", psp.get("width") == 60 * thumbnails.THUMB_W,
          str(psp.get("width")))

    # --- 9) AI 자동편집: 의도적 무음 구분 ---
    print("\n[9] 무음 분류 (의도적 쉼 vs 데드에어)")
    sil = [(1.0, 2.0), (3.0, 3.5), (5.0, 7.0)]
    segs_cls = [{"end": 1.0, "text": "안녕하세요."}, {"end": 3.0, "text": "이것은 테스트"},
                {"end": 5.0, "text": "끝입니다."}]
    scuts = silence.classify_silence_cuts(sil, segs_cls, 8.0, keep_pause=0.5)
    check("문장끝 쉼 보존+초과만 컷",
          any(c["reason"] == "긴 쉼" and abs(c["start"] - 1.5) < 0.01 for c in scuts))
    check("문장중간 데드에어 컷", any(c["reason"] == "무음" for c in scuts))
    # 잔말+무음 병합
    from app import filler
    merged = filler.merge_cuts([{"start": 1.0, "end": 1.5, "reason": "무음", "text": ""},
                                {"start": 1.52, "end": 1.7, "reason": "잔말", "text": "음"}])
    check("인접 컷 병합+이유결합", len(merged) == 1 and "무음" in merged[0]["reason"]
          and "잔말" in merged[0]["reason"])

    # --- 10) 자막 글꼴 + 자유 위치 ASS ---
    print("\n[10] 자막 글꼴·자유위치")
    fp = str(tmp / "fp.ass")
    subtitle.write_ass([subtitle.Cue(0, 1, "x")], fp, play_w=1280, play_h=720,
                       **subtitle.style_to_kwargs({"font": "Black Han Sans",
                                                   "posX": 0.5, "posY": 0.8, "shadow": 2}))
    ass = Path(fp).read_text(encoding="utf-8")
    check("글꼴 반영", "Black Han Sans" in ass)
    check("자유위치 \\an5\\pos", "\\an5\\pos(640,576)" in ass)

    # --- 11) 이미지 오버레이(로고) 합성 ---
    print("\n[11] 오버레이(로고) 합성 + 구간")
    logo = str(SAMPLE.parent / "img_0.png")
    out_ov = str(tmp / "overlay.mp4")
    ov = [{"path": logo, "x": 0.85, "y": 0.12, "scale": 0.18, "opacity": 0.9},
          {"path": logo, "x": 0.2, "y": 0.8, "scale": 0.25, "opacity": 1.0,
           "start": 1.0, "end": 3.0}]
    pipeline.export_project(str(SAMPLE), [{"srcIn": 0, "srcEnd": 5}], out_ov,
                            subtitles=False, overlays=ov)
    pov = probe(out_ov)
    check("오버레이 합성 디코딩 OK", pov.get("decodes", False))
    check("오버레이 비디오+오디오 유지", set(pov.get("types", [])) >= {"video", "audio"})

    # --- 12) 효과음(SFX) + 오버레이 페이드 + 내장 프리셋 ---
    print("\n[12] 효과음 믹스 + 페이드 + 내장 프리셋")
    from app import assets
    assets.ensure_assets()
    pr = assets.presets()
    check("내장 버튼 프리셋 존재", len(pr["buttons"]) >= 1)
    check("내장 효과음 프리셋 존재", len(pr["sounds"]) >= 1)
    click = assets.preset_path("sfx_click")
    btn = assets.preset_path("btn_subscribe")
    check("프리셋 경로 해석", bool(click) and bool(btn) and Path(click).exists())
    out_sx = str(tmp / "sfx.mp4")
    ov = [{"path": btn, "x": 0.85, "y": 0.85, "scale": 0.22, "opacity": 1.0,
           "start": 1.0, "end": 3.0, "fade": 0.4}]
    sx = [{"path": click, "at": 1.0, "volume": 1.0},
          {"path": assets.preset_path("sfx_ding"), "at": 3.0, "volume": 0.8}]
    pipeline.export_project(str(SAMPLE), [{"srcIn": 0, "srcEnd": 5}], out_sx,
                            subtitles=False, overlays=ov, sfx=sx)
    psx = probe(out_sx)
    check("SFX+오버레이 합성 디코딩 OK", psx.get("decodes", False))
    check("비디오+오디오 유지", set(psx.get("types", [])) >= {"video", "audio"})
    # SFX만(오버레이 없이) 도 동작
    out_s2 = str(tmp / "sfxonly.mp4")
    pipeline.export_project(str(SAMPLE), [{"srcIn": 0, "srcEnd": 4}], out_s2,
                            subtitles=False, sfx=[{"path": click, "at": 0.5, "volume": 1.0}])
    check("SFX 단독 합성 OK", probe(out_s2).get("decodes", False))

    # --- 13) JOBS 디스크 영속성 라운드트립 ---
    print("\n[13] 상태 영속성 (save→load 라운드트립)")
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
