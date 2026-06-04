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
AUTOCUT: Dict[str, dict] = {}     # id -> {pct, done, res, extras, error}
AUTOCUT_PLAN: Dict[str, dict] = {}  # id -> {fmt, media, plan} (확정 전 임시 보관)

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


_AC_DIMS = {"shorts": (1080, 1920), "square": (1080, 1080), "wide": (1920, 1080)}


def _palette(path: str, n: int = 4):
    """이미지 주요색 n개 추출(팔레트). + 따뜻함 추정. (내용분석 아님, 색감만)"""
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB").resize((96, 96))
        q = im.quantize(colors=max(2, n)).convert("RGB")
        cols = sorted(q.getcolors(96 * 96) or [], reverse=True)[:n]
        rgb = [c[1] for c in cols]
        hexes = ["#%02x%02x%02x" % c for c in rgb]
        if rgb:
            warm = sum(c[0] - c[2] for c in rgb) / len(rgb) / 255.0   # R-B 평균
        else:
            warm = 0.0
        return hexes, max(-0.5, min(0.5, round(warm, 3)))
    except Exception:  # noqa: BLE001
        return [], 0.0


@app.post("/api/autocut/plan")
async def autocut_plan(goal: str = Form("shorts"), request: str = Form(""),
                       template: str = Form(""), ref_url: str = Form(""), duration: str = Form("0"),
                       files: List[UploadFile] = File(...),
                       ref_image: UploadFile | None = File(None)) -> JSONResponse:
    """1단계 — 소재 업로드 + AI 구성안 생성(렌더 X). 대본/장면을 돌려줘 사용자가 확정."""
    import asyncio
    from . import llm
    jid = uuid.uuid4().hex[:12]
    fmt = goal if goal in _AC_DIMS else "shorts"
    saved = []
    for i, f in enumerate(files):
        ext = Path(f.filename or "").suffix.lower()
        is_img = ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic"}
        dest = config.UPLOAD_DIR / f"{jid}_ac{i:03d}_{f.filename}"
        with open(dest, "wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
        saved.append({"path": str(dest), "kind": "image" if is_img else "video",
                      "name": f.filename})
    if not saved:
        return JSONResponse({"error": "소재가 없습니다"}, status_code=400)
    try:
        tsec = float(duration or 0)
    except ValueError:
        tsec = 0
    # 참고 이미지 → 색감 팔레트 추출(내용분석 아님). 톤 힌트 + warmth 넛지.
    palette, ref_warm, ref_tone = [], 0.0, ""
    if ref_image is not None and ref_image.filename:
        rp = config.UPLOAD_DIR / f"{jid}_ref_{ref_image.filename}"
        with open(rp, "wb") as f:
            while chunk := await ref_image.read(1 << 20):
                f.write(chunk)
        palette, ref_warm = await asyncio.to_thread(_palette, str(rp))
        if palette:
            ref_tone = f"참고 이미지 주요색 {', '.join(palette)} ({'따뜻한' if ref_warm > 0.04 else '차분한' if ref_warm < -0.04 else '중간'} 톤)"
    ref_arg = (ref_url + (" / " + ref_tone if ref_tone else "")).strip(" /")
    plan = await asyncio.to_thread(llm.plan_project, fmt, fmt, saved, request, template, ref_arg, tsec)
    AUTOCUT_PLAN[jid] = {"fmt": fmt, "media": saved, "plan": plan, "template": template,
                         "ref_warm": ref_warm, "palette": palette}
    return JSONResponse({"id": jid, "plan": plan, "template": template, "palette": palette,
                         "media": [{"kind": m["kind"], "name": m["name"]} for m in saved]})


@app.post("/api/autocut/render")
async def autocut_render(req: Request) -> JSONResponse:
    """2단계 — 사용자가 확정/편집한 구성안으로 1차 완성 렌더(→ 편집기 핸드오프)."""
    import asyncio
    body = await req.json()
    jid = body.get("id")
    st = AUTOCUT_PLAN.get(jid)
    if not st:
        return JSONResponse({"error": "계획이 만료됐어요. 다시 시작해 주세요."}, status_code=404)
    plan = dict(st["plan"])
    if isinstance(body.get("scenes"), list):              # 사용자가 편집한 대본 반영
        plan["scenes"] = body["scenes"]
    if body.get("hook") is not None:
        plan["hook"] = body["hook"]
    if isinstance(body.get("grade"), dict):
        plan["grade"] = body["grade"]
    music = bool(body.get("music", plan.get("music", True)))
    voice = body.get("voice") or None
    sp = int(body.get("speed", 0) or 0)
    rate = f"+{sp}%" if sp >= 0 else f"{sp}%"
    from . import templates as _tpl
    tid = body.get("template") or st.get("template") or ""
    tp = _tpl.apply(tid, load_brandkit())               # 템플릿+브랜드 → 빌드 파라미터
    rw = float(st.get("ref_warm") or 0)                 # 참고 이미지 색감 → warmth 넛지
    if rw and tp.get("grade"):
        tp["grade"]["warmth"] = max(-1.0, min(1.0, float(tp["grade"].get("warmth", 0)) + rw))
    AUTOCUT[jid] = {"pct": 0.0, "done": False, "res": None, "extras": None, "error": None}

    def _cb(p):
        AUTOCUT[jid]["pct"] = max(AUTOCUT[jid]["pct"], min(0.99, p))

    async def _run():
        try:
            res, extras = await asyncio.to_thread(_build_autocut, jid, st["fmt"], st["media"],
                                                  plan, music, voice, _cb, tp, rate)
            if jid in JOBS:
                JOBS[jid]["result"] = res        # /api/job 복원용(뒤로가기/새로고침 시 살아남음)
            AUTOCUT[jid].update(pct=1.0, done=True, res=res, extras=extras)
            save_jobs()
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            AUTOCUT[jid].update(done=True, error=str(e))

    asyncio.create_task(_run())
    return JSONResponse({"id": jid})


@app.get("/api/autocut/status")
async def autocut_status(id: str) -> JSONResponse:
    st = AUTOCUT.get(id) or {"error": "no job"}
    return JSONResponse(st)


def _build_autocut(jid, fmt, media, plan, music, voice, cb, tp=None, rate="+0%"):
    """동기 빌드 — 확정된 구성안 + 템플릿(tp) → 이미지=모드B / 영상·혼합 → (res, extras)."""
    from . import render
    from .silence import probe_video, probe_duration
    import asyncio
    cb(0.05)
    tp = tp or {}
    scenes = plan["scenes"]
    grade = tp.get("grade") or plan.get("grade") or {}     # 템플릿 룩 우선
    tdur = float(tp.get("dur", 3.0))
    ttrans = tp.get("transition", "dissolve")
    tcolor = tp.get("textColor", "#ffffff")
    tanim = tp.get("textAnim", "pop")
    w, h = _AC_DIMS.get(fmt, (1080, 1920))
    has_video = any(m["kind"] == "video" for m in media)
    hook = (plan.get("hook") or "").strip()

    if not has_video:
        # 이미지 전용 → 모드 B(내레이션 슬라이드쇼) 재사용
        sc = [pipeline.Scene(text=(scenes[i]["text"] or " "), image=media[i]["path"])
              for i in range(len(media))]
        out = str(config.UPLOAD_DIR / f"{jid}_autocut.mp4")
        res = asyncio.run(pipeline.process_mode_b(sc, out, voice=voice, rate=rate, w=w, h=h, fps=30,
                                                  progress=lambda *a: cb(0.1 + 0.8 * 0.5)))
        JOBS[jid] = {"mode": "b", "path": out, "filename": "AI 첫 컷"}
        cb(0.95)
    else:
        # 영상/혼합 → 첫 영상=메인('0'), 나머지=소스. AI 순서대로 클립.
        vids = [m for m in media if m["kind"] == "video"]
        mainm = vids[0]
        try:
            mw, mh, mfps = probe_video(mainm["path"])
        except Exception:  # noqa: BLE001
            mw, mh, mfps = 1280, 720, 30.0
        sources, srcMeta = {}, {}
        token_of = {}
        for m in media:
            if m is mainm:
                token_of[id(m)] = "0"
                continue
            tok = uuid.uuid4().hex[:8]
            dur = round(probe_duration(m["path"]), 2) if m["kind"] == "video" else None
            sources[tok] = {"path": m["path"], "kind": m["kind"], "name": m["name"], "duration": dur}
            srcMeta[tok] = {"kind": m["kind"], "url": f"/api/media?id={jid}&src={tok}",
                            "name": m["name"], "duration": dur}
            token_of[id(m)] = tok
        clips, cid = [], 0
        for i, m in enumerate(media):
            tok = token_of[id(m)]
            if m["kind"] == "video":
                d = probe_duration(m["path"]) if tok == "0" else sources[tok]["duration"]
                length = max(1.5, min(float(d or 4), float(scenes[i].get("dur") or tdur)))
                clip = {"src": tok, "srcIn": 0.0, "srcEnd": length}
            else:
                clip = {"src": tok, "srcIn": 0.0, "srcEnd": float(scenes[i].get("dur") or tdur)}
            clip["transition"] = {"type": "none" if i == 0 else ttrans, "dur": 0.5}
            clips.append(clip)
        JOBS[jid] = {"mode": "a", "path": mainm["path"], "filename": "AI 첫 컷", "sources": sources}
        starts, total, _ = render.output_layout(clips)
        # 장면 자막 → 텍스트박스(클립 출력 구간에 표시)
        texts = []
        for i, c in enumerate(clips):
            t = (scenes[i].get("text") or "").strip()
            if not t:
                continue
            s0 = starts[i]; e0 = s0 + (c["srcEnd"] - c["srcIn"])
            texts.append({"text": t, "x": 0.5, "y": 0.82, "fontSize": 64 if fmt == "shorts" else 52,
                          "color": tcolor, "outlineColor": "#000000", "outlineW": 4, "bold": True,
                          "font": "Black Han Sans", "start": round(s0, 2), "end": round(e0, 2),
                          "anim": tanim, "opacity": 1})
        ovs = _autocut_decor(jid, tp, hook, total, w, h, fmt, texts)   # 훅+아웃트로 CTA+로고
        clip_objs = [{"srcIn": c["srcIn"], "srcEnd": c["srcEnd"], "src": c["src"],
                      "transition": c["transition"]} for c in clips]
        res = {"mode": "a", "duration": total, "w": mw, "h": mh, "fps": mfps,
               "clips": clip_objs, "cuts": [], "cues": []}
        # clips를 extras(restore.clips)로도 — initEditor가 res.clips의 src를 누락하므로 src 보존
        extras = {"format": fmt, "grade": grade, "texts": texts, "overlays": ovs,
                  "srcMeta": srcMeta, "clips": clip_objs, "layout": tp.get("layout")}
        cb(0.95)
        return res, extras

    # 이미지 경로 res/extras — 자막(내레이션) 스타일은 템플릿 sub, + 훅/아웃트로/로고
    texts = []
    total_img = float(res.get("duration", 0) or 0)
    ovs = _autocut_decor(jid, tp, hook, total_img, w, h, fmt, texts)
    extras = {"format": fmt, "grade": grade, "texts": texts, "overlays": ovs,
              "style": tp.get("sub"), "layout": tp.get("layout"), "srcMeta": {}}
    return res, extras


def _autocut_decor(jid, tp, hook, total, w, h, fmt, texts):
    """훅(도입 큰 글자) + 아웃트로 CTA + 브랜드 로고 오버레이를 texts/overlays에 추가."""
    tp = tp or {}
    brand = tp.get("brand") or {}
    big = 80 if fmt == "shorts" else 60
    if hook:
        texts.append({"text": hook, "x": 0.5, "y": 0.18, "fontSize": big, "color": tp.get("textColor", "#ffffff"),
                      "outlineColor": "#000000", "outlineW": 4, "bold": True, "font": "Black Han Sans",
                      "start": 0.0, "end": 2.5, "anim": "pop", "opacity": 1})
    if tp.get("outro") and total > 2.2:
        cta = (tp.get("cta") or "").strip()
        name = (brand.get("name") or "").strip()
        label = (name + ("  ·  " + cta if cta else "")) if name else cta
        if label:
            texts.append({"text": label, "x": 0.5, "y": 0.5, "fontSize": big, "color": tp.get("textColor", "#ffffff"),
                          "outlineColor": "#000000", "outlineW": 4, "bold": True, "font": "Black Han Sans",
                          "start": round(total - 2.2, 2), "end": round(total, 2), "anim": "pop", "opacity": 1})
    ovs = []
    logo = brand.get("logo")
    if logo and Path(logo).exists():           # 브랜드 로고 → 우상단 상시 오버레이
        tok = uuid.uuid4().hex[:8]
        JOBS.setdefault(jid, {}).setdefault("assets", {})[tok] = logo
        ovs.append({"token": tok, "url": f"/api/asset?id={jid}&token={tok}", "name": "logo",
                    "x": 0.87, "y": 0.12, "scale": 0.16, "opacity": 1, "start": 0, "end": total, "fade": 0.3, "kf": []})
    return ovs


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


# ===== 드래프트(내 작업) 라이브러리 — 서버 영속, 어디서나 이어서 =====
_DRAFTS_DIR = config.UPLOAD_DIR / "drafts"


@app.post("/api/draft/save")
async def draft_save(req: Request) -> JSONResponse:
    """편집기 상태(state)를 job id 기준으로 1개 보관. 자동저장+수동저장 공용."""
    body = await req.json()
    jid = body.get("id")
    if not jid or jid not in JOBS:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    _DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    import time
    rec = {"id": jid, "name": (body.get("name") or "무제 작업").strip()[:60],
           "savedAt": int(time.time()), "state": body.get("state") or {}}
    (_DRAFTS_DIR / f"{jid}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return JSONResponse({"ok": True, "id": jid})


@app.get("/api/draft/list")
async def draft_list() -> JSONResponse:
    """최근 작업 목록(최신순). 미디어(job)가 아직 살아있는 것만."""
    if not _DRAFTS_DIR.exists():
        return JSONResponse([])
    out = []
    for p in _DRAFTS_DIR.glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            if r.get("id") in JOBS:                    # 미디어 사라진 작업은 숨김
                out.append({"id": r["id"], "name": r.get("name", "무제"), "savedAt": r.get("savedAt", 0)})
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda x: x["savedAt"], reverse=True)
    return JSONResponse(out[:30])


@app.get("/api/draft/get")
async def draft_get(id: str) -> JSONResponse:
    p = _DRAFTS_DIR / f"{id}.json"
    if not p.exists():
        return JSONResponse({"error": "no draft"}, status_code=404)
    return JSONResponse(json.loads(p.read_text(encoding="utf-8")))


@app.post("/api/draft/delete")
async def draft_delete(req: Request) -> JSONResponse:
    body = await req.json()
    p = _DRAFTS_DIR / f"{body.get('id')}.json"
    p.unlink(missing_ok=True)
    return JSONResponse({"ok": True})


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


@app.post("/api/shape")
async def make_shape(req: Request) -> JSONResponse:
    """도형 PNG 생성 → 오버레이용 토큰. kind/color/stroke/opacity/radius."""
    from . import shapes
    import asyncio
    body = await req.json()
    job = JOBS.get(body.get("id"))
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    kind = (body.get("kind") or "round")
    token = uuid.uuid4().hex[:8]
    dest = config.UPLOAD_DIR / f"{body['id']}_shp_{token}_{kind}.png"
    try:
        await asyncio.to_thread(shapes.make_shape, kind, str(dest),
                                color=body.get("color", "#ff3d8b"), stroke=body.get("stroke", ""),
                                stroke_w=float(body.get("strokeW", 0) or 0),
                                opacity=float(body.get("opacity", 1.0)),
                                radius=float(body.get("radius", 0.25)),
                                w=int(body.get("w", 400)), h=int(body.get("h", 400)))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    job.setdefault("assets", {})[token] = str(dest)
    save_jobs()
    return JSONResponse({"token": token, "url": f"/api/asset?id={body['id']}&token={token}", "name": kind})


@app.post("/api/emoji")
async def make_emoji(req: Request) -> JSONResponse:
    """이모지 문자 → 컬러 PNG 토큰(오버레이용)."""
    from . import shapes
    import asyncio
    body = await req.json()
    job = JOBS.get(body.get("id"))
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    ch = (body.get("ch") or "😀")
    token = uuid.uuid4().hex[:8]
    dest = config.UPLOAD_DIR / f"{body['id']}_emo_{token}.png"
    try:
        await asyncio.to_thread(shapes.make_emoji, ch, str(dest))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    job.setdefault("assets", {})[token] = str(dest)
    save_jobs()
    return JSONResponse({"token": token, "url": f"/api/asset?id={body['id']}&token={token}", "name": ch})


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
                    "fade": ov.get("fade", 0.0), "kf": ov.get("kf") or []})
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


