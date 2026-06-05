"""FastAPI 웹 셸 — 업로드 → SSE 처리 → 미리보기/추출. 모드 A·B 공용."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Dict, List

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

from contextvars import ContextVar

from . import asr, assets, auth, config, pipeline, stock, thumbnails, waveform

_CUR_UID: ContextVar = ContextVar("cur_uid", default=None)


def owner() -> str:
    """현재 요청 사용자 id(없으면 'anon'). 사용자별 작업 격리 기준."""
    return _CUR_UID.get() or "anon"

config.ensure_dirs()
assets.ensure_assets()
app = FastAPI(title="캡컷 에이전트")
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
app.mount("/out", StaticFiles(directory=str(config.OUTPUT_DIR)), name="out")


@app.middleware("http")
async def _auth_ctx(request, call_next):
    """요청마다 현재 사용자 id를 컨텍스트에 — 작업 격리(owner) 기준."""
    try:
        _CUR_UID.set(auth.uid_from_request(request))
    except Exception:  # noqa: BLE001
        _CUR_UID.set(None)
    return await call_next(request)


@app.post("/api/auth/signup")
async def auth_signup(req: Request) -> JSONResponse:
    b = await req.json()
    try:
        uid = auth.signup(b.get("email"), b.get("password"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    tok = auth.new_session(uid)
    r = JSONResponse({"ok": True, "user": auth.user_info(uid)})
    r.set_cookie("oncut_session", tok, max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
    return r


@app.post("/api/auth/login")
async def auth_login(req: Request) -> JSONResponse:
    b = await req.json()
    try:
        tok = auth.login(b.get("email"), b.get("password"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=401)
    uid = auth._uid_by_session(tok)
    r = JSONResponse({"ok": True, "user": auth.user_info(uid)})
    r.set_cookie("oncut_session", tok, max_age=60 * 60 * 24 * 90, httponly=True, samesite="lax")
    return r


@app.post("/api/auth/logout")
async def auth_logout(req: Request) -> JSONResponse:
    auth.logout(req.cookies.get("oncut_session"))
    r = JSONResponse({"ok": True})
    r.delete_cookie("oncut_session")
    return r


@app.get("/api/auth/me")
async def auth_me() -> JSONResponse:
    u = auth.user_info(_CUR_UID.get())
    return JSONResponse({"user": u})

JOBS: Dict[str, dict] = {}
EXPORT: Dict[str, dict] = {}      # id -> {pct, done, url, error}
PREVIEW: Dict[str, dict] = {}     # id -> {pct, done, url, error}
AUTOCUT: Dict[str, dict] = {}     # id -> {pct, done, res, extras, error}
AUTOCUT_PLAN: Dict[str, dict] = {}  # id -> {fmt, media, plan} (확정 전 임시 보관)
HL: Dict[str, dict] = {}          # id -> {path, segs, dur, w, h, fps} (멀티 하이라이트 분석)

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
    JOBS[job_id] = {"mode": "a", "path": str(dest), "filename": file.filename, "owner": owner()}
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
    JOBS[job_id] = {"mode": "b", "scenes": scenes, "out": out, "owner": owner()}
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
    own = owner()

    def _cb(p):
        AUTOCUT[jid]["pct"] = max(AUTOCUT[jid]["pct"], min(0.99, p))

    async def _run():
        try:
            res, extras = await asyncio.to_thread(_build_autocut, jid, st["fmt"], st["media"],
                                                  plan, music, voice, _cb, tp, rate)
            if jid in JOBS:
                JOBS[jid]["result"] = res        # /api/job 복원용(뒤로가기/새로고침 시 살아남음)
                JOBS[jid]["owner"] = own
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


@app.post("/api/shortify")
async def shortify_ep(target: str = Form("30"), goal: str = Form("shorts"),
                      file: UploadFile = File(...)) -> JSONResponse:
    """긴 영상 업로드 → AI가 하이라이트 구간 골라 9:16 숏폼 클립으로 편집기에 핸드오프."""
    import asyncio
    from . import shortify
    jid = uuid.uuid4().hex[:12]
    ext = Path(file.filename or "v.mp4").suffix or ".mp4"
    dest = config.UPLOAD_DIR / f"{jid}_short{ext}"
    with open(dest, "wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)
    try:
        tsec = max(8.0, min(90.0, float(target or 30)))
    except ValueError:
        tsec = 30.0
    try:
        from .silence import probe_video
        mw, mh, mfps = await asyncio.to_thread(probe_video, str(dest))
    except Exception:  # noqa: BLE001
        mw, mh, mfps = 1280, 720, 30.0
    try:
        start, end, segs = await asyncio.to_thread(shortify.pick_highlight, str(dest), tsec)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"분석 실패: {e}"}, status_code=500)
    cues = shortify.window_cues(segs, start, end)
    from . import face
    fx, fy = await asyncio.to_thread(face.detect_focus, str(dest), start, end)   # 인물 추적 크롭
    JOBS[jid] = {"mode": "a", "path": str(dest), "filename": "숏폼 자동추출", "owner": owner()}
    res = {"mode": "a", "duration": round(end - start, 2), "w": mw, "h": mh, "fps": mfps,
           "clips": [{"srcIn": start, "srcEnd": end, "src": "0",
                      "transition": {"type": "none", "dur": 0.5}}], "cuts": [], "cues": cues}
    extras = {"format": goal if goal in _AC_DIMS else "shorts",
              "clips": res["clips"], "srcMeta": {}, "focus": [fx, fy]}
    JOBS[jid]["result"] = res
    save_jobs()
    return JSONResponse({"id": jid, "res": res, "extras": extras,
                         "highlight": {"start": start, "end": end, "ai": bool(segs)}})


@app.post("/api/highlights")
async def highlights_ep(target: str = Form("30"), count: str = Form("3"),
                        file: UploadFile = File(...)) -> JSONResponse:
    """긴 영상 → 여러 하이라이트 후보(썸네일 포함). 사용자가 골라 숏폼으로."""
    import asyncio
    from . import shortify, thumbmaker
    jid = uuid.uuid4().hex[:12]
    ext = Path(file.filename or "v.mp4").suffix or ".mp4"
    dest = config.UPLOAD_DIR / f"{jid}_hl{ext}"
    with open(dest, "wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)
    try:
        tsec = max(8.0, min(90.0, float(target or 30)))
        ncand = max(1, min(6, int(float(count or 3))))
    except ValueError:
        tsec, ncand = 30.0, 3
    try:
        from .silence import probe_video
        mw, mh, mfps = await asyncio.to_thread(probe_video, str(dest))
    except Exception:  # noqa: BLE001
        mw, mh, mfps = 1280, 720, 30.0
    try:
        wins, segs = await asyncio.to_thread(shortify.pick_highlights, str(dest), tsec, ncand)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"분석 실패: {e}"}, status_code=500)
    HL[jid] = {"path": str(dest), "segs": segs, "dur": (wins[-1][1] if wins else 0),
               "w": mw, "h": mh, "fps": mfps}
    JOBS[jid] = {"mode": "a", "path": str(dest), "filename": "하이라이트", "owner": owner()}
    cands = []
    for i, (s, e, sc) in enumerate(wins):
        thumb = config.OUTPUT_DIR / f"hlthumb_{jid}_{i}.jpg"
        try:
            await asyncio.to_thread(thumbmaker._extract, str(dest), (s + e) / 2, str(thumb))
            turl = f"/out/{thumb.name}" if thumb.exists() else None
        except Exception:  # noqa: BLE001
            turl = None
        cands.append({"i": i, "start": s, "end": e, "score": sc,
                      "dur": round(e - s, 1), "thumb": turl, "ai": bool(segs)})
    save_jobs()
    return JSONResponse({"id": jid, "candidates": cands, "ai": bool(segs)})


@app.post("/api/highlight_open")
async def highlight_open(req: Request) -> JSONResponse:
    """선택한 하이라이트 구간 → 9:16 숏폼 편집기 프로젝트."""
    from . import shortify
    body = await req.json()
    st = HL.get(body.get("id"))
    if not st:
        return JSONResponse({"error": "분석이 만료됐어요. 다시 시도해 주세요."}, status_code=404)
    import asyncio
    from . import face
    start = float(body.get("start", 0)); end = float(body.get("end", 0))
    goal = body.get("goal") or "shorts"
    cues = shortify.window_cues(st.get("segs"), start, end)
    fx, fy = await asyncio.to_thread(face.detect_focus, st["path"], start, end)
    jid = body.get("id")
    res = {"mode": "a", "duration": round(end - start, 2), "w": st["w"], "h": st["h"], "fps": st["fps"],
           "clips": [{"srcIn": start, "srcEnd": end, "src": "0",
                      "transition": {"type": "none", "dur": 0.5}}], "cuts": [], "cues": cues}
    extras = {"format": goal if goal in _AC_DIMS else "shorts", "clips": res["clips"],
              "srcMeta": {}, "focus": [fx, fy]}
    JOBS[jid]["result"] = res
    save_jobs()
    return JSONResponse({"id": jid, "res": res, "extras": extras})


@app.post("/api/raw_open")
async def raw_open(goal: str = Form("wide"),
                   files: List[UploadFile] = File(...)) -> JSONResponse:
    """AI 없이 '바로 편집기로' — 올린 소재를 그대로 클립으로 만들어 편집기에 넘김.
    영상은 원본 길이 유지, 사진은 3초. 자동 컷·자막·TTS·템플릿 장식 전부 없음."""
    import asyncio
    jid = uuid.uuid4().hex[:12]
    fmt = goal if goal in _AC_DIMS else "wide"
    media = []
    for i, f in enumerate(files):
        ext = Path(f.filename or "").suffix.lower()
        is_img = ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic"}
        dest = config.UPLOAD_DIR / f"{jid}_raw{i:03d}_{f.filename}"
        with open(dest, "wb") as out:
            while chunk := await f.read(1 << 20):
                out.write(chunk)
        media.append({"path": str(dest), "kind": "image" if is_img else "video",
                      "name": f.filename})
    if not media:
        return JSONResponse({"error": "소재가 없습니다"}, status_code=400)
    # 영상=원본 전체 길이(큰 dur로 min이 자연길이 선택), 사진=3초. 텍스트/훅/장식 없음.
    plan = {"scenes": [{"text": "", "dur": 99999.0 if m["kind"] == "video" else 3.0}
                       for m in media], "hook": "", "grade": {}, "music": False}
    try:
        res, extras = await asyncio.to_thread(_build_autocut, jid, fmt, media, plan,
                                              False, None, lambda p: None, {"transition": "none"}, "+0%")
        if jid in JOBS:
            JOBS[jid]["result"] = res
            JOBS[jid]["owner"] = owner()
        save_jobs()
        return JSONResponse({"id": jid, "res": res, "extras": extras})
    except Exception as e:  # noqa: BLE001
        import traceback; traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


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


@app.post("/api/subtitles")
async def gen_subtitles(req: Request) -> JSONResponse:
    """온디맨드 자막 자동생성 — 작업 메인 영상 ASR → cues. AI 제안카드 '자막 자동'."""
    import asyncio
    from . import asr, subtitle
    body = await req.json()
    job = JOBS.get(body.get("id"))
    if not job or "path" not in job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    try:                                       # asr.transcribe 내부에 asyncio.Lock 직렬화(§4)
        tr = await asr.transcribe(job["path"], config.WHISPER_MODEL)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"자막 생성 실패: {e}"}, status_code=500)
    cues = subtitle.build_cues(tr.get("segments") or [])
    return JSONResponse({"cues": [{"start": c.start, "end": c.end, "text": c.text} for c in cues]})


@app.post("/api/thumbnail")
async def gen_thumbnail(req: Request) -> JSONResponse:
    """현재 영상에서 좋은 장면 + 제목으로 썸네일 PNG 자동 생성."""
    import asyncio
    from . import thumbmaker
    body = await req.json()
    job = JOBS.get(body.get("id"))
    if not job or "path" not in job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    title = (body.get("title") or "").strip() or "오늘의 영상"
    style = body.get("style") or "band"
    fmt = body.get("format") or "wide"
    t = body.get("t")
    tok = uuid.uuid4().hex[:8]
    out = config.OUTPUT_DIR / f"thumb_{body.get('id')}_{tok}.jpg"
    try:
        await asyncio.to_thread(thumbmaker.make_thumbnail, job["path"], title, str(out),
                                t=(float(t) if t is not None else None), style=style,
                                fmt=fmt, brand_color=(load_brandkit().get("color") or "#ff3d8b"))
    except Exception as e:  # noqa: BLE001
        import traceback; traceback.print_exc()
        return JSONResponse({"error": f"썸네일 생성 실패: {e}"}, status_code=500)
    return JSONResponse({"url": f"/out/{out.name}", "style": style})


@app.post("/api/translate")
async def translate_ep(req: Request) -> JSONResponse:
    """자막 텍스트들을 목표 언어로 번역(AI). 길이·순서 유지."""
    import asyncio
    from . import llm
    body = await req.json()
    texts = body.get("texts") or []
    lang = body.get("lang") or "en"
    if not texts:
        return JSONResponse({"error": "번역할 자막이 없어요"}, status_code=400)
    out = await asyncio.to_thread(llm.translate_cues, texts, lang)
    if out is None:
        return JSONResponse({"error": "AI 번역 사용 불가 (Claude CLI/DeepSeek 키 필요 — /api/admin)"},
                            status_code=503)
    return JSONResponse({"texts": out, "lang": lang})


@app.post("/api/titles")
async def gen_titles(req: Request) -> JSONResponse:
    """대본/자막 → 제목·해시태그·설명 추천 (AI 있으면 AI, 없으면 규칙기반)."""
    import asyncio
    from . import llm
    body = await req.json()
    script = (body.get("script") or "").strip()
    fmt = body.get("format") or "wide"
    meta = await asyncio.to_thread(llm.suggest_meta, script, fmt)
    return JSONResponse(meta)


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
    if job.get("owner", "anon") != owner():           # 남의 작업 복원 차단
        return JSONResponse({"error": "forbidden"}, status_code=403)
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
    rec = {"id": jid, "name": (body.get("name") or "무제 작업").strip()[:60], "owner": owner(),
           "savedAt": int(time.time()), "state": body.get("state") or {}}
    (_DRAFTS_DIR / f"{jid}.json").write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return JSONResponse({"ok": True, "id": jid})


@app.get("/api/draft/list")
async def draft_list() -> JSONResponse:
    """최근 작업 목록(최신순). 미디어(job)가 아직 살아있는 것만."""
    if not _DRAFTS_DIR.exists():
        return JSONResponse([])
    me = owner()
    out = []
    for p in _DRAFTS_DIR.glob("*.json"):
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
            if r.get("owner", "anon") != me:           # 본인 작업만
                continue
            if r.get("id") in JOBS:                     # 미디어 사라진 작업은 숨김
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
    rec = json.loads(p.read_text(encoding="utf-8"))
    if rec.get("owner", "anon") != owner():
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return JSONResponse(rec)


@app.post("/api/draft/delete")
async def draft_delete(req: Request) -> JSONResponse:
    body = await req.json()
    p = _DRAFTS_DIR / f"{body.get('id')}.json"
    if p.exists():
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            if rec.get("owner", "anon") != owner():
                return JSONResponse({"error": "forbidden"}, status_code=403)
        except Exception:  # noqa: BLE001
            pass
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


@app.get("/api/library/backgrounds")
async def library_backgrounds() -> JSONResponse:
    """내장 배경(단색·그라데이션) — 키 없이 즉시 사용."""
    return JSONResponse({"backgrounds": assets.backgrounds()})


@app.get("/api/stock/search")
async def stock_search(q: str = "", kind: str = "photo",
                       provider: str = "all", page: int = 1) -> JSONResponse:
    """무료 스톡(Pexels/Pixabay) 검색. 키 없으면 available로 안내."""
    import asyncio
    res = await asyncio.to_thread(stock.search, provider, kind, q, max(1, int(page or 1)))
    return JSONResponse(res)


@app.post("/api/stock/import")
async def stock_import(req: Request) -> JSONResponse:
    """검색결과 1건을 작업에 다운로드. 사진=오버레이 토큰, 영상=소스 토큰."""
    import asyncio
    body = await req.json()
    jid = body.get("id"); url = body.get("url"); kind = body.get("kind", "photo")
    name = (body.get("name") or "stock")[:40]
    job = JOBS.get(jid)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    if not url:
        return JSONResponse({"error": "no url"}, status_code=400)
    token = uuid.uuid4().hex[:8]
    ext = ".mp4" if kind == "video" else ".jpg"
    dest = config.UPLOAD_DIR / f"{jid}_stock_{token}{ext}"
    try:
        await asyncio.to_thread(stock.download, url, str(dest))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"다운로드 실패: {e}"}, status_code=502)
    if kind == "video":
        info = {"path": str(dest), "kind": "video", "name": name}
        try:
            from .silence import probe_duration
            info["duration"] = round(await asyncio.to_thread(probe_duration, str(dest)), 2)
        except Exception:  # noqa: BLE001
            info["duration"] = 5.0
        job.setdefault("sources", {})[token] = info
        save_jobs()
        return JSONResponse({"token": token, "kind": "video",
                             "url": f"/api/media?id={jid}&src={token}",
                             "duration": info["duration"], "name": name})
    job.setdefault("assets", {})[token] = str(dest)
    save_jobs()
    return JSONResponse({"token": token, "kind": "photo",
                         "url": f"/api/asset?id={jid}&token={token}", "name": name})


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
                    "scale": ov.get("scale", 0.2), "scaleH": ov.get("scaleH"),
                    "opacity": ov.get("opacity", 1.0), "rot": ov.get("rot", 0),
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


@app.post("/api/capcut")
async def export_capcut(req: Request) -> JSONResponse:
    """편집기 전체 프로젝트 → 캡컷 드래프트(Win/Mac 핸드오프). 영상·자막·텍스트·
    오버레이·오디오를 트랙으로. OUTPUT_DRAFT_DIR 없으면 OS Projects 폴더/out."""
    import asyncio
    from . import draft, render, subtitle
    body = await req.json()
    job = JOBS.get(body.get("id"))
    if not job or "path" not in job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    clips = _body_clips(body)
    if not clips:
        return JSONResponse({"error": "클립이 없어요"}, status_code=400)
    srcs = job.get("sources") or {}
    srcpaths = {"0": job["path"]}
    for tok, info in srcs.items():
        if info.get("path"):
            srcpaths[str(tok)] = info["path"]
    fmt = body.get("format") or "wide"
    canvas = _canvas(fmt)
    try:
        from .silence import probe_video
        sw, sh, sfps = probe_video(job["path"])
    except Exception:  # noqa: BLE001
        sw, sh, sfps = 1280, 720, 30.0
    w, h = canvas if canvas else (sw, sh)
    # 자막 → 출력 타임라인 remap
    cues = None
    if body.get("subtitles", True) and body.get("cues"):
        layout, _ = render.clip_layout(clips)
        cobjs = [subtitle.Cue(float(c["start"]), float(c["end"]), c["text"])
                 for c in body["cues"] if (c.get("text") or "").strip()]
        cues = subtitle.remap_cues_clips(cobjs, layout)
    overlays = _resolve_overlays(job, body)
    sfx = _resolve_sfx(job, body)
    audios = _resolve_audios(job, body)
    bgm = job.get("bgm") if body.get("bgm") else None
    bvol = float((body.get("bgmOpts") or {}).get("volume", 0.16))
    name = (body.get("draftName") or f"ONCUT_{body.get('id')}")[:48]
    out_root = os.environ.get("OUTPUT_DRAFT_DIR") or None
    try:
        res = await asyncio.to_thread(draft.build_from_project, job["path"], clips, srcpaths,
                                      w=w, h=h, fps=sfps, cues=cues, texts=body.get("texts") or [],
                                      overlays=overlays, audios=audios, sfx=sfx, bgm=bgm,
                                      bgm_volume=bvol, draft_name=name, out_root=out_root)
        # 미디어 동봉 + 경로 치환 + ZIP (SaaS 다운로드용 — 캡컷에서 영상 안 깨지게)
        zip_path = await asyncio.to_thread(draft.bundle_and_zip, res["dir"], res.get("media") or [])
        zname = Path(zip_path).name
        # /out 으로 서빙 가능하게(기본 드래프트 루트가 OUTPUT_DIR이면 그대로, 아니면 복사)
        if Path(zip_path).resolve().parent != config.OUTPUT_DIR.resolve():
            import shutil
            shutil.copy2(zip_path, config.OUTPUT_DIR / zname)
        res["zip"] = f"/out/{zname}"
    except Exception as e:  # noqa: BLE001
        import traceback; traceback.print_exc()
        return JSONResponse({"error": f"드래프트 생성 실패: {e}"}, status_code=500)
    res.pop("media", None)
    return JSONResponse(res)


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
                                    focus=body.get("focus"),
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
    sk = stock.available()
    px = "✅ 키 등록됨" if sk["pexels"] else "❌ 미등록"
    pb = "✅ 키 등록됨" if sk["pixabay"] else "❌ 미등록"
    return f"""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>ONCUT 관리자</title>
