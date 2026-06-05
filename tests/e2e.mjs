// 편집기 e2e 골든 — 이번 세션 UX 기능 회귀 방지(브라우저 자동 검수).
// 회전/스핀 보간, 타임라인 길이조절, 붙여넣기, 우클릭 추가메뉴, 에셋 배경,
// AI 제안 카드(적용/무시)를 실제 DOM 이벤트로 검증. 실패 시 비0 종료.
//
// 실행: node tests/e2e.mjs   (서버 http://localhost:8300 + TEST_VIDEO 필요)
import { createRequire } from 'module';
import { pathToFileURL } from 'url';
// playwright가 프로젝트에 없을 수 있음(전역/임시 설치). 여러 경로에서 찾고, 없으면 건너뜀.
let chromium;
const req = createRequire(import.meta.url);
for (const base of [process.env.PW_PATH, '/tmp/node_modules/playwright',
  process.env.HOME + '/.nvm/versions/node/' + process.version + '/lib/node_modules/playwright', 'playwright']) {
  if (!base) continue;
  try { const m = await import(pathToFileURL(req.resolve(base)).href);
    chromium = m.chromium || (m.default && m.default.chromium); if (chromium) break; } catch { /* try next */ }
}
if (!chromium) {
  console.log('⚠ playwright 미설치 — e2e 건너뜀(렌더 골든/스모크로 검증). 설치: npm i -g playwright');
  process.exit(0);
}

const BASE = process.env.BASE || 'http://localhost:8300';
const VIDEO = process.env.TEST_VIDEO || '/tmp/oncut_golden/blk.mp4';
const fails = [];
const ok = (cond, label, extra = '') => {
  console.log((cond ? '  ✓ ' : '  ✗ ') + label + (extra ? `  [${extra}]` : ''));
  if (!cond) fails.push(label);
};

// 편집기 A를 직접 세팅(파일 업로드 없이 단위 검증용)
const SET_A = `(() => {
  document.querySelector('#editorA').classList.remove('hide');
  A={id:'t',duration:10,w:1280,h:720,pxPerSec:40,playIdx:0,thumbs:'',srcMeta:{},
     clips:[{src:'0',srcIn:0,srcEnd:10,transition:{type:'none'}}],cues:[],
     texts:[{id:1,text:'글자',x:.5,y:.5,fontSize:60,start:2,end:6}],
     overlays:[{id:2,token:'x',url:'',name:'😀',x:.5,y:.5,scale:.18,opacity:1,start:7,end:7.5,fade:0,kf:[]}],
     sfx:[],audios:[],pips:[],bgm:{enabled:false},style:{font:'Noto Sans KR'},subsOn:false,sel:null};
  cid=2; save=()=>{}; pushHistory=()=>{};
})()`;

