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
