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

  // --- 8. JS 에러 없음 ---
  console.log('[8] 콘솔');
  ok(errs.length === 0, 'JS 런타임 에러 없음', errs.slice(0, 2).join(' | '));

  await b.close();
  console.log('\n' + (fails.length ? `✗ 실패 ${fails.length}개: ${fails.join(', ')}` : '전체 통과 ✓ — e2e 골든(회전·타임라인·붙여넣기·우클릭·에셋·AI제안)'));
  return fails.length ? 1 : 0;
};

run().then(c => process.exit(c)).catch(e => { console.error('e2e 실행오류:', e); process.exit(2); });
