'use strict';
// 앱 화면 언어 (2026-11-24 영문판 2단계, codex1 협의). 화면 코드는 한국어 원문을 그대로 쓰고,
// 그려진 글자(글자 마디·title·placeholder·aria-label·alt)를 사전(i18n-en.js)으로 바꾼다. 사전에 없으면 원문 그대로 — 깨지지 않는다.
// 문장 가운데 값이 끼는 곳은 코드에서 VaderI18n.t('원문 {0} …', 값) 으로 쓴다 — 사전 키도 그 원문이다.
// 사용자가 쓴 글(대화·메신저·할 일·파일 이름)은 [data-raw] 안이라 건드리지 않는다. 언어는 코어가 정한다(기기 설정 → Windows 표시 언어).
(() => {
 const EN = window.VADER_EN || {};
 let lang = 'ko';
 const ATTRS = ['title', 'placeholder', 'aria-label', 'alt'];
 const HANGUL = /[가-힣]/;
 const own = (o, k) => o != null && Object.prototype.hasOwnProperty.call(o, k);   // 상속 속성은 사전이 아니다 (codex1 A·4·5 3차)
 const fill = (text, args) => text.replace(/\{(\d+)\}/g, (_, i) => String(args[i] ?? ''));
 // 값이 끼는 문장('{0}개 적용했어요.')은 글자 마디 전체가 그 모양일 때 바꾼다. 끼인 값은 그대로 둔다 —
 // 사용자가 지은 이름('승인' 그룹)이 사전 낱말과 같아도 바뀌지 않게 (codex1 2단계 3차).
 const compile = k => new RegExp('^' + k.split(/\{\d+\}/).map(s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('(.*?)') + '$', 's');
 const PATTERNS = Object.keys(EN).filter(k => /\{\d+\}/.test(k)).map(k => {
  const order = [...k.matchAll(/\{(\d+)\}/g)].map(m => +m[1]);
  const re = compile(k);
  // 고정 한글이 짧은 틀('{0} 실행')은 끼인 값에 번역 못 할 한글이 있으면 쓰지 않는다 — 사용자가 쓴 '회의 실행' 을 바꾸지 않게
  const strict = (k.replace(/\{\d+\}/g, '').match(/[가-힣]/g) || []).length < 4;
  return [re, order, EN[k], strict];
 });
 function word(text) {
  if (lang === 'ko' || !text || !HANGUL.test(text)) return text;
  const core = text.trim();
  let en = own(EN, core) ? EN[core] : null;
  if (en == null) for (const [re, order, out, strict] of PATTERNS) {
   const m = core.match(re);
   if (!m) continue;
   const args = [], holes = m.slice(1);
   if (strict && holes.some(h => HANGUL.test(h))) continue;
   order.forEach((n, i) => { args[n] = holes[i]; });
   en = fill(out, args); break;
  }
  return en == null ? text : text.replace(core, () => en);
 }
 function t(template, ...args) {
  return fill(lang === 'en' && own(EN, template) ? EN[template] : template, args);
 }
 const raw = node => node.parentElement?.closest('[data-raw],script,style,textarea');
 // 이 층이 쓴 글자는 다시 번역하지 않는다 — 번역문 끝의 사용자 값이 다른 틀('{0} 실행')에 걸리지 않게 (codex1 2단계 4차)
 const mine = new WeakMap();
 function text(n) {
  if (raw(n) || mine.get(n) === n.nodeValue) return;
  const v = word(n.nodeValue);
  if (v === n.nodeValue) return;
  // value 없는 <option> 은 글자가 곧 보낼 값이다 — 바꾸기 전에 원문을 value 로 박는다 (codex1 2단계 1차 P1-1)
  const opt = n.parentElement;
  if (opt?.tagName === 'OPTION' && !opt.hasAttribute('value')) opt.setAttribute('value', opt.textContent);
  n.nodeValue = v;
  mine.set(n, v);
 }
 const mineAttr = new WeakMap();
 function attr(el, a) {
  if (el.closest('[data-raw]')) return;
  const v = el.getAttribute(a), seen = mineAttr.get(el) || {};
  if (!v || !HANGUL.test(v) || seen[a] === v) return;
  const w = word(v);
  if (w !== v) { seen[a] = w; mineAttr.set(el, seen); el.setAttribute(a, w); }
 }
 function walk(root) {
  if (lang === 'ko' || !root) return;
  if (root.nodeType === 3) return text(root);
  if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
  const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = tw.nextNode(); n; n = tw.nextNode()) text(n);
  const els = root.querySelectorAll ? [root.nodeType === 1 ? root : null, ...root.querySelectorAll('[title],[placeholder],[aria-label],[alt]')] : [];
  for (const el of els) if (el) for (const a of ATTRS) attr(el, a);
 }
 const observer = new MutationObserver(list => {
  for (const m of list) {
   if (m.type === 'characterData') walk(m.target);
   else if (m.type === 'attributes') attr(m.target, m.attributeName);   // 바뀐 속성 하나만
   else m.addedNodes.forEach(walk);
  }
 });
 function apply(next) {
  lang = next === 'en' ? 'en' : 'ko';
  document.documentElement.lang = lang;
  if (lang === 'en') {
   walk(document.body);
   observer.observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ATTRS});
  }
  document.documentElement.dataset.i18n = 'ready';
  done();
 }
 let done;
 const ready = new Promise(r => { done = r; });   // 화면 코드는 언어가 정해진 뒤에 처음 그린다 — t() 문장이 한국어로 박히지 않게
 // 룬 문구 — 설치된 그 버전 룬 패키지 안 i18n_en.py 를 코어가 카탈로그로 보낸다(setRunes). 같은 틀 규칙, 끼인 값은 그대로. 없으면 원문.
 let RUNES = {};
 let runePatterns = new Map();   // 룬 id 가 'constructor' 여도 상속 속성과 섞이지 않게 (codex1 후속 2차)
 function setRunes(map) { RUNES = Object.assign(Object.create(null), map || {}); runePatterns = new Map(); }
 // 선택지는 질문마다 — 같은 글자라도 열 이름(파일에서 옴)과 룬 고정 항목을 가른다 (codex1 A·4·5 2차 P1).
 // choices: {질문 원문(틀 가능): {선택지: 영어}}, values: {요약 키: {값: 영어}}. 정확히 같을 때만.
 function scoped(table, key, value) {
  if (lang === 'ko' || !table || value == null) return value;
  const k = String(key ?? '').trim();
  const scope = own(table, k) ? table[k] : Object.entries(table).find(([q]) => /\{\d+\}/.test(q) && compile(q).test(k))?.[1];
  const v = String(value);
  if (own(scope, v)) return scope[v];   // 정확히 같을 때 — 다듬지 않는다
  // 아니면 **그 범위 안의** {0} 틀만('열: {0}' → 'Column: {0}', 끼인 이름 그대로) — 룬의 다른 문구 틀은 보지 않는다 (codex1 6차)
  for (const [q, out] of Object.entries(scope || {})) {
   if (!/\{\d+\}/.test(q)) continue;
   const m = v.match(compile(q));
   if (m) { const order = [...q.matchAll(/\{(\d+)\}/g)].map(x => +x[1]), args = []; order.forEach((n, i) => { args[n] = m[i + 1]; }); return fill(out, args); }
  }
  return value;
 }
 const runeOf = id => own(RUNES, id) ? RUNES[id] : null;
 const choice = (id, prompt, option) => scoped(runeOf(id)?.choices, prompt, option);
 const value = (id, key, v) => scoped(runeOf(id)?.values, key, v);
 // 룬 문구 사전 d(text) 로 번역 — [결과, 맞았는지]. 맞았는지는 '원문 규칙이 걸렸는지' — 끼인 값(한국어 파일명)과 무관 (codex1 후속 P2)
 function lookup(d, text, cacheKey) {
  if (lang === 'ko' || !d || text == null) return [text, false];
  const s = String(text), core = s.trim();
  if (own(d, core)) return [s.replace(core, () => d[core]), true];
  const pats = cacheKey != null && runePatterns.get(cacheKey) || Object.keys(d).filter(k => /\{\d+\}/.test(k)).map(k => [
   compile(k), [...k.matchAll(/\{(\d+)\}/g)].map(m => +m[1]), d[k]]);
  if (cacheKey != null) runePatterns.set(cacheKey, pats);
  for (const [re, order, out] of pats) {
   const m = core.match(re);
   if (m) { const args = []; order.forEach((n, i) => { args[n] = m[i + 1]; }); const done = fill(out, args); return [s.replace(core, () => done), true]; }
  }
  return [s, false];
 }
 const textOf = id => own(runeOf(id), 'text') ? runeOf(id).text : null;
 const rune = (id, text) => lookup(textOf(id), text, id)[0];
 const runeHit = (id, text) => lookup(textOf(id), text, id)[1];
 // 아직 설치 안 된 후보(설치 미리보기)의 패키지 사전으로 — 설치된 사전과 섞지 않는다 (codex1 후속 P1)
 const runeWith = (pack, text) => lookup(own(pack, 'text') ? pack.text : null, text, null)[0];
 const hasRune = id => lang === 'en' && !!runeOf(id);
 window.VaderI18n = {t, word, apply, ready, rune, runeHit, runeWith, choice, value, hasRune, setRunes, get lang() { return lang; }};
 // 언어를 알기 전엔 본문을 숨긴다 — 한국어가 잠깐 비쳤다 바뀌지 않게. 코어 연결이 늦으면 1.5초 뒤 본문만 보인다.
 // 언어 결정(ready)은 연결된 뒤 app_lang 답으로만 — 화면 코드의 첫 상태 조회는 그 뒤 한 번 (codex1 2단계 1차 P1-3)
 const style = document.createElement('style');
 style.textContent = 'html:not([data-i18n=ready]):not([data-i18n=wait]) body{visibility:hidden}';
 document.head.appendChild(style);
 setTimeout(() => { if (!document.documentElement.dataset.i18n) document.documentElement.dataset.i18n = 'wait'; }, 1500);
 const ask = () => window.pywebview.api.call('app_lang', {}).then(r => apply(r && r.ok ? r.data : 'ko'), () => apply('ko'));
 if (window.pywebview?.api) ask(); else window.addEventListener('pywebviewready', ask, {once: true});
})();
