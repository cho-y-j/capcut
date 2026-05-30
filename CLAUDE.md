# 캡컷 에이전트 (CapCut Agent) — 하이브리드 (C안)

> 한국어 토킹 영상 자동 편집기. 입력 mp4/mov →
> **① 브라우저 웹 편집기에서 AI 자동편집 + 사람 마킹 + MP4 추출 (주력, 全 OS)**
> **② 원하면 캡컷 드래프트도 출력 (Win/Mac 핸드오프, 보너스)**
> 형태: 로컬 웹 (FastAPI + 정적 HTML 1장). 엔진은 ①②가 공유.

---

## 0. 정체성 / 채널 메시지
- 빌드 파트너: 자동화 영상 편집 도구를 함께 만든다.
- 콘텐츠 금광 = **"AI 자동 / 사람 손 경계"**. 어디까지 자동이고 어디부터 사람이
  마킹하는지가 채널 서사의 핵심. → C안에선 그 경계가 **우리 화면 안**(타임라인
  토글)에서 일어나므로 서사가 더 강력함.
- 타깃: **초보자**. "말 영상 컷편집 + 자막"이라는 한 가지를 5분 만에. 복잡한
  전환·이펙트·음악 라이브러리는 의도적으로 배제(그건 캡컷 핸드오프로).

## 1. 핵심 원칙 (절대 위반 금지)
1. **전체 대본(script) 먼저 추출 → 그걸로 NG/자막 결정.** 단어 단위로 즉흥
   판단하지 않는다. 세그먼트+단어 타임스탬프는 자막 정렬용일 뿐, 컷 판단의
   1차 근거는 "추출된 전체 대본"이다.
2. **검증 = 결과물을 실제로 재생.** 빌드 성공 ≠ 검증.
   - ① 웹 편집기 경로: **브라우저 미리보기 + 추출된 MP4 재생**으로 검증 (全 OS,
     리눅스 포함 완전한 루프).
   - ② 캡컷 경로: 사용자가 Win/Mac 캡컷에서 직접 열어 재생해 검증.
3. 기술 함정은 §7(기술 함정 노트)을 자동 참조. (원래 `pycapcut-mac` 스킬
   대상이나 이 환경에 부재 → §7에 직접 인코딩함.)

## 2. 환경 / 트랙 결정  (Step 0, 2026-05-30 점검)
- 감지: `Linux x86_64` → 기획상 `Darwin x86_64 / 그 외` → **fallback 트랙**.
- **fallback 트랙 = faster-whisper 사용** (mlx-whisper는 Mac 전용, 여기선 불가).
- 설치 상태: python3.12 ✓ / venv ✓ / pip ❌ / ffmpeg ❌ / pycapcut ❌ /
  CapCut 데스크톱 ❌ (리눅스 버전 자체가 없음).
- **리눅스 제약**: CapCut 데스크톱이 없으므로 이 머신에서 재생 검증 불가.
  드래프트는 여기서 생성 → `OUTPUT_DRAFT_DIR`에 떨어뜨림 → 사용자가 Win/Mac의
  CapCut Projects 폴더로 동기화해 연다.
  - Win 경로: `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft\`
  - Mac 경로: `~/Movies/CapCut/User Data/Projects/com.lveditor.draft/`

### 설치 명령 (사용자 실행 대기 — 빠진 것만)
```bash
# pip + ffmpeg (sudo 경로)
sudo apt update && sudo apt install -y python3-pip python3-venv ffmpeg
```
- sudo 없이 가려면: venv `ensurepip`로 pip 확보 + ffmpeg static 빌드 다운로드
  (johnvansickle amd64) 가능. 자동화 모드에선 이 무-sudo 경로를 우선 시도.

## 3. 빌드 적층 (하이브리드 C안 — 각 단계 실제 재생 검증 후 다음으로)
1. **1단 (엔진 코어)**: `silence_detect` → 보존/컷 세그먼트 산출 + ffmpeg로
   **점프컷 MP4 추출**. UI 없음. 가장 중요한 핵심 검증 (리눅스에서 MP4 재생).
2. **2단 (웹 셸)**: FastAPI + 정적 HTML 1장 (drag/drop + SSE stepper) +
   업로드→처리→다운로드 루프.
3. **3단 (자막)**: whisper Transcript(세그먼트+단어) + **세그먼트 단위** 자막 →
   편집기 자막 패널 + MP4에 자막 번인 옵션.
4. **4단 (AI 컷 + 사람 마킹)**: `filler_ng` + `cuts` → AI 컷 제안을 타임라인
   스트립에 표시 → 사람이 클릭/`[`·`]`로 살리기·자르기 토글, 자막 인라인 수정.
   ("AI 자동 / 사람 손 경계"의 핵심 화면.)
5. **5단 (미리보기+추출)**: 남긴 구간만 자막 얹어 브라우저 재생 + 최종 **MP4 추출**
   (자막 번인 또는 .srt 사이드카).
6. **6단 (보너스, 캡컷 어댑터)**: 동일 편집 결과를 `pycapcut` 캡컷 드래프트로도
   출력 → Win/Mac 핸드오프. 엔진/편집결과는 ①과 공유.

## 4. 런타임 단계 (영상 1개 처리 시 SSE 이벤트)
순서: `silence → asr → filler → draft+자막`
- **ASR은 `asyncio.Lock`으로 직렬화.** numba 비안전 → 동시 호출 시 segfault.
- 단계당 **최소 0.5s 강제 지연** (캐시 hit 시에도 애니메이션 가시화).
- **ASR 캐시는 content hash(파일 내용 해시) 기반.** mtime 기반은 매 업로드 miss.

## 5. 디자인 톤
- Linear / Vercel / Notion 모노 + 그린 `#22c55e` **1색 액센트만**.
- 다크 배경 `#0a0a0a` + 카드 `#161616`. 숫자는 `tabular-nums`.
- ❌ 그라데이션 / 좌측 컬러 띠 / 큰 ✓ / 박스 안에 박스
- ✓ 6px 점 + 글로우 / 3컬럼 grid 통계 / 1px hr 구분선 / 코드 칩

