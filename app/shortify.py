"""긴 영상 → 숏폼 자동 추출. AI(자막)로 가장 말이 알찬 구간을 골라 9:16 클립으로.

하이라이트 선택 체인(가용한 것부터):
  ① ASR 세그먼트 밀도 — target초 창에서 글자수 최대(말 많은=알찬 구간). 우리 니치(말영상)에 강함.
  ② 오디오 에너지(waveform.peaks) — 말 없는 영상도 시끌한 구간 우선.
  ③ 휴리스틱 — 도입부 살짝 건너뛴 창.
9:16 변환은 기존 format=shorts 커버크롭(render canvas)으로 처리(중앙 크롭).
"""
from __future__ import annotations

from typing import List, Optional

from . import config, waveform
from .silence import probe_duration


def _density_window(segs: List[dict], dur: float, target: float) -> tuple[float, float]:
    """세그먼트 시작점마다 [t, t+target] 글자수 합 최대 창 선택."""
    starts = sorted({max(0.0, float(s["start"])) for s in segs})
    starts = [t for t in starts if t <= max(0.0, dur - target)] or [0.0]
    best_t, best_score = starts[0], -1.0
    for t in starts:
        a, b = t, t + target
        score = sum(len((s.get("text") or "")) for s in segs
                    if float(s["end"]) > a and float(s["start"]) < b)
        if score > best_score:
            best_score, best_t = score, t
    return best_t, min(dur, best_t + target)


def _energy_window(path: str, dur: float, target: float) -> tuple[float, float]:
    """오디오 진폭 피크 합이 최대인 창."""
    try:
        pk = waveform.peaks(path, buckets=600)
    except Exception:  # noqa: BLE001
        pk = []
    if not pk:
        start = min(dur * 0.1, max(0.0, dur - target))   # 휴리스틱: 도입부 살짝 건너뜀
        return start, min(dur, start + target)
    n = len(pk)
    win = max(1, int(n * target / dur))
    pre = [0.0]
    for v in pk:
        pre.append(pre[-1] + v)
    best_i, best_s = 0, -1.0
    for i in range(0, max(1, n - win + 1)):
        s = pre[i + win] - pre[i]
        if s > best_s:
            best_s, best_i = s, i
    start = best_i / n * dur
    return start, min(dur, start + target)


def pick_highlight(path: str, target: float = 30.0):
    """→ (start, end, segments|None). segments는 ASR 성공 시 자막 재사용용."""
    dur = probe_duration(path)
    if dur <= target * 1.12:                 # 이미 충분히 짧으면 통째로
        return 0.0, dur, None
    segs = None
    try:
        from . import asr
        tr = asr._transcribe_sync(path, config.WHISPER_MODEL, "ko")
        segs = tr.get("segments") or None
    except Exception:  # noqa: BLE001
        segs = None
    if segs:
        s, e = _density_window(segs, dur, target)
    else:
        s, e = _energy_window(path, dur, target)
    return round(s, 2), round(e, 2), segs


def window_cues(segs: Optional[List[dict]], start: float, end: float) -> List[dict]:
    """창 구간 세그먼트 → 출력 타임라인(0 기준) 자막."""
    if not segs:
        return []
    from . import subtitle
    win = [{"start": max(0.0, float(s["start"]) - start),
            "end": min(end - start, float(s["end"]) - start),
            "text": (s.get("text") or "").strip()}
           for s in segs if float(s["end"]) > start and float(s["start"]) < end
           and (s.get("text") or "").strip()]
    try:
        cues = subtitle.build_cues([{"start": c["start"], "end": c["end"], "text": c["text"],
                                     "words": []} for c in win])
        return [{"start": c.start, "end": c.end, "text": c.text} for c in cues]
    except Exception:  # noqa: BLE001
        return win
