"""계정/인증 — 무료 가입, 로그인(쿠키 세션), API 키(임베딩·프로그램 연동).

외부 DB 없이 config/users.json 1개로. 비밀번호는 pbkdf2 솔트 해시. 사용자별 격리는
main에서 owner로 처리. 인증 경로 2가지를 모두 연다:
  ① 쿠키 세션  — 단독 사용(가입/로그인 화면)
  ② API 키      — 다른 프로그램 임베딩(iframe ?key=, 또는 X-API-Key 헤더로 REST 호출)
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

from . import config

AUTH_FILE = config.BASE_DIR / "config" / "users.json"
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(AUTH_FILE.read_text(encoding="utf-8")) if AUTH_FILE.exists() else {}
        except Exception:  # noqa: BLE001
            _cache = {}
        _cache.setdefault("users", {})
        _cache.setdefault("sessions", {})
    return _cache


def _save() -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")


def _hash(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), bytes.fromhex(salt), 120000).hex()


def signup(email: str, pw: str) -> str:
    d = _load()
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("이메일 형식을 확인하세요")
    if len(pw or "") < 4:
        raise ValueError("비밀번호는 4자 이상")
    if any(u["email"] == email for u in d["users"].values()):
        raise ValueError("이미 가입된 이메일이에요")
    uid = secrets.token_hex(8)
    salt = secrets.token_hex(16)
    d["users"][uid] = {"email": email, "salt": salt, "pwhash": _hash(pw, salt),
                       "api_key": secrets.token_urlsafe(24), "created": int(time.time())}
    _save()
    return uid


def login(email: str, pw: str) -> str:
    d = _load()
    email = (email or "").strip().lower()
    for uid, u in d["users"].items():
        if u["email"] == email and secrets.compare_digest(u["pwhash"], _hash(pw, u["salt"])):
            return new_session(uid)
    raise ValueError("이메일 또는 비밀번호가 틀려요")


def new_session(uid: str) -> str:
    d = _load()
    tok = secrets.token_urlsafe(32)
    d["sessions"][tok] = {"uid": uid, "t": int(time.time())}
    _save()
    return tok


def logout(tok: str) -> None:
    d = _load()
    if tok and tok in d["sessions"]:
        d["sessions"].pop(tok, None)
        _save()


def _uid_by_session(tok: str | None):
    if not tok:
        return None
    s = _load()["sessions"].get(tok)
    return s["uid"] if s else None


def _uid_by_apikey(key: str | None):
    if not key:
        return None
    for uid, u in _load()["users"].items():
        if u.get("api_key") and secrets.compare_digest(u["api_key"], key):
            return uid
    return None


def uid_from_request(request) -> str | None:
    """① X-API-Key/Bearer(프로그램·임베딩) → ② 쿠키 세션(단독). 순서대로."""
    key = request.headers.get("x-api-key") or ""
    if not key:
        a = request.headers.get("authorization", "")
        if a.lower().startswith("bearer "):
            key = a[7:].strip()
    if key:
        uid = _uid_by_apikey(key)
        if uid:
            return uid
    return _uid_by_session(request.cookies.get("oncut_session"))


def user_info(uid: str | None):
    if not uid:
        return None
    u = _load()["users"].get(uid)
    return {"id": uid, "email": u["email"], "api_key": u["api_key"],
            "created": u["created"]} if u else None


def list_users() -> list:
    d = _load()
    return [{"id": uid, "email": u["email"], "created": u.get("created", 0)}
            for uid, u in sorted(d["users"].items(), key=lambda kv: -kv[1].get("created", 0))]


def delete_user(uid: str) -> None:
    d = _load()
    d["users"].pop(uid, None)
    for t in [t for t, s in d["sessions"].items() if s.get("uid") == uid]:
        d["sessions"].pop(t, None)
    _save()
