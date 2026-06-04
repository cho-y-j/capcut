"""FastAPI 웹 셸 — 업로드 → SSE 처리 → 미리보기/추출. 모드 A·B 공용."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Dict, List

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

from . import asr, assets, config, pipeline, thumbnails, waveform

config.ensure_dirs()
assets.ensure_assets()
app = FastAPI(title="캡컷 에이전트")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
app.mount("/out", StaticFiles(directory=str(config.OUTPUT_DIR)), name="out")

JOBS: Dict[str, dict] = {}
EXPORT: Dict[str, dict] = {}      # id -> {pct, done, url, error}
PREVIEW: Dict[str, dict] = {}     # id -> {pct, done, url, error}

JOBS_FILE = config.UPLOAD_DIR / "_jobs.json"


def _body_clips(body: dict) -> list:
    """요청 바디 → 클립 리스트. clips(순서·트랜지션) 우선, 없으면 keep 폴백."""
    clips = body.get("clips")
    if clips:
        return clips
    return [{"srcIn": float(a), "srcEnd": float(b)} for a, b in body.get("keep", [])]


def _serialize_job(job: dict) -> dict:
    """JSON 저장용 뷰 — 모드 B의 Scene 데이터클래스를 dict로."""
    j = dict(job)
    if j.get("mode") == "b" and "scenes" in j:
        j["scenes"] = [{"text": s.text, "image": s.image} for s in j["scenes"]]
    return j


def save_jobs() -> None:
    """JOBS를 디스크에 영속화 — 서버 재시작해도 편집 작업 유지."""
    try:
        data = {k: _serialize_job(v) for k, v in JOBS.items()}
        tmp = JOBS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(JOBS_FILE)
    except Exception:  # noqa: BLE001  (영속화 실패가 처리를 막아선 안 됨)
        pass


def load_jobs() -> None:
    """기동 시 디스크에서 JOBS 복원 — 원본 파일이 사라진 작업은 건너뜀."""
    if not JOBS_FILE.exists():
        return
    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    for k, v in data.items():
        if v.get("mode") == "a" and not Path(v.get("path", "")).exists():
            continue
        if v.get("mode") == "b":
            v["scenes"] = [pipeline.Scene(**s) for s in v.get("scenes", [])]
        JOBS[k] = v


load_jobs()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # no-cache: 테스트 중 브라우저가 옛 index.html을 붙잡지 않도록(자동 편집기 점프 등 방지)
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# ---------------- 모드 A ----------------
@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> JSONResponse:
    job_id = uuid.uuid4().hex[:12]
    dest = config.UPLOAD_DIR / f"{job_id}_{file.filename}"
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    JOBS[job_id] = {"mode": "a", "path": str(dest), "filename": file.filename}
    save_jobs()
    return JSONResponse({"id": job_id, "filename": file.filename})


@app.post("/api/start")
async def start(req: Request) -> JSONResponse:
    """AI 시작 화면 옵션 저장 → 이후 /api/process 가 이 옵션으로 처리."""
    body = await req.json()
    job = JOBS.get(body.get("id"))
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    job["opts"] = body.get("opts") or {}
    save_jobs()
    return JSONResponse({"ok": True})


_FORMATS = {"shorts": (1080, 1920), "square": (1080, 1080)}   # 그 외(wide)=원본 유지


def _canvas(fmt):
    return _FORMATS.get(fmt)


@app.post("/api/modeb/upload")
async def modeb_upload(script: str = Form(...),
                       images: List[UploadFile] = File(...)) -> JSONResponse:
    job_id = uuid.uuid4().hex[:12]
    lines = [ln.strip() for ln in script.splitlines() if ln.strip()]
    paths: List[str] = []
    for i, img in enumerate(images):
        dest = config.UPLOAD_DIR / f"{job_id}_img{i:03d}_{img.filename}"
        with open(dest, "wb") as f:
            while chunk := await img.read(1 << 20):
                f.write(chunk)
        paths.append(str(dest))
    # 장면 수 = min(대본줄, 이미지). 부족하면 마지막 이미지/대본 반복.
    n = max(len(lines), len(paths))
    scenes = []
    for i in range(n):
        text = lines[i] if i < len(lines) else lines[-1]
        image = paths[i] if i < len(paths) else paths[-1]
        scenes.append(pipeline.Scene(text=text, image=image))
    # 모드 B 산출 = 편집 가능한 소스(자막 미번인). 모드 A와 같은 편집기로 들어간다.
    out = str(config.UPLOAD_DIR / f"{job_id}_modeb_src.mp4")
    JOBS[job_id] = {"mode": "b", "scenes": scenes, "out": out}
    save_jobs()
    return JSONResponse({"id": job_id, "scenes": len(scenes)})


async def _sse(job_id: str):
    import asyncio
    job = JOBS.get(job_id)
    if not job:
        yield f"data: {json.dumps({'type': 'error', 'message': 'unknown job'})}\n\n"
        return
    q: "asyncio.Queue" = asyncio.Queue()

    def cb(step: str, status: str, detail: str) -> None:
        q.put_nowait({"type": "step", "step": step, "status": status, "detail": detail})

    async def run():
        try:
            if job["mode"] == "a":
                res = await pipeline.process_mode_a(job["path"], opts=job.get("opts"), progress=cb)
            else:
                res = await pipeline.process_mode_b(job["scenes"], job["out"], progress=cb)
            job["result"] = res
            if isinstance(res, dict) and res.get("path"):
                job["path"] = res["path"]          # 모드 B: 생성 소스를 편집 대상으로
            save_jobs()
            await q.put({"type": "result", "data": res})
        except Exception as e:  # noqa: BLE001
            await q.put({"type": "error", "message": str(e)})
        await q.put(None)

    asyncio.create_task(run())
    while True:
        item = await q.get()
        if item is None:
            break
        yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"


@app.get("/api/process")
async def process(id: str) -> StreamingResponse:
    return StreamingResponse(_sse(id), media_type="text/event-stream")


@app.get("/api/media")
async def media(id: str, src: str = "0"):
    """원본/추가 소스 영상·이미지 서빙. src=토큰(0=메인)."""
    job = JOBS.get(id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    if src and src != "0":
        info = (job.get("sources") or {}).get(src)
        if info and Path(info["path"]).exists():
            return FileResponse(info["path"])
    if "path" not in job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return FileResponse(job["path"])


@app.post("/api/addsource")
async def add_source(id: str = Form(...), file: UploadFile = File(...)) -> JSONResponse:
    """타임라인에 이어붙일 추가 영상/이미지 업로드 → 소스 토큰."""
    job = JOBS.get(id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    token = uuid.uuid4().hex[:8]
    dest = config.UPLOAD_DIR / f"{id}_src_{token}_{file.filename}"
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    ct = (file.content_type or "")
    ext = Path(file.filename or "").suffix.lower()
    is_img = ct.startswith("image") or ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
    info = {"path": str(dest), "kind": "image" if is_img else "video", "name": file.filename}
    if not is_img:
        try:
            import asyncio
            from .silence import probe_duration
            info["duration"] = round(await asyncio.to_thread(probe_duration, str(dest)), 2)
        except Exception:  # noqa: BLE001
            info["duration"] = 5.0
    job.setdefault("sources", {})[token] = info
    save_jobs()
    return JSONResponse({"token": token, "kind": info["kind"],
                         "duration": info.get("duration"), "name": file.filename})


@app.get("/api/waveform")
async def get_waveform(id: str) -> JSONResponse:
    """타임라인 파형(peaks) — 말/무음을 눈으로."""
    job = JOBS.get(id)
    if not job or "path" not in job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    import asyncio
    pk = await asyncio.to_thread(waveform.peaks, job["path"])
    return JSONResponse({"peaks": pk})


@app.get("/api/thumbs")
async def get_thumbs(id: str, n: int = 120):
    """타임라인 썸네일 스프라이트(JPEG) 직접 서빙 — 비디오 레인 배경용."""
    job = JOBS.get(id)
    if not job or "path" not in job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    import asyncio
    try:
        path = await asyncio.to_thread(thumbnails.sprite, job["path"], n)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/api/job")
async def get_job(id: str) -> JSONResponse:
    """처리 결과 복원(새로고침 후)."""
    job = JOBS.get(id)
    if not job or "result" not in job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return JSONResponse({"result": job["result"]})


@app.post("/api/bgm")
async def upload_bgm(id: str = Form(...), file: UploadFile = File(...)) -> JSONResponse:
    job = JOBS.get(id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    token = uuid.uuid4().hex[:8]
    dest = config.UPLOAD_DIR / f"{id}_bgm_{file.filename}"
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    job["bgm"] = str(dest)
    job.setdefault("assets", {})[token] = str(dest)   # 브라우저 라이브 미리보기용 서빙
    save_jobs()
    return JSONResponse({"ok": True, "token": token,
                         "url": f"/api/asset?id={id}&token={token}", "name": file.filename})


@app.post("/api/overlay")
async def upload_overlay(id: str = Form(...), file: UploadFile = File(...)) -> JSONResponse:
    """로고/이미지 오버레이 업로드 → 토큰 반환. 미리보기는 /api/asset로 서빙."""
    job = JOBS.get(id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    token = uuid.uuid4().hex[:8]
    dest = config.UPLOAD_DIR / f"{id}_ov_{token}_{file.filename}"
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    job.setdefault("assets", {})[token] = str(dest)
    save_jobs()
    return JSONResponse({"token": token, "url": f"/api/asset?id={id}&token={token}",
                         "name": file.filename})


@app.post("/api/sfx")
async def upload_sfx(id: str = Form(...), file: UploadFile = File(...)) -> JSONResponse:
    """효과음 업로드 → 토큰. (내장 프리셋과 함께 사용 가능)"""
    job = JOBS.get(id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    token = uuid.uuid4().hex[:8]
    dest = config.UPLOAD_DIR / f"{id}_sfx_{token}_{file.filename}"
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    job.setdefault("assets", {})[token] = str(dest)
    save_jobs()
    return JSONResponse({"token": token, "url": f"/api/asset?id={id}&token={token}",
                         "name": file.filename})


@app.post("/api/audio")
async def upload_audio(id: str = Form(...), file: UploadFile = File(...)) -> JSONResponse:
    """자유 오디오 클립(mp3 등) 업로드 → 토큰 + 길이. 특정 시간대에 깔고 길이조절."""
    job = JOBS.get(id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    token = uuid.uuid4().hex[:8]
    dest = config.UPLOAD_DIR / f"{id}_aud_{token}_{file.filename}"
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    job.setdefault("assets", {})[token] = str(dest)
    save_jobs()
    import asyncio
    from .silence import probe_duration
    try:
        dur = await asyncio.to_thread(probe_duration, str(dest))
    except Exception:  # noqa: BLE001
        dur = 0.0
    return JSONResponse({"token": token, "url": f"/api/asset?id={id}&token={token}",
                         "name": file.filename, "duration": dur})


@app.get("/api/presets")
async def get_presets() -> JSONResponse:
    """내장 버튼·효과음 프리셋 목록."""
    return JSONResponse(assets.presets())


@app.get("/api/asset")
async def get_asset(id: str, token: str):
    """업로드된 오버레이 자산 서빙 — 편집기 라이브 미리보기용."""
    job = JOBS.get(id)
    path = (job or {}).get("assets", {}).get(token)
    if not path or not Path(path).exists():
        return JSONResponse({"error": "unknown asset"}, status_code=404)
    return FileResponse(path)


def _asset_path(job: dict, spec: dict) -> str | None:
    """업로드 토큰 또는 내장 프리셋 id → 실제 파일 경로."""
    ja = job.get("assets", {})
    if spec.get("token") and ja.get(spec["token"]):
        return ja[spec["token"]]
    if spec.get("preset"):
        return assets.preset_path(spec["preset"])
    return None


def _resolve_overlays(job: dict, body: dict) -> list:
    """요청 overlays(업로드 토큰/내장 프리셋) → 파일 경로 포함 스펙."""
    out = []
    for ov in body.get("overlays") or []:
        path = _asset_path(job, ov)
        if not path:
            continue
        out.append({"path": path, "x": ov.get("x", 0.5), "y": ov.get("y", 0.1),
                    "scale": ov.get("scale", 0.2), "opacity": ov.get("opacity", 1.0),
                    "start": ov.get("start"), "end": ov.get("end"),
                    "fade": ov.get("fade", 0.0)})
    return out


def _resolve_sfx(job: dict, body: dict) -> list:
    """요청 sfx(업로드 토큰/내장 프리셋) → 경로·시각·볼륨."""
    out = []
    for sx in body.get("sfx") or []:
        path = _asset_path(job, sx)
        if not path:
            continue
        out.append({"path": path, "at": float(sx.get("at", 0.0)),
                    "volume": float(sx.get("volume", 1.0))})
    return out


def _resolve_audios(job: dict, body: dict) -> list:
    """요청 audios(업로드 토큰) → 경로·구간·길이·볼륨·페이드 (자유 오디오 클립)."""
    out = []
    for au in body.get("audios") or []:
        path = _asset_path(job, au)
        if not path:
            continue
        out.append({"path": path, "at": float(au.get("at", 0.0)),
                    "in": float(au.get("in", 0.0)),
                    "dur": float(au.get("dur", 0.0)),
                    "volume": float(au.get("volume", 1.0)),
                    "fadeIn": float(au.get("fadeIn", 0.0)),
                    "fadeOut": float(au.get("fadeOut", 0.0))})
    return out


@app.post("/api/export")
async def export(req: Request) -> JSONResponse:
    """백그라운드 추출 시작 → /api/export/status 로 진행률 폴링."""
    import asyncio
    body = await req.json()
    jid = body["id"]
    job = JOBS.get(jid)
    if not job or "path" not in job:        # 처리 완료된 모드 A·B 모두 편집 가능
        return JSONResponse({"error": "unknown job"}, status_code=404)
    clips = _body_clips(body)
    subtitles = bool(body.get("subtitles", True))
    cues = body.get("cues")
    style = body.get("style")
    bgm = job.get("bgm") if body.get("bgm") else None
    bgm_opts = body.get("bgmOpts") or {}
    overlays = _resolve_overlays(job, body)
    sfx = _resolve_sfx(job, body)
    audios = _resolve_audios(job, body)
    texts = body.get("texts") or []
    canvas = _canvas(body.get("format"))
    out = str(config.OUTPUT_DIR / f"{jid}_cut.mp4")
    EXPORT[jid] = {"pct": 0.0, "done": False, "url": None, "error": None}

    def _cb(p: float) -> None:
        EXPORT[jid]["pct"] = p

    async def _run():
        try:
            await asyncio.to_thread(pipeline.export_project, job["path"], clips, out,
                                    subtitles=subtitles, cues=cues, style=style,
                                    bgm=bgm, bgm_opts=bgm_opts, overlays=overlays,
                                    sfx=sfx, audios=audios, texts=texts, canvas=canvas,
                                    sources=job.get("sources"), progress=_cb)
            EXPORT[jid].update(pct=1.0, done=True, url=f"/out/{Path(out).name}")
        except Exception as e:  # noqa: BLE001
            EXPORT[jid].update(done=True, error=str(e))

    asyncio.create_task(_run())
    return JSONResponse({"started": True})


@app.get("/api/export/status")
async def export_status(id: str) -> JSONResponse:
    return JSONResponse(EXPORT.get(id, {"error": "no export"}))


@app.post("/api/preview")
async def preview(req: Request) -> JSONResponse:
    """확정 보존구간을 저화질 프록시로 빠르게 렌더 → 실제 컷/오디오 미리보기."""
    import asyncio
    body = await req.json()
    jid = body["id"]
    job = JOBS.get(jid)
    if not job or "path" not in job:        # 처리 완료된 모드 A·B 모두 편집 가능
        return JSONResponse({"error": "unknown job"}, status_code=404)
    clips = _body_clips(body)
    bgm = job.get("bgm") if body.get("bgm") else None
    bgm_opts = body.get("bgmOpts") or {}
    overlays = _resolve_overlays(job, body)
    sfx = _resolve_sfx(job, body)
    audios = _resolve_audios(job, body)
    canvas = _canvas(body.get("format"))
    out = str(config.OUTPUT_DIR / f"{jid}_preview.mp4")
    PREVIEW[jid] = {"pct": 0.0, "done": False, "url": None, "error": None}

    def _cb(p: float) -> None:
        PREVIEW[jid]["pct"] = p

    async def _run():
        try:
            await asyncio.to_thread(pipeline.preview_mode_a, job["path"], clips, out,
                                    bgm=bgm, bgm_opts=bgm_opts, overlays=overlays,
                                    sfx=sfx, audios=audios, canvas=canvas, sources=job.get("sources"), progress=_cb)
            PREVIEW[jid].update(pct=1.0, done=True, url=f"/out/{Path(out).name}")
        except Exception as e:  # noqa: BLE001
            PREVIEW[jid].update(done=True, error=str(e))

    asyncio.create_task(_run())
    return JSONResponse({"started": True})


@app.get("/api/preview/status")
async def preview_status(id: str) -> JSONResponse:
    return JSONResponse(PREVIEW.get(id, {"error": "no preview"}))


@app.post("/api/capcut")
async def capcut(req: Request) -> JSONResponse:
    """편집 결과를 캡컷 드래프트로 출력 (Win/Mac 핸드오프).

    v1: 클립 순서는 반영, 트랜지션은 캡컷에서 추가(드래프트 매핑 한계). 자막은
    하드컷 누적 레이아웃으로 remap(드래프트 세그먼트 배치와 일치).
    """
    from . import draft, render, subtitle
    body = await req.json()
    job = JOBS.get(body["id"])
    if not job or "path" not in job:        # 처리 완료된 모드 A·B 모두 편집 가능
        return JSONResponse({"error": "unknown job"}, status_code=404)
    clips = _body_clips(body)
    ranges = [(float(c["srcIn"]), float(c["srcEnd"])) for c in clips]   # 클립 순서대로
    hc = [{"srcIn": a, "srcEnd": b} for a, b in ranges]                 # 트랜지션 무시
    layout, _ = render.clip_layout(hc)
    cues = body.get("cues") or []
    cue_objs = [subtitle.Cue(float(c["start"]), float(c["end"]), c["text"])
                for c in cues if c.get("text", "").strip()]
    cue_objs = subtitle.remap_cues_clips(cue_objs, layout)
    import asyncio
    name = f"{body['id']}_capcut"
    ddir = await asyncio.to_thread(draft.build_capcut, job["path"], ranges, name,
                                   cues=cue_objs, out_root=str(config.OUTPUT_DIR))
    return JSONResponse({"dir": ddir})


_CURATED_FONTS = [
    ("노토 산스 (기본·가독)", "Noto Sans CJK KR"),
    ("나눔고딕", "NanumGothic"),
    ("나눔바른고딕", "NanumBarunGothic"),
    ("나눔스퀘어라운드", "NanumSquareRound"),
    ("고딕 A1", "Gothic A1"),
    ("검은고딕 (임팩트)", "Black Han Sans"),
    ("도현 (임팩트)", "Do Hyeon"),
    ("주아 (둥글둥글)", "Jua"),
    ("구기 (붓느낌)", "Gugi"),
    ("나눔손글씨 펜", "Nanum Pen Script"),
    ("개구 (손글씨)", "Gaegu"),
]


@app.get("/api/fonts")
async def fonts() -> JSONResponse:
    """설치된 한글 자막 글꼴 후보(큐레이션 ∩ 설치됨)."""
    import asyncio
    import subprocess

    def _installed() -> set:
        # :lang=ko 필터는 일부 한글 디스플레이 폰트(검은고딕 등)를 누락시켜 전체에서 매칭
        try:
            out = subprocess.run(["fc-list", "", "family"],
                                 capture_output=True, text=True).stdout
        except Exception:  # noqa: BLE001
            return set()
        names = set()
        for line in out.splitlines():
            for part in line.split(","):
                names.add(part.strip())
        return names

    inst = await asyncio.to_thread(_installed)
    avail = [{"label": lbl, "family": fam} for lbl, fam in _CURATED_FONTS
             if any(fam == n or fam in n for n in inst)]
    if not avail:
        avail = [{"label": "기본", "family": "Noto Sans CJK KR"}]
    return JSONResponse(avail)


@app.get("/api/voices")
async def voices() -> JSONResponse:
    from . import tts
    return JSONResponse(await tts.list_korean_voices())


@app.post("/api/assist")
async def assist(req: Request) -> JSONResponse:
    """자연어 요청 → 편집 액션(JSON). Claude CLI 우선, 실패 시 DeepSeek."""
    from . import llm
    import asyncio
    body = await req.json()
    msg = (body.get("message") or "").strip()
    if not msg:
        return JSONResponse({"error": "빈 요청"}, status_code=400)
    try:
        res = await asyncio.to_thread(llm.assist, msg, body.get("ctx") or {})
        return JSONResponse(res)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=503)


@app.get("/api/llm/status")
async def llm_status() -> JSONResponse:
    from . import llm
    return JSONResponse(llm.status())


@app.get("/api/admin", response_class=HTMLResponse)
async def admin() -> str:
    from . import llm
    st = llm.status()
    cli = "✅ 사용 가능" if st["claude_cli"] else "❌ 없음"
    ds = "✅ 키 등록됨" if st["deepseek"] else "❌ 미등록"
    return f"""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>ONCUT 관리자</title>
