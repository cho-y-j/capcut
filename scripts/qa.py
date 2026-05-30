"""전 모드 통합 QA — 실제 산출물 생성·검증."""
import asyncio
import glob
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, pipeline, draft, subtitle  # noqa: E402

config.ensure_dirs()
WORK = config.BASE_DIR / "samples"
WORK.mkdir(exist_ok=True)
NANUM = (glob.glob("/usr/share/fonts/truetype/nanum/NanumGothic.ttf")
         or glob.glob("/usr/share/fonts/**/NanumGothic*.ttf", recursive=True))[0]


def probe_dur(p):
    o = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries",
                        "format=duration", "-of",
                        "default=noprint_wrappers=1:nokey=1", p],
                       capture_output=True, text=True)
    return float(o.stdout.strip() or 0)


def make_images():
    from PIL import Image, ImageDraw, ImageFont
    font = ImageFont.truetype(NANUM, 90)
    paths = []
    for i, (label, color) in enumerate([("가을 산", (40, 70, 40)),
                                        ("호수", (30, 50, 90)),
                                        ("도시 야경", (60, 30, 60))]):
        img = Image.new("RGB", (1280, 720), color)
        d = ImageDraw.Draw(img)
        d.text((120, 300), label, fill=(240, 240, 240), font=font)
        p = str(WORK / f"img_{i}.png")
        img.save(p)
        paths.append(p)
    return paths


async def test_mode_b():
    print("\n=== [QA] 모드 B: 이미지→내레이션 ===")
    images = make_images()
    scenes = [
        pipeline.Scene("안녕하세요, 오늘은 가을 풍경을 소개합니다.", images[0]),
        pipeline.Scene("첫 번째는 단풍이 아름다운 산입니다.", images[1]),
        pipeline.Scene("마지막은 도시의 밤 풍경입니다.", images[2]),
    ]
    out = str(config.OUTPUT_DIR / "qa_modeb.mp4")
    res = await pipeline.process_mode_b(scenes, out, w=1280, h=720, fps=30,
                                        progress=lambda *a: print("   ", *a))
    d = probe_dur(out)
    assert Path(out).exists() and d > 3, f"mode B 출력 이상: {d}"
    print(f"   ✓ 모드B MP4 {d:.1f}s → {out}")
    return out


async def make_korean_clip():
    """edge-tts 한국어 내레이션 + 색 배경 → 테스트용 토킹 클립."""
    from app import tts
    voice_mp3 = str(WORK / "kr_voice.mp3")
    text = ("안녕하세요. 음 오늘은 자동 편집 테스트입니다. "
            "그 어 이 영상에는 약간의 잔말이 들어 있습니다. "
            "끝까지 들어 주셔서 감사합니다.")
    dur, _ = await tts.synth(text, voice_mp3)
    clip = str(WORK / "kr_talk.mp4")
    subprocess.run([config.FFMPEG, "-y", "-f", "lavfi", "-i",
                    f"color=c=#101820:s=1280x720:d={dur+0.5}:r=30",
                    "-i", voice_mp3, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", clip],
                   capture_output=True, text=True, check=True)
    return clip


async def test_mode_a():
    print("\n=== [QA] 모드 A: 토킹 편집 (ASR base) ===")
    clip = await make_korean_clip()
    res = await pipeline.process_mode_a(clip, model="base",
                                        progress=lambda *a: print("   ", *a))
    print(f"   대본: {res['script'][:60]}…")
    print(f"   보존 {len(res['keep'])}구간 · 컷후보 {len(res['cuts'])}개")
    # 사용자가 모든 보존구간 채택했다고 가정해 추출
    ranges = [(k["start"], k["end"]) for k in res["keep"]]
    out = str(config.OUTPUT_DIR / "qa_modea.mp4")
    pipeline.export_mode_a(clip, ranges, out, subtitles=True, model="base")
    d = probe_dur(out)
    assert Path(out).exists() and d > 1, f"mode A 출력 이상: {d}"
    print(f"   ✓ 모드A MP4 {d:.1f}s (자막 번인) → {out}")
    # 캡컷 드래프트
    cues = subtitle.build_cues(res["segments"])
    cues = subtitle.remap_cues(cues, [pipeline.Segment(a, b) for a, b in ranges])
    ddir = draft.build_capcut(clip, ranges, "qa_draft", cues=cues,
                              out_root=str(config.OUTPUT_DIR))
    info = Path(ddir) / "draft_content.json"
    assert info.exists(), f"draft_content.json 없음: {ddir}"
    print(f"   ✓ 캡컷 드래프트 → {ddir} (draft_content.json {info.stat().st_size}B)")


async def main():
    out_b = await test_mode_b()
    await test_mode_a()
    print("\n=== [QA] 전체 통과 ✓ ===")


if __name__ == "__main__":
    asyncio.run(main())