def _has_audio(path: str) -> bool:
    import subprocess
    try:
        r = subprocess.run([config.FFPROBE, "-v", "error", "-select_streams", "a",
                            "-show_entries", "stream=index", "-of", "csv=p=0", path],
                           capture_output=True, text=True)
        return bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def _resolve_pips(job: dict, body: dict) -> list:
    """요청 pips(소스 토큰=addsource 영상) → 경로·위치·크기·구간·트림·볼륨 (영상 위 영상)."""
    srcs = job.get("sources") or {}
    out = []
    for pp in body.get("pips") or []:
        info = srcs.get(pp.get("src") or pp.get("token") or "")
        if not info or info.get("kind") == "image" or not Path(info["path"]).exists():
            continue
        out.append({"path": info["path"], "x": float(pp.get("x", 0.5)), "y": float(pp.get("y", 0.5)),
                    "scale": float(pp.get("scale", 0.4)), "opacity": float(pp.get("opacity", 1.0)),
                    "start": float(pp.get("start", 0.0)), "end": float(pp.get("end", 0.0)),
                    "in": float(pp.get("in", 0.0)), "volume": float(pp.get("volume", 1.0)),
                    "kf": pp.get("kf") or [], "hasAudio": _has_audio(info["path"]),
                    "chromaKey": pp.get("chromaKey"), "chromaSim": pp.get("chromaSim", 0.3),
                    "mask": pp.get("mask")})
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
    pips = _resolve_pips(job, body)
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
                                    sfx=sfx, audios=audios, pips=pips, texts=texts, canvas=canvas,
                                    sources=job.get("sources"), grade=body.get("grade"),
                                    layout=body.get("layout"),
                                    src_h=body.get("srcH"), progress=_cb)
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
    pips = _resolve_pips(job, body)
    canvas = _canvas(body.get("format"))
    subtitles = bool(body.get("subtitles", True))   # 미리보기도 자막·텍스트 반영(추출과 일치)
    cues = body.get("cues")
    style = body.get("style")
    texts = body.get("texts") or []
    out = str(config.OUTPUT_DIR / f"{jid}_preview.mp4")
    PREVIEW[jid] = {"pct": 0.0, "done": False, "url": None, "error": None}

    def _cb(p: float) -> None:
        PREVIEW[jid]["pct"] = p

    async def _run():
        try:
            await asyncio.to_thread(pipeline.export_project, job["path"], clips, out,
                                    subtitles=subtitles, cues=cues, style=style,
                                    bgm=bgm, bgm_opts=bgm_opts, overlays=overlays,
                                    sfx=sfx, audios=audios, pips=pips, texts=texts, canvas=canvas,
                                    sources=job.get("sources"), grade=body.get("grade"),
                                    layout=body.get("layout"),
                                    src_h=body.get("srcH"), scale_h=480, preset="ultrafast", crf="30",
                                    progress=_cb)
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


