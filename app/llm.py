"""대화형 편집 LLM — 자연어 요청 → 편집 액션(JSON).

우선순위: ① Claude CLI(`claude -p`, 로그인된 구독 사용) → 실패/만료 시
② DeepSeek API(키 필요). 키는 config/keys.json 또는 환경변수. 둘 다 없으면
프론트의 규칙기반 폴백이 처리한다.

assist(message, ctx) → {"actions":[...], "reply": "..."}  (검증된 dict)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request

from . import config

KEYS_FILE = config.BASE_DIR / "config" / "keys.json"

SYSTEM = """너는 한국어 영상 편집기 ONCUT의 어시스턴트다. 사용자의 요청을 편집 '액션'
JSON으로만 변환한다. 설명·코드펜스 없이 **순수 JSON 1개**만 출력한다.

출력 형식: {"actions":[...], "reply":"한 줄 한국어 요약"}

가능한 액션(time/at/dur 단위=초, 출력 타임라인 기준):
- {"type":"format","value":"wide|shorts|square"}            # 가로/쇼츠세로/정사각
- {"type":"text","text":"문구","at":2.0,"dur":3,"anim":"none|pop|grow|shrink","pos":"top|center|bottom","color":"#RRGGBB","fontSize":64}
- {"type":"overlay","preset":"btn_subscribe|btn_like|btn_click","at":1,"dur":4,"corner":"lt|rt|lb|rb"}
- {"type":"sfx","preset":"sfx_click|sfx_ding|sfx_pop","at":1.0}
- {"type":"bgm_volume","value":0.12}                        # 0~0.6
- {"type":"subtitle_style","fontSize":80,"color":"#ffffff","align":"top|center|bottom","box":true}
- {"type":"subtitles","on":true|false}

규칙: at을 안 주면 현재 재생 위치(now)를 쓴다. 모호하면 합리적 기본값. 요청에 없는
액션은 만들지 마라. 실행 불가하면 actions=[] 로 두고 reply에 이유."""


def keys() -> dict:
    out = {}
    try:
        if KEYS_FILE.exists():
            out.update(json.loads(KEYS_FILE.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        pass
    if os.environ.get("DEEPSEEK_API_KEY"):
        out.setdefault("deepseek", os.environ["DEEPSEEK_API_KEY"])
    return out


def save_keys(d: dict) -> None:
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    cur = {}
    if KEYS_FILE.exists():
        try:
            cur = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cur = {}
    cur.update({k: v for k, v in d.items() if v})
    KEYS_FILE.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")


def _claude_available() -> bool:
    return shutil.which("claude") is not None


def status() -> dict:
    return {"claude_cli": _claude_available(), "deepseek": bool(keys().get("deepseek"))}


def _extract_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(json)?|```$", "", t, flags=re.MULTILINE).strip()
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        return json.loads(t[a:b + 1])
    raise ValueError("JSON 파싱 실패")


def _claude_cli(prompt: str) -> str:
    proc = subprocess.run(
        ["claude", "-p", prompt, "--append-system-prompt", SYSTEM, "--model", "haiku"],
        capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "claude cli 실패")[-300:])
    return proc.stdout


def _deepseek(prompt: str, key: str) -> str:
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": 0.2, "stream": False,
    }).encode()
    req = urllib.request.Request("https://api.deepseek.com/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


def assist(message: str, ctx: dict | None = None) -> dict:
    """자연어 → {actions, reply, provider}. CLI 우선, 실패 시 DeepSeek."""
    ctx = ctx or {}
    prompt = (f"현재 영상 길이(now/총): now={ctx.get('now', 0):.1f}s, "
              f"총={ctx.get('total', 0):.1f}s, 형식={ctx.get('format', 'wide')}.\n"
              f"요청: {message}")
    errs = []
    if _claude_available():
        try:
            return {**_parse(_claude_cli(prompt)), "provider": "claude-cli"}
        except Exception as e:  # noqa: BLE001
            errs.append(f"claude-cli: {e}")
    k = keys().get("deepseek")
    if k:
        try:
            return {**_parse(_deepseek(prompt, k)), "provider": "deepseek"}
        except Exception as e:  # noqa: BLE001
            errs.append(f"deepseek: {e}")
    raise RuntimeError("LLM 사용 불가 — " + ("; ".join(errs) if errs else "키 없음(어드민에서 DeepSeek 키 입력)"))


def _parse(raw: str) -> dict:
    obj = _extract_json(raw)
    acts = obj.get("actions") if isinstance(obj, dict) else None
    if not isinstance(acts, list):
        acts = []
    return {"actions": acts, "reply": (obj.get("reply") if isinstance(obj, dict) else "") or ""}


# ===== AI 첫 컷 메이커 — 소재+목적+요청 → 프로젝트 구성안 =====
PLAN_SYSTEM = """너는 한국어 영상 디렉터다. 사용자가 올린 소재(사진/영상)와 목적·요청을 받아
'첫 컷' 구성안을 순수 JSON 1개로만 출력한다(설명·코드펜스 금지).

