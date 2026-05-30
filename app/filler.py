"""잔말/NG 컷 제안 — 전체 대본(단어 타임스탬프) 기반.

원칙(CLAUDE.md §1): 전체 대본을 먼저 보고 판단. 여기선 단어 타임스탬프를 근거로
① 단독 간투사(잔말), ② 즉시 반복(말 더듬음/재시도)을 컷 후보로 제안한다.
사람은 타임라인에서 이 제안을 토글(살리기/자르기)한다 — 자동 삭제 아님.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import List

# 단독으로 나오면 잔말로 보는 간투사
FILLERS = {
    "음", "음.", "어", "어,", "어.", "아", "아,", "에", "에,", "그",
    "저", "뭐", "막", "인제", "이제", "그게", "뭐랄까", "뭐지", "그러니까",
    "어어", "음음", "그그",
}
_PUNCT = re.compile(r"[\s.,!?…~\"'\-]+")


def _norm(word: str) -> str:
    return _PUNCT.sub("", word).strip()


@dataclass
class Cut:
    start: float
    end: float
    reason: str        # "잔말" | "반복"
    text: str


def suggest_cuts(segments: List[dict], *, min_gap: float = 0.0) -> List[dict]:
    """컷 후보 리스트 반환 (dict). 단어 타임스탬프 필요."""
    cuts: List[Cut] = []
    prev_norm = None
    prev_word = None
    for seg in segments:
        for w in seg.get("words", []):
            raw = w.get("word", "")
            n = _norm(raw)
            if not n:
                prev_norm, prev_word = n, w
                continue
            # ① 단독 간투사
            if n in {_norm(f) for f in FILLERS} or raw.strip() in FILLERS:
                cuts.append(Cut(w["start"], w["end"], "잔말", raw.strip()))
            # ② 즉시 반복 (앞 단어와 동일)
            elif prev_norm == n and prev_word is not None:
                cuts.append(Cut(prev_word["start"], prev_word["end"], "반복",
                                prev_word.get("word", "").strip()))
            prev_norm, prev_word = n, w
    # 시작순 정렬 + 인접 컷 병합
    cuts.sort(key=lambda c: c.start)
    merged: List[Cut] = []
    for c in cuts:
        if merged and c.start - merged[-1].end <= 0.05 and c.reason == merged[-1].reason:
            merged[-1].end = max(merged[-1].end, c.end)
            merged[-1].text = (merged[-1].text + " " + c.text).strip()
        else:
            merged.append(c)
    return [asdict(c) for c in merged]