# ===== 템플릿(틀) + 브랜드키트 (onimage와 공유 스키마) =====
_BRANDKIT_FILE = config.BASE_DIR / "config" / "brandkit.json"


def load_brandkit() -> dict:
    try:
        if _BRANDKIT_FILE.exists():
            return json.loads(_BRANDKIT_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return {}


@app.get("/api/templates")
async def templates_list() -> JSONResponse:
    from . import templates
    return JSONResponse(templates.list_public())


@app.post("/api/admin/template")
async def admin_template_save(req: Request) -> JSONResponse:
    """어드민/외부에서 만든 템플릿 1개 등록(저장). 스키마=내장과 동일."""
    from . import templates
    t = await req.json()
    if not isinstance(t, dict) or not (t.get("name") or t.get("id")):
        return JSONResponse({"error": "템플릿 형식이 올바르지 않습니다"}, status_code=400)
    tid = templates.save_custom(t)
    return JSONResponse({"ok": True, "id": tid})


@app.post("/api/admin/template/upload")
async def admin_template_upload(file: UploadFile = File(...)) -> JSONResponse:
    """템플릿 JSON 파일 업로드(외부 제작 → 가져오기)."""
    from . import templates
    try:
        t = json.loads((await file.read()).decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"JSON 파싱 실패: {e}"}, status_code=400)
    if not isinstance(t, dict):
        return JSONResponse({"error": "객체(JSON) 하나여야 합니다"}, status_code=400)
    tid = templates.save_custom(t)
    return JSONResponse({"ok": True, "id": tid})


