"""영상 템플릿(틀) 엔진 — onimage와 통일된 스키마.

템플릿 = 결과물의 '룩과 구조'를 실제로 바꾸는 프리셋:
  grade(색감) · 자막/텍스트 스타일 · 전환 · 장면 기본길이 · 아웃트로 CTA.
브랜드키트({color, name, logo})를 {{brand.*}} 토큰처럼 끼워넣어 회사별 맞춤.
onimage의 brand={color,logo,name} / theme 개념과 호환(나중에 한 세트로 묶음).
"""
from __future__ import annotations

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
}

# 한국어 별칭(LLM 프롬프트/프론트 호환) → 템플릿 id
ALIAS = {"감성 브이로그": "vlog", "강렬한 홍보": "promo", "깔끔한 정보": "info", "기본": "basic", "": "basic"}


def resolve(tid: str) -> dict:
    """id 또는 한국어 별칭 → 템플릿 dict(기본=basic). 항상 사본 반환."""
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
        "brand": {"color": brand.get("color") or "#ff3d8b",
                  "name": brand.get("name") or "", "logo": brand.get("logo") or ""},
    }


def list_public() -> list:
    return [{"id": k, "name": v["name"], "desc": v["desc"]} for k, v in TEMPLATES.items()]
