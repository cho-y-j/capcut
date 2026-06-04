# ONCUT(영상 편집기) — 임베드 통합 플랜 (Embed Module 전략, B안)

> 작성 2026-06-04. 짝 문서: `/home/cho/pro/onimage/INTEGRATION-PLAN.md`.
> **결정: (B) 각자 임베드 모듈.** ONCUT(영상)·onimage(이미지/블로그)는 한 셸로 합치지
> 않고, **각각 독립 임베드 가능한 공유 모듈 1개씩**으로 만들어 호스트(marketing-pro,
> mybot, 단독)가 필요할 때 끼워 쓴다. 두 모듈은 **같은 임베드 계약·브랜드키트·owner
> 스코프·디자인 라이브러리 스키마**를 공유해 "한 세트"처럼 동작한다.

---

## 0. 한 줄 원칙
**영상 편집기 = 하나의 공유 모듈. 인증·고객·데이터의 "두뇌"는 호스트가 가진다.**
호스트가 `owner/brand/seed`를 주입 → ONCUT이 1차 완성/편집 → 결과를 호스트로 `publish`.

## 1. 현재 상태 (2026-06-04 감사 결과)
**된 것 (엔진·UX):**
- AI 첫 컷(소재+목적+요청 → 대본 확정 → 1차 완성), 편집기(컷·자막·텍스트·로고·오디오·
  PIP·키프레임·색보정·크로마키·마스크), 미리보기=추출 일치, 드래프트 자동저장/복원,
  템플릿 엔진 4종 + 브랜드키트({color,name,logo}, `{{brand.*}}` 치환), onimage 통일 디자인.

**없는 것 (임베드 인프라) — grep 0건 확인:**
- `owner / embed / postMessage / iframe` 전무 → 완전 독립 앱
- 인증·레이트리밋 없음 (현재 공개 노출 = 리스크)
- 동시처리 제한 없음 (ffmpeg N개 동시 = CPU 폭주)
- 정리(cleanup/TTL) 없음 → uploads/out/_jobs.json 무한 누적
- 저장 = 평면파일(JOBS 메모리+json, drafts 파일), DB 아님 → 멀티인스턴스 불가
- 저장 포맷이 onimage `/designs`와 다름 (통일 필요)

## 2. 아키텍처 (B안)
```
[marketing-pro web]   [mybot 홈페이지]   [단독]
        \                  |                /
         \   iframe + postMessage (공통 계약 v1)
          \________________|______________/
              v                        v
   onimage 편집기(이미지/블로그)   ONCUT 편집기(영상)   ← 각각 공유 모듈 1개
        - 같은 계약 v1            - 같은 계약 v1
        - 같은 브랜드키트/owner 스코프/디자인 라이브러리 스키마
        - 공유 LLM 게이트웨이(host:8900) 지향
```
- 인증/계정/고객관리: **호스트 보유**(marketing-pro=Firebase, mybot=자체). 모듈은 안 만든다.
- 모듈은 `owner`로 저장 스코프만 분리.

## 3. 임베드 계약 (Embed Contract v1) — ★onimage와 100% 동일하게
> onimage INTEGRATION-PLAN §3과 **같은 메시지/URL 규약**을 ONCUT도 그대로 구현한다.
> (필드 추가는 하위호환 OK, 제거/의미변경은 `/embed/v2`)

### 3.1 임베드 URL
```
https://<oncut>/embed?owner=<ownerId>&mode=<shorts|promo|free>&host=<marketing-pro|mybot|standalone>
```
- `owner` 필수: 저장/불러오기 스코프(호스트가 발급)
- `mode`: 시작 프리셋(쇼츠/홍보/자유) — ONCUT 템플릿 id와 매핑

### 3.2 postMessage API (onimage와 공통)
- 편집기 → 호스트
  - `{type:'editor:ready'}`
  - `{type:'editor:publish', kind:'video', url|dataUrl, designId}` — "완료/추출" 시
  - `{type:'editor:saved', designId}`
- 호스트 → 편집기
  - `{type:'editor:init', owner, brand:{color,logo,name,font}, seed:{prompt?,media?[],template?}, mode}`
  - `{type:'editor:loadDesign', designId}`