@app.post("/api/admin/template/delete")
async def admin_template_delete(req: Request) -> JSONResponse:
    from . import templates
    body = await req.json()
    (templates.CUSTOM_DIR / f"{body.get('id')}.json").unlink(missing_ok=True)
    return JSONResponse({"ok": True})


@app.get("/api/brandkit")
async def brandkit_get() -> JSONResponse:
    bk = load_brandkit()
    if bk.get("logo"):
        bk = {**bk, "hasLogo": True}
    return JSONResponse({"color": bk.get("color", ""), "name": bk.get("name", ""),
                         "hasLogo": bool(bk.get("logo"))})


@app.post("/api/brandkit")
async def brandkit_save(req: Request) -> JSONResponse:
    body = await req.json()
    bk = load_brandkit()
    bk["color"] = (body.get("color") or "").strip()
    bk["name"] = (body.get("name") or "").strip()[:40]
    _BRANDKIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BRANDKIT_FILE.write_text(json.dumps(bk, ensure_ascii=False), encoding="utf-8")
    return JSONResponse({"ok": True})


@app.post("/api/brandkit/logo")
async def brandkit_logo(file: UploadFile = File(...)) -> JSONResponse:
    dest = config.BASE_DIR / "config" / f"brand_logo{Path(file.filename or '').suffix.lower() or '.png'}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    bk = load_brandkit(); bk["logo"] = str(dest)
    _BRANDKIT_FILE.write_text(json.dumps(bk, ensure_ascii=False), encoding="utf-8")
    return JSONResponse({"ok": True})