<style>body{{background:#0a0a0a;color:#ededed;font-family:ui-monospace,monospace;max-width:560px;margin:40px auto;padding:0 20px}}
h1{{font-size:16px}}.card{{background:#161616;border:1px solid #262626;border-radius:12px;padding:20px;margin-top:16px}}
input{{width:100%;background:#0e0e0e;border:1px solid #262626;color:#ededed;border-radius:8px;padding:10px;font-family:inherit;margin:6px 0}}
button{{background:#22c55e;color:#04130a;border:0;border-radius:8px;padding:10px 18px;font-weight:600;cursor:pointer}}
.s{{color:#8a8a8a;font-size:12px}}.ok{{color:#22c55e}}</style></head><body>
<h1>ONCUT 관리자</h1>
<div class=card>
<h1 style=font-size:14px>👤 계정 관리</h1>
<p class=s>무료 가입한 사용자 목록 · 작업은 사용자별로 격리됩니다(본인 것만 보임).</p>
<div id=users class=s>불러오는 중…</div>
</div>
<div class=card>
<h1 style=font-size:14px>🔗 다른 프로그램에 임베딩(2가지 길)</h1>
<p class=s><b>① iframe 임베드</b> — 호스트 페이지에 끼워넣기. 사용자의 API 키를 붙이면 그 계정으로 바로:<br>
<code>&lt;iframe src="https://이앱주소/?embed=1&amp;key=API_KEY"&gt;&lt;/iframe&gt;</code></p>
<p class=s><b>② REST API</b> — 다른 프로그램이 직접 호출. 모든 요청 헤더에:<br>
<code>X-API-Key: API_KEY</code> (또는 <code>Authorization: Bearer API_KEY</code>)</p>
<p class=s>API 키는 각 사용자가 가입 시 자동 발급(위 목록의 🔑). 로그인하면 본인 화면에서도 확인 가능.</p>
</div>
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
<h1 style=font-size:14px>🖼 무료 스톡 사진·영상 (에셋 라이브러리)</h1>
<p class=s>편집기 "에셋"에서 무료 사진·영상을 검색해 바로 추가. 무료 키 발급:
 <a style=color:#22c55e href="https://www.pexels.com/api/" target=_blank>Pexels</a> ·
 <a style=color:#22c55e href="https://pixabay.com/api/docs/" target=_blank>Pixabay</a></p>
<p>Pexels: <b>{px}</b> &nbsp; Pixabay: <b>{pb}</b></p>
<input id=pex type=password placeholder="Pexels API Key">
<input id=pix type=password placeholder="Pixabay API Key">
<button onclick="saveStock()">저장</button> <span id=smsg class=s></span>
</div>
<div class=card>
<h1 style=font-size:14px>🎬 제공 템플릿 만들기 (사용자 길잡이)</h1>
<p class=s>색감·자막·전환·CTA·상하 띠까지 정해 등록하면, 모든 사용자의 템플릿 갤러리에
'제공 템플릿'으로 떠서 영상 만들 때 길잡이가 됩니다.</p>
<div style="display:flex;gap:14px;flex-wrap:wrap">
 <div style="flex:1;min-width:240px">
  <input id=tn placeholder="템플릿 이름 (예: 카페 홍보)">
  <input id=tdesc placeholder="길잡이 설명 (예: 메뉴 강조·따뜻한 톤)">
  <div class=s style=margin-top:8px>색감
   <select id=tgrade><option value=original>원본</option><option value=vivid selected>선명</option><option value=warm>따뜻필름</option><option value=cool>시원</option><option value=vintage>빈티지</option><option value=bw>흑백</option></select>
   · 전환 <select id=ttrans><option value=none>없음</option><option value=dissolve selected>디졸브</option><option value=fadeblack>검정</option><option value=slideleft>슬라이드</option><option value=wipeleft>와이프</option></select></div>
  <div class=s style=margin-top:8px>자막색 <input id=tcol type=color value=#ffffff style=width:40px;vertical-align:middle>
   외곽선 <input id=toc type=color value=#000000 style=width:40px;vertical-align:middle>
   두께 <input id=tow type=number value=3 min=0 max=8 style=width:48px>
   크기 <input id=tfs type=number value=54 min=20 max=120 style=width:56px></div>
  <div class=s style=margin-top:8px>자막위치
   <select id=talign><option value=bottom selected>하단</option><option value=center>중앙</option><option value=top>상단</option></select>
   · 텍스트 애니 <select id=tanim><option value=pop selected>팝</option><option value=none>없음</option><option value=grow>커짐</option><option value=shrink>작아짐</option></select></div>
  <div class=s style=margin-top:8px>장면길이 <input id=tdur type=number value=3 min=1 max=10 step=0.5 style=width:56px>초
   · 텍스트색 <input id=ttcol type=color value=#ffffff style=width:40px;vertical-align:middle></div>
  <div class=s style=margin-top:8px><label><input type=checkbox id=toutro checked> 아웃트로(끝에 문구)</label>
   <input id=tcta placeholder="CTA 문구 (예: 구독 부탁해요)" style=margin-top:4px></div>
  <div class=s style=margin-top:8px><label><input type=checkbox id=tlay> 상하 띠 레이아웃</label>
   띠색 <input id=tb type=color value=#1f1d3d style=width:40px;vertical-align:middle>
   영상Y <input id=ty type=number value=20 min=0 max=80 style=width:52px>% 높이 <input id=th type=number value=60 min=20 max=100 style=width:52px>%</div>
  <div class=s style=margin-top:8px>미리보기 비율 <select id=tfmt><option value=wide>16:9</option><option value=shorts selected>9:16</option><option value=square>1:1</option></select></div>
  <button onclick="saveT()" style=margin-top:10px>＋ 제공 템플릿 등록</button> <span id=tmsg class=s></span>
 </div>
 <div style="width:150px;flex:none">
  <div class=s style=margin-bottom:4px>미리보기</div>
  <div id=tprev style="width:150px;background:#000;border-radius:8px;overflow:hidden;display:flex;flex-direction:column"></div>
 </div>
</div>
<div style=margin-top:14px>
 <div class=s style=margin-bottom:6px>등록된 제공 템플릿</div>
 <div id=tlist class=s>불러오는 중…</div>
</div>
<p class=s style=margin-top:10px>외부 JSON 가져오기:
 <input id=tf type=file accept=.json style=display:inline;width:auto> <button onclick="upT()">업로드</button> <span id=umsg class=s></span></p>
</div>
<script>
async function loadUsers(){{try{{const j=await(await fetch('/api/admin/users')).json();
const el=document.getElementById('users');
if(!j.users.length){{el.textContent='아직 가입한 사용자가 없어요.';return;}}
el.innerHTML='<div style=margin-bottom:6px>총 '+j.total+'명</div>'+j.users.map(u=>
  '<div style="display:flex;gap:8px;align-items:center;padding:5px 0;border-top:1px solid #222">'+
  '<span style="flex:1">'+u.email+' · 작업 '+(u.drafts||0)+'개</span>'+
  '<button data-id="'+u.id+'" class=del style="background:#3a1a1a;color:#f88;padding:4px 8px">삭제</button></div>').join('');
el.querySelectorAll('.del').forEach(b=>b.onclick=async()=>{{if(!confirm('이 계정을 삭제할까요?'))return;
  await fetch('/api/admin/users/delete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id:b.dataset.id}})}});loadUsers();}});
}}catch(e){{document.getElementById('users').textContent='목록 로드 실패';}}}}
loadUsers();
async function save(){{const k=document.getElementById('ds').value.trim();if(!k)return;
const r=await fetch('/api/admin/keys',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{deepseek:k}})}});
document.getElementById('msg').textContent=r.ok?'저장됨 ✓':'실패';document.getElementById('msg').className='ok';}}
async function saveStock(){{const pex=document.getElementById('pex').value.trim(),pix=document.getElementById('pix').value.trim();
const r=await fetch('/api/admin/keys',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{pexels:pex,pixabay:pix}})}});
document.getElementById('smsg').textContent=r.ok?'저장됨 ✓ (편집기 새로고침)':'실패';document.getElementById('smsg').className='ok';}}
const GP={{original:{{brightness:1,contrast:1,saturation:1,warmth:0}},
vivid:{{brightness:1.03,contrast:1.12,saturation:1.35,warmth:.1}},
warm:{{brightness:1.02,contrast:1.05,saturation:1.1,warmth:.5}},
cool:{{brightness:1,contrast:1.05,saturation:1.05,warmth:-.5}},
vintage:{{brightness:1.05,contrast:.9,saturation:.72,warmth:.32}},
bw:{{brightness:1,contrast:1.06,saturation:0,warmth:0}}}};
const gV=id=>document.getElementById(id).value, gC=id=>document.getElementById(id).checked;
function tplObj(){{
  const lay=gC('tlay')?{{videoY:+gV('ty')/100,videoH:+gV('th')/100,bg:gV('tb')}}:null;
  return {{name:gV('tn').trim(),desc:gV('tdesc').trim()||'제공 템플릿',grade:GP[gV('tgrade')],
    sub:{{fontSize:+gV('tfs'),color:gV('tcol'),outlineColor:gV('toc'),outlineW:+gV('tow'),align:gV('talign')}},
    textAnim:gV('tanim'),textColor:gV('ttcol'),transition:gV('ttrans'),dur:+gV('tdur'),
    outro:gC('toutro'),cta:gV('tcta').trim(),layout:lay}};
}}
function buildPrev(){{const t=tplObj(),fmt=gV('tfmt');
  const ar=fmt==='wide'?'16/9':fmt==='square'?'1/1':'9/16',g=t.grade;
  const flt='brightness('+g.brightness+') contrast('+g.contrast+') saturate('+g.saturation+')';
  const warm=g.warmth>.02?'rgba(255,150,50,'+(.4*g.warmth)+')':g.warmth<-.02?'rgba(60,150,255,'+(.4*-g.warmth)+')':'transparent';
  const bg=t.layout?t.layout.bg:'#2a2a4a';
  const vid='<div style="position:relative;flex:1;background:linear-gradient(135deg,#667,#334);filter:'+flt+'">'+
    '<div style="position:absolute;inset:0;background:'+warm+'"></div>'+
    '<div style="position:absolute;left:2px;right:2px;'+(t.sub.align==='top'?'top:6px':t.sub.align==='center'?'top:45%':'bottom:14px')+';text-align:center;font-size:11px;font-weight:800;color:'+t.sub.color+';text-shadow:0 0 2px '+t.sub.outlineColor+',0 0 2px '+t.sub.outlineColor+'">샘플 자막</div>'+
    (t.outro&&t.cta?'<div style="position:absolute;bottom:3px;right:4px;font-size:8px;background:#ff3d8b;color:#fff;padding:1px 4px;border-radius:3px">'+t.cta+'</div>':'')+'</div>';
  const prev=document.getElementById('tprev');prev.style.aspectRatio=ar;
  prev.innerHTML=t.layout?('<div style="height:'+(t.layout.videoY*100)+'%;background:'+bg+'"></div>'+vid+'<div style="height:'+((1-t.layout.videoY-t.layout.videoH)*100)+'%;background:'+bg+'"></div>'):vid;
}}
['tgrade','ttrans','tcol','toc','tow','tfs','talign','tanim','tdur','ttcol','toutro','tcta','tlay','tb','ty','th','tfmt'].forEach(id=>{{const el=document.getElementById(id);if(el)el.addEventListener('input',buildPrev);}});
buildPrev();
async function loadTpls(){{try{{const ts=await(await fetch('/api/templates')).json();const cs=ts.filter(t=>t.custom);
  const el=document.getElementById('tlist');
  if(!cs.length){{el.textContent='아직 제공 템플릿이 없어요. 위에서 만들어 등록하세요.';return;}}
  el.innerHTML=cs.map(t=>'<div style="display:flex;gap:8px;align-items:center;padding:5px 0;border-top:1px solid #222"><span style="flex:1">'+t.name+' — '+(t.desc||'')+'</span><button data-id="'+t.id+'" class=tdel style="background:#3a1a1a;color:#f88;padding:4px 8px">삭제</button></div>').join('');
  el.querySelectorAll('.tdel').forEach(b=>b.onclick=async()=>{{if(!confirm('이 템플릿을 삭제할까요?'))return;await fetch('/api/admin/template/delete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id:b.dataset.id}})}});loadTpls();}});
}}catch(e){{document.getElementById('tlist').textContent='목록 로드 실패';}}}}
loadTpls();
async function saveT(){{const t=tplObj();if(!t.name){{document.getElementById('tmsg').textContent='이름을 입력하세요';return;}}
const r=await fetch('/api/admin/template',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(t)}});
const j=await r.json();document.getElementById('tmsg').textContent=j.ok?('등록됨 ✓ '+j.id):('실패: '+(j.error||''));document.getElementById('tmsg').className='ok';loadTpls();}}
async function upT(){{const f=document.getElementById('tf').files[0];if(!f)return;const fd=new FormData();fd.append('file',f);
const r=await fetch('/api/admin/template/upload',{{method:'POST',body:fd}});const j=await r.json();
document.getElementById('umsg').textContent=j.ok?('가져옴 ✓ '+j.id):('실패: '+(j.error||'')); document.getElementById('umsg').className='ok';}}
</script></body></html>"""


@app.post("/api/admin/keys")
async def admin_keys(req: Request) -> JSONResponse:
    from . import llm
    body = await req.json()
    upd = {}
    for k in ("deepseek", "pexels", "pixabay"):
        v = (body.get(k) or "").strip()
        if v:
            upd[k] = v
    if upd:
        llm.save_keys(upd)
    return JSONResponse({"ok": True})


@app.get("/api/admin/users")
async def admin_users() -> JSONResponse:
    """가입 계정 목록 + 사용자별 작업 수(계정 관리)."""
    counts: dict = {}
    if _DRAFTS_DIR.exists():
        for p in _DRAFTS_DIR.glob("*.json"):
            try:
                o = json.loads(p.read_text(encoding="utf-8")).get("owner", "anon")
                counts[o] = counts.get(o, 0) + 1
            except Exception:  # noqa: BLE001
                continue
    us = auth.list_users()
    for u in us:
        u["drafts"] = counts.get(u["id"], 0)
    return JSONResponse({"users": us, "total": len(us)})


@app.post("/api/admin/users/delete")
async def admin_user_delete(req: Request) -> JSONResponse:
    b = await req.json()
    auth.delete_user(b.get("id"))
    return JSONResponse({"ok": True})


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "version": "0.1.0"}