<style>body{{background:#0a0a0a;color:#ededed;font-family:ui-monospace,monospace;max-width:560px;margin:40px auto;padding:0 20px}}
h1{{font-size:16px}}.card{{background:#161616;border:1px solid #262626;border-radius:12px;padding:20px;margin-top:16px}}
input{{width:100%;background:#0e0e0e;border:1px solid #262626;color:#ededed;border-radius:8px;padding:10px;font-family:inherit;margin:6px 0}}
button{{background:#22c55e;color:#04130a;border:0;border-radius:8px;padding:10px 18px;font-weight:600;cursor:pointer}}
.s{{color:#8a8a8a;font-size:12px}}.ok{{color:#22c55e}}</style></head><body>
<h1>ONCUT 관리자 — AI 대화형 편집 설정</h1>
<div class=card>
<p class=s>대화형 편집은 <b>Claude CLI</b>를 먼저 쓰고, 만료·실패 시 <b>DeepSeek</b>로 자동 전환합니다.</p>
<p>Claude CLI: <b>{cli}</b><br>DeepSeek: <b>{ds}</b></p>
</div>
<div class=card>
<p>DeepSeek API 키 (폴백용)</p>
<input id=ds type=password placeholder="sk-...">
<button onclick="save()">저장</button> <span id=msg class=s></span>
<p class=s style=margin-top:10px>키는 서버 config/keys.json에 저장(깃 비추적). 입력 후 Claude CLI가 안 되면 자동으로 DeepSeek 사용.</p>
</div>
<script>
async function save(){{const k=document.getElementById('ds').value.trim();if(!k)return;
const r=await fetch('/api/admin/keys',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{deepseek:k}})}});
document.getElementById('msg').textContent=r.ok?'저장됨 ✓':'실패';document.getElementById('msg').className='ok';}}
</script></body></html>"""


@app.post("/api/admin/keys")
async def admin_keys(req: Request) -> JSONResponse:
    from . import llm
    body = await req.json()
    llm.save_keys({"deepseek": (body.get("deepseek") or "").strip()})
    return JSONResponse({"ok": True})


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "version": "0.1.0"}