@app.get("/api/voice_preview")
async def voice_preview(voice: str, rate: str = "0"):
    """성우 미리듣기 — 짧은 한국어 샘플(속도 반영) 합성 mp3."""
    from . import tts
    try:
        sp = int(float(rate))
    except ValueError:
        sp = 0
    r = f"+{sp}%" if sp >= 0 else f"{sp}%"
    safe = "".join(c for c in voice if c.isalnum() or c in "-_") or "default"
    out = config.UPLOAD_DIR / f"_vp_{safe}_{sp}.mp3"
    if not out.exists():
        try:
            await tts.synth("안녕하세요, 이 목소리로 내레이션을 만들어요.", str(out), voice=voice, rate=r)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=500)
    return FileResponse(str(out), media_type="audio/mpeg")


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
<div class=card>
<h1 style=font-size:14px>🎬 템플릿(틀) 만들기 — 상하 띠</h1>
<p class=s>위아래 색 띠 + 중앙 영상 틀을 만들어 등록. AI 첫 컷의 템플릿 갤러리에 바로 나옵니다.</p>
<input id=tn placeholder="템플릿 이름 (예: 카페 세로띠)">
<div class=s style=margin-top:6px>띠 색 <input id=tb type=color value=#1f1d3d style=width:46px;vertical-align:middle>
 · 중앙영상 위치 <input id=ty type=number value=20 min=0 max=80 style=width:60px>% · 높이 <input id=th type=number value=60 min=20 max=100 style=width:60px>%</div>
