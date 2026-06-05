#!/usr/bin/env bash
# 전체 자동 검수 — 스모크 + 렌더 골든(브라우저 불필요) + e2e 골든(playwright).
# 사용: bash tests/run_all.sh    (e2e는 서버 8300 + playwright 있을 때만, 없으면 건너뜀)
set -u
cd "$(dirname "$0")/.."
PY=.venv/bin/python3
FAIL=0

echo "═══ 1) 스모크 (편집→추출→프리뷰→영속성) ═══"
PYTHONPATH="$PWD" "$PY" tests/smoke.py || FAIL=1

echo; echo "═══ 2) 렌더 골든 (회전·비정사각·배경·텍스트스핀·에셋) ═══"
PYTHONPATH="$PWD" "$PY" tests/render_golden.py || FAIL=1

echo; echo "═══ 3) e2e 골든 (편집기 UX) ═══"
if curl -s -o /dev/null -w "%{http_code}" http://localhost:8300/ 2>/dev/null | grep -q 200; then
  TEST_VIDEO=/tmp/oncut_golden/blk.mp4 node tests/e2e.mjs || FAIL=1
else
  echo "  ⚠ 서버(8300) 미응답 — e2e 건너뜀. (.venv/bin/uvicorn app.main:app --port 8300)"
fi

echo
if [ "$FAIL" = "0" ]; then echo "✅ 전체 자동 검수 통과"; else echo "❌ 실패 항목 있음"; fi
exit $FAIL
