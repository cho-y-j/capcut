"""말하는 사람 추적용 초점 검출 — 세로(9:16) 리프레이밍 시 인물 중앙 크롭.

OpenCV Haar 정면 얼굴로 샘플 프레임에서 얼굴 위치를 모아 대표 초점(fx,fy 0~1)을
구한다. 얼굴이 없으면 (0.5,0.5)=중앙(기존 동작). 가벼운 정적 오프셋(프레임마다
크롭이 출렁이지 않게 중앙값 사용).
"""
from __future__ import annotations

import subprocess
from statistics import median
from typing import Optional

from . import config


def _cascade():
    try:
        import cv2
        return cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    except Exception:  # noqa: BLE001
        return None


def detect_focus(video: str, start: float = 0.0, end: Optional[float] = None,
                 samples: int = 12) -> tuple[float, float]:
    """[start,end] 구간 샘플 프레임에서 얼굴 중심 중앙값 → (fx, fy) 0~1. 없으면 (0.5,0.5)."""
    cas = _cascade()
    if cas is None:
        return 0.5, 0.5
    try:
        import cv2
        import numpy as np
        from .silence import probe_duration
        dur = end if (end and end > 0) else probe_duration(video)
        a, b = max(0.0, start), max(start + 0.1, dur)
        xs, ys = [], []
        for i in range(1, samples + 1):
            t = a + (b - a) * i / (samples + 1)
            r = subprocess.run([config.FFMPEG, "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1",
                                "-vf", "scale=480:-1", "-f", "image2pipe", "-vcodec", "png", "-"],
                               capture_output=True)
            if not r.stdout:
                continue
            img = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape[:2]
            faces = cas.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5,
                                         minSize=(max(24, w // 12), max(24, h // 12)))
            if len(faces):
                fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])   # 가장 큰 얼굴
                xs.append((fx + fw / 2) / w)
                ys.append((fy + fh / 2) / h)
        if not xs:
            return 0.5, 0.5
        return round(median(xs), 4), round(min(0.6, median(ys)), 4)  # 세로는 살짝 위쪽 허용
    except Exception:  # noqa: BLE001
        return 0.5, 0.5