- 규칙: **모르는 필드 무시(전방호환)**. 새 기능은 필드 추가.

### 3.3 저장 스코프 / 디자인 라이브러리 (onimage `/designs`와 통일)
- 현재 `uploads/drafts/{id}.json` 평면 → **owner별 스코프** + onimage와 같은 레코드 스키마
  `{id, name, template, cat:'video', thumb, data, savedAt, owner}`.
- 공개 템플릿(슈퍼어드민)은 `owner=__public__` + 업종 태그.

## 4. 브랜드 키트 + 업종 템플릿 팩 (확장의 핵심, onimage와 공유)
- **브랜드 키트**: `{color, logo, name, font}` — 이미 ONCUT 구현(config/brandkit.json).
  임베드에선 **호스트가 `editor:init.brand`로 주입** → owner별 보관으로 전환.
- **업종 팩(영상)**: 카페/병원/부동산/정수기… 업종별 **영상 템플릿 20종**을 1회 제작
  (현재 ONCUT 템플릿 4종 = 기반). `{{brand.*}}` 토큰으로 회사별 자동 치환.
- onimage(이미지)·ONCUT(영상) 업종 팩은 **같은 브랜드 키트**를 공유 → 한 회사가
  이미지·영상 일관 톤.

## 5. 호스트별 연동
- **marketing-pro(분석)**: 분석결과(키워드·타겟·카피) → `editor:init.seed.prompt`로 자동 초안 →
  `editor:publish`로 캠페인 영상 슬롯에 연결. 인증=Firebase(호스트).
- **mybot 홈페이지**: 관리자 임베드, SSO. 홈페이지 로고/제품 = 브랜드키트 공급.
  publish → 홈/배너 영상 슬롯 게시.
- **단독**: 자체 경량 로그인은 마지막(호스트 임베드 우선).

## 6. 단계별 로드맵 (★ blocker 순)
1. **임베드 모드 + 계약 v1 + owner 스코프** ← 모든 연동의 토대 (drafts/brandkit/uploads/jobs를 owner별로)
2. **프로덕션 하드닝**: 동시처리 큐(세마포어) · cleanup(TTL) · 인증/레이트리밋 · DB 전환(파일→SQLite)
3. **publish 흐름** + 브랜드키트 `init` 주입 + iframe 호환(헤더/풀스크린 처리)
4. **업종 템플릿 팩 20종 + 슈퍼어드민**(팩 발행·테넌트·브랜드키트 관리)
5. **호스트 실연동**(marketing-pro seed→publish, mybot SSO→publish)
6. (제품) 음악 라이브러리, 영상 하이라이트 자동선별(비전), 템플릿 깊이

## 7. 임베드 전 반드시 메울 격차 (체크리스트)
- [ ] `/embed` 진입점 + postMessage 계약 v1 (onimage와 동일)
- [ ] owner 스코프: drafts·brandkit·uploads·jobs 분리 (`?owner=`)
- [ ] publish: 추출 결과를 호스트로 반환(url/designId)
- [ ] 동시처리 제한(렌더 세마포어) — 가정용/단일서버 보호
- [ ] cleanup/TTL — uploads·out·jobs 자동 정리
- [ ] 인증/레이트리밋 (최소 owner 토큰 검증) — 공개 노출 리스크 해소
- [ ] 디자인 라이브러리 스키마 onimage `/designs`와 통일
- [ ] (선택) 공유 LLM 게이트웨이(host:8900)로 전환
- [ ] DB 전환(파일→SQLite/PG) — 멀티인스턴스/스케일

## 8. 업그레이드 정책 (onimage와 동일 규칙)
- 모듈 내부 기능 추가/수정 → **계약 안 건드리면 자유**(restart 즉시 반영).
- 계약 변경 → 필드 **추가만**(하위호환). 불가피하면 `/embed/v2` 신설.
- 배포 = 단일 이미지/서비스. 검수(문법·서빙·핵심 동작) 후 반영.

## 9. 다음 착수
**로드맵 1단계(임베드 모드 + 계약 v1 + owner 스코프)** 부터. onimage와 **같은 계약**을
구현하는 게 핵심 — 두 모듈이 같은 호스트 코드로 끼워진다.
