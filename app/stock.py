"""무료 스톡 사진·영상 검색 — Pexels / Pixabay. 키는 config/keys.json(어드민 입력).

키 없으면 빈 결과 + provider 가용여부만 알려줘 프론트가 안내. 표준 외부의존 없이
urllib만 사용(서버에 requests 없을 수 있음). 미리보기는 원격 썸네일 URL을 그대로
쓰고, 실제 사용 시점(import)에만 서버가 원본을 내려받아 작업에 붙인다.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import List

from . import llm


def _key(name: str) -> str:
    try:
        from . import auth
        return (auth.eff_key(name) or "").strip()   # 본인 키 우선 → 전역 폴백
    except Exception:  # noqa: BLE001
        try:
            return (llm.keys().get(name) or "").strip()
        except Exception:  # noqa: BLE001
            return ""


def available() -> dict:
    return {"pexels": bool(_key("pexels")), "pixabay": bool(_key("pixabay"))}


def _get(url: str, headers: dict | None = None, timeout: float = 12.0) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _pexels(kind: str, q: str, page: int) -> List[dict]:
    key = _key("pexels")
    if not key:
        return []
    qq = urllib.parse.quote(q or "background")
    hdr = {"Authorization": key}
    out: List[dict] = []
    if kind == "video":
        d = _get(f"https://api.pexels.com/videos/search?query={qq}&per_page=18&page={page}", hdr)
        for v in d.get("videos", []):
            files = sorted(v.get("video_files", []), key=lambda f: (f.get("width") or 0))
            mp4 = next((f["link"] for f in files if (f.get("width") or 0) >= 960), None) \
                or (files[-1]["link"] if files else None)
            if mp4:
                out.append({"id": f"pexels_v_{v['id']}", "provider": "pexels", "kind": "video",
                            "thumb": v.get("image"), "url": mp4,
                            "w": v.get("width"), "h": v.get("height"), "name": "pexels video"})
    else:
        d = _get(f"https://api.pexels.com/v1/search?query={qq}&per_page=24&page={page}", hdr)
        for p in d.get("photos", []):
            src = p.get("src", {})
            out.append({"id": f"pexels_p_{p['id']}", "provider": "pexels", "kind": "photo",
                        "thumb": src.get("medium"), "url": src.get("large2x") or src.get("large") or src.get("original"),
                        "w": p.get("width"), "h": p.get("height"),
                        "name": (p.get("alt") or "pexels photo")[:40]})
    return out


def _pixabay(kind: str, q: str, page: int) -> List[dict]:
    key = _key("pixabay")
    if not key:
        return []
    qq = urllib.parse.quote(q or "background")
    out: List[dict] = []
    if kind == "video":
        d = _get(f"https://pixabay.com/api/videos/?key={key}&q={qq}&per_page=18&page={page}")
        for v in d.get("hits", []):
            vids = v.get("videos", {})
            f = vids.get("medium") or vids.get("small") or vids.get("large") or {}
            if f.get("url"):
                out.append({"id": f"pixabay_v_{v['id']}", "provider": "pixabay", "kind": "video",
                            "thumb": (vids.get("small") or {}).get("thumbnail") or f.get("thumbnail"),
                            "url": f["url"], "w": f.get("width"), "h": f.get("height"), "name": "pixabay video"})
    else:
        d = _get(f"https://pixabay.com/api/?key={key}&q={qq}&image_type=photo&per_page=24&page={page}")
        for p in d.get("hits", []):
            out.append({"id": f"pixabay_p_{p['id']}", "provider": "pixabay", "kind": "photo",
                        "thumb": p.get("webformatURL"), "url": p.get("largeImageURL") or p.get("webformatURL"),
                        "w": p.get("imageWidth"), "h": p.get("imageHeight"),
                        "name": (p.get("tags") or "pixabay")[:40]})
    return out


def search(provider: str, kind: str, q: str, page: int = 1) -> dict:
    """provider: pexels|pixabay|all. kind: photo|video. 실패해도 빈 결과로."""
    kind = "video" if kind == "video" else "photo"
    items: List[dict] = []
    errs = []
    provs = ["pexels", "pixabay"] if provider == "all" else [provider]
    for pv in provs:
        try:
            items += _pexels(kind, q, page) if pv == "pexels" else _pixabay(kind, q, page)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{pv}: {e}")
    return {"items": items, "available": available(), "errors": errs}


def download(url: str, dest: str, timeout: float = 30.0) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ONCUT"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
