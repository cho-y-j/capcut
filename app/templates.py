"""영상 템플릿(틀) 엔진 — onimage와 통일된 스키마.

템플릿 = 결과물의 '룩과 구조'를 실제로 바꾸는 프리셋:
  grade(색감) · 자막/텍스트 스타일 · 전환 · 장면 기본길이 · 아웃트로 CTA.
브랜드키트({color, name, logo})를 {{brand.*}} 토큰처럼 끼워넣어 회사별 맞춤.
onimage의 brand={color,logo,name} / theme 개념과 호환(나중에 한 세트로 묶음).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config

# 커스텀 템플릿 보관(어드민/외부 업로드) — 내장과 같은 스키마
CUSTOM_DIR = config.BASE_DIR / "config" / "templates"


def load_custom() -> dict:
    out = {}
    try:
        for p in CUSTOM_DIR.glob("*.json"):
            try:
                t = json.loads(p.read_text(encoding="utf-8"))
                if t.get("id"):
                    out[t["id"]] = t
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return out


def save_custom(t: dict) -> str:
    CUSTOM_DIR.mkdir(parents=True, exist_ok=True)
    tid = (t.get("id") or "").strip() or ("tpl" + str(abs(hash(t.get("name", "")))) [:6])
    t["id"] = tid
    t.setdefault("name", "내 템플릿"); t.setdefault("desc", "사용자 템플릿"); t["custom"] = True
    (CUSTOM_DIR / f"{tid}.json").write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")
    return tid


# 내장 템플릿. (업종팩/사용자 템플릿은 추후 같은 스키마로 추가)
TEMPLATES = {
    "basic": {
        "name": "기본", "desc": "무난한 자동 편집",
        "grade": {"brightness": 1.0, "contrast": 1.06, "saturation": 1.12, "warmth": 0.0},
        "sub": {"fontSize": 56, "color": "#ffffff", "outlineColor": "#000000", "outlineW": 3, "align": "bottom"},
        "textAnim": "pop", "textColor": "#ffffff", "transition": "dissolve", "dur": 3.0,
        "outro": False, "cta": "",
    },
    "vlog": {
        "name": "감성 브이로그", "desc": "따뜻하고 잔잔하게",
        "grade": {"brightness": 1.03, "contrast": 1.0, "saturation": 1.05, "warmth": 0.38},
        "sub": {"fontSize": 50, "color": "#fff7ee", "outlineColor": "#3a2a1a", "outlineW": 2, "align": "bottom"},
        "textAnim": "none", "textColor": "#fff7ee", "transition": "dissolve", "dur": 3.4,
        "outro": True, "cta": "함께해요",
    },
    "promo": {
        "name": "강렬한 홍보", "desc": "임팩트·CTA 강조",
        "grade": {"brightness": 1.04, "contrast": 1.18, "saturation": 1.38, "warmth": 0.06},
        "sub": {"fontSize": 72, "color": "#ffffff", "outlineColor": "#000000", "outlineW": 5, "align": "bottom"},
        "textAnim": "pop", "textColor": "{{brand.color}}", "transition": "slideleft", "dur": 2.2,
        "outro": True, "cta": "지금 만나보세요",
    },
    "info": {
        "name": "깔끔한 정보", "desc": "정보 전달형",
        "grade": {"brightness": 1.02, "contrast": 1.05, "saturation": 1.0, "warmth": -0.05},
        "sub": {"fontSize": 54, "color": "#111111", "outlineColor": "#ffffff", "outlineW": 4, "align": "top", "box": True},
        "textAnim": "grow", "textColor": "#111111", "transition": "fadeblack", "dur": 3.6,
        "outro": True, "cta": "더 알아보기",
    },
    "band": {
        "name": "상하 띠 (중앙 영상)", "desc": "위아래 색띠 + 가운데 영상, 자막은 띠에",
        "grade": {"brightness": 1.0, "contrast": 1.06, "saturation": 1.15, "warmth": 0.0},
        "sub": {"fontSize": 50, "color": "#ffffff", "outlineColor": "#000000", "outlineW": 2, "align": "bottom"},
        "textAnim": "pop", "textColor": "#ffffff", "transition": "dissolve", "dur": 3.0,
        "outro": True, "cta": "",
        "layout": {"videoY": 0.2, "videoH": 0.6, "bg": "{{brand.color}}"},
    },
}

# 한국어 별칭(LLM 프롬프트/프론트 호환) → 템플릿 id
ALIAS = {"감성 브이로그": "vlog", "강렬한 홍보": "promo", "깔끔한 정보": "info", "기본": "basic", "": "basic"}


def resolve(tid: str) -> dict:
    """id 또는 한국어 별칭 → 템플릿 dict(기본=basic). 커스텀 포함. 항상 사본 반환."""
    custom = load_custom()
    if tid in custom:
        return {**TEMPLATES["basic"], **custom[tid], "id": tid}
    key = ALIAS.get(tid, tid) if tid not in TEMPLATES else tid
    t = TEMPLATES.get(key) or TEMPLATES["basic"]
    return {**t, "id": key}


def _sub(v, brand: dict):
    """{{brand.color}} 등 토큰 치환."""
    if isinstance(v, str) and "{{brand." in v:
        return (v.replace("{{brand.color}}", brand.get("color") or "#ff3d8b")
                 .replace("{{brand.name}}", brand.get("name") or "")
                 .replace("{{brand.logo}}", brand.get("logo") or ""))
    return v


def apply(tid: str, brand: dict | None = None) -> dict:
    """템플릿+브랜드 → 빌드에 쓸 실제 파라미터(토큰 치환 완료)."""
    brand = brand or {}
    t = resolve(tid)
    return {
        "id": t["id"], "name": t["name"],
        "grade": dict(t["grade"]),
        "sub": dict(t["sub"]),
        "textAnim": t["textAnim"],
        "textColor": _sub(t["textColor"], brand),
        "transition": t["transition"],
        "dur": float(t["dur"]),
        "outro": bool(t["outro"]),
        "cta": _sub(t["cta"], brand),
        "layout": ({**t["layout"], "bg": _sub(t["layout"].get("bg", "#000000"), brand)}
                   if t.get("layout") else None),
        "brand": {"color": brand.get("color") or "#ff3d8b",
                  "name": brand.get("name") or "", "logo": brand.get("logo") or ""},
    }


def list_public() -> list:
    items = dict(TEMPLATES)
    items.update(load_custom())          # 커스텀이 같은 id면 덮어씀
    return [{"id": k, "name": v.get("name", k), "desc": v.get("desc", ""),
             "layout": v.get("layout"), "grade": v.get("grade"), "sub": v.get("sub"),
             "custom": bool(v.get("custom"))} for k, v in items.items()]