<button onclick="saveT()" style=margin-top:8px>틀 등록</button> <span id=tmsg class=s></span>
<p class=s style=margin-top:10px>외부에서 만든 템플릿 JSON 가져오기:
 <input id=tf type=file accept=.json style=display:inline;width:auto> <button onclick="upT()">업로드</button> <span id=umsg class=s></span></p>
</div>
<script>
async function save(){{const k=document.getElementById('ds').value.trim();if(!k)return;
const r=await fetch('/api/admin/keys',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{deepseek:k}})}});
document.getElementById('msg').textContent=r.ok?'저장됨 ✓':'실패';document.getElementById('msg').className='ok';}}
async function saveT(){{const nm=document.getElementById('tn').value.trim();if(!nm){{document.getElementById('tmsg').textContent='이름을 입력';return;}}
const y=+document.getElementById('ty').value/100,h=+document.getElementById('th').value/100;
const t={{name:nm,desc:'상하 띠 템플릿',grade:{{brightness:1,contrast:1.06,saturation:1.15,warmth:0}},
  sub:{{fontSize:50,color:'#ffffff',outlineColor:'#000000',outlineW:2,align:'bottom'}},
  textAnim:'pop',textColor:'#ffffff',transition:'dissolve',dur:3.0,outro:true,cta:'',
  layout:{{videoY:y,videoH:h,bg:document.getElementById('tb').value}}}};
const r=await fetch('/api/admin/template',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(t)}});
document.getElementById('tmsg').textContent=r.ok?'등록됨 ✓':'실패';document.getElementById('tmsg').className='ok';}}
async function upT(){{const f=document.getElementById('tf').files[0];if(!f)return;const fd=new FormData();fd.append('file',f);
const r=await fetch('/api/admin/template/upload',{{method:'POST',body:fd}});const j=await r.json();
document.getElementById('umsg').textContent=j.ok?('가져옴 ✓ '+j.id):('실패: '+(j.error||'')); document.getElementById('umsg').className='ok';}}
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