## 6. 아키텍처 (파일 레이아웃, 계획)
```
capcut/
  CLAUDE.md            ← 이 문서 (단일 진실)
  .venv/               ← faster-whisper, fastapi, uvicorn, pycapcut, ...
  bin/ffmpeg           ← 무-sudo static 빌드 (없으면 다운로드)
  app/
    main.py            ← FastAPI + SSE 엔드포인트 (업로드/처리/추출/다운로드)
    silence.py         ← ffmpeg silencedetect 래퍼 → 세그먼트 산출
    asr.py             ← faster-whisper (asyncio.Lock 직렬화 + content-hash 캐시)
    filler.py          ← 잔말/NG 대본 분석 → 컷 구간 산출
    render.py          ← ① ffmpeg 점프컷 MP4 추출 (자막 번인/.srt)
    draft.py           ← ② pycapcut로 draft_info.json 생성 (캡컷 핸드오프)
    subtitle.py        ← 한국어 의존명사 청크 줄바꿈 + .srt 생성
    static/index.html  ← 정적 1장 (drag/drop + SSE stepper + 타임라인 편집기)
  cache/asr/           ← {content_hash}.json
  out/                 ← ① 추출 MP4  ② 캡컷 드래프트 (OUTPUT_DRAFT_DIR)
  samples/             ← 테스트용 mp4/mov
```
- 설정: `OUTPUT_DIR`(기본 `./out`), `WHISPER_MODEL`(기본 자원에 맞춰 `medium`,
  여유 있으면 `large-v3`), `SILENCE_DB`(기본 -30dB), `SILENCE_MIN`(기본 0.4s).
- **출력 모드**: `mode=mp4`(주력, 全 OS) / `mode=capcut`(Win/Mac 드래프트).
  캡컷 모드면 OS 감지해 Projects 폴더 자동 탐지, 없으면 `./out`.

## 9. Git
- 리모트: `https://github.com/cho-y-j/capcut.git`
- `.gitignore`: `.venv/`, `bin/ffmpeg`, `cache/`, `out/`, `samples/`,
  `__pycache__/`, `*.mp4`, `*.mov` (모델·바이너리·미디어 비추적).
- 단계별로 의미 있는 커밋 후 푸시.

## 7. 기술 함정 노트  (pycapcut-mac 스킬 대체본)
- **단위**: pycapcut 시간은 **마이크로초(µs)**. `tim("1s")`/`trange` 헬퍼 사용.
- **`draft_info.json`**: 캡컷이 읽는 핵심 파일. 드래프트 폴더명 = 프로젝트명.
  폴더 안에 `draft_info.json`(+`draft_meta_info.json`) 생성.
- **`tm_duration` / 타임라인 길이**: 트랙·세그먼트 길이 합이 타임라인 총 길이와
  일치해야 캡컷이 깨지지 않음. 세그먼트 target_timerange 누적 검증.
- **`transform_y`**: 자막 세로 위치. 화면 하단 안전영역(예: +0.6~+0.8 정규화)로.
- **샌드박스**: macOS 캡컷은 샌드박스 → 외부 경로 영상 참조 시 권한 문제 가능.
  리눅스 생성분을 Mac에 옮길 때 **영상 파일 절대경로**가 대상 머신에 유효해야 함
  (영상도 함께 동기화하거나 캡컷이 접근 가능한 경로에 둘 것).
- **ASR Lock**: faster-whisper/numba 동시 실행 segfault → `asyncio.Lock` 직렬화.
- **의존명사 청크**: 한국어 자막 줄바꿈 시 "것/수/때/줄/뿐" 등 의존명사는 앞
  어절과 붙여 끊는다 (어절 단위로만 자르면 어색). subtitle.py에서 처리.
- **content-hash 캐시**: 업로드 임시파일은 mtime이 매번 바뀜 → 내용 해시로 캐시.

## 8. 자동화 계약 (★중요)
- 사용자가 **"자동화로 진행"** 이라고 말하면: 그 시점부터 위 기획을 기준으로
  **모든 결정을 스스로 내려 1→5단을 자율 진행**한다. 중간 확인을 최소화하되,
  - 캡컷 재생 검증이 필요한 지점은 산출물(드래프트 폴더 경로)을 명확히 제시하고
    사용자가 동기화·재생할 수 있게 안내한 뒤 계속 진행.
  - 비가역/위험 작업(sudo, 외부 전송)은 무-sudo 경로 우선.
- "자동화로 진행" 전까지는 단계별로 확인받는다.
```