형식: {"scenes":[{"text":"장면 자막/내레이션 한 줄(짧게)","dur":3.0}, ...],
  "hook":"도입에 크게 띄울 한 줄(선택)","music":true,
  "grade":{"brightness":1.0,"contrast":1.0,"saturation":1.0,"warmth":0.0}}

규칙:
- scenes 개수는 '소재 개수'와 같게(각 소재 1장면). 각 text는 8~20자 한국어.
- 목적이 숏폼이면 임팩트 있게 짧고, 유튜브 홍보면 정보전달형, 정사각이면 간결.
- grade는 분위기에 맞게(감성=따뜻 warmth 0.3, 선명=contrast 1.1 saturation 1.3, 차분=낮게). 범위는 brightness/contrast/saturation 0.5~1.6, warmth -1~1.
- 참고 URL/스타일이 주어지면 그 톤을 반영(직접 분석은 못 하니 설명·제목 기반 추정).
- 요청이 비면 소재·목적에 맞는 합리적 기본."""


# 규칙 폴백용 짧은 카피 뱅크(요청문 복붙 금지 — 목적/톤에 맞는 그럴듯한 기본)
_FB_BANK = {
    "promo": ["지금 만나보세요", "특별한 순간", "놓치지 마세요", "오늘의 추천", "바로 여기",
              "특별 혜택", "당신을 위한", "지금 시작"],
    "vlog": ["오늘의 기록", "이 순간", "함께한 시간", "잊지 못할", "소소한 행복",
             "어느 멋진 날", "기억하고 싶은", "다시 보고 싶은"],
    "info": ["핵심만 콕", "이것만 알면", "쉽게 정리", "한눈에 보기", "포인트 정리",
             "꼭 기억할 것", "마지막 정리", "결론은"],
}


def _plan_fallback(media: list, request: str, n: int, template: str = "") -> dict:
    """LLM 없을 때 규칙기반 — 요청문은 '훅'으로만, 장면 자막은 톤 맞는 기본 카피(복붙 금지)."""
    import re as _re
    req = (request or "").strip()
    t = (template or "") + " " + req
    tone = ("vlog" if _re.search(r"감성|브이로그|여행|일상|vlog", t) else
            "info" if _re.search(r"정보|정리|소개|튜토|방법|가이드", t) else "promo")
    bank = _FB_BANK[tone]
    scenes = [{"text": bank[i % len(bank)], "dur": 3.0} for i in range(n)]
    warm = 0.3 if tone == "vlog" else 0.0
    sat = 1.3 if tone == "promo" else 1.1
    hook = req.split("\n")[0][:24] if req else bank[0]
    return {"scenes": scenes, "hook": hook, "music": True,
            "grade": {"brightness": 1.0, "contrast": 1.08, "saturation": sat, "warmth": warm}}


def _looks_json(s: str) -> bool:
    return "{" in (s or "") and "}" in s and '"scenes"' in s


def _fetch_ref(url: str) -> str:
    """참고 링크의 <title>·meta description만 가볍게 가져와 톤 힌트로(영상 분석 아님)."""
    if not url or not url.startswith("http"):
        return ""
    try:
        import re as _re
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=4) as r:
            html = r.read(60000).decode("utf-8", "ignore")
        title = (_re.search(r"<title[^>]*>(.*?)</title>", html, _re.S | _re.I) or [None, ""])[1].strip()
        desc = (_re.search(r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']', html, _re.I) or [None, ""])[1].strip()
        out = (title + " / " + desc)[:200].strip(" /")
        return out
    except Exception:  # noqa: BLE001
        return ""


def plan_project(goal: str, fmt: str, media: list, request: str,
                 template: str = "", ref_url: str = "") -> dict:
    """목적·소재·요청 → 첫 컷 구성안 {scenes,hook,music,grade,provider}. CLI→DeepSeek→규칙.

    핵심: LLM이 잡담하면 1회 재시도, 그래도 실패면 '복붙 금지' 규칙 폴백.
    """
    n = max(1, len(media))
    ref_info = _fetch_ref(ref_url)                      # 참고 링크 제목·설명(있으면)
    kinds = ", ".join(f"{i+1}.{m.get('kind','?')}" for i, m in enumerate(media))
    prompt = (
        "아래 정보로 영상 '첫 컷' 구성안을 만들어라.\n"
        "반드시 '{' 로 시작하는 순수 JSON 1개만 출력. 인사·질문·설명·코드펜스 절대 금지.\n"
        f'형식: {{"scenes":[{{"text":"장면 자막 8~20자","dur":3.0}}](정확히 {n}개),'
        '"hook":"도입 한 줄","music":true,'
        '"grade":{"brightness":1.0,"contrast":1.0,"saturation":1.0,"warmth":0.0}}\n'
        "주의: scenes의 text는 요청문을 그대로 복사하지 말고 장면에 어울리게 새로 써라.\n"
        f"목적={goal}, 형식={fmt}, 소재 {n}개({kinds}).\n"
        f"템플릿/스타일={template or '없음'}, 참고링크내용={ref_info or ref_url or '없음'}.\n"
        f"요청: {request or '(비어있음 — 알아서 멋지게)'}")
    errs = []
    if _claude_available():
        for _ in range(2):                                  # 잡담 시 1회 재시도
            try:
                raw = _claude_cli_plan(prompt)
                if not _looks_json(raw):
                    continue
                return {**_plan_parse(raw, n), "provider": "claude-cli"}
            except Exception as e:  # noqa: BLE001
                errs.append(f"cli:{e}")
    k = keys().get("deepseek")
    if k:
        try:
            return {**_plan_parse(_deepseek_plan(prompt, k), n), "provider": "deepseek"}
        except Exception as e:  # noqa: BLE001
            errs.append(f"ds:{e}")
    return {**_plan_fallback(media, request, n, template), "provider": "rule"}


def _claude_cli_plan(prompt: str) -> str:
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", "haiku"],
        capture_output=True, text=True, timeout=120, cwd="/tmp")
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "claude cli 실패")[-200:])
    return proc.stdout


def _deepseek_plan(prompt: str, key: str) -> str:
    body = json.dumps({"model": "deepseek-chat",
                       "messages": [{"role": "system", "content": PLAN_SYSTEM},
                                    {"role": "user", "content": prompt}],
                       "temperature": 0.5, "stream": False}).encode()
    req = urllib.request.Request("https://api.deepseek.com/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"]


def _plan_parse(raw: str, n: int) -> dict:
    obj = _extract_json(raw)
    scenes = obj.get("scenes") if isinstance(obj, dict) else None
    if not isinstance(scenes, list) or not scenes:
        scenes = [{"text": "", "dur": 3.0}]
    out = []
    for i in range(n):                                  # 소재 수에 정확히 맞춤
        s = scenes[i] if i < len(scenes) else scenes[-1]
        out.append({"text": str(s.get("text", "") if isinstance(s, dict) else s)[:40],
                    "dur": float(s.get("dur", 3.0)) if isinstance(s, dict) else 3.0})
    g = obj.get("grade") if isinstance(obj.get("grade"), dict) else {}
    grade = {"brightness": float(g.get("brightness", 1.0)), "contrast": float(g.get("contrast", 1.0)),
             "saturation": float(g.get("saturation", 1.0)), "warmth": float(g.get("warmth", 0.0))}
    return {"scenes": out, "hook": str(obj.get("hook", ""))[:40],
            "music": bool(obj.get("music", True)), "grade": grade}