const run = async () => {
  const errs = [];
  const b = await chromium.launch({ channel: 'chrome' });
  const p = await b.newPage();
  p.on('pageerror', e => errs.push('PAGEERR: ' + e.message));

  // --- 1. 랜딩 로드 + 핵심 함수 존재 ---
  console.log('[1] 랜딩 로드 + 함수');
  await p.goto(BASE, { waitUntil: 'networkidle' });
  const fns = await p.evaluate(() => ['kfAt', 'applyKfPreset', 'elRotate', 'buildSuggestions',
    'renderSuggestions', 'addTokenOverlay', 'openAssets', 'rawOpen'].map(n => typeof window[n] === 'function'));
  ok(fns.every(Boolean), '핵심 함수 전역 노출', fns.join(','));

  // --- 2. 회전/스핀 키프레임 보간 + 프리셋 ---
  console.log('[2] 회전·반짝임 키프레임');
  const kf = await p.evaluate(() => {
    const o = { start: 0, end: 4, scale: .3, x: .5, y: .5, kf: [] };
    applyKfPreset(o, 'spin', 'scale'); const spin = o.kf.map(k => k.rot);
    const o2 = { start: 0, end: 4, scale: .3, x: .5, y: .5, kf: [] }; applyKfPreset(o2, 'blink', 'scale');
    const mid = kfAt({ kf: [{ t: 0, rot: 0, x: .5, y: .5 }, { t: 4, rot: 90, x: .5, y: .5 }] }, 2);
    return { spin, blinkN: o2.kf.length, midRot: mid && mid.rot };
  });
  ok(kf.spin[0] === 0 && kf.spin[kf.spin.length - 1] === 360, '스핀 0→360', JSON.stringify(kf.spin));
  ok(kf.blinkN >= 4, '반짝임 다중 키프레임', kf.blinkN);
  ok(Math.abs(kf.midRot - 45) < 1, '회전 중간 보간 45°', kf.midRot);

  // --- 3. 타임라인 길이조절(텍스트 + 짧은 오버레이) ---
  console.log('[3] 타임라인 핸들 길이조절');
  await p.evaluate(SET_A); await p.evaluate(() => redraw());
  const drag = async (lane, cls) => p.evaluate(({ lane, cls }) => {
    const bar = document.querySelector(lane + ' .bar2.' + cls); const h = bar.querySelector('.h.r');
    const r = h.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
    h.dispatchEvent(new PointerEvent('pointerdown', { clientX: x, clientY: y, bubbles: true }));
    document.dispatchEvent(new PointerEvent('pointermove', { clientX: x + 60, clientY: y, bubbles: true }));
    document.dispatchEvent(new PointerEvent('pointerup', { clientX: x + 60, clientY: y, bubbles: true }));
  }, { lane, cls });
  const te0 = await p.evaluate(() => A.texts[0].end); await drag('#laneT', 'text');
  const te1 = await p.evaluate(() => A.texts[0].end);
  ok(te1 > te0, '텍스트 바 끝 드래그로 늘어남', `${te0}->${te1}`);
  const oe0 = await p.evaluate(() => A.overlays[0].end); await drag('#laneO', 'ov');
  const oe1 = await p.evaluate(() => A.overlays[0].end);
  ok(oe1 > oe0, '오버레이(이모지) 바 끝 드래그로 늘어남', `${oe0}->${oe1}`);

  // --- 4. 붙여넣기(이미지→오버레이 라우팅, 텍스트→글상자) ---
  console.log('[4] Ctrl+V 붙여넣기');
  const paste = await p.evaluate(() => {
    let got = null; addOverlayFile = async (f) => { got = f.type; };
    const png = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='), c => c.charCodeAt(0));
    const dt = new DataTransfer(); dt.items.add(new File([png], 'x.png', { type: 'image/png' }));
    document.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true }));
    const n0 = A.texts.length; const dt2 = new DataTransfer(); dt2.setData('text/plain', '붙여넣기글');
    document.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt2, bubbles: true }));
    return { img: got, addedText: A.texts.length - n0, last: A.texts[A.texts.length - 1].text };
  });
  ok(paste.img === 'image/png', '이미지 붙여넣기→오버레이 업로드 라우팅');
  ok(paste.addedText === 1 && paste.last === '붙여넣기글', '텍스트 붙여넣기→글상자');

  // --- 5. 스테이지 우클릭 추가 메뉴(전체 항목) ---
  console.log('[5] 우클릭 추가 메뉴');
  const items = await p.evaluate(() => {
    const st = document.querySelector('.stage'), r = st.getBoundingClientRect();
    st.dispatchEvent(new MouseEvent('contextmenu', { clientX: r.left + r.width * .6, clientY: r.top + r.height * .4, bubbles: true }));
    return [...document.querySelectorAll('.ctxmenu .mi')].map(m => m.textContent);
  });
  const need = ['글자', '이모지', '도형', '에셋', '효과음', '배경음악'];
  ok(need.every(k => items.some(i => i.includes(k))), '우클릭에 글자·이모지·도형·에셋·소리 모두', items.length + '개');
  await p.evaluate(() => closeCtx && closeCtx());

  // --- 6. 에셋 배경 라이브러리 ---
  console.log('[6] 에셋 배경');
  await p.evaluate(() => openAssets()); await p.waitForTimeout(500);
  const bgN = await p.$$eval('#assetGrid > div', e => e.length);
  ok(bgN >= 16, '배경 16종 로드', bgN);
  await p.evaluate(() => document.querySelector('#assetGrid > div').click()); await p.waitForTimeout(200);
  const bgov = await p.evaluate(() => { const o = A.overlays[A.overlays.length - 1]; return { preset: o.preset, s: o.scale, sh: o.scaleH }; });
  ok(bgov.preset && bgov.preset.indexOf('bg_') === 0 && bgov.s === 1 && bgov.sh === 1, '배경 추가(preset+풀커버)', JSON.stringify(bgov));

  // --- 7. AI 제안 카드(실제 raw_open 진입 → 적용/무시) ---
  console.log('[7] AI 제안 카드');
  await p.goto(BASE, { waitUntil: 'networkidle' });
  await p.setInputFiles('#fileC', VIDEO); await p.waitForTimeout(400);
  await p.click('#acRaw');
  await p.waitForFunction(() => { const e = document.querySelector('#editorA'); return e && !e.classList.contains('hide'); }, { timeout: 15000 });
  await p.waitForTimeout(700);
  const shown = await p.evaluate(() => !document.querySelector('#suggestPanel').classList.contains('hide'));
  ok(shown, 'AI 제안 패널 자동 표시');
  const titles = await p.$$eval('#suggestList .sgcard .t', e => e.map(x => x.textContent));
  ok(titles.length >= 2, '제안 카드 ≥2개', titles.length);
  const g0 = await p.evaluate(() => JSON.stringify(A.grade));
  await p.evaluate(() => { const c = [...document.querySelectorAll('#suggestList .sgcard')].find(x => x.querySelector('.t').textContent.includes('색감')); if (c) c.querySelector('.ap').click(); });
  await p.waitForTimeout(400);
  const g1 = await p.evaluate(() => JSON.stringify(A.grade));
  const t2 = await p.$$eval('#suggestList .sgcard .t', e => e.map(x => x.textContent));
  ok(g0 !== g1, '색감 카드 적용→grade 변경', `${g0}->${g1}`);
  ok(!t2.some(t => t.includes('색감')), '적용된 카드 사라짐');
  const before = t2.length;
  await p.evaluate(() => { const c = document.querySelector('#suggestList .sgcard'); if (c) c.querySelector('.ig').click(); });
  await p.waitForTimeout(150);
  const after = await p.$$eval('#suggestList .sgcard .t', e => e.length);
  ok(after === before - 1, '무시→카드 1개 감소', `${before}->${after}`);

  // --- 7b. 진입 자동마감 + 전부적용 + 제목/해시태그 ---
  console.log('[7b] 자동마감·전부적용·제목');
  await p.evaluate(SET_A); await p.evaluate(() => { A.cues = [{ start: 0, end: 2, text: '안녕하세요 제주 여행입니다' }]; A.style.outlineW = 0; redraw(); });
  const ap = await p.evaluate(async () => { const g0 = JSON.stringify(A.grade), o0 = A.style.outlineW; await autoPolish(); return { g0, g1: JSON.stringify(A.grade), o0, o1: A.style.outlineW, flag: A._autoPolished }; });
  ok(ap.g0 !== ap.g1 && ap.o1 >= 2 && ap.flag, '자동마감: 색감+자막 외곽선 자동 적용', `grade${ap.g0!==ap.g1?'✓':'✗'} outline ${ap.o0}->${ap.o1}`);
  const all = await p.evaluate(async () => { const n0 = A.overlays.length + A.texts.length; await applyAllSuggestions(); return { added: (A.overlays.length + A.texts.length) - n0, fmt: A.format }; });
  ok(all.added > 0, '전부 적용: 제안들이 실제 반영(요소 추가)', `+${all.added}`);
  const meta = await p.evaluate(async () => { const r = await fetch('/api/titles', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ script: '제주도 여행 브이로그. 성산일출봉 바다가 예뻤어요. 카페 추천.', format: 'shorts' }) }); return r.json(); });
  ok(!!meta.title && Array.isArray(meta.hashtags) && meta.hashtags.length > 0, '제목·해시태그 추천 반환', `${meta.title} ${(meta.hashtags||[]).join('')}`);

  // --- 7c. 긴 영상 → 숏폼 자동 추출 ---
  console.log('[7c] 숏폼 자동 추출');
  const LONG = process.env.TEST_VIDEO_LONG || '/tmp/oncut_golden/long.mp4';
  await p.goto(BASE, { waitUntil: 'networkidle' });
  await p.evaluate(() => { document.querySelector('[data-mode="a"]').click(); }); // 토킹영상 탭
  await p.waitForTimeout(200);
  await p.setInputFiles('#shortFile', LONG);
  await p.waitForFunction(() => { const e = document.querySelector('#editorA'); return e && !e.classList.contains('hide'); }, { timeout: 20000 });
  await p.waitForTimeout(500);
  const sh = await p.evaluate(() => ({ fmt: A.format, clips: A.clips.length, dur: A.duration }));
  ok(sh.fmt === 'shorts' && sh.clips === 1, '숏폼 추출→9:16 단일 클립 진입', JSON.stringify(sh));

  // --- 7d. 자동 썸네일 ---
  console.log('[7d] 자동 썸네일');
  await p.evaluate(() => showThumb());
  const thumbOk = await p.waitForFunction(() => {
    const i = document.querySelector('#thumbImg'); return i && i.style.display !== 'none' && i.naturalWidth > 100;
  }, { timeout: 15000 }).then(() => true).catch(() => false);
  const dl = await p.$eval('#thumbDl', a => a.getAttribute('href'));
  ok(thumbOk && /\/out\/thumb_/.test(dl || ''), '썸네일 생성→미리보기+다운로드 링크', dl || '(없음)');

  // --- 7e. 멀티 하이라이트 ---
  console.log('[7e] 여러 하이라이트');
  const LONG2 = process.env.TEST_VIDEO_LONG || '/tmp/oncut_golden/long.mp4';
  await p.goto(BASE, { waitUntil: 'networkidle' });
  await p.setInputFiles('#hlFile', LONG2);
  const hlShown = await p.waitForFunction(() => !document.querySelector('#hlModal').classList.contains('hide')
    && document.querySelectorAll('#hlGrid > div').length > 0, { timeout: 20000 }).then(() => true).catch(() => false);
  const cands = await p.$$eval('#hlGrid > div', e => e.length);
  ok(hlShown && cands >= 1, '하이라이트 후보 모달 표시', `${cands}개`);
  await p.evaluate(() => document.querySelector('#hlGrid > div button').click());
  await p.waitForFunction(() => { const e = document.querySelector('#editorA'); return e && !e.classList.contains('hide'); }, { timeout: 20000 }).catch(() => {});
  await p.waitForTimeout(400);
  const hlsh = await p.evaluate(() => ({ fmt: A.format, clips: A.clips.length }));
  ok(hlsh.fmt === 'shorts' && hlsh.clips === 1, '후보 선택→9:16 숏폼 진입', JSON.stringify(hlsh));

  // --- 7f. 자막 번역(LLM 있을 때만) ---
  console.log('[7f] 자막 번역');
  const tr = await p.evaluate(async () => {
    const r = await fetch('/api/translate', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ texts: ['안녕하세요', '구독 부탁해요'], lang: 'en' }) });
    return { status: r.status, body: await r.json() };
  });
  if (tr.status === 503) {
    ok(true, '번역: LLM 미설정 → graceful 503 (스킵)', '');
  } else {
    ok(tr.status === 200 && Array.isArray(tr.body.texts) && tr.body.texts.length === 2
       && tr.body.texts[0] !== '안녕하세요', '번역: 2줄 영어로 변환', (tr.body.texts || []).join(' / '));
  }

  // --- 7g. 캡컷 드래프트 내보내기 ---
  console.log('[7g] 캡컷 드래프트');
  await p.goto(BASE, { waitUntil: 'networkidle' });
  await p.setInputFiles('#shortFile', '/tmp/oncut_golden/long.mp4');
  await p.waitForFunction(() => { const e = document.querySelector('#editorA'); return e && !e.classList.contains('hide'); }, { timeout: 20000 });
  await p.waitForTimeout(500);
  const cap = await p.evaluate(async () => {
    A.texts.push({ id: ++cid, text: '캡컷 텍스트', x: .5, y: .3, fontSize: 60, start: 0, end: 3, outlineW: 3, color: '#ffffff', bold: true });
    const r = await fetch('/api/capcut', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload()) });
    return { status: r.status, body: await r.json() };
  });
  ok(cap.status === 200 && cap.body.zip && /\.zip$/.test(cap.body.zip) && cap.body.segments >= 1,
     '캡컷 드래프트 ZIP(미디어포함) 생성', `${cap.body.segments}seg zip=${(cap.body.zip || '').split('/').pop()}`);

  // --- 7h. 에디터 템플릿 적용 ---
  console.log('[7h] 에디터 템플릿 적용');
  await p.goto(BASE, { waitUntil: 'networkidle' });
  await p.setInputFiles('#fileC', VIDEO); await p.waitForTimeout(400);
  await p.click('#acRaw');
  await p.waitForFunction(() => { const e = document.querySelector('#editorA'); return e && !e.classList.contains('hide'); }, { timeout: 15000 });
  await p.waitForTimeout(400);
  await p.evaluate(() => showEditorTpl());
  await p.waitForFunction(() => document.querySelectorAll('#etplGrid .tplcard').length > 0, { timeout: 8000 });
  const tplN = await p.$$eval('#etplGrid .tplcard', e => e.length);
  const tb = await p.evaluate(() => JSON.stringify({ g: A.grade, ly: A.layout }));
  await p.evaluate(() => document.querySelector('#etplGrid .tplcard').click());
  await p.waitForTimeout(300);
  const ta = await p.evaluate(() => JSON.stringify({ g: A.grade, ly: A.layout }));
  ok(tplN >= 1 && tb !== ta, '템플릿 적용→색감/레이아웃 변경', `${tplN}개 ${tb}→${ta}`.slice(0, 90));

  // --- 7i. 홈(자동저장 후 랜딩) ---
  console.log('[7i] 홈 버튼');
  await p.evaluate(() => { $('#homeA').click(); });
  const home = await p.waitForFunction(() =>
    document.querySelector('#editorA').classList.contains('hide') &&
    !document.querySelector('#start').classList.contains('hide'), { timeout: 8000 }).then(() => true).catch(() => false);
  ok(home, '홈→에디터 숨김+랜딩 표시(작업 자동저장)');

  // --- 7j. 미리보기 클릭→클립 선택 + 배경 띠 제거 ---
  console.log('[7j] 클립 선택/배경 제거');
  await p.goto(BASE, { waitUntil: 'networkidle' });
  await p.evaluate(SET_A); await p.evaluate(() => { A.playIdx = 0; A.layout = { videoY: .12, videoH: .6, bg: '#1f1d3d' }; redraw(); });
  await p.evaluate(() => { const st = document.querySelector('.stage'); st.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  const selClip = await p.evaluate(() => A.sel && A.sel.type === 'clip');
  ok(selClip, '미리보기 클릭→클립 선택');
  const hadLayout = await p.evaluate(() => !!A.layout);
  await p.evaluate(() => { const b = document.querySelector('#fC_nobg'); if (b) b.click(); });
  const noLayout = await p.evaluate(() => A.layout === null);
  ok(hadLayout && noLayout, '배경 띠 제거 버튼 동작');

  // --- 8. JS 에러 없음 ---
  console.log('[8] 콘솔');
  ok(errs.length === 0, 'JS 런타임 에러 없음', errs.slice(0, 2).join(' | '));

  await b.close();
  console.log('\n' + (fails.length ? `✗ 실패 ${fails.length}개: ${fails.join(', ')}` : '전체 통과 ✓ — e2e 골든(회전·타임라인·붙여넣기·우클릭·에셋·AI제안)'));
  return fails.length ? 1 : 0;
};

run().then(c => process.exit(c)).catch(e => { console.error('e2e 실행오류:', e); process.exit(2); });
