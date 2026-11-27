// The screen is driven exclusively by the existing Python JobRunner.
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// 사용자 글·룬이 준 글 — 화면 번역(i18n.js)이 건드리지 않게 [data-raw] 로 감싼다
const raw = s => '<span data-raw>'+esc(s)+'</span>';
// 영어 화면에서 룬 문구가 원문으로 남을 때 — 사전이 없거나(실행 전에도), 이 버전 사전 규칙에 안 걸린 룬 원문이 있을 때.
// texts: 룬이 쓴 **원문**. 끼인 값(한국어 파일명)은 규칙이 걸렸으면 문제가 아니다 (codex1 1차 협의·후속 P2)
const runeLangNote = (id,...texts) => VaderI18n.lang==='en'&&(!VaderI18n.hasRune(id)||texts.some(s=>s!=null&&!VaderI18n.runeHit(id,s)))?'<p class="small muted">'+T('룬 원문 언어: 한국어')+'</p>':'';
const T = (s,...a) => VaderI18n.t(s,...a);
const state = {dismissed:new Set(),page:'core', task:null, job:null, jobs:[], dryRun:true, startPage:1,
  motion:matchMedia('(prefers-reduced-motion: reduce)').matches, busy:false, connected:false, failures:[]};
let taskRunes = {}, pendingDrop = null, toastTimer, lastSnapshot = '', settingsRune = null, polling = false;
const agent = $('#agent'), main = $('#main'), canvas = $('canvas',agent), ctx = canvas.getContext('2d');
const logTerminal = new LiveLogTerminal();
$('.wave',agent).innerHTML = Array.from({length:13},(_,i)=>'<i style="--n:'+i+'"></i>').join('');
const pages = [['core','메인'],['todo','할 일'],['runes','내 룬'],['workshop','제작소'],['market','광장']];
$('.rail-pages').innerHTML = pages.map(([p,t])=>'<a href="#'+p+'" data-page="'+p+'" title="'+t+'">'+icon(p)+'<span>'+t+'</span></a>').join('');
$('.rail-pages a').after($('#msgToggle'));   // 메인 다음이 메신저 (마스터 요청 2026-11-10). 메신저는 화면이 아니라 서랍이다
document.querySelectorAll('.rail-icon[data-icon]').forEach(el=>el.outerHTML=icon(el.dataset.icon));   // 오른쪽 메뉴 도구 아이콘
const button = (action,text,ico='arrow',cls='') => '<button type="button" class="button '+cls+'" data-action="'+action+'">'+icon(ico)+esc(text)+'</button>';
const glyphs = [
 'M35 25v27l15 24 15-24V25M43 34v19l7 10 7-10V34M24 43v17l8 10M76 43v17l-8 10',
 'M27 29h32l14 14v30H27ZM57 29v17h16M38 54h24M38 64h16',
 'M50 22 76 39v27L50 81 24 66V39ZM24 39l26 16 26-16M50 55v26M37 31l26 17',
 'M25 36h50M25 64h50M35 24v52M65 24v52M43 44h14v12H43Z',
 'M50 20 78 50 50 80 22 50ZM50 34v32M35 50h30',
 // 광장 룬이 늘어도 겹치지 않게 — 같은 선 굵기·범위(24–76)로 손으로 그린 문양 (2026-10-30)
 'M28 74h44M34 66V52M46 66V38M58 66V46M70 66V30',
 'M36 30h28v46H36ZM44 25h12v9H44ZM42 54l7 7 13-14',
 'M45 28a17 17 0 1 1 0 34a17 17 0 1 1 0-34M57 57l17 17',
 'M27 33h46v41H27ZM27 45h46M38 26v13M62 26v13M37 56h8M55 56h8M37 66h8',
 'M50 25 76 39 50 53 24 39ZM24 51l26 14 26-14M24 63l26 14 26-14',
 'M33 67a12 12 0 0 1 0-17l17-17a12 12 0 0 1 17 17L50 67a12 12 0 0 1-17 0ZM41 42l17 17',
 'M29 45a22 22 0 0 1 39-11M71 55a22 22 0 0 1-39 11M68 23v11H57M32 77V66h11'
];
// 시작 페이지 칸을 보여줄 룬. 임시 목록이다 — 어떤 룬에 이 칸이 필요한지 기준은 마스터와 논의 중.
const START_PAGE_RUNES=new Set(['eonm_ipgo','ins_singo']);
const descriptions={
 eonm_ipgo:'입고 서류를 읽고 품목을 대조해, 병원 프로그램의 입력 흐름을 이어갑니다.',
 ins_singo:'보험 신고 문서를 읽어 항목을 정리하고, CSV 자료로 변환합니다.',
 table_merge:'여러 엑셀·CSV 를 칸 이름으로 맞춰 하나로 합치고, 같은 건은 정리해요. 애매한 것만 물어보고, 원본은 그대로 두고 통합본·제외목록을 새로 만들어요. 인터넷을 쓰지 않아요.'
};
const RUNE_COLORS=[['#fa4859','250,72,89'],['#8c91ff','140,145,255'],['#74dfbb','116,223,187'],['#ffb35d','255,179,93'],['#62c9ef','98,201,239']];
// 룬 모양은 id 로 정한다 — 설치 순서와 무관하게, 내 룬과 광장에서 같은 룬은 같은 색·문양이다.
// 이온엠·보험 신고는 지금 모양 그대로. 나머지는 id 해시로 색 5 × 문양 12 에서 고른다 — 겹칠 수 있고, 모양은 출처 증거가 아니다.
function runeLook(id) {
 const fixed={eonm_ipgo:0,ins_singo:1}[id]??-1;
 let h=2166136261;for(const c of String(id))h=Math.imul(h^c.codePointAt(0),16777619)>>>0;
 const n=fixed>=0?fixed:h%RUNE_COLORS.length;
 let g=fixed>=0?fixed:(h>>>8)%glyphs.length;
 if(fixed<0&&n===g&&n<2)g+=2;                       // 이온엠·보험 신고와 같은 조합은 피한다
 return {color:RUNE_COLORS[n][0],rgb:RUNE_COLORS[n][1],glyph:glyphs[g]};
}
function sigil(id,large=false) {
 const r=taskRunes[id]||runeLook(id);
 return '<span class="rune-sigil '+(large?'large':'')+'" style="--rune:'+r.color+';--rune-rgb:'+r.rgb+'"><span class="sigil-ring"></span><svg viewBox="0 0 100 100" aria-hidden="true"><path class="sigil-frame" d="M50 3 83 18 97 50 83 82 50 97 17 82 3 50 17 18Z"/><path class="sigil-inner" d="m50 13 30 18 8 33-38 23-38-23 8-33Z"/><path class="sigil-stroke" d="'+r.glyph+'"/></svg><span class="sigil-spark"></span></span>';
}
function toast(text) { $('#toast').textContent=text;$('#toast').classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').classList.remove('show'),5500); }
async function api(method,args={}) {
 if(!window.pywebview?.api) throw Error('데스크톱 앱에서 열어주세요. 시작: python -m vader --desktop');
 const response=await window.pywebview.api.call(method,args);
 if(response.locked)location.replace('gate.html');   // 잠겼다 — 업무 화면을 버리고 문으로
 if(!response.ok) throw Error(response.error);
 return response.data;
}
function catalog(data) {
 VaderI18n.setRunes(Object.fromEntries(data.runes.filter(r=>r.i18n_en).map(r=>[r.id,r.i18n_en])));   // 룬 패키지 안 번역(그 버전)
 taskRunes=Object.fromEntries(data.runes.map(r=>[r.id,{...r,...runeLook(r.id),name:VaderI18n.rune(r.id,r.name),fields:(r.fields||[]).map(f=>({...f,label:VaderI18n.rune(r.id,f.label||f.name)}))}]));
 state.failures=data.failures;state.catalogNotices=data.catalog_notices||[];state.exitRequired=data.exit_required||null;state.devMode=!!data.dev_mode;state.clients=data.clients||[];state.testRequests=data.test_requests||[];state.projects=data.projects||[];state.console=data.console||{};state.aiReady=data.ai_ready!==false;
 if(!state.projects.some(p=>p.id===state.project))state.project=state.projects[0]?.id||null;
 if(!taskRunes[state.task]) state.task=data.runes[0]?.id||null;
 if(taskRunes[state.task]?.locked) state.dryRun=true;
}
const active = () => state.jobs.find(j=>!j.terminal);
const statusText = j => ({queued:'실행 준비 중',running:'처리하고 있어요.',waiting_user:'확인이 필요해요.',cancel_requested:'현재 항목 완료 후 중단합니다.',cancelled:'작업을 중단했어요.',succeeded:'작업을 마쳤어요.',failed:'작업을 완료하지 못했어요.'}[j.state]);
function mountAgent() {
 const id=(state.page==='run'||state.page==='result')&&state.job?state.job.rune_id:state.task;
 const template=document.createElement('template');template.innerHTML=sigil(id);
 const mark=$('.agent-mark',agent);mark.replaceChildren(template.content.querySelector('svg'));mark.dataset.rune=id||'';
 mark.style.setProperty('--rune',taskRunes[id]?.color||'#fa4859');mark.style.setProperty('--rune-rgb',taskRunes[id]?.rgb||'250,72,89');
 $('[data-agent-mount]')?.prepend(agent);   // 제작소·광장에는 에이전트 자리가 없다
 const j=active()||(['run','result'].includes(state.page)?state.job:null);
 agent.dataset.state=j?({succeeded:'complete',waiting_user:'confirm',failed:'stopped',cancel_requested:'stopped',cancelled:'paused'}[j.state]||'running'):'idle';
 $('#headerStatus').textContent=active()?statusText(active()):'대기 중';
}
// 코어 탭 하나가 대기·실행·결과를 돈다 (codex1 합의). 들어올 때: 실행 중 → 확인 안 한 최신 결과 → 대기.
const unseen=()=>state.jobs.find(j=>j.terminal&&!j.seen);
function navigate(page) {
 if(!['core','todo','runes','workshop','market','run','result'].includes(page)||(page==='result'&&!state.job))page='core';
 if(page==='core'){const t=active()||unseen();if(t){state.job=t;page=t.terminal?'result':'run';}}
 state.page=page;render(true);history.replaceState(null,'','#'+(['run','result'].includes(page)?'core':page));
 if(page==='market')loadListing();
 if(page==='todo'){loadTodos();api('messenger_groups').then(g=>{state.todoGroups=g;if(state.page==='todo')render();}).catch(()=>{});}
}
// ── 광장 목록: 운영자가 검토·서명한 패키지. 판정은 코어가, 화면은 목록과 버튼만 ──
async function loadListing(refresh=false) {try{state.listing=await api('market_list',{refresh});}catch(e){state.listing={enabled:true,fresh:false,releases:[],error:e.message};}if(state.page==='market')render();}
// 받기 전 확인 — 내 룬 [업데이트] 와 광장이 같은 창 (codex1 2026-11-25). 확인한 패키지(sha256)만 받는다.
function updateButton(id,u) {return '<button type="button" class="button ghost attention" data-action="marketConfirm" data-id="'+esc(id)+'" data-version="'+esc(u.version)+'" data-sha="'+esc(u.archive_sha256)+'">'+icon('down')+esc(T('v{0} → v{1} 업데이트',u.installed,u.version))+'</button>';}
// 버튼이 보여 준 바로 그 후보(버전·sha256)만 — 두 목록이 어긋나면 그 버튼의 후보를 가진 쪽만 쓴다 (codex1 P1)
function marketConfirm(a) {
 const id=a.dataset.id,want=x=>x&&x.version===a.dataset.version&&x.archive_sha256===a.dataset.sha;
 const r0=(state.listing?.releases||[]).find(x=>x.rune_id===id),r=want(r0)?r0:null,u=want(updateOf(id))?updateOf(id):null;
 const pick=u?{...u,rune_id:id}:r;if(!pick)return toast('광장 목록을 새로 고친 뒤 다시 골라 주세요.');
 const upgrade=!!(u||r.installed),name=VaderI18n.rune(id,pick.name||id);
 const perm=u?(u.added.length?T('새로 요구하는 권한 · {0}',esc(u.added.map(x=>T(x)).join(' · '))):T('늘어나는 권한 없음'))
  :(r.permissions.length?T('필요한 권한 · {0}',esc((r.permission_text||r.permissions).map(x=>T(x)).join(' · '))):T('추가 권한 없음'));
 modal(heading(upgrade?T('{0} 업데이트',name):T('{0} 설치',name)).replace('<h2','<h2 data-raw')
  +'<p class="dialog-copy">'+esc(upgrade?'v'+pick.installed+' → v'+pick.version:'v'+pick.version)+'</p><p class="dialog-copy">'+perm+'</p>'
  +(u?.local?'<p class="small market-warn">'+T('이 PC 에 직접 설치한 룬을 광장 버전으로 바꿔요. 지금 룬 폴더는 따로 보관하고, 설정·학습 데이터는 그대로예요. 하던 작업은 새 버전에서 처음부터 다시 해야 할 수 있어요.')+'</p>':'')
  +'<p class="small muted">받은 뒤에는 연습 실행으로 확인하고 다시 승인해야 실사용할 수 있어요.</p><p id="dialogError" role="alert"></p>'
  +'<div class="actions"><button type="button" class="button primary" data-action="marketInstall" data-id="'+esc(id)+'" data-version="'+esc(pick.version)+'" data-sha="'+esc(pick.archive_sha256)+'"'+(u?.local?' data-replace="1"':'')+'>'+icon('down')+(u?.local?T('광장 버전으로 바꾸기'):'받기')+'</button>'+button('closeDialog','취소','stop','ghost')+'</div>');
}
async function marketInstall(a) {
 a.disabled=true;a.textContent='받는 중…';
 try{const data=await api('market_install',{rune_id:a.dataset.id,version:a.dataset.version,archive_sha256:a.dataset.sha,replace_local:a.dataset.replace==='1'});const d=$('#settingsDialog');if(d.open)d.close();if(state.updates)delete state.updates[a.dataset.id];runesBadge();catalog(data);state.listing=data.market;const done=data.installed;
  toast(done.name+' v'+done.version+' 를 받았어요. 내 룬에서 연습 실행으로 확인한 뒤 승인하세요.'+(done.kept?' '+T('예전 룬 폴더는 {0} 에 보관했어요.',done.kept):''));render();}
 catch(err){toast(err.message);loadListing();}
}
function render(route=false) {
 const focus=document.activeElement;
 const focusId=main.contains(focus)?focus.id:null;
 const focusAction=main.contains(focus)?focus.dataset.action:null;
 logTerminal.detach();
 const termFocus=consoleBox.contains(document.activeElement);
 const scroll=main.scrollTop;
 agent.remove();
 document.querySelectorAll('[data-page]').forEach(el=>{
  const selected=el.dataset.page===(['run','result'].includes(state.page)?'core':state.page);
  el.classList.toggle('active',selected);selected?el.setAttribute('aria-current','page'):el.removeAttribute('aria-current');
 });
 railBadge();
  main.innerHTML=({core:coreView,todo:todoView,runes:runesView,workshop:workshopView,market:marketView,run:runView,result:resultView}[state.page])();
 if(!route&&$('.scene',main))$('.scene',main).style.animation='none';
 mountAgent();applyMotion();
 main.classList.toggle('core-fit',state.page==='core');
 if(state.page==='core'&&$('#dropzone')){bindDrop();bindOrbit();placeGraph();fitGraph();animateGraph();}
 logTerminal.mount($('[data-log-mount]'),state.job);
 mountConsole(termFocus);
 logTerminal.setConnected(state.connected);
 main.scrollTop=route?0:scroll;
 if(route)main.focus({preventScroll:true});else if(focusId)$('#'+CSS.escape(focusId))?.focus({preventScroll:true});else if(focusAction)$('[data-action="'+CSS.escape(focusAction)+'"]')?.focus({preventScroll:true});
 $('#modeStatus').textContent=(['run','result'].includes(state.page)&&state.job?state.job.dry_run:state.dryRun)?'연습 모드':'실사용 모드';
 window.VaderChat?.sync();   // AI 연결이 바뀌면 대화 서랍도 따라간다
}
// 다른 탭에 있는 동안 끝났거나 답을 기다리면 코어 탭에 점 — 화면을 빼앗지는 않는다.
function railBadge() {
 const link=$('.rail a[data-page="core"]');if(!link)return;
 const away=!['core','run','result'].includes(state.page);
 const need=!away?'':active()?.state==='waiting_user'?'응답 필요':unseen()?'확인할 결과':'';
 if(need){link.dataset.badge=need;link.title='메인 · '+need;}else{delete link.dataset.badge;link.removeAttribute('title');}
}
function emptyRunes() {
 if(state.connected&&state.task)return '<section class="scene result-layout"><div class="result-agent" data-agent-mount></div><h1>켜 둔 룬이 없어요.</h1><p class="lead">내 룬에서 메인에 쓸 룬에 체크하세요.</p>'+button('runes','내 룬','runes')+'</section>';
 return '<section class="scene result-layout"><div class="result-agent" data-agent-mount></div><h1>'+(state.connected?'설치된 룬이 없어요.':'앱에 연결하고 있어요.')+'</h1><p class="lead">'+(state.connected?'관리 화면에서 업무에 필요한 룬을 설치하세요.':'데스크톱 실행기를 확인해주세요.')+'</p>'+button(state.connected?'install':'connect',state.connected?'룬 설치':'다시 연결',state.connected?'up':'settings')+catalogView()+failuresView()+'</section>';
}
// 내 룬에서 체크를 끈 룬은 코어에 안 나온다 (마스터 요청 2026-10-20). 설치·승인·설정은 그대로다.
const enabledIds=()=>Object.keys(taskRunes).filter(id=>taskRunes[id].enabled!==false);
// 메인의 3D 그래프 — 구심점이 둘: 룬은 가운데 VaderAI(파일 놓는 원)를, 친구는 '메신저' 점을 공 모양으로 둘러싼다.
// 옵시디언 3D 그래프처럼 빈 곳을 끌면 화면(두 구심점 함께)이 옮겨지고, 노드를 잡고 끌거나 오른쪽 끌기는 돌리기,
// 휠은 확대·축소, 빈 곳을 두 번 누르면 처음 자리 (마스터 요청 2026-11-09).
// 룬 공과 메신저 공은 따로 돈다. 놓으면 관성으로 미끄러지고, 노드는 살짝 떠다닌다 (2026-11-10). 고른 룬은 원 앞 아래로 온다 (2026-10-20 그대로).
// 선은 '이 PC 에 켜 둔 룬'·'친구' 라는 실제 관계만 — 노드끼리 잇는 가짜 관계선은 없다 (codex1).
// ponytail: DOM 버튼 + JS 투영이라 WebGL 실패가 없고 버튼이 곧 키보드 목록이다. 노드가 수백 개가 되면 WebGL 로.
const TAU=2*Math.PI,FOCAL=3.2,FRONT_DROP=.75;   // 고른 룬은 원 바로 아래 앞으로
const wrap=a=>((a%TAU)+TAU+Math.PI)%TAU-Math.PI;
// 황금각 나선으로 공 위에 고르게. squash 로 극지방을 비워 둔다(맨 위·아래 노드는 원에 가려 안 보인다).
const sphere=(i,n,squash,turn=0)=>{const y=n<2?0:(1-2*(i+.5)/n)*squash,r=Math.sqrt(1-y*y),a=i*2.39996+turn;return [Math.sin(a)*r,y,Math.cos(a)*r];};
const runeDir=(i,n)=>sphere(i,n,.62);
// 회오리 팔 여섯 개 — 가운데로 감겨 들어가는 로그 나선. 한 번만 만든다.
const SPIRAL=Array.from({length:6},(_,k)=>'<path d="'+Array.from({length:40},(_,j)=>{const r=.12+.88*j/39,a=k*TAU/6+2.6*Math.log(r/.12);return (j?'L':'M')+(Math.cos(a)*r).toFixed(3)+' '+(Math.sin(a)*r).toFixed(3);}).join('')+'"/>').join('');
function coreStage(ids, nexus) {
 const n=ids.length, r=taskRunes[state.task];
 if(state.yaw==null||state.graphN!==n){state.graphN=n;const t=aimAt(runeDir(ids.indexOf(state.task),n));state.yaw=state.yawTo=t[0];state.pitch=state.pitchTo=t[1];}
 return '<div class="core-stage spins'+(state.msgReady?' has-hub':'')+(state.todos!=null?' has-todo':'')+'" style="--rune:'+r.color+';--rune-rgb:'+r.rgb+'"><div class="graph-world">'
  +'<div class="nebula neb-rune" aria-hidden="true"></div>'+(state.msgReady?'<div class="nebula neb-msg" aria-hidden="true"></div>':'')+(state.todos!=null?'<div class="nebula neb-todo" aria-hidden="true"></div>':'')+nexus
  +'<svg class="graph-links" aria-hidden="true">'+ids.map(id=>'<line style="--rune-rgb:'+taskRunes[id].rgb+'"/>').join('')+'</svg>'
  +'<div class="orbit" role="group" aria-label="실행할 룬 · 끌어서 돌리거나 ←/→ 로 골라요">'
  +ids.map(id=>'<button type="button" id="rune-'+esc(id)+'" class="orbit-item'+(id===state.task?' front':'')+'" data-ring="'+esc(id)+'" aria-pressed="'+(id===state.task)+'" tabindex="'+(id===state.task?0:-1)+'">'+sigil(id)+'<span>'+esc(taskRunes[id].name)+'</span></button>').join('')
  +'</div>'+friendRing()+todoRing()+'<svg class="vortex" viewBox="-1 -1 2 2" aria-hidden="true">'+SPIRAL+'</svg></div><p class="vortex-note">'+VaderI18n.t('놓으면 {0} 실행','<b>'+esc(r.name)+'</b>')+'</p>'
  +'<button type="button" class="graph-home" data-action="graphHome" title="처음 자리로 (그래프 빈 곳을 두 번 눌러도 돼요)">'+icon('core')+'<span>처음 자리</span></button></div>';
}
// 실행 기록 찾기 (2026-11-10). 실행 기록은 그래프에 별로 두지 않고(마스터 2026-11-12) 찾은 것만 목록으로 보인다.
// 코어 journal.runs·search_runs 가 일지의 결말 줄에서 만든다. 화면엔 룬·시각·결과만(실패 사유 안 보냄).
const runWhen=r=>{const d=r.at.slice(0,10),t=ymd(new Date());return (d===t?'오늘 ':d.slice(5).replace('-','/')+' ')+r.at.slice(11,16);};
const runWord=r=>r.failed?'실패':r.state==='succeeded'?'성공':r.state==='cancelled'?'취소':r.state;
function graphSearch() {
 if(!(state.runs||[]).length)return '';
 const f=state.found;
 return '<form class="graph-search" role="search"><label class="sr-only" for="graphQ">실행 기록 찾기</label><input id="graphQ" type="search" placeholder="기록 찾기 · 예: 최근 실패" value="'+esc(state.query||'')+'" autocomplete="off">'+button('runSearch','찾기','search','ghost')+'</form>'
  +'<p class="graph-found" role="status">'+(f?esc(f):'')+'</p>'
  +(state.foundRuns?.length?'<ul class="graph-results">'+state.foundRuns.map(r=>{const t=(taskRunes[r.rune_id]?.name||r.rune_id)+' · '+runWhen(r)+' · '+runWord(r);return '<li><button type="button" class="text-button'+(r.failed?' failed':'')+'" data-run="'+esc(r.job_id)+'" title="'+esc(t)+'">'+esc(t)+'</button></li>';}).join('')+'</ul>':'');
}
async function loadRuns() {try{const rs=await api('run_history'),next=JSON.stringify(rs);if(next!==loadRuns.last){loadRuns.last=next;state.runs=rs;if(state.page==='core')render();}}catch(e){}}
async function runSearch() {
 const q=($('#graphQ')?.value||'').trim(),seq=runSearch.seq=(runSearch.seq||0)+1;state.query=q;   // 늦게 온 앞 검색 답은 버린다
 if(!q){state.lit=null;state.found='';state.foundRuns=null;render();return;}
 const stage=$('.core-stage',main);stage?.classList.add('searching');state.found='찾는 중…';const st=$('.graph-found',main);if(st)st.textContent=state.found;
 try{const names=Object.fromEntries(Object.entries(taskRunes).map(([id,d])=>[id,d.name]));
  const r=await api('run_search',{query:q,names});if(seq!==runSearch.seq)return;
  state.lit=new Set(r.job_ids);state.foundRuns=r.runs;state.runs=r.all;loadRuns.last=JSON.stringify(r.all);   // 별도 검색한 그 묶음으로
  const scope='최근 '+r.days+'일 실행 기록 중 최신 '+r.checked+'건'+(r.truncated?'(더 오래된 것은 찾지 않음)':'');
  state.found=r.job_ids.length?scope+'에서 '+r.job_ids.length+'건을 찾았어요.':scope+'에서 맞는 기록을 찾지 못했어요.';}
 catch(e){if(seq!==runSearch.seq)return;state.lit=null;state.foundRuns=null;state.found='찾지 못했어요: '+e.message;}
 render();$('#graphQ')?.focus();
}
// 메신저 친구 — 룬보다 한 겹 바깥 공. 상태는 messenger.js 가 state.friends 에 넣는다.
// 누르면 그 친구와의 대화 서랍. 마지막 '친구' 노드는 아이디로 찾기.
// ponytail: 12명까지 그래프에, 나머지는 서랍 목록에서. 더 많아지면 WebGL 그래프로.
const FRIEND_MAX=12;
function friendRing() {
 if(!state.msgReady)return '';
 const fs=(state.friends||[]).slice(0,FRIEND_MAX),tag=f=>f.state==='pending'?(f.mine?'기다리는 중':'친구 요청'):f.changed?'키 바뀜':!f.verified?'확인 필요':f.unread?'새 메시지':'';
 return '<svg class="graph-links friend-links" aria-hidden="true">'+[...groupsShown(fs),...fs.filter(f=>!memberOf(f,fs))].map(()=>'<line/>').join('')+'<line/></svg>'
  +'<div class="friend-orbit" role="group" aria-label="메신저 친구">'
  +'<button type="button" id="friend-hub" class="hub-node" data-friend="" aria-label="메신저 · 친구 목록 열기"><span class="hub-orb" aria-hidden="true">'+icon('chat')+'</span><span class="friend-name">메신저</span></button>'
  // 그룹 (마스터 2026-11-19) — 소속 친구가 있는 그룹은 메신저 공에 그룹 노드로, 그 친구들은 그룹 노드 둘레에. 그룹 없는 친구는 지금처럼 메신저에 바로.
  +groupsShown(fs).map(g=>'<button type="button" id="group-'+esc(g.id)+'" class="friend-node group-node" data-friend="" data-group="'+esc(g.id)+'" aria-label="'+esc(T('그룹 {0} · 친구 {1}명',g.name,g.n))+'" data-raw><span class="friend-orb" aria-hidden="true">'+icon('people')+'</span><span class="friend-name" data-raw>'+esc(g.name)+'</span><small>'+T('{0}명',g.n)+'</small></button>').join('')
  +fs.map(f=>{const t=tag(f),gid=memberOf(f,fs);return '<button type="button" id="friend-'+esc(f.account_id)+'" class="friend-node'+(gid?' member':'')+(f.state==='pending'?' pending':'')+(f.changed?' changed':!f.verified&&f.state==='accepted'?' unverified':'')+(f.unread?' unread':'')+'" data-friend="'+esc(f.account_id)+'"'+(gid?' data-gid="'+esc(gid)+'"':'')+' aria-label="@'+esc(f.handle)+(t?' · '+t:'')+'"><span class="friend-orb" aria-hidden="true">'+esc((f.handle||'?')[0].toUpperCase())+'</span><span class="friend-name">@'+esc(f.handle)+'</span>'+(t?'<small>'+t+'</small>':'')+'</button>';}).join('')
  +'<button type="button" id="friend-add" class="friend-node add" data-friend="" aria-label="아이디로 친구 찾기"><span class="friend-orb" aria-hidden="true">'+icon('plus')+'</span><span class="friend-name">친구</span></button>'
  +'</div><svg class="graph-links member-links" aria-hidden="true">'+fs.filter(f=>memberOf(f,fs)).map(()=>'<line/>').join('')+'</svg>'+keywordRing(fs);
}
const groupsShown=fs=>(state.msgGroups||[]).map(g=>({...g,n:fs.filter(f=>f.state==='accepted'&&f.group===g.id).length})).filter(g=>g.n);
const memberOf=(f,fs)=>f.state==='accepted'&&f.group&&groupsShown(fs).some(g=>g.id===f.group)?f.group:'';
// 할 일 — 셋째 구심점. 다시 알릴 것·오늘 것 먼저 최대 8개, 누르면 할 일 화면. 공은 메신저 공과 같이 돈다.
// ponytail: 8개까지 그래프에, 나머지는 할 일 화면에서.
const TODO_MAX=8;
function todoShown() {
 const today=ymd(new Date()),open=(state.todos||[]).filter(c=>c.state==='open');
 return [...open.filter(c=>c.ringing),...open.filter(c=>!c.ringing&&c.due<=today),...open.filter(c=>!c.ringing&&c.due>today)].slice(0,TODO_MAX);
}
function todoRing() {
 if(state.todos==null)return '';
 const cs=todoShown(),n=(state.todos||[]).filter(c=>c.state==='open').length,cut=s=>s.length>14?s.slice(0,13)+'…':s;
 return '<svg class="graph-links todo-links" aria-hidden="true">'+cs.map(()=>'<line/>').join('')+'</svg>'
  +'<div class="todo-orbit" role="group" aria-label="할 일">'
  +'<button type="button" id="todo-hub" class="hub-node todo-hub" data-todo="" aria-label="'+esc(VaderI18n.t('할 일 {0}개 · 할 일 화면 열기',n))+'"><span class="hub-orb" aria-hidden="true">'+icon('todo')+'</span><span class="friend-name">'+VaderI18n.t('할 일')+(n?' '+n:'')+'</span></button>'
  +cs.map(c=>'<button type="button" id="todo-'+esc(c.id)+'" class="friend-node todo-node'+(c.ringing?' ringing':'')+'" data-todo="'+esc(c.id)+'" title="'+esc(c.title)+'" aria-label="'+esc(c.title)+(c.ringing?' · '+T('다시 알림'):'')+'" data-raw><span class="friend-orb" aria-hidden="true"></span><span class="friend-name">'+esc(cut(c.title))+'</span>'+(c.ringing?'<small>다시 알림</small>':'')+'</button>').join('')
  +'</div>';
}
// 대화 키워드 — 친구 공 바깥 겹. 같은 낱말을 쓴 친구마다 선을 잇는다. 누르면 그 친구와의 대화 (마스터 요청 2026-11-10).
// 낱말은 코어가 이 PC 의 대화 기록에서 센다 (messenger._keywords).
const KW_MAX=10;
function keywords(fs) {
 const by=new Map();fs.forEach(f=>(f.keywords||[]).forEach((w,rank)=>{const k=by.get(w)||{w,ids:[],score:0};k.ids.push(f.account_id);k.score+=3-rank;by.set(w,k);}));
 return [...by.values()].sort((a,b)=>b.score-a.score||a.w.localeCompare(b.w)).slice(0,KW_MAX);
}
function keywordRing(fs) {
 const ks=keywords(fs);if(!ks.length)return '';
 const handle=id=>fs.find(f=>f.account_id===id)?.handle||'';
 return '<svg class="graph-links kw-links" aria-hidden="true">'+ks.map(k=>k.ids.map(()=>'<line/>').join('')).join('')+'</svg>'
  +'<div class="kw-orbit" role="group" aria-label="대화 키워드">'+ks.map(k=>'<button type="button" class="kw-node" data-friend="'+esc(k.ids[0])+'" aria-label="키워드 '+esc(k.w)+' · @'+esc(handle(k.ids[0]))+' 와의 대화 열기">'+esc(k.w)+'</button>').join('')+'</div>';
}
// 점 p 를 (yaw, pitch) 로 돌린다. y 는 화면 아래가 +.
function turn([x,y,z],yaw,pitch) {
 const x1=x*Math.cos(yaw)+z*Math.sin(yaw),z1=z*Math.cos(yaw)-x*Math.sin(yaw);
 return [x1,y*Math.cos(pitch)-z1*Math.sin(pitch),y*Math.sin(pitch)+z1*Math.cos(pitch)];
}
// 이 점이 원 앞 조금 아래로 오게 하는 (yaw, pitch). yaw 는 지금에서 가까운 쪽으로.
function aimAt([x,y,z]) {
 const yaw=Math.atan2(-x,z);return [(state.yaw??0)+wrap(yaw-(state.yaw??0)),Math.atan2(y,Math.hypot(x,z))-FRONT_DROP];
}
const view={x:0,y:0,zoom:1},viewTo={x:0,y:0,zoom:1},pan={vx:0,vy:0};   // 화면 옮기기·확대 — 그래프 세계 전체에. view 가 viewTo 를 부드럽게 따라간다
const fview={yaw:0,pitch:-.35,vy:0,vp:0,drag:false};   // 메신저 공 — 룬 공과 따로 돈다. vy·vp 는 놓은 뒤 관성(라디안/ms)
const DRIFT=.00006;   // 손을 놓으면 메신저 공은 아주 천천히 돈다 (초당 약 3.4도)
// 메신저 구심점 — 룬 공 옆(좁은 창은 위). 반지름 R 단위.
const hubAt=wide=>wide?[2.6,-.35]:[0,-1.55];   // 넓은 창은 키워드 겹까지 룬 공과 안 겹치게
const todoAt=wide=>wide?[-2.6,-.35]:[0,2.05];   // 할 일은 메신저 반대편 (좁은 창은 아래 — CSS has-todo 가 아래 여백을 늘린다)
const vortex={on:false,spin:0,pull:0,gone:0};
// 성운 — 룬·메신저·할 일이 저마다의 색 구름 안에 모인다(우리은하, 마스터 2026-11-12). 크기는 그 묶음의 반지름, 짙기는 노드 수.
// 회오리가 일면 같이 감기고 빨려든다. 데이터가 없는 묶음엔 성운도 없다.
function nebula(stage,cls,x,y,size,count,dim){const el=$(cls,stage);if(!el)return;
 el.style.setProperty('--s',Math.max(0,size).toFixed(0)+'px');el.style.transform='translate('+x.toFixed(1)+'px,'+y.toFixed(1)+'px) rotate('+(vortex.spin*.6).toFixed(3)+'rad)';
 el.style.opacity=((.45+.55*Math.min(1,count/6))*dim).toFixed(3);}   // 파일을 끌어 오면 회오리 — spin 은 더 도는 각, pull 은 안으로 빨려드는 정도, gone 은 놓은 뒤 사라짐
function placeGraph(now=performance.now()) {
 const stage=$('.core-stage',main),core=stage&&$('.nexus',stage);if(!core)return false;
 const world=$('.graph-world',stage);world.style.transform='translate('+view.x.toFixed(1)+'px,'+view.y.toFixed(1)+'px) scale('+view.zoom.toFixed(3)+')';
 {const h=homeView();$('.graph-home',stage).classList.toggle('moved',Math.abs(viewTo.x-h.x)>2||Math.abs(viewTo.y-h.y)>2||Math.abs(viewTo.zoom-h.zoom)>.01);}
 const cx=world.clientWidth/2,cy=core.offsetTop+core.offsetHeight/2;
 // 친구 겹이 창 폭 안에 들도록 반지름을 정한다. 좁은 창(카톡 크기)은 세로로 늘린 공 — 남는 세로 공간을 쓴다.
 const wide=world.clientWidth>=520,hub=state.msgReady?hubAt(wide):state.todos!=null?todoAt(wide):[0,0],ky=wide?1:1.35;
 const squeeze=(1-.42*vortex.pull)*(1-vortex.gone),outer=cx-44,R=Math.min(core.offsetHeight*1.05,outer/(Math.abs(hub[0])+.75))*squeeze,RF=R*.62;
 const dim=1-vortex.gone,bob=state.motion?0:3;
 // 노드는 자기 구심점(ox, oy) 둘레의 공 위에, 공마다 제 각도(yaw, pitch)로. 선은 구심점 가장자리(inner)에서 노드까지.
 const put=(el,line,p,radius,depthZ,[yaw,pitch],ox,oy,inner,phase)=>{
  const [x,y,z]=turn(p,yaw,pitch),s=FOCAL/(FOCAL-z),face=(z+1)/2,sx=x*radius*s,sy=y*radius*s*ky+Math.sin(now/1400+phase)*bob,d=Math.hypot(sx,sy)||1;
  el.style.transform='translate('+(ox+sx).toFixed(1)+'px,'+(oy+sy).toFixed(1)+'px) scale('+(.5+.5*face).toFixed(3)+')';
  el.style.opacity=((.3+.7*face)*dim).toFixed(3);el.style.zIndex=z>depthZ?3:1;el.classList.toggle('back',z<-.35);
  if(line){const o=d<inner?0:1;line.setAttribute('x1',ox+sx/d*inner*o);line.setAttribute('y1',oy+sy/d*inner*o);line.setAttribute('x2',ox+sx);line.setAttribute('y2',oy+sy);line.style.opacity=(o*(.08+.42*face)*dim).toFixed(3);}
  return [ox+sx,oy+sy,face];
 };
 const items=stage.querySelectorAll('.orbit-item'),lines=stage.querySelectorAll('.graph-links:not(.friend-links):not(.kw-links):not(.todo-links) line'),n=items.length;
 // 각이 숫자가 아니게 되면(NaN) 룬이 한 점에 겹치고 다시 안 풀린다 (2026-11-18 마스터 화면) — 고른 룬 쪽으로 다시 맞춘다.
 if(!Number.isFinite(vortex.spin))vortex.spin=0;
 if(!Number.isFinite(state.yaw)||!Number.isFinite(state.pitch)){console.warn('graph angle reset',state.yaw,state.pitch);state.yaw=null;
  const t=aimAt(runeDir(Math.max(0,[...items].findIndex(el=>el.classList.contains('front'))),n));state.yaw=state.yawTo=t[0];state.pitch=state.pitchTo=t[1];}
 const rv=[state.yaw+vortex.spin,state.pitch];
 items.forEach((el,i)=>{const q=put(el,lines[i],runeDir(i,n),R,0,rv,cx,cy,core.offsetHeight*.5,i*1.9);el.style.filter='saturate('+(.25+.75*q[2]).toFixed(3)+')';});
 nebula(stage,'.neb-rune',cx,cy,R*2.7,n,dim);
 placeTodos(put,cx,cy,R,ky,dim,now,wide);
 const hubEl=$('#friend-hub',stage);if(!hubEl)return true;
 const hx=cx+hub[0]*R,hy=cy+hub[1]*R*ky+Math.sin(now/1700)*bob;hubEl.style.transform='translate('+hx.toFixed(1)+'px,'+hy.toFixed(1)+'px)';hubEl.style.opacity=dim.toFixed(3);
 const fv=[fview.yaw,fview.pitch],at={},gat={},fr=stage.querySelectorAll('.friend-node:not(.todo-node):not(.member)'),fl=stage.querySelectorAll('.friend-links line'),m=fr.length;
 fr.forEach((el,j)=>{const q=put(el,fl[j],sphere(j,m,.8,1.2),RF,-.1,fv,hx,hy,hubEl.offsetHeight*.3,j*2.3+1);if(el.dataset.friend)at[el.dataset.friend]=q;if(el.dataset.group)gat[el.dataset.group]=[q,el];});
 // 그룹 소속 친구 — 그 그룹 노드 둘레의 작은 고리. 그룹 노드가 뒤로 가면 같이 흐려진다.
 const mem=stage.querySelectorAll('.friend-node.member'),ml=stage.querySelectorAll('.member-links line'),byGroup={};
 mem.forEach(el=>(byGroup[el.dataset.gid]||=[]).push(el));
 let mi=0;
 Object.entries(byGroup).forEach(([gid,els])=>{const g=gat[gid];if(!g)return;const [[gx,gy,gf],gel]=g;
  els.forEach((el,j)=>{const a=j*TAU/els.length+fview.yaw*.8-Math.PI/2,q=put(el,ml[mi++],[Math.cos(a),Math.sin(a)*.7,.3],RF*.68,-.1,[0,0],gx,gy,gel.offsetHeight*.3,j*1.7+3);
   el.style.opacity=(Number(el.style.opacity)*(.35+.65*gf)).toFixed(3);at[el.dataset.friend]=q;});});
 nebula(stage,'.neb-msg',hx,hy,RF*3.3,m+stage.querySelectorAll('.kw-node').length,dim);
 // 키워드 — 바깥 겹(1.3배). 선은 그 낱말을 쓴 친구 노드까지.
 const kw=stage.querySelectorAll('.kw-node'),kl=stage.querySelectorAll('.kw-links line'),pos=[];
 kw.forEach((el,j)=>{pos[j]=put(el,null,sphere(j,kw.length,.7,.4),RF*1.3,-.2,fv,hx,hy,0,j*1.3+.5);});
 let li=0;
 keywords((state.friends||[]).slice(0,FRIEND_MAX)).forEach((k,j)=>k.ids.forEach(id=>{const line=kl[li++],a=pos[j],b=at[id];if(!line||!a||!b)return;
  line.setAttribute('x1',a[0]);line.setAttribute('y1',a[1]);line.setAttribute('x2',b[0]);line.setAttribute('y2',b[1]);line.style.opacity=((.06+.3*Math.min(a[2],b[2]))*dim).toFixed(3);}));
 return true;
}
function placeTodos(put,cx,cy,R,ky,dim,now,wide) {
 const stage=$('.core-stage',main),hubEl=$('#todo-hub',stage);if(!hubEl)return;
 const at=todoAt(wide),bob=state.motion?0:3,hx=cx+at[0]*R,hy=cy+at[1]*R*ky+Math.sin(now/1900+1)*bob;
 hubEl.style.transform='translate('+hx.toFixed(1)+'px,'+hy.toFixed(1)+'px)';hubEl.style.opacity=dim.toFixed(3);
 const ns=stage.querySelectorAll('.todo-node'),ls=stage.querySelectorAll('.todo-links line'),m=ns.length;
 // 구심점을 둘러싼 납작한 고리 — 카드가 구심점을 가리지 않는다. 메신저 공과 같이 천천히 돈다.
 ns.forEach((el,j)=>{const a=j*TAU/m+fview.yaw*.5-Math.PI/2;put(el,ls[j],[Math.cos(a)*.85,Math.sin(a)*.55,.35],R*.85,-.1,[0,0],hx,hy,hubEl.offsetHeight*.35,j*1.7+2);});
 nebula(stage,'.neb-todo',hx,hy,R*2.2,m,dim);
}
// 한 루프가 다 움직인다: 고른 룬 쪽으로 감속 회전, 메신저 공 관성·느린 회전, 화면 옮기기 관성·부드러운 확대, 노드 떠다니기.
// 효과를 끄면(Windows 애니메이션 끄기) 바로 그 자리, 떠다니기·느린 회전 없음. 창이 숨으면 브라우저가 프레임을 멈춘다.
let graphRaf=0;
function animateGraph() {
 if(graphRaf)return;
 let prev=performance.now();
 const step=now=>{graphRaf=0;const dt=Math.max(0,Math.min(now-prev,50)),calm=state.motion;prev=now;  // 프레임이 느려도 같은 시간에 닿는다. 앞선 시각의 프레임(숨김 뒤 등)은 0 — 음수면 exp 가 넘쳐 각이 무한대→NaN (2026-11-18 룬 겹침)
  const ease=tau=>1-Math.exp(-dt/tau),near=(a,b,k,eps=.0005)=>calm||Math.abs(b-a)<eps?b:a+(b-a)*k;
  const kr=ease(240);state.yaw=near(state.yaw,state.yawTo,kr);state.pitch=near(state.pitch,state.pitchTo,kr);
  if(!fview.drag){const fade=calm?0:Math.exp(-dt/600);
   fview.yaw+=(fview.vy+(calm?0:DRIFT))*dt;fview.pitch=Math.max(-1.3,Math.min(1.3,fview.pitch+fview.vp*dt));fview.vy*=fade;fview.vp*=fade;}
  if(!calm){viewTo.x+=pan.vx*dt;viewTo.y+=pan.vy*dt;}
  const fp=calm?0:Math.exp(-dt/180);pan.vx*=fp;pan.vy*=fp;if(Math.hypot(pan.vx,pan.vy)<.005)pan.vx=pan.vy=0;
  const kv=ease(90);view.x=near(view.x,viewTo.x,kv,.1);view.y=near(view.y,viewTo.y,kv,.1);view.zoom=near(view.zoom,viewTo.zoom,kv,.001);
  if(!calm){
   if(vortex.on){vortex.pull=Math.min(1,vortex.pull+dt/450);vortex.spin+=dt/1000*(1.2+5*vortex.pull);}
   else if(vortex.gone){vortex.gone=Math.min(1,vortex.gone+dt/320);vortex.spin+=dt/1000*7;}
   else{vortex.pull=Math.max(0,vortex.pull-dt/300);if(!vortex.pull)vortex.spin=0;}
  }
  const moving=!calm||state.yaw!==state.yawTo||state.pitch!==state.pitchTo||view.x!==viewTo.x||view.y!==viewTo.y||view.zoom!==viewTo.zoom;
  if(document.hidden||window.vaderTray)return;   // 트레이·숨김에서는 프레임을 내지 않는다 — 다시 보일 때 아래에서 잇는다 (codex1 지적 2026-11-10)
  if(placeGraph(now)&&moving)graphRaf=requestAnimationFrame(step);};
 graphRaf=requestAnimationFrame(step);
}
const resumeGraph=()=>{if(!document.hidden&&!window.vaderTray&&state.page==='core')animateGraph();};
addEventListener('visibilitychange',resumeGraph);addEventListener('focus',resumeGraph);
// 빈 곳 끌기는 옮기기, 노드를 잡고 끌거나 오른쪽·Shift 끌기는 돌리기 — 잡은 쪽(또는 가까운 구심점)의 공만 돈다.
// 놓으면 끌던 빠르기로 미끄러진다. 룬 공은 미끄러진 끝에서 가장 앞의 룬에 멈춘다. 6px 안쪽이면 끈 게 아니라 클릭이다.
function bindOrbit() {
 const stage=$('.core-stage.spins',main),ids=enabledIds(),n=ids.length;if(!stage)return;
 stage.oncontextmenu=e=>e.preventDefault();
 stage.ondblclick=e=>{if(!e.target.closest('button,.nexus'))graphHome();};
 // 휠: 커서 자리를 붙잡고 확대·축소 (0.5–2.5배). 목표만 바꾸고 화면은 따라간다.
 stage.onwheel=e=>{e.preventDefault();const b=stage.getBoundingClientRect(),cx=e.clientX-b.left,cy=e.clientY-b.top,z=Math.max(.5,Math.min(2.5,viewTo.zoom*Math.exp(-e.deltaY*.0015)));
  viewTo.x=cx-(cx-viewTo.x)*z/viewTo.zoom;viewTo.y=cy-(cy-viewTo.y)*z/viewTo.zoom;viewTo.zoom=z;viewMoved=true;animateGraph();};
 stage.onpointerdown=e=>{
  if(![0,1,2].includes(e.button))return;
  const pan0=e.button===1||(e.button===0&&!e.shiftKey&&!e.target.closest('button,.nexus'));   // 빈 곳 = 옮기기
  const hubEl=$('.hub-node',stage),dist=el=>{const r=el.getBoundingClientRect();return Math.hypot(e.clientX-r.left-r.width/2,e.clientY-r.top-r.height/2);};
  const friends=!pan0&&!!hubEl&&(e.target.closest('.friend-orbit,.kw-orbit,.todo-orbit')?true:e.target.closest('.orbit,.nexus')?false:dist(hubEl)<dist($('.nexus',stage)));
  const x0=e.clientX,y0=e.clientY,yaw0=friends?fview.yaw:state.yaw,pitch0=friends?fview.pitch:state.pitch,v0={...viewTo},per=Math.max(1,$('.nexus',stage).offsetHeight*.9);   // 원이 아직 안 그려졌으면 0 — 0 으로 나누면 각이 NaN
  let moved=false,lx=x0,ly=y0,lt=e.timeStamp,vx=0,vy=0;
  if(pan0)pan.vx=pan.vy=0;
  stage.onpointermove=m=>{const dx=m.clientX-x0,dy=m.clientY-y0;
   if(!moved){if(Math.hypot(dx,dy)<6)return;moved=true;stage.setPointerCapture(e.pointerId);stage.classList.add(pan0?'panning':'turning');if(friends){fview.drag=true;fview.vy=fview.vp=0;}}
   const t=Math.max(1,m.timeStamp-lt);vx=vx*.5+(m.clientX-lx)/t*.5;vy=vy*.5+(m.clientY-ly)/t*.5;lx=m.clientX;ly=m.clientY;lt=m.timeStamp;   // 손 빠르기 (px/ms)
   const yaw=yaw0+dx/per,pitch=Math.max(-1.3,Math.min(1.3,pitch0-dy/per));
   if(pan0){viewTo.x=v0.x+dx;viewTo.y=v0.y+dy;viewMoved=true;animateGraph();return;}
   if(friends){fview.yaw=yaw;fview.pitch=pitch;}
   else{state.yaw=state.yawTo=yaw;state.pitch=state.pitchTo=pitch;}
   placeGraph();};
  stage.onpointerup=stage.onpointercancel=u=>{stage.onpointermove=stage.onpointerup=stage.onpointercancel=null;stage.classList.remove('panning','turning');fview.drag=false;if(!moved)return;
   if(u.timeStamp-lt>90)vx=vy=0;   // 멈췄다가 놓으면 미끄러지지 않는다
   const cap=v=>Math.max(-1.2,Math.min(1.2,v));vx=cap(vx);vy=cap(vy);   // 휙 던져도 화면 밖으로 날아가지 않게 (px/ms)
   if(pan0){pan.vx=vx;pan.vy=vy;animateGraph();return;}
   if(friends){fview.vy=vx/per;fview.vp=-vy/per;animateGraph();return;}
   // 룬 공 — 미끄러질 자리를 미리 보고 그 자리에서 가장 앞의 룬으로
   const yaw=state.yaw+vx/per*260,pitch=Math.max(-1.3,Math.min(1.3,state.pitch-vy/per*260));
   let best=0,bz=-2;ids.forEach((id,i)=>{const z=turn(runeDir(i,n),yaw,pitch)[2];if(z>bz){bz=z;best=i;}});
   turnTo(ids[best]);render();};
 };
}
// 처음 자리 = 그래프 전체가 창 높이에 들어오는 배율, 가로 가운데 (마스터 2026-11-19: 세로 스크롤 없이). 1 보다 키우지는 않는다.
function homeView() {
 const stage=$('.core-stage',main),world=stage&&$('.graph-world',stage);if(!world)return {x:0,y:0,zoom:1};
 const z=Math.max(.35,Math.min(1,stage.clientHeight/Math.max(1,world.offsetHeight)));
 return {x:stage.clientWidth*(1-z)/2,y:Math.max(0,(stage.clientHeight-world.offsetHeight*z)/2),zoom:z};
}
let viewMoved=false;   // 사람이 옮기거나 확대했나 — 안 했으면 창 크기가 바뀔 때 다시 맞춘다
function fitGraph() {if(viewMoved)return;const h=homeView();Object.assign(view,h);Object.assign(viewTo,h);placeGraph();}
function graphHome() {viewMoved=false;Object.assign(viewTo,homeView());pan.vx=pan.vy=0;animateGraph();}
function turnTo(id) {
 const ids=enabledIds(),[yaw,pitch]=aimAt(runeDir(ids.indexOf(id),ids.length));
 state.yawTo=yaw;state.pitchTo=pitch;state.task=id;if(taskRunes[id].locked)state.dryRun=true;
}
function spinRing(delta) {
 const ids=enabledIds(),n=ids.length;if(n<2||!delta)return;
 const keep=!!document.activeElement?.closest('.orbit');
 turnTo(ids[(((ids.indexOf(state.task)+delta)%n)+n)%n]);
 render();if(keep)$('#rune-'+CSS.escape(state.task))?.focus({preventScroll:true});
}
function ringTo(id) {turnTo(id);render();}
async function toggleRune(a) {
 const id=a.dataset.id,on=taskRunes[id]?.enabled===false;
 try{catalog(await api('set_rune_enabled',{rune_id:id,on}));render();toast(taskRunes[id].name+(on?' · 메인에 보여요.':' · 메인에서 숨겼어요. 설치·승인은 그대로예요.'));}
 catch(err){toast(err.message);}
}
// 목록 배포 안내 — 새 버전·회수(실전만 막힘)·갱신 지연, 그리고 악성 회수로 멈춘 실행 (vader/core/catalog.py notices)
function catalogView() {const name=id=>id?esc(taskRunes[id]?.name||id)+' · ':'',x=state.exitRequired;return (x?'<p class="ai-missing catalog-stop" role="alert">'+icon('stop')+'<span>'+name(x.rune_id)+esc(x.message)+'</span></p>':'')+(state.catalogNotices||[]).map(n=>'<p class="ai-missing" role="status">'+icon('info')+'<span>'+name(n.rune_id)+esc(n.message)+'</span>'+(n.action==='resign'?button('resign','다시 서명','confirm','ghost'):n.action==='push'?button('pushCatalog','다시 올리기','up','ghost'):'')+'</p>').join('');}
function failuresView() {return state.failures.length?'<details><summary>불러오지 못한 룬 '+state.failures.length+'개</summary>'+state.failures.map(f=>'<p>'+esc(f.rune_id)+' · '+esc(f.reason)+'</p>').join('')+'</details>':'';}
function coreView() {
 const ids=enabledIds();
 if(!ids.length)return emptyRunes();
 if(!ids.includes(state.task)){state.task=ids[0];if(taskRunes[state.task].locked)state.dryRun=true;}
 const r=taskRunes[state.task];
 return '<section class="scene nexus-scene"><h1 class="sr-only">메인</h1>'+graphSearch()+coreStage(ids,'<div class="nexus" id="dropzone" role="button" tabindex="0" aria-label="VaderAI에 파일 놓기 또는 파일 선택" aria-disabled="'+state.busy+'"><div class="nexus-field" aria-hidden="true"></div><div class="nexus-agent" data-agent-mount></div><span class="nexus-hint">'+icon('up')+'<span>'+(state.busy?'시작하는 중':'파일 선택')+'</span></span></div>')+catalogView()+failuresView()+historyView()+'</section>';
}
function runesView() {
 if(!state.task)return emptyRunes();
 const r=taskRunes[state.task];
 return '<section class="scene grimoire" style="--rune:'+r.color+';--rune-rgb:'+r.rgb+'"><div class="rune-atmosphere" aria-hidden="true"></div><div class="rune-left"><h1>나의 룬</h1><p class="rune-intro">업무 하나, 룬 하나.</p><div class="rune-tree">'+Object.entries(taskRunes).map(([id,d])=>{const on=d.enabled!==false;return '<div class="tree-row'+(on?'':' off')+'" style="--rune:'+d.color+';--rune-rgb:'+d.rgb+'"><button id="tree-'+esc(id)+'" class="tree-rune '+(state.task===id?'selected':'')+'" data-task="'+esc(id)+'" aria-pressed="'+(state.task===id)+'">'+sigil(id)+'<span class="tree-copy"><strong>'+esc(d.name)+'</strong><small>v'+esc(d.version)+(updateOf(id)?'<span class="needs-approval">'+T('업데이트 v{0}',esc(updateOf(id).version))+'</span>':'')+(d.locked?'<span class="needs-approval">승인 필요</span>':'')+(on?'':' · 메인에서 숨김')+'</small></span></button><button type="button" class="tree-check" role="checkbox" aria-checked="'+on+'" aria-label="'+esc(d.name)+' 메인에 표시" title="'+(on?'메인에 보여요 · 누르면 숨겨요':'메인에서 숨김 · 누르면 보여요')+'" data-action="toggleRune" data-id="'+esc(id)+'">'+(on?icon('confirm'):'')+'</button></div>';}).join('')+'</div><span class="rune-count">설치된 룬 <b>'+Object.keys(taskRunes).length+'</b></span>'+button('install','룬 설치','up','ghost')+catalogView()+failuresView()+'</div><div class="rune-showcase"><div class="rune-relic" aria-hidden="true"><div class="relic-aura"></div><div class="relic-orbit orbit-one"></div><div class="relic-orbit orbit-two"></div><div class="relic-stone stone-back"></div><div class="relic-stone stone-front"></div>'+sigil(state.task,true)+'<div class="relic-floor"></div><i class="ember e1"></i><i class="ember e2"></i><i class="ember e3"></i><i class="ember e4"></i></div><div class="rune-description"><h2>'+esc(r.name)+'</h2><p>'+(descriptions[r.id]?esc(descriptions[r.id]):r.target_apps.length?'대상 프로그램 · '+esc(r.target_apps.join(', ')):'선택한 파일을 이 룬의 처리 흐름으로 실행합니다.')+'</p><p class="small muted">'+esc(r.locked?r.reason:'승인됨 · 실사용 가능 · v'+r.version)+'</p>'+(r.recordings?.length?'<p class="small muted">함께 설치된 녹화 · '+esc(r.recordings.join(', '))+'</p>':'')+'<div class="actions">'+(updateOf(r.id)?updateButton(r.id,updateOf(r.id)):'')+button('core','이 룬으로 시작','arrow','primary')+(r.samples?.length?button('trySamples','샘플로 연습','play','ghost'):'')+button('settings','설정','settings','ghost')+(r.mapping?button('mapping','매핑 관리','match','ghost'):'')+(r.locked?button('seal','승인','confirm','ghost attention'):'')+button('editRune','수정','workshop','ghost')+'</div><button type="button" class="text-button danger" data-action="uninstall">룬 제거</button></div></div><div data-agent-mount hidden></div></section>';
}
function logsView(j) {return '<div data-log-mount></div>';}
// 표 편집 초안은 화면 밖에 둔다 — 진행 이벤트로 다시 그려도 고치던 칸이 안 지워진다.
let editDraft={id:null,rows:[]};
function editRowsView(q) {
 const c=q.context;if(editDraft.id!==q.id)editDraft={id:q.id,rows:c.rows.map(r=>({...r}))};
 const label=k=>(c.columns.find(x=>x[0]===k)||[k,k])[1],cls=k=>'k-'+k+(EDIT_NUMBERS.has(k)?' num':'');
 const cell=(r,i,k)=>{const v=r[k]??'';
  // 원래 행의 쪽은 룬이 바꾸지 않는다 — 새로 넣는 줄만 쪽을 고른다.
  if(!c.editable.includes(k)||(k==='page'&&r._i!=null))return '<td class="'+cls(k)+'" data-raw>'+esc(EDIT_NUMBERS.has(k)&&v!==''&&!isNaN(v)?Number(v).toLocaleString():String(v))+'</td>';
  if(k==='page'&&c.pages)return '<td class="'+cls(k)+'"><select class="edit-cell" data-i="'+i+'" data-k="page" aria-label="'+esc(label(k))+'">'+c.pages.map(p=>'<option value="'+esc(String(p))+'" data-raw '+(String(p)===String(v)?'selected':'')+'>'+esc(String(p))+'</option>').join('')+'</select></td>';
  return '<td class="'+cls(k)+'"><input class="edit-cell" data-i="'+i+'" data-k="'+esc(k)+'" value="'+esc(String(v))+'" autocomplete="off"'+(EDIT_NUMBERS.has(k)?' inputmode="numeric"':'')+' aria-label="'+esc(label(k))+'"></td>';};
 const [text,ok]=editSum(c);
 return '<div class="table-wrap tall"><table class="data-table editable edit-table"><thead><tr>'+c.columns.map(x=>'<th class="'+cls(x[0])+'">'+esc(x[1])+'</th>').join('')+(c.can_add?'<th><span class="sr-only">줄 빼기</span></th>':'')+'</tr></thead><tbody>'+
  editDraft.rows.map((r,i)=>'<tr>'+c.columns.map(x=>cell(r,i,x[0])).join('')+(c.can_add?'<td><button type="button" class="text-button danger edit-del" data-action="editDelRow" data-i="'+i+'" aria-label="'+(i+1)+'번째 줄 빼기">빼기</button></td>':'')+'</tr>').join('')+
  '</tbody></table></div><div class="edit-foot">'+(c.can_add?button('editAddRow','줄 추가','input','ghost'):'')+'<p class="edit-sum '+(ok?'ok':'off')+'" id="editSum" role="status">'+esc(text)+'</p></div>';
}
const EDIT_NUMBERS=new Set(['quantity','unit_price','total_price','stmt_quantity']);
// 금액을 고칠 수 있으면 표의 합을, 잠겼으면(이온엠 입력 교정) 이온엠 화면 합계를 명세서와 견준다.
function editSum(c) {
 const n=v=>Number(String(v??'').replace(/,/g,''))||0,won=v=>v.toLocaleString()+'원';
 if(!c.sum_column)return ['',true];
 const target=(c.target_label||'목표')+' '+won(n(c.target));
 if(!c.editable.includes(c.sum_column)){const diff=n(c.ui_total)-n(c.target);
  return c.ui_total==null?[target,true]:[target+' · 이온엠 화면 합계 '+won(n(c.ui_total))+' · 차액 '+(diff>0?'+':'')+won(diff),!diff];}
 const sum=editDraft.rows.reduce((a,r)=>a+n(r[c.sum_column]),0),diff=sum-n(c.target);
 return [target+' · 표의 합 '+won(sum)+(diff?' · 차액 '+(diff>0?'+':'')+won(diff):' · 맞아요'),!diff];
}
function requestView(j) {
 const q=j.request;if(!q)return '';
 let input='';
 if(q.kind==='edit_rows')return '<section class="confirmation request-panel" aria-labelledby="requestTitle"><h3 id="requestTitle" data-raw>'+esc(VaderI18n.rune(j.rune_id,q.prompt))+'</h3>'+runeLangNote(j.rune_id,q.prompt)+editRowsView(q)+'<div class="actions">'+button('answer','고쳐서 다시 입고','confirm','primary')+button('skip','건너뛰기','arrow','ghost')+button('abort','중단','stop','ghost')+'</div><p id="responseError" role="alert"></p></section>';
 if(q.kind==='choice')input='<label for="answerValue">선택</label><select id="answerValue">'+q.options.map(o=>'<option value="'+esc(o)+'" data-raw>'+esc(VaderI18n.choice(j.rune_id,q.prompt,o))+'</option>').join('')+'</select>';
 if(q.kind==='correct_value')input='<label for="answerValue">수정할 값</label><input id="answerValue" autocomplete="off">';
 return '<section class="confirmation request-panel" aria-labelledby="requestTitle"><h3 id="requestTitle" data-raw>'+esc(VaderI18n.rune(j.rune_id,q.prompt))+'</h3>'+runeLangNote(j.rune_id,q.prompt)+(Object.keys(q.context).length?'<pre data-raw>'+esc(JSON.stringify(Object.fromEntries(Object.entries(q.context).map(([k,v])=>[VaderI18n.rune(j.rune_id,k),v])),null,2))+'</pre>':'')+input+'<div class="actions">'+button('answer','확인하고 계속','confirm','primary')+button('skip','건너뛰기','arrow','ghost')+button('abort','중단','stop','ghost')+'</div><p id="responseError" role="alert"></p></section>';
}
function runView() {
 const j=state.job;if(!j)return resultView();
 const total=j.total, pct=total>0?Math.min(100,j.current/total*100):null;
 return '<section class="scene"><div class="page-top"><h2>'+esc(taskRunes[j.rune_id]?.name||j.rune_id)+'</h2><span class="pill">'+(j.dry_run?'연습 실행':'실사용 실행')+'</span></div><div class="run-layout"><div class="run-core" data-agent-mount></div><div class="run-copy"><p class="small muted">'+esc(j.files.join(' · '))+'</p><h1>'+statusText(j)+'</h1><p class="lead">'+esc(VaderI18n.rune(j.rune_id,j.failure?.message||j.message||'룬의 응답을 기다리고 있어요.'))+'</p><div class="progress '+(pct===null?'indeterminate':'')+'" role="progressbar" aria-label="작업 진행" '+(pct!==null?'aria-valuemin="0" aria-valuemax="100" aria-valuenow="'+Math.round(pct)+'"':'')+'><span style="transform:scaleX('+(pct===null?.25:pct/100)+')"></span></div><div class="run-count"><span>'+j.current+'</span><small>'+(total!==null?' / '+total:' · 진행 항목')+'</small></div><div class="actions">'+(j.terminal?button('result','결과 보기','arrow','primary'):j.state==='cancel_requested'?'<span class="muted">중단 완료를 기다리는 중</span>':button('cancel','중단','stop','ghost'))+'</div></div></div>'+catalogView()+requestView(j)+logsView(j)+'</section>';
}
function resultView() {
 const j=state.job;
 if(!j)return coreView();
 const r=taskRunes[j.rune_id], next=promoteBubble(j)+retryBubble(j);
 const done=j.seen?button('back','돌아가기','back','ghost'):button('confirmResult','확인하고 돌아가기','confirm',next?'ghost':'primary');
 return '<section class="scene result-layout"><div class="result-agent" data-agent-mount></div><h1>'+statusText(j)+'</h1><p class="lead">'+raw(r?.name||j.rune_id)+' · '+T(j.dry_run?'연습 실행':'실사용 실행')+(j.attempt>1?' '+T('· {0}번째 시도',j.attempt):'')+'</p><p class="small muted">'+esc(j.files.join(' · '))+'</p>'
  +(j.failure?'<p class="error-copy">'+esc(VaderI18n.rune(j.rune_id,j.failure.message))+'</p>':'')
  +(Object.keys(j.summary).length?'<dl class="result-summary">'+Object.entries(j.summary).map(([k,v])=>'<div><dt data-raw>'+esc(VaderI18n.rune(j.rune_id,k))+'</dt><dd data-raw>'+esc(typeof v==='object'?JSON.stringify(v):VaderI18n.value(j.rune_id,k,v))+'</dd></div>').join('')+'</dl>':'')
  +(j.artifacts.length?'<div class="artifact-list">'+j.artifacts.map((name,i)=>'<button class="button" data-artifact="'+i+'">'+icon('file')+esc(name)+' · 위치 보기</button>').join('')+'</div>':'')
  +(j.failures.length?'<details><summary>처리 오류 '+j.failures.length+'건</summary>'+j.failures.map(f=>'<p data-raw>'+esc(VaderI18n.rune(j.rune_id,f.message))+'</p>').join('')+'</details>':'')
  +next+'<div class="actions">'+(j.failure&&/AI 연결/.test(j.failure.message)?button('openAi','AI 연결','link','primary'):'')+done+'</div>'+logsView(j)+historyView(j.id)+'</section>';
}
// 이번 세션 기록 — 한 건만 있어도 다시 열 수 있게. 앱을 닫으면 사라진다(파일은 남는다).
function historyView(current) {
 const past=state.jobs.filter(h=>h.terminal&&h.id!==current);
 if(!past.length)return '';
 return '<details class="session-history"><summary>이번 세션 기록 '+past.length+'건</summary><div class="history-list">'+past.map(h=>'<button type="button" class="history-row" data-job="'+h.id+'"><span><strong>'+esc(taskRunes[h.rune_id]?.name||h.rune_id)+'</strong><small>'+esc(h.files.join(' · '))+'</small></span><span class="history-meta">'+(h.dry_run?'연습':'실사용')+(h.attempt>1?' · '+h.attempt+'번째':'')+' · <b class="'+(h.state==='succeeded'?'good':h.state==='failed'?'bad':'')+'">'+({succeeded:'완료',failed:'실패',cancelled:'중단'}[h.state]||statusText(h))+'</b>'+(h.seen?'':' · 확인 전')+'</span></button>').join('')+'</div></details>';
}
async function confirmResult() {
 const j=state.job;if(!j)return;
 try{const done=await api('confirm_job',{job_id:j.id});state.jobs=state.jobs.map(x=>x.id===done.id?done:x);state.job=done;lastSnapshot='';}
 catch(err){toast(err.message);return;}
 navigate('core');
}
// 연습이 성공했으면 에이전트가 묻는다 — 같은 파일로 실전 실행할까요?
function promoteBubble(j) {
 if(!j.dry_run||j.state!=='succeeded'||j.promoted||state.dismissed.has(j.id))return '';
 const r=taskRunes[j.rune_id],files=esc(j.files.join(' · '));
 if(r?.locked)return '<div class="agent-bubble" role="group" aria-label="에이전트 제안"><p><b>연습 실행이 끝났어요.</b> 실전 실행은 이 룬을 승인한 뒤에 할 수 있어요.</p><div class="bubble-actions">'+button('approveFromResult','승인하러 가기','confirm','primary')+'<button type="button" class="button ghost" data-action="dismissPromote">닫기</button>'+'</div></div>';
 return '<div class="agent-bubble" role="group" aria-label="에이전트 제안"><p><b>실전 실행을 진행할까요?</b></p><p class="bubble-detail">방금 연습한 같은 파일로 이번엔 실제로 처리합니다'+(r?.target_apps?.length?' — '+esc(r.target_apps.join(', '))+'에 입력돼요':'')+'.<br><span>'+files+'</span></p><div class="bubble-actions">'+button('promote','예, 실전 실행','arrow','primary')+'<button type="button" class="button ghost" data-action="dismissPromote">아니요</button>'+'</div></div>';
}
// 실전이 실패·중단으로 끝났으면 — 같은 연습으로 이어서 다시. 이미 입력한 건은 룬이 건너뛴다.
function retryBubble(j) {
 if(!j.retryable||state.dismissed.has(j.id))return '';
 const tries=j.attempt>=3?'<p class="bubble-detail bubble-warn">같은 실패가 '+j.attempt+'번째예요. 다시 실행하기 전에 진행 기록에서 원인을 먼저 확인해 주세요.</p>':'';
 const apps=(taskRunes[j.rune_id]?.target_apps||[]).join(', ');
 return '<div class="agent-bubble" role="group" aria-label="에이전트 제안"><p><b>실전 실행이 끝나지 못했어요.</b></p><p class="bubble-detail">같은 연습 기준으로 <b>실전을 다시</b> 실행할까요? 이미 처리한 건은 룬의 진행 기록대로 건너뜁니다.'+(apps?'<br>멈출 때 입력 중이던 건은 '+esc(apps)+'에 일부만 들어갔을 수 있어요 — 진행 기록의 마지막 건을 '+esc(apps)+'에서 먼저 확인해 주세요.':'')+'</p>'+tries+'<div class="bubble-actions">'+button('retry','실전 다시 실행','arrow','primary')+'<button type="button" class="button ghost" data-action="retryPick">파일 다시 고르기</button><button type="button" class="button ghost" data-action="dismissPromote">아니요</button></div></div>';
}
async function retry(paths) {
 try{const job=await api('promote',{job_id:state.job.practice,...(paths?{paths}:{})});state.job=job;state.jobs=[job,...state.jobs.filter(x=>x.id!==job.id)];lastSnapshot='';navigate(job.terminal?'result':'run');}
 catch(e){toast(e.message);}
}
async function promote() {
 try{const job=await api('promote',{job_id:state.job.id});state.job=job;state.jobs=[job,...state.jobs.filter(x=>x.id!==job.id)];lastSnapshot='';navigate(job.terminal?'result':'run');}
 catch(e){toast(e.message);}
}
function applyMotion() {
 document.body.classList.toggle('motion-paused',state.motion);   // 효과 끄기 버튼은 뺐다 (2026-11-09) — Windows '애니메이션 효과' 끄기를 따른다
}
function intent() {return {rune_id:state.task,dry_run:state.dryRun,start_page:START_PAGE_RUNES.has(state.task)?state.startPage:1};}
// 실행 확인 창 (마스터 요청 2026-11-19) — 파일을 고르거나 놓으면 바로 돌지 않고, 여기서 연습/실사용·시작 페이지를 정해 [실행].
// 메인 화면에는 그래프만 남긴다.
function start(paths, selected=intent()) {
 if(!paths.length||state.busy)return;
 const r=taskRunes[selected.rune_id];if(!r)return;
 const names=paths.map(p=>String(p).split(/[\\/]/).pop()),dry=r.locked||selected.dry_run;
 const d=modal('<form id="runAsk">'+heading(r.name+' 실행')
  +runeLangNote(r.id)+'<p class="dialog-copy run-files">'+esc(names.slice(0,3).join(' · '))+(names.length>3?' 외 '+(names.length-3)+'개':'')+'</p>'
  +'<label class="check"><input id="runDry" type="checkbox" '+(dry?'checked ':'')+(r.locked?'disabled':'')+'> 연습 실행</label>'
  +(START_PAGE_RUNES.has(r.id)?'<div class="settings-field"><label for="runPage">시작 페이지</label><input id="runPage" type="number" min="1" value="'+(selected.start_page||1)+'"></div>':'')
  +'<p class="small muted" id="runNote">'+esc(r.locked?r.reason:dry?'연습 모드 · 실제 저장 동작은 룬의 연습 정책을 따릅니다.':'실사용 모드 · 실제 처리가 시작됩니다.')+'</p>'
  +(state.aiReady?'':'<p class="ai-missing">'+icon('info')+'<span>판단·대화에 쓸 AI 가 준비되지 않았어요. 문서 읽기·판단이 필요한 룬은 AI 를 연결한 뒤에 쓸 수 있어요.</span>'+button('openAi','AI 연결','link','ghost')+'</p>')
  +'<div class="actions"><button type="submit" class="button primary">'+icon('arrow')+'실행</button><button type="button" class="button ghost" data-action="closeDialog">취소</button></div></form>');
 const dryBox=$('#runDry',d);dryBox.onchange=()=>{$('#runNote',d).textContent=dryBox.checked?'연습 모드 · 실제 저장 동작은 룬의 연습 정책을 따릅니다.':'실사용 모드 · 실제 처리가 시작됩니다.';};
 $('#runAsk',d).onsubmit=e=>{e.preventDefault();
  state.dryRun=r.locked||dryBox.checked;
  const page=$('#runPage',d);if(page)state.startPage=Math.max(1,Math.floor(Number(page.value)||1));
  d.close();submitRun(paths,{...selected,dry_run:state.dryRun,start_page:START_PAGE_RUNES.has(r.id)?state.startPage:1});};
 $('#runAsk button[type=submit]',d).focus();
}
async function submitRun(paths, selected) {
 if(!paths.length||state.busy)return;
 state.busy=true;
 try {
  const job=await api('submit',{...selected,paths});
  state.job=job;state.jobs=[job,...state.jobs.filter(j=>j.id!==job.id)];lastSnapshot='';navigate(job.terminal?'result':'run');
 } catch(e) {toast(e.message);} finally {state.busy=false;}
}
// 파일은 그래프 어디에 놓아도 된다. 끌어 오면 회오리가 일고, 놓으면 빨려 들어가며 고른 룬이 실행된다 (마스터 요청 2026-11-09).
function bindDrop() {
 const zone=$('#dropzone'),stage=$('.core-stage',main)||zone;let depth=0;
 const choose=async()=>{if(state.busy)return;const selected=intent();try{await start(await api('pick'),selected);}catch(e){toast(e.message);}};
 zone.onclick=choose;zone.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();choose();}};
 vortex.on=false;vortex.gone=0;
 const swirl=on=>{vortex.on=on&&!state.busy;stage.classList.toggle('dragging',vortex.on);zone.classList.toggle('dragging',vortex.on);animateGraph();};
 stage.ondragenter=e=>{e.preventDefault();if(!depth++)swirl(true);};
 stage.ondragover=e=>{e.preventDefault();e.dataTransfer.dropEffect='copy';};
 stage.ondragleave=()=>{if(--depth<=0){depth=0;swirl(false);}};
 stage.ondrop=e=>{e.preventDefault();depth=0;swirl(false);if(state.busy)return;
  vortex.gone=state.motion?0:.001;stage.classList.add('dropped');animateGraph();
  pendingDrop={...intent(),at:Date.now()};
  // 실행이 시작되면 화면이 바뀐다. 경로를 못 받아 그대로 남으면 그래프를 되돌린다.
  setTimeout(()=>{if(stage.isConnected){vortex.gone=0;stage.classList.remove('dropped');placeGraph();}},1500);};
}
async function poll() {
 try {
  const jobs=await api('poll');const reconnected=!state.connected;state.connected=true;$('#connectionStatus').textContent='앱 연결됨';
  const snapshot=JSON.stringify(jobs);
  if(snapshot!==lastSnapshot||reconnected) {
   lastSnapshot=snapshot;state.jobs=jobs;railBadge();
   const previous=state.job;state.job=jobs.find(j=>j.id===previous?.id)||jobs[0]||null;
   if(state.job?.request?.id!==previous?.request?.id&&state.job?.request)$('#announcer').textContent=VaderI18n.rune(state.job.rune_id,state.job.request.prompt);
   if(state.page==='run'&&state.job?.terminal&&!previous?.terminal)navigate('result');
   else if(['run','result'].includes(state.page)) {
    const withoutLogs=j=>j?{...j,logs:null}:null;
    if(!reconnected&&logTerminal.element.isConnected&&JSON.stringify(withoutLogs(previous))===JSON.stringify(withoutLogs(state.job))){
     logTerminal.mount(logTerminal.element,state.job);
    }else{
    // Do not erase a user's correction while progress/log events arrive.
    const answer=$('#answerValue')?.value;
    render();if(answer!==undefined&&$('#answerValue')&&state.job?.request?.id===previous?.request?.id)$('#answerValue').value=answer;
    }
   }
  }
 } catch(e) {state.connected=false;logTerminal.setConnected(false);$('#connectionStatus').textContent='연결 확인 필요 · 다시 연결 중';}
 finally {setTimeout(poll,400);}
}
// 자동 업데이트 — 뒤에서 받는 동안만 머리띠에 보인다. 받아 두면 창을 닫을 때 바뀐다 (vader/core/update.py).
async function watchUpdate() {
 let u;try{u=await api('update_status');}catch(e){setTimeout(watchUpdate,5000);return;}
 const el=$('#updateStatus'),v=u.version?' v'+u.version:'';
 const text={downloading:'새 버전'+v+' 받는 중'+(u.progress?' '+u.progress+'%':'')+' · 끝나기 전에 닫으면 다음에 다시 받아요',
  ready:VaderI18n.t('새 버전{0} 준비됨 · 창을 닫으면 바뀌어요',v),failed:'업데이트 확인 실패 · 다음 실행 때 다시 시도해요'}[u.state];
 el.hidden=!text;el.textContent=text||'';el.className='update-status '+u.state;el.title=u.state==='failed'?(u.error||''):'';
 if(u.state==='ready')toast(VaderI18n.t('새 버전{0}을 받았어요. 작업이 없을 때 창을 닫으면 새 버전으로 바뀌어요.',v));
 if(['checking','downloading'].includes(u.state))setTimeout(watchUpdate,2000);
}
// 목록 안내 — 작업 중에도 회수가 화면에 닿게 몇 초마다 따로 묻는다. 바뀌었을 때만 다시 그린다.
async function watchCatalog() {
 try{const a=await api('catalog_alerts'),next=JSON.stringify(a);
  if(next!==watchCatalog.last){watchCatalog.last=next;state.catalogNotices=a.catalog_notices||[];state.exitRequired=a.exit_required||null;state.updates=a.updates||{};runesBadge();render();}}
 catch(e){}
 loadRuns();   // 기록 찾기 칸은 실행 기록이 있을 때만
 loadTodos(true);   // 다시 알릴 때가 된 카드 표시 (트레이 알림은 코어가 따로)
 setTimeout(watchCatalog,10000);
}
async function connect() {
 refreshAccount();   // 코어 다리가 붙은 뒤에 묻는다 — 먼저 물으면 실패해 로그인 뒤에도 '로그인' 으로 남았다
 try {catalog(await api('catalog'));state.connected=true;
  // 화면만 다시 열렸고 콘솔은 살아 있다 — 이어서 보여 준다 (지난 출력은 없다)
  if(state.console.alive&&!consoleSession){ensureTerminal();consoleSession=state.console.session;consoleClient=state.console.client;consoleRune=state.console.rune_id||null;consoleName={claude:'Claude Code',codex:'Codex'}[consoleClient]||consoleClient;readConsole(consoleSession);}
  render();loadRuns();loadTodos();if(!polling){polling=true;poll();watchUpdate();watchCatalog();api('app_version').then(v=>{$('#appVersion').textContent='v'+v;}).catch(()=>{});}}
 catch(e){$('#connectionStatus').textContent=e.message;toast(e.message);}
}
async function openSettings() {
 settingsRune=state.task;const r=taskRunes[settingsRune];if(!r)return;
 const dialog=$('#settingsDialog');dialog.classList.remove('wide');
 dialog.innerHTML='<form id="settingsForm"><div class="dialog-heading"><h2 id="settingsTitle">'+esc(r.name)+' 설정</h2><button type="button" class="text-button" id="closeSettings">닫기</button></div>'+r.fields.map((f,i)=>{
  const id='field-'+i, value=f.value??'';
  const input=f.kind==='choice'?'<select id="'+id+'" name="'+esc(f.name)+'">'+f.options.map(o=>'<option value="'+esc(o)+'" data-raw '+(o===value?'selected':'')+'>'+esc(VaderI18n.choice(settingsRune,f.name,o))+'</option>').join('')+'</select>':'<input id="'+id+'" name="'+esc(f.name)+'" type="'+(f.kind==='secret'?'password':f.kind==='number'?'number':'text')+'" value="'+esc(value)+'" '+(f.kind==='secret'&&f.configured?'placeholder="저장된 값 유지"':'')+' autocomplete="off">';
  return '<div class="settings-field"><label for="'+id+'">'+esc(f.label||f.name)+(f.optional?' · 선택':'')+'</label><div>'+input+(['path','dir'].includes(f.kind)?'<button type="button" class="text-button" data-pick-field="'+id+'" data-kind="'+f.kind+'">찾기</button>':'')+'</div></div>';
 }).join('')+(r.fields.length?'':'<p>이 룬에는 추가 설정이 없습니다.</p>')+'<p id="settingsError" role="alert"></p><div class="actions"><button type="submit" class="button primary">저장</button></div></form>';
 $('#closeSettings').onclick=()=>dialog.close();
 $('#settingsForm').onsubmit=async e=>{e.preventDefault();try{catalog(await api('save_settings',{rune_id:settingsRune,values:Object.fromEntries(new FormData(e.target))}));dialog.close();render();toast('룬 설정을 저장했어요.');}catch(err){$('#settingsError').textContent=err.message;}};
 dialog.showModal();
}
// 룬 제작소 — Claude Code 의 VaderAI MCP 가 만든 룬을 이 PC 에서 시험해도 되나. 사람이 켠다.
function devSwitch() {
 return '<div class="dev-mode"><button type="button" class="dev-switch" role="switch" aria-checked="'+state.devMode+'" data-action="devMode"><span class="knob" aria-hidden="true"></span>개발 모드</button><p>'+(state.devMode?'켜져 있어요. 아래에서 룬을 만들 프로그램을 여세요. 룬을 만들 때만 켜 두세요.':'룬을 만들 때 켜면 Claude Code·Codex 로 룬을 만들고 이 PC 에서 시험할 수 있어요.')+'</p>'+(state.devMode?clientPicker()+testRequests():'')+'</div>';
}
// ── 제작소 — 프로젝트 고르기 → 만들 프로그램 → (다음 단계) 콘솔. 시험·포장 상태는 선택한 프로젝트만 ──
const TEST_STATE={none:['시험 전','idle'],pass:['시험 통과','good'],fail:['시험 실패','bad'],stale:['고친 뒤 시험 전','warn']};
// 기존 방식 룬은 파일 시험이 없다 — 설치본과의 차이를 보인다
const INSTALL_STATE={same:['설치됨','good'],changed:['고친 뒤 설치 전','warn'],none:['설치 전','idle']};
const projectState=p=>p.profile==='table'?TEST_STATE[p.test]:INSTALL_STATE[p.installed];
function workshopView() {
 const list=state.projects.length?'<div class="project-list" role="list">'+state.projects.map(p=>{const [t,tone]=projectState(p);
  return '<button type="button" role="listitem" class="project-row'+(p.id===state.project?' selected':'')+'" data-project="'+esc(p.id)+'" aria-pressed="'+(p.id===state.project)+'"><span class="project-name"><strong>'+esc(p.name)+'</strong><small>'+esc(p.id)+(p.version?' · v'+esc(p.version):'')+'</small></span><span class="chip '+(p.waiting?'warn':tone)+'">'+(p.waiting?'시험 요청':t)+'</span></button>';}).join('')+'</div>'
  :'<p class="muted small">아직 만든 룬이 없어요.</p>';
 return '<section class="scene studio"><div class="studio-left"><h1>제작소</h1><p class="rune-intro">업무를 설명하면 AI 가 룬을 만들고, 여기서 시험·포장해요.</p>'+devSwitch()+'<h2 class="section-label">만들고 있는 룬</h2>'+list+'</div><div class="studio-main'+(consoleSession?' with-console':'')+'">'+consolePanel()+projectDetail()+'</div></section>';
}
function projectDetail() {
 const p=state.projects.find(x=>x.id===state.project);
 if(!p)return '<div class="studio-empty"><h2>처음 룬 만들기</h2><ol class="steps"><li><b>개발 모드</b>를 켜요.</li><li>만들 프로그램(<b>Claude Code</b> 또는 <b>Codex</b>)을 열어요. 첫 마디는 자동으로 들어가요.</li><li>AI 가 묻는 대로 업무를 고르고, 예시 파일과 정답 파일을 넣어 줘요.</li><li>시험 요청이 오면 여기서 코드 요약을 보고 허용해요.</li><li>포장이 끝나면 이 화면에서 <b>내 룬에 설치</b>해요.</li></ol></div>';
 const [t,tone]=TEST_STATE[p.test], legacy=p.profile!=='table', id='data-id="'+esc(p.id)+'"';
 const next=p.waiting?button('reviewTest','시험 요청 보기','confirm','primary').replace('data-action="reviewTest"','data-action="reviewTest" data-id="'+esc(p.id)+'"')
  :legacy&&p.installed!=='same'?button('installProject',p.installed==='none'?'내 룬에 설치':'고친 내용 다시 설치','up','primary').replace('data-action="installProject"','data-action="installProject" '+id)
  :p.packed?button('installProject','내 룬에 설치','up','primary').replace('data-action="installProject"','data-action="installProject" '+id)
  :'<p class="muted small">'+({none:'만드는 프로그램에서 시험을 요청하게 하세요.',fail:'차이를 고친 뒤 다시 시험하게 하세요.',stale:'고친 코드로 다시 시험하게 하세요.',pass:'만드는 프로그램에서 포장하게 하세요.'}[p.test])+'</p>';
 return '<div class="project-detail"><p class="eyebrow">'+(p.profile==='table'?'표 룬':'기존 방식 · 이 PC 에서만')+'</p><h2>'+esc(p.name)+'</h2><p class="muted">'+esc(p.id)+(p.version?' · v'+esc(p.version):'')+'</p><dl class="project-facts">'+(legacy?'':'<dt>시험</dt><dd><span class="chip '+tone+'">'+t+'</span>'+(p.tested_at?' <span class="muted small">'+esc(p.tested_at.replace('T',' '))+'</span>':'')+'</dd>')+(p.cases.length?'<dt>사례</dt><dd><ul class="case-list">'+p.cases.map(c=>'<li class="'+(c.ok?'good':'bad')+'">'+(c.ok?'맞음':'다름')+' · '+esc(c.case)+'</li>').join('')+'</ul></dd>':'')+''+(legacy?'':'<dt>포장</dt><dd>'+(p.packed?'지금 코드 그대로 포장됨':'<span class="muted">아직</span>')+'</dd>')+editFacts(p)+'</dl>'+(legacy?'<p class="small muted">기존 방식 룬은 파일 사례 시험·포장 없이 고친 폴더를 이 PC 에 다시 설치해요. 화면 동작은 설치 뒤 연습 실행으로 확인하세요.</p>':'')+'<div class="actions">'+next+'</div>'+editClients(p)+'</div>';
}
// 고치는 룬 — 어디를 고치나(하나뿐) · 설치본과의 차이 · 룬 안의 녹화 (2026-10-15 마스터 결정, codex1 합의)
function editFacts(p) {
 const id='data-id="'+esc(p.id)+'"';
 const where=p.source?'<span class="mono-path">'+esc(p.source)+'</span> <span class="muted small">연결한 원본</span>'
  :'<span class="muted">제작소 사본</span> <button type="button" class="text-button" data-action="linkSource" '+id+'>원본 폴더 연결…</button>';
 const inst={same:'고친 것 없음 · 설치본과 같아요',changed:'<span class="chip warn">고친 내용이 아직 설치 전</span>',none:'<span class="muted">설치 안 됨</span>'}[p.installed];
 const recs=p.recordings.map(n=>'<span class="chip idle">'+esc(n)+'</span>').join(' ')+p.legacy_recordings.map(n=>' <button type="button" class="text-button" data-action="importRecording" '+id+' data-name="'+esc(n)+'">옛 녹화 \''+esc(n)+'\' 가져오기</button>').join('');
 return '<dt>고칠 곳</dt><dd>'+where+'</dd><dt>설치본</dt><dd>'+inst+(p.installed_moved?'<p class="small ai-bad">가져온 뒤 설치본이 따로 바뀌었어요. 이 편집본이 더 오래됐을 수 있어요.</p>':'')+'</dd><dt>녹화</dt><dd>'+(recs||'<span class="muted">없음</span>')+'</dd>';
}
function editClients(p) {
 if(!state.devMode)return '<p class="small muted">고치려면 왼쪽에서 <b>개발 모드</b>를 켜세요.</p>';
 const ready=state.clients.filter(c=>c.connected);
 return ready.length?'<div class="edit-with"><span class="small muted">이 룬 고치기</span>'+ready.map(c=>'<button type="button" class="button ghost" data-action="editWith" data-id="'+esc(p.id)+'" data-client="'+esc(c.id)+'">'+icon('workshop')+esc(c.name)+'</button>').join('')+'</div>'
  :'<p class="small muted">왼쪽에서 Claude Code 나 Codex 를 먼저 연결하세요.</p>';
}
// ── 광장 — 내 룬과 같은 모양: 왼쪽 목록, 고르면 오른쪽에 설명과 설치. 파일 설치는 내 룬 '룬 설치' 에 ──
const MARKET_STATE={new:'설치 전',update:'업데이트 있음',installed:'설치됨',newer:'더 새 버전 설치됨',check:'설치 확인 필요',local:'직접 설치한 룬 있음'};
function marketView() {
 const l=state.listing;
 if(!l||!l.enabled)return '<section class="scene market"><h1>광장</h1><p class="muted market-off">광장 기능을 준비하고 있어요.</p></section>';
 const refresh='<button type="button" class="text-button" data-action="marketRefresh">목록 새로 고침</button>'+(l.error?'<p class="small ai-bad">'+esc(l.error)+'</p>':'');
 if(!l.releases.length)return '<section class="scene market"><h1>광장</h1><p class="muted market-off">'+(l.error?'':l.fresh?'아직 올라온 룬이 없어요.':'목록 정보를 아직 받지 못했어요. 인터넷 연결을 확인한 뒤 새로 고쳐 주세요.')+'</p>'+refresh+'</section>';
 const r=l.releases.find(x=>x.rune_id===state.marketPick)||l.releases[0], look=runeLook(r.rune_id);
 const rows=l.releases.map(x=>{const d=runeLook(x.rune_id),on=x===r;
  return '<div class="tree-row" style="--rune:'+d.color+';--rune-rgb:'+d.rgb+'"><button type="button" class="tree-rune '+(on?'selected':'')+'" data-action="marketPick" data-id="'+esc(x.rune_id)+'" aria-pressed="'+on+'">'+sigil(x.rune_id)+'<span class="tree-copy"><strong data-raw>'+esc(VaderI18n.rune(x.rune_id,x.name))+'</strong><small>v'+esc(x.version)+' · '+T(MARKET_STATE[x.state])+'</small></span></button></div>';}).join('');
 const take=(text)=>'<button type="button" class="button primary" data-action="marketConfirm" data-id="'+esc(r.rune_id)+'" data-version="'+esc(r.version)+'" data-sha="'+esc(r.archive_sha256)+'">'+icon('down')+esc(text)+'</button>';
 const act={new:take('설치'),check:take('다시 받기'),update:take('v'+r.installed+' → v'+r.version+' 업데이트'),
  installed:'<p class="small muted">설치됨 · 내 룬에서 연습 실행으로 확인한 뒤 승인하면 실사용할 수 있어요.</p>',
  newer:'<p class="small muted">이 PC 에 v'+esc(r.installed||'')+' 이 설치돼 있어요.</p>',
  local:updateOf(r.rune_id)?.local?updateButton(r.rune_id,updateOf(r.rune_id)):'<p class="small market-warn">이 PC 에 직접 설치한 같은 룬이 있어요. 광장 버전으로 바꾸려면 내 룬에서 제거한 뒤 받으세요.</p>'}[r.state]||'';
 return '<section class="scene grimoire market" style="--rune:'+look.color+';--rune-rgb:'+look.rgb+'"><div class="rune-atmosphere" aria-hidden="true"></div>'
  +'<div class="rune-left"><h1>광장</h1><p class="rune-intro">운영자가 검토하고 서명한 룬.</p><div class="rune-tree">'+rows+'</div>'+refresh+'</div>'
  +'<div class="rune-showcase"><div class="rune-relic" aria-hidden="true"><div class="relic-aura"></div><div class="relic-orbit orbit-one"></div><div class="relic-orbit orbit-two"></div><div class="relic-stone stone-back"></div><div class="relic-stone stone-front"></div>'+sigil(r.rune_id,true)+'<div class="relic-floor"></div><i class="ember e1"></i><i class="ember e2"></i><i class="ember e3"></i><i class="ember e4"></i></div>'
  +'<div class="rune-description"><h2 data-raw>'+esc(VaderI18n.rune(r.rune_id,r.name))+'</h2><p>'+esc(r.summary||descriptions[r.rune_id]||'운영자가 검토하고 서명한 룬이에요.')+'</p>'
  +'<p class="small muted">v'+esc(r.version)+' · '+(r.permissions.length?T('필요한 권한 · {0}',esc((r.permission_text||r.permissions).join(' · '))):T('추가 권한 없음'))+'</p>'
  +'<div class="actions">'+act+(l.publisher?'<button type="button" class="button ghost" data-action="marketShare" data-id="'+esc(r.rune_id)+'">'+icon('people')+'공유</button>':'')+'</div><p class="small muted market-after">받은 뒤에는 이 PC 에서 연습 실행으로 확인하고 승인해야 실사용할 수 있어요.</p></div></div></section>';
}
// 받은 패키지의 서명 상태. 깨진 서명은 서버 쪽에서 이미 거부됐다.
function signatureView(s) {
 const g=s.signature||{state:'none'};
 if(g.state==='trusted')return '<p class="signature trusted">'+icon('confirm')+'<span>'+T('{0} 서명 · 등록된 서명자예요.','<b data-raw>'+esc(g.signer)+'</b>')+'</span></p>';
 if(g.state==='unknown')return '<div class="signature unknown"><p>'+T('{0} 서명 · 아직 등록하지 않은 서명자예요. 확인 번호 {1}','<b data-raw>'+esc(g.signer)+'</b>','<b class="key-id">'+esc(g.key_id)+'</b>')+'</p><label class="check"><input type="checkbox" id="trustSigner"> 보낸 사람에게 전화 등으로 이 번호가 맞는지 확인했어요 (서명자로 등록)</label></div>';
 return '<p class="signature none">서명 없음 · 직접 만든 룬이면 괜찮아요. 다른 사람에게 받은 파일이면 설치하지 마세요.</p>';
}
// 시험 요청 — 만드는 프로그램이 rune_test 를 불렀는데 이 버전을 아직 사람이 허용하지 않았다 (마스터 결정 2026-10-14).
function testRequests() {
 return state.testRequests.map(t=>'<button type="button" class="test-request" data-action="reviewTest" data-id="'+esc(t.id)+'"><strong>시험 요청 · '+esc(t.name)+'</strong><small>'+(t.watch.length||t.needs.length?T('확인할 동작 {0}가지 ·',t.watch.length+t.needs.length)+' ':'')+T('요약 보고 허용')+'</small></button>').join('');
}
// 코드를 실행하지 않고 읽은 요약. 설치 확인과 시험 허용이 같은 모양을 쓴다.
function codeSummary(s) {
 const list=(items,none)=>items.length?'<ul>'+items.map(i=>'<li>'+esc(i)+'</li>').join('')+'</ul>':'<span class="muted">'+none+'</span>';
 const mode=s.profile==='table'?'표 룬 · 정해진 규칙 안에서만 표를 읽고 써요':'기존 방식 · 제한 없음 (이 PC 에서만 쓰는 룬)';
 return '<dl class="code-summary"><dt>실행 방식</dt><dd'+(s.profile==='table'?'':' class="legacy"')+'>'+mode+'</dd><dt>필요한 권한</dt><dd>'+list(s.needs,'없음')+'</dd><dt>살펴볼 동작</dt><dd>'+list(s.watch,'찾지 못함')+'</dd><dt>쓰는 모듈</dt><dd>'+(s.modules.length?esc(s.modules.join(', ')):'<span class="muted">없음</span>')+'</dd><dt>파일 '+s.files.length+'개</dt><dd>'+esc(s.files.join(', '))+'</dd></dl><p class="small muted">코드를 실행하지 않고 읽은 요약이에요. 숨긴 동작까지 잡지는 못해요.</p>';
}
function reviewTest(a) {
 const t=state.testRequests.find(x=>x.id===a.dataset.id);if(!t)return;
 modal(heading(T('{0} · 시험 허용',t.name)).replace('<h2','<h2 data-raw')+'<p class="dialog-copy">만드는 프로그램이 이 버전을 <b>이 PC 에서</b> 시험하려고 해요. 보안 격리는 아니에요. 코드를 고치면 다시 허용해야 해요.</p>'+codeSummary(t)+'<p id="dialogError" role="alert"></p><div class="actions"><button type="button" class="button primary" data-action="allowTest" data-id="'+esc(t.id)+'">'+icon('confirm')+'이 버전만 시험 허용</button>'+button('closeDialog','취소','stop','ghost')+'</div>',true);
}
async function allowTest(a) {
 const t=state.testRequests.find(x=>x.id===a.dataset.id);if(!t)return;
 try{catalog(await api('allow_test',{rune_id:t.id,fingerprint:t.fingerprint}));}catch(err){dialogError(err.message);return;}
 $('#settingsDialog').close();render();
 toast(t.name+' 시험을 허용했어요. 만드는 창에 "허용했어" 라고 말해 주세요.');
}
// 룬을 만들 프로그램. 연결됨=뚜렷하게, 연결 안 됨=흐리게(누르면 연결할지 묻는다), 설치 안 됨=더 흐리게(누르면 설치할지 묻는다).
function clientPicker() {
 return '<div class="dev-clients" role="group" aria-label="룬을 만들 프로그램">'+state.clients.map(c=>{
  const s=c.connected?'connected':c.installed?'disconnected':'missing';
  return '<button type="button" class="dev-client" data-state="'+s+'" data-action="openClient" data-client="'+esc(c.id)+'"><strong>'+esc(c.name)+'</strong><small>'+(c.connected?'연결됨 · 열기':c.installed?'연결 안 됨':'설치 안 됨 · 설치')+'</small></button>';
 }).join('')+'</div>';
}
async function openClient(a) {
 const c=state.clients.find(x=>x.id===a.dataset.client);if(!c)return;
 if(!c.installed){
  modal(heading(c.name+'를 설치할까요?')+'<p>Windows 앱 설치 관리자로 '+esc(c.name)+' 공식 패키지를 이 PC 에 설치하고 VaderAI 에 연결해요. '+(c.id==='claude'?'Git 이 없으면 함께 설치해요. ':'')+'몇 분 걸릴 수 있고, Windows 가 허용할지 물으면 <b>예</b>를 눌러 주세요. 설치하면 그 프로그램의 이용 약관에 동의하는 것이에요.</p><p class="small muted">처음 열 때 '+esc(c.name)+' 계정으로 로그인해요.</p><p id="dialogError" role="alert"></p><div class="actions"><button type="button" class="button primary" data-action="installClient" data-client="'+esc(c.id)+'">'+icon('confirm')+'설치하고 연결</button>'+button('closeDialog','취소','stop','ghost')+'</div>');
  return;
 }
 if(!c.connected){
  modal(heading(c.name+'에 연결할까요?')+'<p>'+esc(c.name)+' 에 VaderAI 룬 제작 도구를 등록하고 제작소에서 열어요. 이 PC 의 '+esc(c.name)+' 설정에 한 줄이 더해지고, 언제든 그 프로그램에서 지울 수 있어요.</p><p id="dialogError" role="alert"></p><div class="actions"><button type="button" class="button primary" data-action="connectClient" data-client="'+esc(c.id)+'">'+icon('confirm')+'연결하고 열기</button>'+button('closeDialog','취소','stop','ghost')+'</div>');
  return;
 }
 await startConsole(c);
}
async function installClient(a) {
 const id=a.dataset.client;a.disabled=true;a.textContent='설치하는 중… 창을 닫지 마세요';
 try{catalog(await api('install_client',{client:id}));await connectClient(a);}
 catch(err){a.disabled=false;a.textContent='다시 시도';dialogError(err.message);}
}
async function connectClient(a) {
 const c=state.clients.find(x=>x.id===a.dataset.client);
 try{catalog(await api('connect_client',{client:c.id}));}catch(err){dialogError(err.message);return;}
 $('#settingsDialog').close();
 await startConsole(state.clients.find(x=>x.id===c.id)||c);
}
// ── 제작소 안 콘솔 (xterm.js ↔ ConPTY) — 권한 장치가 아니다. 출력은 그리기만 하고 HTML 로 넣지 않는다 ──
const consoleBox=document.createElement('div');consoleBox.className='console-screen';
let term=null,fitter=null,consoleSession=null,consoleClient=null,consoleRune=null,consoleName='',pendingInput='',writingInput=false,fitTimer;
function consolePanel() {
 if(!consoleSession)return '';
 return '<div class="console-panel"><div class="console-bar"><span class="console-title"><i class="console-dot" aria-hidden="true"></i>'+esc(consoleName)+' <small>제작 폴더에서 실행 중 · 이 PC 의 파일·명령을 쓸 수 있어요</small></span><span class="console-tools"><button type="button" class="text-button" data-action="attachFile" title="고른 파일의 경로를 콘솔에 넣어요">'+icon('attach')+'파일</button><button type="button" class="text-button record" data-action="recordOpen" title="대상 프로그램에서 시연한 클릭·이동 키를 '+(consoleRune?'이 룬의':'만들 룬의')+' 녹화로 넘겨요">'+icon('record')+'녹화</button><button type="button" class="text-button" data-action="stopConsole">'+icon('stop')+'닫기</button></span></div><div class="console-mount" data-console-mount></div></div>';
}
function ensureTerminal() {
 if(term)return;
 term=new Terminal({fontFamily:'"Cascadia Mono","D2Coding",Consolas,"Malgun Gothic",monospace',fontSize:13,lineHeight:1.15,cursorBlink:true,scrollback:5000,
  theme:{background:'#07090d',foreground:'#e6ebf2',cursor:'#ffb35d',cursorAccent:'#07090d',selectionBackground:'#ffb35d44'}});
 fitter=new FitAddon.FitAddon();term.loadAddon(fitter);term.open(consoleBox);term.attachCustomKeyEventHandler(onConsoleKey);
 term.onData(d=>{pendingInput+=d;if(!writingInput)flushInput();});
 term.onResize(({cols,rows})=>{if(consoleSession)api('console_resize',{session:consoleSession,cols,rows}).catch(()=>{});});
 new ResizeObserver(()=>{clearTimeout(fitTimer);fitTimer=setTimeout(fitConsole,60);}).observe(consoleBox);
}
// 복사·붙여넣기 (2026-10-27 시험 PC 피드백, codex1 검토) — Windows 터미널 관례.
// 선택이 있으면 Ctrl+C·우클릭이 복사, 없으면 Ctrl+C 는 그대로 중단. Ctrl+V 는 브라우저 붙여넣기를 xterm 이 받는다(글자).
// 글자가 아니면 탐색기에서 복사한 파일·스크린샷을 **경로**로 넣는다 — Claude Code·Codex 는 경로로 파일·이미지를 읽는다. Enter 는 보내지 않는다.
const quotePaths=paths=>' '+paths.map(p=>/\s/.test(p)?'"'+p+'"':p).join(' ')+' ';
function consoleInsert(session,text){if(term&&session===consoleSession){term.paste(text);term.focus();}}
async function copySelection(){
 const text=term.getSelection();term.clearSelection();
 try{await navigator.clipboard.writeText(text);}catch(e){toast('복사하지 못했어요. 다시 선택해 주세요.');}
}
function onConsoleKey(e) {
 if(e.type!=='keydown'||!e.ctrlKey||e.altKey||e.metaKey)return true;
 if(e.code==='KeyV')return false;
 if(e.code==='KeyC'&&(e.shiftKey||term.hasSelection())){e.preventDefault();if(term.hasSelection())copySelection();return false;}
 return true;
}
async function pasteAttachment(session,image) {
 try{
  const clip=await api('clipboard');
  if(clip.files.length)return consoleInsert(session,quotePaths(clip.files));
  if(image){
   const data=await new Promise((ok,fail)=>{const r=new FileReader();r.onload=()=>ok(String(r.result).split(',')[1]);r.onerror=fail;r.readAsDataURL(image);});
   return consoleInsert(session,quotePaths([await api('console_paste_image',{session,data})]));
  }
  if(clip.text)return consoleInsert(session,clip.text);
  toast('붙여넣을 글자·파일·이미지가 없어요.');
 }catch(err){toast(err.message);}
}
consoleBox.addEventListener('paste',e=>{
 const d=e.clipboardData;if(!d||d.getData('text/plain'))return;
 e.preventDefault();e.stopImmediatePropagation();
 pasteAttachment(consoleSession,[...d.items].find(i=>i.type==='image/png')?.getAsFile());
},true);
consoleBox.addEventListener('contextmenu',e=>{e.preventDefault();if(term.hasSelection())copySelection();else pasteAttachment(consoleSession);});
let consoleDrop=null;
consoleBox.addEventListener('drop',()=>{consoleDrop={session:consoleSession,at:Date.now()};});
// 녹화 (2026-10-27) — 끝내면 단계를 거르고 [이걸로 넘기기] → 고치는 룬의 recordings/<이름>.json,
// 새 룬을 만드는 콘솔이면 제작 폴더의 _recordings/<이름>.json (AI 가 룬을 만들며 옮긴다, 2026-10-28).
function recordOpen() {
 const p=state.projects.find(x=>x.id===consoleRune),have=p?.recordings||[];
 const d=modal('<form id="recordForm">'+heading('녹화')+'<p class="dialog-copy">시작하면 VaderAI 창이 내려가고 <b>녹화 중</b> 표시가 위에 떠요. 대상 프로그램에서 평소대로 누른 뒤 <b>끝내기</b>를 누르세요. 클릭과 이동 키(방향키·Tab·Enter 등)만 담고 <b>글자는 담지 않아요</b>.</p><div class="settings-field"><label for="recordName">녹화 이름</label><input id="recordName" list="recordNames" autocomplete="off" required pattern="[a-z][a-z0-9_]{0,40}" value="'+esc(have.includes('navigation')?'navigation':have[0]||'navigation')+'"><datalist id="recordNames">'+have.map(n=>'<option value="'+esc(n)+'">').join('')+'</datalist><small class="muted">룬 코드의 <code>ctx.replay("이름")</code> 과 같아야 쓰여요. 있는 이름을 고르면 넘길 때 바꿔 써요.</small></div><p id="dialogError" role="alert"></p><div class="actions"><button type="submit" class="button primary">'+icon('record')+'녹화 시작</button>'+button('closeDialog','취소','stop','ghost')+'</div></form>');
 $('#recordForm').onsubmit=async e=>{e.preventDefault();
  try{await api('record_start',{session:consoleSession,name:$('#recordName').value.trim()});d.close();}catch(err){dialogError(err.message);}};
}
let recordSteps=[];
async function recordingEnded() {
 let r;try{r=await api('record_stop');}catch(err){toast(err.message);return;}
 recordSteps=r.steps;
 if(!r.steps.length){modal(heading('녹화 · '+r.name)+'<p class="dialog-copy">잡힌 단계가 없어요. 대상 프로그램 창을 눌렀는지 확인하고 다시 해 보세요.</p><div class="actions">'+button('recordAgain','다시 녹화','record','primary')+button('recordDiscard','버리기','stop','ghost')+'</div>');$('#closeDialog').onclick=recordDiscard;return;}
 const d=modal(heading('녹화 · '+r.name+' · '+r.steps.length+'단계')+'<p class="dialog-copy">필요 없는 단계는 체크를 끄세요. 넘기면 아래 파일에 '+(r.exists?'<b>있던 녹화를 바꿔 써요</b>':'저장돼요')+'.</p><ol class="record-steps">'+r.steps.map((s,i)=>'<li><label class="check"><input type="checkbox" data-step="'+i+'" checked><span>'+esc(s.label)+(s.findable?'':' <small class="ai-bad">좌표로만 찾음 — 창 크기가 바뀌면 깨져요</small>')+'</span></label></li>').join('')+'</ol><p class="small muted record-path">'+esc(r.path)+'</p><p class="small muted">창 제목·메뉴 이름이 룬 파일에 들어가요. 룬을 커밋하거나 공유하면 함께 나가요.</p><p id="dialogError" role="alert"></p><div class="actions">'+button('recordSave','이걸로 넘기기','confirm','primary')+button('recordAgain','다시 녹화','record','ghost')+button('recordDiscard','버리기','stop','ghost')+'</div>',true);
 d.oncancel=e=>e.preventDefault();$('#closeDialog').onclick=recordDiscard;   // 고르지 않고 닫으면 녹화가 붙잡힌 채 남는다 — 닫기는 버리기
}
async function recordSave() {
 const keep=[...document.querySelectorAll('[data-step]')].filter(x=>x.checked).map(x=>Number(x.dataset.step));
 let r;try{r=await api('record_save',{keep});}catch(err){dialogError(err.message);return;}
 try{catalog(await api('catalog'));}catch(e){}
 const note=r.rune_id?'녹화 recordings/'+r.name+'.json ('+r.count+'단계)을 넘겼어. 읽고 셀렉터와 stop(인계 지점)을 맞춰줘.'
  :'녹화 '+r.path+' ('+r.count+'단계)을 만들었어. 룬을 만들 때 그 룬 폴더의 recordings/'+r.name+'.json 으로 옮기고, 셀렉터와 stop(인계 지점)을 맞춰줘.';
 modal(heading('넘겼어요')+'<p class="dialog-copy">'+r.count+'단계를 '+(r.rune_id?'이 룬의 녹화로 저장했어요.':'제작 폴더에 잠시 맡겨 뒀어요. <b>콘솔에 넣기</b>를 누르면 AI 가 만들 룬에 옮겨 넣어요.')+' 화면에서 실제로 맞게 누르는지는 아직 확인되지 않았어요 — 고친 뒤 연습 실행으로 확인하세요.</p><p class="small muted record-path">'+esc(r.path)+'</p><div class="actions"><button type="button" class="button primary" data-action="recordTell" data-session="'+esc(r.session)+'" data-note="'+esc(note)+'">'+icon('input')+'콘솔에 넣기</button>'+button('closeDialog','닫기','stop','ghost')+'</div>');
}
function recordTell(a) {
 if(a.dataset.session!==consoleSession){toast('녹화를 시작한 콘솔이 닫혔어요. 파일은 저장돼 있어요.');return;}
 $('#settingsDialog').close();consoleInsert(a.dataset.session,a.dataset.note);
}
async function recordDiscard() {try{await api('record_discard');}catch(e){}$('#settingsDialog').close();toast('녹화를 버렸어요.');}
async function recordAgain() {try{await api('record_discard');}catch(e){}recordOpen();}
async function attachFile() {const session=consoleSession;try{const p=await api('pick');if(p.length)consoleInsert(session,quotePaths(p));}catch(err){toast(err.message);}}
function fitConsole() {if(term&&consoleBox.isConnected&&consoleBox.offsetWidth>0&&consoleBox.offsetHeight>0)fitter.fit();}
function mountConsole(focus) {
 const slot=$('[data-console-mount]',main);
 if(!slot){consoleBox.remove();return;}
 slot.appendChild(consoleBox);fitConsole();term?.refresh(0,term.rows-1);if(focus)term?.focus();
}
// 입력은 순서대로 한 통로로 — 호출마다 다른 스레드에서 돌아 순서가 뒤바뀔 수 있다. 보내는 동안 온 것은 모아서 한 번에.
async function flushInput() {
 writingInput=true;
 while(pendingInput&&consoleSession){const data=pendingInput;pendingInput='';
  try{await api('console_write',{session:consoleSession,data});}catch(err){pendingInput='';toast(err.message);}}
 writingInput=false;
}
async function startConsole(c,runeId) {
 if(consoleSession){toast('이미 열린 콘솔이 있어요. 닫은 뒤 다시 여세요.');navigate('workshop');return;}
 ensureTerminal();consoleClient=c.id;consoleRune=runeId||null;consoleName=c.name||c.id;consoleSession='starting';navigate('workshop');
 const dims=fitter.proposeDimensions()||{cols:100,rows:30};
 try{const s=await api('console_start',{client:c.id,cols:Math.max(10,dims.cols),rows:Math.max(4,dims.rows),rune_id:runeId||null});consoleSession=s.session;}
 catch(err){consoleSession=null;render();toast(err.message);return;}
 term.reset();render();term.focus();readConsole(consoleSession);
}
// 화면이 다 그린 뒤에 다음 조각을 가져간다 — 출력이 화면보다 빠르면 코어 쪽이 기다린다.
async function readConsole(session) {
 while(consoleSession===session){
  let r;try{r=await api('console_read',{session});}catch(err){break;}
  if(r.data)await new Promise(done=>term.write(r.data,done));
  if(!r.alive)break;
 }
 if(consoleSession===session){consoleSession=null;render();toast(consoleName+' 를 닫았어요.');}
}
async function stopConsole() {
 const session=consoleSession;if(!session||session==='starting')return;
 toast('닫는 중이에요…');
 try{await api('console_stop',{session});}catch(err){toast(err.message);}
}
// 콘솔에서 일하는 동안에도 시험 요청이 보이게 — 바뀐 것이 있을 때만 다시 그린다.
setInterval(async()=>{
 if(state.page!=='workshop'||!state.connected||$('#settingsDialog').open)return;
 try{const data=await api('catalog');const key=d=>JSON.stringify([d.projects,d.test_requests]);
  if(key(data)!==key({projects:state.projects,test_requests:state.testRequests})){catalog(data);render();}}catch(e){}
},3000);
async function devMode() {
 const on=!state.devMode;catalog(await api('set_dev_mode',{on}));render();
 toast(on?'개발 모드를 켰어요. 보안 격리는 아니니 직접 만드는 룬만 시험하세요.':'개발 모드를 껐어요.');
}
// 설치는 두 걸음: 고르면 코드 요약을 보이고, 사람이 확인한 그 지문만 설치한다 (2026-10-14 합의).
let pendingInstall=null;
async function installRune() {
 const shown=await api('install');if(shown)showInstall(shown);   // 비면 고르기를 취소했다
}
async function editRune() {
 const id=state.task;if(!id)return;
 try{catalog(await api('edit_rune',{rune_id:id}));}catch(err){toast(err.message);return;}
 state.project=id;navigate('workshop');
}
async function linkSource(a) {try{const d=await api('link_source',{rune_id:a.dataset.id});if(d){catalog(d);render();toast('원본 폴더를 연결했어요. 이제 그 폴더를 고쳐요.');}}catch(err){toast(err.message);}}
async function importRecording(a) {try{catalog(await api('import_recording',{rune_id:a.dataset.id,name:a.dataset.name}));render();toast('녹화를 룬 안으로 가져왔어요. 원래 파일은 그대로 있어요.');}catch(err){toast(err.message);}}
function editWith(a) {const c=state.clients.find(x=>x.id===a.dataset.client);if(c)startConsole(c,a.dataset.id);}
async function installProject(a) {
 try{showInstall(await api('preview_project',{rune_id:a.dataset.id}));}catch(err){toast(err.message);}
}
function showInstall(shown) {
 pendingInstall=shown;
 modal(heading(T('{0} 설치',VaderI18n.runeWith(shown.i18n_en,shown.name||shown.id)+(shown.version?' v'+shown.version:''))).replace('<h2','<h2 data-raw')+'<p class="dialog-copy">설치하면 이 PC 에서 이 룬의 코드가 돌 수 있어요. 아래 내용이 맞는지 보고 설치하세요.</p>'+signatureView(shown)+codeSummary(shown)+'<p id="dialogError" role="alert"></p><div class="actions">'+button('confirmInstall','설치','confirm','primary')+button('closeDialog','취소','stop','ghost')+'</div>',true);
}
async function confirmInstall() {
 const shown=pendingInstall;if(!shown)return;
 let data;
 try{data=await api('install_confirm',{source:shown.path,expect:shown.fingerprint,trust_signer:!!$('#trustSigner')?.checked});}catch(err){dialogError(err.message);return;}
 pendingInstall=null;$('#settingsDialog').close();
 catalog(data);const done=data.installed;state.task=done.id;navigate('runes');
 toast(done.change==='same_version'?done.name+' 코드가 바뀌었는데 버전이 v'+done.version+' 그대로예요. 룬의 version 을 올려 주세요.'
  :done.name+' v'+done.version+(done.change==='new'?' 설치':done.change==='reinstall'?' 다시 설치':done.change==='upgrade'?'로 올림':'로 내림')+'했어요.'+(done.granted.length?' 권한: '+done.granted.join(', '):''));
 if(done.missing.length)openSettings();                      // 빠진 설정은 지금 받는다 — 실행하다 막히는 것보다 낫다
}
// ── 관리 화면에서 옮겨 온 것: 실사용 승인(코어 용어로는 봉인) · 제거 · 매핑 관리 · AI 배정 ──
// 할 일: 평소에는 목록만, AI 전송은 별도 정리 서랍에서 사용자가 요청할 때만.
// 미루기는 '언제 다시 알릴지' 를 골라야 끝난다. 지우기는 한 번 더 묻는다.
const ymd=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
const at9=(d,days)=>{const x=new Date(d);x.setDate(x.getDate()+days);x.setHours(9,0,0,0);return x;};
const local=d=>ymd(d)+'T'+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
const md=s=>s.slice(5,10).replace('-','/');
async function loadTodos(quiet) {
 // 메인 그래프에도 할 일이 서므로 늘 읽는다. 날짜가 바뀌어도 '오늘/다음' 을 다시 나눈다
 try{const cs=await api('todo_list'),day=ymd(new Date()),next=JSON.stringify(cs)+day;state.todoError='';
  if(state.todoAiTarget===undefined){state.todoAiTarget='';api('todo_ai_target').then(t=>{state.todoAiTarget=t?.label||'';if(state.page==='todo')render();}).catch(()=>{});}   // 보내는 곳 — 보내기 전에 보이게
  const links=await api('todo_runes').catch(()=>state.todoRunes||{}),lk=JSON.stringify(links);if(lk!==loadTodos.links){loadTodos.links=lk;state.todoRunes=links;if(state.page==='todo')render();}
  if(next!==loadTodos.last){loadTodos.last=next;state.todos=cs;todoBadge();if((state.page==='todo'&&!dragCard)||state.page==='core')render();}}   // 끄는 중엔 다시 그리지 않는다
 catch(e){state.todoError=e.message;if(state.page==='todo')render();}
}
// 광장에 더 새 판이 있는 룬 — [내 룬] 에 점 (코어가 받아 둔 목록만 본다, 통신 없음)
const updateOf=id=>state.updates&&Object.hasOwn(state.updates,id)?state.updates[id]:null;   // 'constructor' 같은 룬 id 가 상속 값을 읽지 않게
function runesBadge() {const a=$('.rail a[data-page="runes"]');if(!a)return;const n=Object.keys(state.updates||{}).length;if(n)a.dataset.badge=T('업데이트 {0}',n);else delete a.dataset.badge;}
function todoBadge() {const a=$('.rail a[data-page="todo"]');if(!a)return;const n=(state.todos||[]).filter(c=>c.ringing).length;if(n)a.dataset.badge='다시 알림 '+n;else delete a.dataset.badge;}
// 멤버 공유 (2026-11-18) — 친구 이름은 메신저가 알려 준 목록(state.friends)에서. 모르는 사람은 '친구'.
const handleOf=id=>{const f=(state.friends||[]).find(x=>x.account_id===id)||(state.msgRooms||[]).flatMap(r=>r.members).find(x=>x.account_id===id);return f?.handle?'@'+f.handle:'친구';};
const roomName=id=>(state.msgRooms||[]).find(r=>r.id===id)?.name||'방';
const shareTargets=()=>{const m=state.todoMember||'';if(m.startsWith('g:'))return (state.friends||[]).filter(f=>f.state==='accepted'&&f.verified&&f.group===m.slice(2)).map(f=>f.account_id);return m.startsWith('f:')?[m.slice(2)]:[];};
function todoShareBits(c) {
 const sent=c.shared_with||[];
 const who=c.owner?'<span class="todo-shared from">'+esc(handleOf(c.owner))+' 님이 공유'+(c.share_room?' · <span data-raw>'+esc(roomName(c.share_room))+'</span>':'')+'</span>'
   +(c.share_req?'<span class="todo-shared">'+T(c.share_req==='done'?'끝냄 요청함 · 주인 확인 기다리는 중':'되돌림 요청함 · 주인 확인 기다리는 중')+'</span>':'')
  :c.share_room?'<span class="todo-shared">'+T('방 공유')+' · <span data-raw>'+esc(roomName(c.share_room))+'</span></span>'
  :sent.length?'<span class="todo-shared">공유 · '+esc(handleOf(sent[0]))+(sent.length>1?' 외 '+(sent.length-1):'')+'</span>':'';
 // 방에 공유 (2026-11-26) — 카드 주인이 판정. 1:1 공유와 섞지 않는다
 if((state.todoMember||'').startsWith('r:')){const rid=state.todoMember.slice(2),name=roomName(rid);
  const rb=c.owner||c.state!=='open'?''
   :c.share_room===rid?'<button type="button" class="button ghost todo-share shared" data-action="todoUnshareRoom" data-id="'+c.id+'" aria-pressed="true" title="다시 누르면 공유를 취소해요">'+icon('confirm')+'<span>공유함</span></button>'
   :c.share_id?'<span class="todo-shared done-mark">'+T('다른 곳에 공유됨')+'</span>'
   :'<button type="button" class="button ghost todo-share" data-action="todoShareRoom" data-id="'+c.id+'" data-room="'+esc(rid)+'" title="한 번 누르면 이 방에 공유">'+icon('send')+'<span data-raw>'+esc(name)+'</span></button>';
  return [who,rb];}
 const to=shareTargets(),rest=to.filter(a=>!sent.includes(a));
 const name=todoMemberName().replace(/ 그룹$/,'');
 const btn=!state.todoMember||c.owner||c.state!=='open'?''
  :rest.length?'<button type="button" class="button ghost todo-share" data-action="todoShare" data-id="'+c.id+'" aria-label="'+esc(c.title)+' 를 '+esc(name)+'에 공유" title="한 번 누르면 '+esc(name)+'에 공유">'+icon('send')+'<span>'+esc(name)+'</span></button>'
  :to.length?'<button type="button" class="button ghost todo-share shared" data-action="todoUnshare" data-id="'+c.id+'" aria-pressed="true" aria-label="'+esc(c.title)+' 를 '+esc(name)+'에 공유함. 누르면 공유 취소" title="다시 누르면 공유를 취소해요">'+icon('confirm')+'<span>공유함</span></button>'
  :'<span class="todo-shared done-mark" title="이 그룹에 안전 번호를 확인한 친구가 없어요. 메신저에서 친구를 넣고 확인하세요.">보낼 친구 없음</span>';
 return [who,btn];
}
function todoCard(c) {
 const today=ymd(new Date()),late=c.state==='open'&&c.due<today,[who,shareBtn]=todoShareBits(c);
 const meta=c.state==='done'?(c.done_at?esc(md(c.done_at))+' '+T('완료'):c.imported?T('가져온 기록'):''):c.ringing?'<span class="todo-ring">다시 알림'+(c.remind_at?' · '+c.remind_at.slice(11,16):'')+'</span>':late?'<span class="todo-late">'+md(c.due)+' 예정</span>':c.remind_at?esc((c.due>today?md(c.remind_at)+' ':'')+c.remind_at.slice(11,16)+' 알림'):c.due>today?md(c.due):'';
 return '<li class="todo-card'+(c.state==='done'?' done':'')+'"'+(c.state==='open'?' data-card="'+c.id+'"':'')+'>'
  +(c.state==='open'?'<span class="todo-grip" aria-hidden="true">'+icon('grip')+'</span>':'')
  +'<button id="todo-check-'+c.id+'" type="button" class="todo-check" role="checkbox" aria-checked="'+(c.state==='done')+'" aria-label="'+esc(c.title+' · '+T(c.state==='done'?'완료 취소':'완료'))+'" data-raw title="완료 전환 · Alt+↑/↓로 순서 이동" data-action="todoDone" data-id="'+c.id+'"><span>'+(c.state==='done'?icon('confirm'):'')+'</span></button>'
  +'<div class="todo-copy"><button type="button" class="todo-open" data-action="noteOpen" data-id="'+c.id+'" aria-expanded="'+(state.note?.card===c.id)+'" aria-controls="todoNote" title="세부 내용 (카드를 눌러도 열려요)">'+raw(c.title)+'</button>'+(who?'<small>'+who+'</small>':'')+'</div><small class="todo-meta">'+meta+'</small>'+todoRuneChip(c)+shareBtn
  +'<details class="todo-menu"><summary id="todo-more-'+c.id+'" aria-label="'+esc(T('{0} 더보기',c.title))+'" data-raw>'+todoMoreIcon()+'</summary><div class="todo-menu-items">'
  +(c.state==='open'&&!c.owner?'<button type="button" data-action="todoSnooze" data-id="'+c.id+'">미루기</button>':'')
  +'<button type="button" data-action="todoRunePick" data-id="'+c.id+'">'+T(state.todoRunes?.[c.id]?'룬 바꾸기':'룬 연결')+'</button>'
  +'<button type="button" data-action="todoDelete" data-id="'+c.id+'">'+(c.owner?'내 목록에서 빼기':'지우기')+'</button></div></details>'
  +(state.note?.card===c.id?postitView(state.note):'')+'</li>';
}
// 카드 ↔ 룬 (2026-11-27) — 사람이 고른 설치된 룬 하나를 연다. 이 PC 에만(폰·공유·AI 로 안 간다). 연 뒤 실행은 룬 화면에서 사람이.
// 룬이 지워졌으면 이름이 같은 다른 룬을 열지 않고 '룬 없음 · 연결 풀기' (codex1 next3 ③).
function todoRuneChip(c) {
 const id=state.todoRunes?.[c.id];if(!id)return '';
 const r=taskRunes[id];
 return r?'<button type="button" class="button ghost todo-rune" data-action="todoRuneOpen" data-id="'+c.id+'" data-rune="'+esc(id)+'" title="'+esc(T('{0} 룬 화면 열기',r.name))+'">'+icon('arrow')+'<span data-raw>'+esc(r.name)+'</span></button>'
  :'<span class="todo-shared done-mark">'+T('룬 없음')+' · <button type="button" class="text-button" data-action="todoRuneUnlink" data-id="'+c.id+'">'+T('연결 풀기')+'</button></span>';
}
async function todoRuneSet(id,rune) {
 try{state.todoRunes=await api('todo_link_rune',{card_id:id,rune_id:rune});loadTodos.links=JSON.stringify(state.todoRunes);render();$('#todo-more-'+id)?.focus();}
 catch(e){toast(e.message);}
}
function todoRunePick(a) {
 const id=a.dataset.id,c=(state.todos||[]).find(x=>x.id===id);if(!c)return;
 const now=state.todoRunes?.[id],ids=Object.keys(taskRunes);
 const d=modal(heading('룬 연결')+'<p class="dialog-copy"><b>'+raw(c.title)+'</b> '+T('에서 열 룬을 골라요. 누르면 그 룬 화면이 열리고, 실행은 거기서 직접 해요. 이 PC 에만 남아요.')+'</p>'
  +(ids.length?'<div class="todo-rune-list">'+ids.map(r=>'<button type="button" class="button '+(r===now?'primary':'ghost')+'" data-pick="'+esc(r)+'"'+(r===now?' aria-pressed="true"':'')+'>'+sigil(r)+'<span data-raw>'+esc(taskRunes[r].name)+'</span></button>').join('')+'</div>':'<p class="muted">'+T('설치된 룬이 없어요.')+'</p>')
  +'<div class="actions">'+(now?'<button type="button" class="button ghost" data-pick="">'+T('연결 풀기')+'</button>':'')+'</div>');
 d.querySelectorAll('[data-pick]').forEach(b=>b.onclick=()=>{d.close();todoRuneSet(id,b.dataset.pick);});
}
// 멤버 고르기 — 고른 동안만 카드마다 [공유]. 안전 번호를 확인한 친구와 그 친구들이 있는 그룹만 (메신저와 같은 조건).
function todoMemberName(){const m=state.todoMember||'';if(m.startsWith('r:'))return roomName(m.slice(2));if(m.startsWith('g:'))return (state.todoGroups||[]).find(g=>g.id===m.slice(2))?.name+' 그룹';return handleOf(m.slice(2));}
// 탭 줄 오른쪽 끝의 그룹 고르기 (마스터 2026-11-19) — 메신저에서 만든 그룹이 늘 보인다. 고르면 카드마다 [그룹명에 공유] 한 번 누르기.
// 고른 그룹은 이 PC 화면에 기억한다(다음에 켜도 그대로). 친구 한 명에게만 보낼 때는 아래 '친구' 묶음에서.
function todoMemberPick() {
 const fs=(state.friends||[]).filter(f=>f.state==='accepted'&&f.verified),gs=state.todoGroups||[],rs=state.msgRooms||[];
 const opt=(v,t,mine)=>'<option value="'+esc(v)+'"'+(mine?' data-raw':'')+(state.todoMember===v?' selected':'')+'>'+esc(t)+'</option>';
 const live=g=>fs.filter(f=>f.group===g.id).length;
 return '<label class="todo-member"><span class="sr-only">공유할 그룹</span><select id="todoMember" class="select"'+(gs.length||fs.length||rs.length?'':' disabled')+'>'
  +'<option value="">'+(gs.length?'그룹 선택':'그룹 없음 · 메신저에서 만들어요')+'</option>'
  +gs.map(g=>opt('g:'+g.id,g.name+(live(g)?' · '+T('{0}명',live(g)):' '+T('· 대화 가능한 친구 없음')),true)).join('')
  +(rs.length?'<optgroup label="방">'+rs.map(r=>opt('r:'+r.id,r.name,true)).join('')+'</optgroup>':'')
  +(fs.length?'<optgroup label="친구 한 명에게">'+fs.map(f=>opt('f:'+f.account_id,'@'+f.handle)).join('')+'</optgroup>':'')+'</select></label>';
}
try{state.todoMember=localStorage.getItem('vader.todoMember')||'';}catch{}
document.addEventListener('change',e=>{if(e.target.id==='todoMember'){state.todoMember=e.target.value;try{localStorage.setItem('vader.todoMember',state.todoMember);}catch{}render();$('#todoMember')?.focus();}});
function todoMoreIcon(){return '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg>';}
function todoView() {
 const cs=state.todos,now=new Date(),today=ymd(now),due=state.todoDraft?.due||today,tab=state.todoTab||'today';
 if(cs==null)return '<section class="scene todo"><h1>할 일</h1>'+(state.todoError?'<p class="ai-bad">'+esc(state.todoError)+'</p>':'<p class="muted">불러오는 중…</p>')+'</section>';
 const open=cs.filter(c=>c.state==='open'),done=cs.filter(c=>c.state==='done'),groups={today:open.filter(c=>c.due<=today||c.ringing),next:open.filter(c=>c.due>today&&!c.ringing),all:open},shown=groups[tab]||groups.today;
 const form='<form class="todo-add" id="todoAdd">'+icon('plus')+'<label class="sr-only" for="todoTitle">할 일</label><input id="todoTitle" maxlength="500" placeholder="할 일을 적고 Enter" autocomplete="off" value="'+esc(state.todoDraft?.title||'')+'"><label class="todo-date"><span id="todoDateLabel">'+(due===today?'오늘':md(due))+'</span><input id="todoDue" aria-label="할 일 날짜" type="date" required value="'+esc(due)+'"></label><button type="submit" class="button primary">추가</button><button type="button" class="button ghost todo-ask" data-action="todoAsk"'+(todoAsk.busy?' disabled':'')+'>'+icon('chat')+'<span>'+(todoAsk.busy?'묻는 중…':'비서에게 묻기')+'</span></button>'
  +(Object.keys(taskRunes).length?'<button type="button" class="button ghost todo-ask" data-action="todoAsk" data-runes="1"'+(todoAsk.busy?' disabled':'')+' title="'+esc(T('설치된 룬 이름을 {0} 로 보내 카드에 맞는 룬을 추천받아요',state.todoAiTarget||T('연결된 AI')))+'">'+icon('match')+'<span>'+T('룬 추천')+'</span></button>':'')+'</form>';
 return '<section class="scene todo"><div class="todo-heading"><div><h1>할 일</h1><p class="todo-today">'+now.toLocaleDateString(VaderI18n.lang==='en'?'en-US':'ko-KR',{month:'long',day:'numeric',weekday:'long'})+'</p></div><div class="todo-tools"><details class="todo-menu"><summary aria-label="할 일 화면 더보기">'+todoMoreIcon()+'</summary><div class="todo-menu-items"><button type="button" data-action="phoneOpen">폰에서 보기</button><button type="button" data-action="todoPrivacy">저장·전송 안내</button></div></details></div></div>'
  +'<div class="todo-bar"><nav class="todo-tabs" aria-label="할 일 목록">'+[['today','오늘'],['next','예정'],['all','전체'],['archive','완료 기록']].map(([k,label])=>'<button id="todo-tab-'+k+'" type="button" data-action="todoTab" data-tab="'+k+'" aria-pressed="'+(tab===k)+'">'+label+(groups[k]?' <span>'+groups[k].length+'</span>':'')+'</button>').join('')+'</nav>'+todoMemberPick()+'</div>'
  +(state.todoError?'<p class="ai-bad" role="alert">'+esc(state.todoError)+'</p>':'')
  +(tab==='archive'?archiveView():(shown.length?'<ul class="todo-list" aria-label="'+T('{0} 할 일',T({today:'오늘',next:'예정',all:'전체'}[tab]))+'">'+shown.map(todoCard).join('')+'</ul>':'<p class="muted todo-empty">'+(tab==='next'?'예정된 할 일이 없어요.':open.length?'오늘 할 일을 모두 마쳤어요.':'할 일이 없어요. 위 칸에 적고 Enter 를 누르세요.')+'</p>')
  +'<details id="todoCompleted" class="todo-completed"'+(state.todoCompleted?' open':'')+'><summary><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg>오늘 완료 <span>'+done.length+'</span></summary>'+(done.length?'<ul class="todo-list">'+done.map(todoCard).join('')+'</ul>':'<p class="muted">아직 완료한 일이 없어요.</p>')+'</details>')
  +todoFeedView()+form+'<p class="muted todo-note">'+T('Enter 는 이 PC 에만 추가해요. [비서에게 묻기]를 누르면 적은 말, 열린 할 일(제목·날짜·알림 시각), 메신저에서 켠 대화의 내가 보낸 글을 {0} 로 보내요. [룬 추천]은 설치된 룬 이름도 함께 보내요. 받은 글과 끝낸 기록은 보내지 않아요.','<b data-raw>'+esc(state.todoAiTarget||T('연결된 AI'))+'</b>')+'</p></section>';
}
// 세부 포스트잇 (마스터 요청 2026-11-18) — 카드 제목을 누르면 열린다. 적는 칸 + 하위 항목. 항목을 누르면 그 안으로(드릴다운),
// 위 경로를 누르면 밖으로(드릴업). 3단까지. 적은 내용은 손을 멈추면 저장한다 — 다시 그리지 않아 적던 자리가 그대로다.
function postitView(n) {
 const crumbs=n.path.map((p,i)=>i===n.path.length-1?'<li aria-current="page">'+raw(p.title)+'</li>':'<li><button type="button" class="text-button" data-action="noteGo" data-node="'+esc(p.node)+'">'+raw(p.title)+'</button></li>').join('');
 const kids=n.children.map(k=>'<li class="postit-item'+(k.done?' done':'')+'"><button type="button" class="todo-check" role="checkbox" aria-checked="'+k.done+'" aria-label="'+esc(k.title+' · '+T(k.done?'완료 취소':'완료'))+'" data-raw data-action="noteToggle" data-item="'+esc(k.id)+'"><span>'+(k.done?icon('confirm'):'')+'</span></button>'
  +'<button type="button" class="postit-open" data-action="noteGo" data-node="'+esc(k.id)+'">'+raw(k.title)+''+(k.count?'<small>'+k.count+'</small>':'')+'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 5 7 7-7 7"/></svg></button>'
  +'<button type="button" class="text-button postit-del" data-action="noteDelete" data-item="'+esc(k.id)+'" aria-label="'+esc(T('{0} 지우기',k.title))+'" data-raw>'+T('지우기')+'</button></li>').join('');
 return '<div id="todoNote" class="postit-fold'+(noteMotion.fresh?'':' open')+'"><div class="postit-clip"><section class="postit'+(noteMotion.drill?' drill-'+noteMotion.drill:'')+'" aria-labelledby="noteTitle"><div class="postit-head">'
  +(n.depth?'<button type="button" class="icon-button" data-action="noteGo" data-node="'+esc(n.path[n.path.length-2].node)+'" data-dir="out" aria-label="한 단계 밖으로">'+icon('back')+'</button>':'')
  +'<h2 id="noteTitle" tabindex="-1">'+(n.depth?esc(n.path[n.path.length-1].title):'세부 내용')+'</h2><button type="button" class="icon-button" data-action="noteClose" aria-label="세부 내용 접기">'+icon('close')+'</button></div>'
  +(n.depth?'<nav aria-label="포스트잇 경로"><ol class="postit-path">'+crumbs+'</ol></nav>':'')
  +'<label class="sr-only" for="noteBody">세부 내용</label><textarea id="noteBody" maxlength="4000" placeholder="세부 내용을 적어요. 손을 멈추면 저장돼요.">'+esc(n.body)+'</textarea><p id="noteSaved" class="postit-saved muted" role="status"></p>'
  +'<h3 class="postit-sub">하위 항목'+(n.children.length?' <span>'+n.children.length+'</span>':'')+'</h3>'
  +(kids?'<ul class="postit-list">'+kids+'</ul>':'<p class="muted postit-empty">'+(n.can_open?'아래에 적으면 이 안에 항목이 생겨요. 항목을 누르면 그 안으로 들어가요.':'')+'</p>')
  +(n.can_open?'<form id="noteAdd" class="todo-add postit-add">'+icon('plus')+'<label class="sr-only" for="noteItem">하위 항목</label><input id="noteItem" maxlength="200" autocomplete="off" placeholder="하위 항목을 적고 Enter"><button type="submit" class="button ghost">추가</button></form>'
   :'<p class="muted postit-empty">여기가 가장 안쪽이에요(3단).</p>')+'</section></div></div>';
}
// 펼치기·접기·드릴 움직임 — 처음 열 때는 접힌 채 그린 뒤 다음 프레임에 펼친다(높이 0→내용), 드릴은 안 내용만 옆으로 미끄러진다.
const noteMotion={fresh:false,drill:''};
async function noteLoad(card,node,focus,how='open') {
 try{const got=await api('todo_note',{card_id:card,node:node||''});
  noteMotion.fresh=how==='open'&&!state.motion;noteMotion.drill=how==='open'?'':how;
  state.note=got;state.noteArmed=null;render();noteMotion.fresh=false;noteMotion.drill='';
  const fold=$('#todoNote');if(fold&&!fold.classList.contains('open'))requestAnimationFrame(()=>requestAnimationFrame(()=>fold.classList.add('open')));
  ($(focus||'#noteTitle'))?.focus({preventScroll:how!=='open'});
  if(how==='open')setTimeout(()=>fold?.scrollIntoView({block:'nearest',behavior:state.motion?'auto':'smooth'}),state.motion?0:200);}
 catch(e){toast(e.message);}
}
// 접기 — 높이를 줄인 뒤에 지운다. 움직임 끄기면 바로.
async function noteFold() {
 await noteSave();const id=state.note?.card,fold=$('#todoNote');
 const done=()=>{if(state.note?.card!==id)return;state.note=null;render();$('[data-action="noteOpen"][data-id="'+id+'"]')?.focus();};
 if(!fold||state.motion)return done();
 fold.classList.remove('open');fold.addEventListener('transitionend',done,{once:true});setTimeout(done,450);
}
// 카드 아무 곳이나 눌러도 펼친다 — 버튼·메뉴·입력칸·펼친 포스트잇 안, 끌기 뒤의 눌림은 빼고.
document.addEventListener('click',e=>{
 const li=e.target.closest?.('.todo-card');if(!li||dragMoved||e.target.closest('button,summary,a,input,textarea,label,.todo-menu,.postit-fold'))return;
 const b=$('.todo-open',li);if(b)b.click();
});
let noteTimer=0;
async function noteSave() {
 clearTimeout(noteTimer);const n=state.note,t=$('#noteBody');if(!n||!t||t.value===n.body)return;
 const body=t.value,s=$('#noteSaved');if(s)s.textContent='저장하는 중…';
 try{const got=await api('todo_note_body',{card_id:n.card,node:n.node,body});if(state.note&&state.note.node===n.node)state.note.body=got.body;if($('#noteSaved'))$('#noteSaved').textContent='저장했어요';}
 catch(e){if($('#noteSaved'))$('#noteSaved').textContent=e.message;}
}
document.addEventListener('input',e=>{if(e.target.id==='noteBody'){clearTimeout(noteTimer);noteTimer=setTimeout(noteSave,700);}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&state.note&&e.target.closest?.('#todoNote')){e.preventDefault();noteFold();}});
// 말로 정리 (2026-11-11 마스터 결정) — AI 는 제안만, [적용] 을 눌러야 바뀐다. 보내는 범위는 화면에 그대로 적는다.
const opWord=c=>c.op==='add'?T('추가')+' · '+raw(c.title)+' · '+md(c.due):c.op==='done'?T('끝냄')+' · '+raw(c.title):c.op==='order'?T('순서 바꾸기')+' · '+c.titles.slice(0,5).map(raw).join(' → ')+(c.titles.length>5?' …':''):c.op==='link'?T('룬 연결')+' · '+raw(c.title)+' → '+raw(c.rune_name):T('미루기')+' · '+raw(c.title)+' → '+md(c.until)+' '+c.until.slice(11,16);
// 비서 (2026-11-26 마스터 결정 — 비서 정리 + 지난 일 묻기를 한 입력창에서). 이 화면을 연 동안만 남는다(잠그면 문으로 가며 사라진다).
// 제안은 버튼 하나에 하나 — 누른 것만 적용, 처음엔 아무것도 안 바뀐다 (codex1 round6 P1-3·비서 회신 2). 지난 기록은 이 PC 가 찾은 그대로.
const suggestWord=c=>c.request?T('주인에게 끝냄 요청')+' · '+raw(c.title):opWord(c);
function lookupView(l) {
 if(!l)return '';
 if(l.error)return '<p class="muted">'+esc(l.error)+'</p>';
 const when=l.start+' ~ '+l.end+(l.query?' · “<span data-raw>'+esc(l.query)+'</span>”':'');
 if(l.limited)return '<p class="muted">'+when+' · '+T('기록이 너무 많아 찾지 못했어요. 기간을 좁혀 다시 물어봐 주세요.')+'</p>';
 if(!l.total)return '<p class="muted">'+when+' · '+T('끝낸 기록이 없어요. 기간이나 낱말을 바꿔 물어봐 주세요.')+'</p>';
 return '<div class="todo-found"><p>'+T('끝낸 기록')+' · '+when+' · <b>'+T('{0}건',l.total)+'</b></p><ul>'
  +l.items.map(i=>'<li><time datetime="'+esc(i.done_at)+'">'+md(i.done_at.slice(0,10))+'</time><span data-raw>'+esc(i.title)+'</span></li>').join('')+'</ul>'
  +(l.total>l.items.length?'<button type="button" class="text-button" data-action="todoFoundAll" data-start="'+esc(l.start)+'" data-end="'+esc(l.end)+'" data-q="'+esc(l.query)+'">'+T('{0}건 모두 완료 기록에서 보기',l.total)+'</button>':'')+'</div>';
}
function todoFeedView() {
 const feed=state.todoFeed||[];
 if(!feed.length)return '';
 return '<div class="todo-feed" aria-live="polite">'+feed.map((t,n)=>'<section class="todo-turn"><p class="todo-q" data-raw>'+esc(t.q)+'</p><div class="todo-a">'
  +(t.busy?'<p class="muted">'+T('묻는 중…')+'</p>':t.error?'<p class="ai-bad">'+esc(t.error)+'</p>'
   :(t.plan.note&&!t.plan.lookup?'<p data-raw>'+esc(t.plan.note)+'</p>':'')+lookupView(t.plan.lookup)   // 기록을 찾은 답은 이 PC 의 결과만 — AI 는 기록을 못 봐 그 말이 틀릴 수 있다 (비서 회신 2 P2-4)
   +(t.plan.changes.length?'<ul class="todo-suggests">'+t.plan.changes.map((c,i)=>{const r=t.done[i];
     return '<li>'+(r?.ok?'<span class="todo-applied">'+icon('confirm')+suggestWord(c)+'</span>'
      :'<button type="button" class="button ghost todo-suggest" data-action="todoSuggest" data-turn="'+n+'" data-i="'+i+'"'+(r?.busy?' disabled':'')+'>'+suggestWord(c)+'</button>'+(r?.error?'<small class="ai-bad">'+esc(r.error)+'</small>':''))+'</li>';}).join('')+'</ul>'
     :!t.plan.lookup&&!t.plan.note?'<p class="muted">'+T('바꿀 것을 찾지 못했어요.')+'</p>':'')
   +(t.plan.skipped?'<p class="muted">'+T('알아보지 못한 제안 {0}개는 뺐어요.',t.plan.skipped)+'</p>':'')
   +(t.plan.limited?'<p class="muted">'+T('열린 할 일이 많아 앞의 {0}개만 AI 에 보냈어요.',t.plan.sent)+'</p>':'')
   +(t.plan.mine?.messages?'<p class="muted">'+T('켠 대화 {0}곳의 내 글 {1}건을 함께 보냈어요.',t.plan.mine.convs,t.plan.mine.messages)+'</p>':''))
  +'</div></section>').join('')+'</div>';
}
async function todoAsk(a) {
 // [룬 추천] — 이 버튼을 누른 요청에만 설치된 룬 이름이 간다 (codex1 next4 ④). 비어 있으면 기본 물음
 const runes=!!a?.dataset?.runes,t=$('#todoTitle'),q=t?.value.trim()||(runes?T('열린 할 일에 맞는 룬을 추천해줘'):'');
 if(!q||todoAsk.busy||todoAdd.busy)return;
 const turn={q,busy:true,done:[]};state.todoFeed=[...(state.todoFeed||[]),turn].slice(-20);todoAsk.busy=true;render();
 try{turn.plan=await api('todo_propose',{text:turn.q,runes});const now=$('#todoTitle');   // 실패하면 적은 말은 입력창에 남는다
  if(now&&now.value.trim()===turn.q){now.value='';state.todoDraft={...state.todoDraft,title:''};}}   // 기다리는 사이 새로 적은 말·고른 날짜는 그대로 (비서 회신 2 P2-2)
 catch(e){turn.error=e.message;}
 finally{turn.busy=false;todoAsk.busy=false;render();$('#todoTitle')?.focus();}
}
async function todoSuggest(a) {
 const t=(state.todoFeed||[])[+a.dataset.turn],i=+a.dataset.i,c=t?.plan?.changes[i];
 if(!c||t.done[i]?.busy||t.done[i]?.ok)return;   // 두 번 눌러도 한 번만
 t.done[i]={busy:true};render();
 try{const r=await api('todo_apply',{changes:[c]}),got=r.results[0];state.todos=r.cards;loadTodos.last=JSON.stringify(r.cards)+ymd(new Date());todoBadge();if(r.runes){state.todoRunes=r.runes;loadTodos.links=JSON.stringify(r.runes);}
  t.done[i]=got.ok?{ok:true}:{error:got.error};}
 catch(e){t.done[i]={error:e.message};}
 render();
}
async function todoAdd() {
 const t=$('#todoTitle');if(!t.value.trim()||todoAdd.busy)return;todoAdd.busy=true;t.disabled=true;   // Enter 두 번에 카드 두 장 안 생기게, 저장 중엔 다음 초안을 받지 않는다
 let ok=false;
 try{await api('todo_add',{title:t.value,due:$('#todoDue')?.value||''});ok=true;state.todoDraft=null;}catch(e){toast(e.message);}
 finally{todoAdd.busy=false;const now=$('#todoTitle');if(now){now.disabled=false;if(ok)now.value='';now.focus();}}
 if(ok)await loadTodos();
}
document.addEventListener('input',e=>{if(e.target.id==='todoTitle'||e.target.id==='todoDue'){state.todoDraft={title:$('#todoTitle').value,due:$('#todoDue').value};$('#todoDateLabel').textContent=state.todoDraft.due===ymd(new Date())?'오늘':state.todoDraft.due?md(state.todoDraft.due):'날짜';}});   // 목록이 다시 그려져도 적던 것은 남는다
document.addEventListener('toggle',e=>{if(e.target.id==='todoCompleted'&&e.target.isConnected)state.todoCompleted=e.target.open;},true);
document.addEventListener('click',e=>{document.querySelectorAll('.todo-menu[open]').forEach(menu=>{if(!menu.contains(e.target)||e.target.closest('button'))menu.open=false;});});
document.addEventListener('keydown',e=>{if(e.key!=='Escape'||state.page!=='todo'||$('dialog[open]'))return;const menu=$('.todo-menu[open]');if(menu){menu.open=false;$('summary',menu).focus();}});
// 순서 바꾸기 — 같은 묶음(다시 알림·오늘·다음) 안에서 끌어 놓거나, 카드에서 Alt+↑/↓ (마스터 요청 2026-11-12).
// 놓으면 열린 카드 전체의 화면 순서를 코어에 그대로 저장한다.
async function todoOrder() {
 const visible=[...main.querySelectorAll('.todo-card[data-card]')].map(li=>li.dataset.card),selected=new Set(visible);
 // 필터에 가려진 항목의 자리는 유지하고, 보이는 항목만 그 자리 안에서 바꾼다.
 let i=0;const ids=state.todos.filter(c=>c.state==='open').map(c=>selected.has(c.id)?visible[i++]:c.id);
 try{state.todos=await api('todo_move',{card_ids:ids});loadTodos.last=JSON.stringify(state.todos)+ymd(new Date());}catch(e){toast(e.message);}
 render();
}
// 포인터로 끈다 — HTML5 drag 이벤트는 Rust(Tauri) 창의 파일 놓기 처리에 막혀 화면까지 오지 않는다 (codex1 Rust P2).
// 카드의 버튼·메뉴에서 시작한 누름은 끌기가 아니다. 5px 넘게 움직여야 끌기 — 그냥 누름은 그대로.
let dragCard=null,dragFrom=0,dragMoved=false,dragGrab=0;
document.addEventListener('pointerdown',e=>{if(e.button!==0)return;const li=e.target.closest?.('.todo-card[data-card]');if(!li||e.target.closest('button,summary,a,input,textarea,.todo-menu,.postit-fold'))return;
 dragCard=li;dragFrom=e.clientY;dragGrab=e.clientY-li.getBoundingClientRect().top;dragMoved=false;li.setPointerCapture(e.pointerId);});
document.addEventListener('pointermove',e=>{if(!dragCard)return;if(!dragCard.isConnected){dragCard=null;return;}
 if(!dragMoved){if(Math.abs(e.clientY-dragFrom)<5)return;dragMoved=true;dragCard.classList.add('dragging');}
 const sibs=[...dragCard.parentNode.querySelectorAll(':scope>.todo-card[data-card]')].filter(li=>li!==dragCard);   // 같은 묶음 안에서만
 const next=sibs.find(li=>{const r=li.getBoundingClientRect();return e.clientY<r.top+r.height/2;});
 if(next){if(dragCard.nextElementSibling!==next)next.before(dragCard);}else if(sibs.length&&sibs[sibs.length-1].nextElementSibling!==dragCard)sibs[sibs.length-1].after(dragCard);
 dragCard.style.transform='';const top=dragCard.getBoundingClientRect().top;dragCard.style.transform='translateY('+(e.clientY-dragGrab-top)+'px)';});   // 놓일 자리에서 포인터까지
const dragEnd=()=>{if(!dragCard)return;const moved=dragMoved&&dragCard.isConnected;dragCard.classList.remove('dragging');dragCard.style.transform='';dragCard=null;if(moved)todoOrder();};
document.addEventListener('pointerup',dragEnd);document.addEventListener('pointercancel',dragEnd);
document.addEventListener('keydown',e=>{if(!e.altKey||!['ArrowUp','ArrowDown'].includes(e.key))return;const li=e.target.closest?.('.todo-card[data-card]');if(!li)return;
 const to=e.key==='ArrowUp'?li.previousElementSibling:li.nextElementSibling;if(!to)return;e.preventDefault();
 const focusId=e.target.id||null,focusAction=e.target.dataset.action;li.parentNode.insertBefore(li,e.key==='ArrowUp'?to:to.nextSibling);
 todoOrder().then(()=>{(focusId&&$('#'+focusId))||main.querySelector('.todo-card[data-card="'+li.dataset.card+'"] [data-action="'+focusAction+'"]')?.focus();});});
async function todoSet(method,args) {try{state.todos=await api(method,args);loadTodos.last=JSON.stringify(state.todos)+ymd(new Date());todoBadge();render();if(state.todoTab==='archive')loadArchive();}catch(e){toast(e.message);}}
// 완료 기록 (마스터 2026-11-26) — 지난 완료 카드를 기간·제목으로. 통계는 끝낸 날을 아는 카드만, 가져온 기록은 따로.
function archState(){if(!state.arch){const now=new Date(),from=new Date(now);from.setDate(now.getDate()-89);state.arch={start:ymd(from),end:ymd(now),q:'',imported:false,data:null};}return state.arch;}
async function loadArchive(more) {
 // 마지막으로 연 조건의 답만 쓴다 — 그사이 조건을 바꾸거나 지웠으면 옛 답은 버린다 (codex1 아카이브 회신 1 P2-5)
 // [더 보기] 는 화면에 보이는 답의 조건에만 이어 붙인다 — 새 조회 중이면 누르지 못한다 (회신 3 P1)
 const a=archState();if(more&&(a.loading||!a.data))return;
 const ask={...(more?a.data.ask:{start:a.start,end:a.end,query:a.q,imported:a.imported}),offset:more?a.data.items.length:0},seq=a.seq=(a.seq||0)+1;
 a.loading=true;render();
 try{const got=await api('todo_archive',ask);if(seq!==a.seq)return;
  a.data=more&&a.data?{...got,items:a.data.items.concat(got.items),ask:a.data.ask}:{...got,ask};a.error='';}catch(e){if(seq!==a.seq)return;a.error=e.message;}
 finally{if(seq===a.seq)a.loading=false;}
 render();
}
function archiveStats(s) {
 if(!s)return '';
 const top=Math.max(1,...s.weeks.map(w=>w.n)),tile=(n,label)=>'<div class="arch-stat"><strong>'+esc(String(n))+'</strong><span>'+label+'</span></div>';
 return '<div class="arch-stats">'+tile(s.done,T('완료'))+tile(s.snoozed,T('미룬 횟수'))+tile(s.avg_days==null?'–':T('{0}일',s.avg_days),T('평균 걸린 날'))
  +(s.rune_opens||s.rune_done?tile(s.rune_opens,T('룬 열기'))+tile(s.rune_done,T('룬 연결 카드 완료')):'')+'</div>'
  +(s.weeks.length>1?'<figure class="arch-chart"><figcaption class="small muted">'+T('주별 완료')+'</figcaption><div class="arch-bars" role="img" aria-label="'+esc(s.weeks.map(w=>md(w.week)+' '+w.n).join(', '))+'">'+s.weeks.map(w=>'<span title="'+esc(md(w.week)+' · '+w.n)+'"><i style="height:'+Math.round(w.n/top*100)+'%"></i></span>').join('')+'</div><div class="arch-axis small muted"><span>'+esc(md(s.weeks[0].week))+'</span><span>'+esc(md(s.weeks[s.weeks.length-1].week))+'</span></div></figure>':'')
  +(s.months.length?'<p class="small muted arch-months">'+s.months.map(m=>esc(m.month.slice(2).replace('-','.'))+' · '+m.n).join('&emsp;')+'</p>':'');
}
function archiveView() {
 const a=archState(),d=a.data;if(!d&&!a.error&&!a.loading){a.loading=true;queueMicrotask(()=>loadArchive());}   // 다시 그리기가 겹쳐도 한 번만
 const tools='<form class="arch-tools" id="archForm"><label><span class="small muted">'+T('시작')+'</span><input id="archStart" type="date" value="'+esc(a.start)+'"'+(a.imported?' disabled':'')+'></label><label><span class="small muted">'+T('끝')+'</span><input id="archEnd" type="date" value="'+esc(a.end)+'"'+(a.imported?' disabled':'')+'></label>'
  +'<label class="arch-q"><span class="small muted">'+T('제목 검색')+'</span><input id="archQ" type="search" maxlength="100" autocomplete="off" value="'+esc(a.q)+'"></label><button type="submit" class="button ghost">'+T('찾기')+'</button></form>'
  +'<div class="arch-actions"><button type="button" class="text-button" data-action="archImported" aria-pressed="'+a.imported+'">'+(a.imported?T('완료 기록 보기'):T('가져온 기록 보기'))+'</button><button type="button" class="button ghost" data-action="archExport"'+(a.loading||a.error||!d?' disabled':'')+'>'+icon('down')+T('엑셀로 저장')+'</button><button type="button" class="button ghost" data-action="archImport">'+icon('up')+T('md 가져오기')+'</button></div>';
 if(a.error)return tools+'<p class="ai-bad" role="alert">'+esc(a.error)+'</p>';
 if(!d)return tools+'<p class="muted todo-empty">'+T('불러오는 중…')+'</p>';
 const list=d.items.length?'<ul class="todo-list" aria-label="'+esc(a.imported?T('가져온 기록'):T('완료 기록'))+'">'+d.items.map(c=>todoCard({...c,imported:a.imported})).join('')+'</ul>'
  +(d.more?'<button type="button" class="text-button arch-more" data-action="archMore"'+(a.loading?' disabled':'')+'>'+T('더 보기')+' · '+(d.total-d.items.length)+'</button>':'')
  :'<p class="muted todo-empty">'+(d.limited?T('기간 안의 완료 기록이 많아 제목으로 찾을 수 없어요. 기간을 좁혀 주세요.'):a.imported?T('가져온 기록이 없어요. [md 가져오기] 로 옮길 수 있어요.'):a.q?T('찾는 제목이 없어요.'):T('이 기간에 끝낸 할 일이 없어요.'))+'</p>';
 return tools+(a.imported?'<p class="small muted arch-note">'+T('가져온 기록은 날짜를 쓰지 않아 통계에 넣지 않아요.')+'</p>':archiveStats(d.stats))+'<p class="small muted arch-count" role="status">'+T('{0}건',d.total)+'</p>'+list;
}
document.addEventListener('submit',e=>{if(e.target.id!=='archForm')return;e.preventDefault();const a=archState();a.start=$('#archStart').value||a.start;a.end=$('#archEnd').value||a.end;a.q=$('#archQ').value.trim();loadArchive();});
function archExport() {
 const a=archState();if(a.loading||a.error||!a.data)return;
 const ask=a.data.ask,n=a.data.total;   // 확인 창의 건수와 저장 조건이 같은 답에서 (회신 2 P1-1)
 const d=modal(heading(T('엑셀로 저장'))+'<p class="dialog-copy">'+T('지금 보이는 {0}건을 세부 내용까지 엑셀 파일로 저장해요.',n)+'</p>'
  +'<p class="small muted">'+T('다운로드 폴더에 저장하고 탐색기로 보여 드려요. 파일은 잠기지 않으니 보관에 주의하세요.')+'</p><p id="dialogError" role="alert"></p>'
  +'<div class="actions"><button type="button" class="button primary" id="archExportGo"'+(n?'':' disabled')+'>'+T('저장')+'</button></div>');
 $('#archExportGo').onclick=async()=>{const b=$('#archExportGo');b.disabled=true;
  try{const got=await api('todo_export',{start:ask.start,end:ask.end,imported:ask.imported,query:ask.query});d.close();toast(T('{0}건을 엑셀로 저장했어요.',got.rows));}
  catch(e){$('#dialogError').textContent=e.message;b.disabled=false;}};
}
// md 가져오기 — 미리보기에서 고른 줄만. 이미 가져온 줄은 고를 수 없고, 같은 제목이 있는 줄은 처음엔 빼 둔다.
async function archImport() {
 let pre;try{pre=await api('todo_import_pick');}catch(e){return toast(e.message);}if(!pre)return;
 state.importPre=pre;state.importPick=new Set(pre.items.filter(x=>!x.already&&!x.duplicate).map(x=>x.i));state.importPlain='done';importDialog();
}
function importDialog() {
 const p=state.importPre,pick=state.importPick,c=p.counts,chip=s=>s==='done'?T('끝냄'):s==='open'?T('열린 할 일'):T('표시 없음');
 const rows=p.items.map(x=>'<li class="import-row'+(x.already?' off':'')+'"><label><input type="checkbox" data-import="'+x.i+'"'+(pick.has(x.i)?' checked':'')+(x.already?' disabled':'')+'><span class="import-title" data-raw>'+esc(x.title)+'</span></label><small class="muted">'+chip(x.state)+(x.children?' · '+T('하위 {0}',x.children):'')+(x.already?' · '+T('이미 가져옴'):x.duplicate?' · '+T('같은 제목 있음'):'')+(x.lost_items?' · '+T('하위 {0}개 빠짐',x.lost_items):'')+(x.lost_body?' · '+T('본문 {0}자 빠짐',x.lost_body):'')+'</small></li>').join('');
 const lost=p.items.filter(x=>x.lost_items||x.lost_body).length;
 modal(heading(T('md 가져오기'))
  +'<p class="dialog-copy" data-raw>'+esc(p.name)+' · '+T('{0}건',c.all)+(c.already?' · '+T('이미 가져옴 {0}',c.already):'')+(c.duplicate?' · '+T('같은 제목 {0}',c.duplicate):'')+'</p>'
  +'<fieldset class="import-plain"><legend class="small muted">'+T('체크 표시([x]·[ ])가 없는 줄은')+'</legend>'
  +[['done',T('완료 기록으로')],['open',T('열린 할 일로')]].map(([v,t])=>'<label><input type="radio" name="importPlain" value="'+v+'"'+(state.importPlain===v?' checked':'')+'> '+t+'</label>').join('')+'</fieldset>'
  +'<div class="import-bulk"><button type="button" class="text-button" data-action="importAll">'+T('모두 고르기')+'</button><button type="button" class="text-button" data-action="importNone">'+T('모두 빼기')+'</button></div>'
  +'<ul class="import-list">'+rows+'</ul>'
  +(lost?'<p class="small market-warn">'+T('{0}건은 하위 항목이 3단·50개를 넘거나 본문이 4000자를 넘어 일부가 빠져요. 빠지는 양은 줄마다 적었어요.',lost)+'</p>':'')
  +'<p class="small muted">'+T('날짜는 가져오지 않아요. 가져오는 동안 AI·공유·알림은 일어나지 않아요. 열린 할 일로 가져온 것은 연결한 폰에도 보이고, [AI로 정리] 를 누르면 그 제목도 보내져요.')+'</p><p id="dialogError" role="alert"></p>'
  +'<div class="actions"><button type="button" class="button primary" id="importGo"'+(pick.size?'':' disabled')+'>'+T('{0}건 가져오기',pick.size)+'</button></div>',true);
 $('#importGo').onclick=importApply;
 $('#settingsDialog').addEventListener('close',()=>{if(state.importPre&&!importApply.busy){state.importPre=null;api('todo_import_cancel').catch(()=>{});}},{once:true});
}
document.addEventListener('change',e=>{
 const i=e.target.dataset?.import;if(i!=null&&state.importPick){e.target.checked?state.importPick.add(+i):state.importPick.delete(+i);const b=$('#importGo');if(b){b.disabled=!state.importPick.size;b.textContent=T('{0}건 가져오기',state.importPick.size);}}
 if(e.target.name==='importPlain')state.importPlain=e.target.value;
});
async function importApply() {
 const b=$('#importGo');b.disabled=true;importApply.busy=true;
 try{const got=await api('todo_import_apply',{token:state.importPre.token,picks:[...state.importPick],plain:state.importPlain});state.importPre=null;$('#settingsDialog').close();
  toast(T('{0}건을 가져왔어요.',got.added)+(got.skipped?' '+T('이미 있던 {0}건은 건너뛰었어요.',got.skipped):''));await loadTodos();loadArchive();}
 catch(e){$('#dialogError').textContent=e.message;b.disabled=false;}
 finally{importApply.busy=false;}
}
function todoSnooze(a) {
 const id=a.dataset.id,c=state.todos.find(x=>x.id===id),now=new Date();
 const opts=[['1시간 뒤',new Date(now.getTime()+3600e3)],['내일 아침 9시',at9(now,1)],['다음 주 월요일 9시',at9(now,((8-now.getDay())%7)||7)]];
 const d=modal(heading('미루기')+'<p class="dialog-copy"><b>'+raw(c.title)+'</b> '+T('— 언제 다시 알려 드릴까요?')+'</p><div class="actions todo-when">'
  +opts.map(([t,w])=>'<button type="button" class="button ghost" data-when="'+local(w)+'">'+t+'</button>').join('')+'</div>'
  +'<label for="todoAt">직접 고르기</label><div class="todo-at"><input id="todoAt" type="datetime-local" value="'+local(at9(now,1))+'"><button type="button" class="button primary" id="todoAtGo">이때 알림</button></div><p id="dialogError" role="alert"></p>');
 let busy=false;const go=async w=>{if(busy)return;busy=true;try{state.todos=await api('todo_snooze',{card_id:id,until:w});loadTodos.last=JSON.stringify(state.todos)+ymd(new Date());todoBadge();d.close();render();toast('미뤘어요. '+md(w)+' '+w.slice(11,16)+' 에 다시 알려 드려요.');}catch(e){busy=false;dialogError(e.message);}};
 d.querySelectorAll('[data-when]').forEach(b=>b.onclick=()=>go(b.dataset.when));$('#todoAtGo').onclick=()=>go($('#todoAt').value);
}
// 폰에서 보기 (2026-11-11 마스터 결정) — 이 PC 가 연결한 폰만 할 일 내용을 받는다.
// 연결: [폰 연결 시작] → 1회용 코드(10분)를 폰에 친다. 코드는 서버로 가지 않고, 그 코드로 양쪽 키를 서로 확인한다.
async function phoneOpen(start) {
 let r;try{r=await api(start===true?'phone_start':'phone_list');}catch(e){toast(e.message);return;}
 const d=modal(heading('폰에서 보기')+'<p class="dialog-copy">폰에서 <b>'+esc(r.url)+'</b> 를 열고 이 PC 와 같은 계정으로 로그인한 뒤, 아래 연결 코드를 치세요.</p>'
  +(r.code?'<p class="phone-code" aria-live="polite">'+esc(r.code.slice(0,4)+' '+r.code.slice(4))+'</p><p class="dialog-copy muted">'+Math.ceil(r.left/60)+'분 안에 폰에 치세요. 이 창을 열어 두면 폰을 확인하고 바로 연결해요.</p>'
   :'<div class="actions"><button type="button" class="button primary" id="phoneStart">폰 연결 시작</button></div>')
  +(r.waiting?'<p class="dialog-copy muted">코드로 확인되지 않은 연결 요청 '+r.waiting+'개는 연결하지 않았어요. 10분 뒤 사라져요.</p>':'')
  +'<p class="dialog-copy muted">연결한 폰은 할 일 내용(제목·날짜·알림 시각)을 받아요. 그 폰만 열 수 있게 잠그고 이 PC 가 서명해 보내요. 폰에서 끝냄·미루기를 누르면 이 PC 가 켜져 있고 잠겨 있지 않을 때 반영돼요. 끊어도 이미 폰에 보인 내용은 거둘 수 없어요.</p>'
  +(r.phones.length?'<ul class="phone-list">'+r.phones.map(p=>'<li><b>'+esc(p.label)+'</b> · 연결됨 <button type="button" class="text-button muted" data-phone="'+esc(p.id)+'">끊기</button></li>').join('')+'</ul>':'<p class="muted">연결한 폰이 없어요.</p>')
  +'<p id="dialogError" role="alert"></p>');
 const go=$('#phoneStart');if(go)go.onclick=()=>phoneOpen(true);
 d.querySelectorAll('[data-phone]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await api('phone_revoke',{phone_id:b.dataset.phone});toast('폰 연결을 끊었어요.');await phoneOpen();}catch(e){b.disabled=false;dialogError(e.message);}});
 clearTimeout(phoneOpen.t);
 if(r.code)phoneOpen.t=setTimeout(()=>{if(d.open&&$('.phone-code'))phoneOpen();},3000);   // 창이 열린 동안만 폰을 기다린다
 const before=phoneOpen.count;phoneOpen.count=r.phones.length;if(before!=null&&r.phones.length>before)toast('폰을 연결했어요. 30초 안에 할 일이 폰으로 가요.');
}
function todoDelete(a) {
 const id=a.dataset.id,c=state.todos.find(x=>x.id===id)||state.arch?.data?.items.find(x=>x.id===id);if(!c)return;
 const d=modal(heading('할 일 지우기')+'<p class="dialog-copy"><b>'+raw(c.title)+'</b> '+T('를 지울까요? 되돌릴 수 없어요.')+'</p><div class="actions"><button type="button" class="button primary" id="todoDelGo">지우기</button></div>');
 $('#todoDelGo').onclick=async()=>{d.close();await todoSet('todo_delete',{card_id:id});};
}
function modal(html,wide=false) {
 const d=$('#settingsDialog');d.classList.toggle('wide',wide);d.innerHTML=html;d.oncancel=null;
 $('#closeDialog').onclick=()=>d.close();if(!d.open)d.showModal();return d;
}
// 광장 공유 (마스터 2026-11-19, 구글 시트처럼) — 친구·메신저 그룹을 더하고, 일반 액세스를 '제한됨/광장 전체' 로.
// 그룹은 이 PC 에만 있어 서버에는 그 순간의 멤버 계정으로 펼쳐 보낸다. 나중에 그룹에 넣은 친구는 다시 공유해야 받는다.
async function openShare(id) {
 const r=state.listing.releases.find(x=>x.rune_id===id);if(!r)return;
 let all,friends=[],groups=[];
 try{[all,friends,groups]=await Promise.all([api('market_shares'),api('messenger_friends').catch(()=>[]),api('messenger_groups').catch(()=>[])]);}
 catch(e){toast(e.message);return;}
 const cur=all[id]||{restricted:false,people:[]},pals=friends.filter(f=>f.state==='accepted');
 const people=new Map(cur.people.map(p=>[p.account_id,p.handle]));let restricted=cur.restricted;
 const who=h=>h?'@'+h:'메신저 아이디 없음';
 const d=modal('<form id="shareForm" class="share">'+heading(T('‘{0}’ 공유',VaderI18n.rune(id,r.name)))
  +'<div class="share-add"><input id="shareFind" list="shareList" autocomplete="off" placeholder="친구 또는 그룹 추가" aria-label="친구 또는 그룹 추가"><datalist id="shareList">'
  +groups.filter(g=>g.count).map(g=>'<option value="그룹 · '+esc(g.name)+'" data-raw>'+T('{0}명',g.count)+'</option>').join('')
  +pals.map(f=>'<option value="@'+esc(f.handle)+'"></option>').join('')+'</datalist><button type="button" class="button ghost" id="shareAddBtn">'+icon('plus')+'추가</button></div>'
  +'<p class="small share-msg" id="shareMsg" role="status"></p>'
  +'<h3 class="share-h">액세스 권한이 있는 사람</h3><ul class="share-people" id="sharePeople"></ul>'
  +'<h3 class="share-h">일반 액세스</h3><div class="share-general"><span class="share-icon" id="shareIcon"></span><div><select id="shareScope" aria-label="일반 액세스"><option value="restricted">제한됨</option><option value="all">광장 전체</option></select><p class="small muted" id="shareNote"></p></div></div>'
  +'<div class="actions"><button type="submit" class="button primary">'+icon('confirm')+'완료</button><button type="button" class="button ghost" data-action="closeDialog">취소</button></div></form>');
 const draw=()=>{
  $('#sharePeople',d).innerHTML='<li class="share-me"><span class="share-avatar" aria-hidden="true">'+icon('user')+'</span><span>나 <small>발행자</small></span><span class="small muted">소유자</span></li>'
   +[...people].map(([aid,h])=>'<li><span class="share-avatar" aria-hidden="true">'+esc((h||'?')[0].toUpperCase())+'</span><span>'+esc(who(h))+'</span><button type="button" class="text-button" data-drop="'+esc(aid)+'">빼기</button></li>').join('');
  $('#shareScope',d).value=restricted?'restricted':'all';
  $('#shareIcon',d).innerHTML=icon(restricted?'shield':'people');
  $('#shareNote',d).textContent=restricted?'추가한 사람만 받을 수 있어요.':'광장 받기가 허용된 모든 계정이 받을 수 있어요. 추가한 사람도 받아요.';
 };
 const add=()=>{const v=$('#shareFind',d).value.trim(),msg=$('#shareMsg',d);if(!v)return;
  const g=groups.find(x=>v==='그룹 · '+x.name),f=pals.find(x=>'@'+x.handle===v||x.handle===v.replace(/^@/,''));
  const adds=g?pals.filter(x=>x.group===g.id):f?[f]:[];
  if(!adds.length){msg.textContent=g?'이 그룹에 친구가 없어요.':'친구 목록에서 골라 주세요.';return;}
  adds.forEach(x=>people.set(x.account_id,x.handle));$('#shareFind',d).value='';
  msg.textContent=g?'\u2018'+g.name+'\u2019 그룹의 '+adds.length+'명을 더했어요.':'';draw();};
 $('#shareAddBtn',d).onclick=add;
 $('#shareFind',d).onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();add();}};
 $('#sharePeople',d).onclick=e=>{const b=e.target.closest('[data-drop]');if(b){people.delete(b.dataset.drop);draw();}};
 $('#shareScope',d).onchange=e=>{restricted=e.target.value==='restricted';draw();};
 $('#shareForm',d).onsubmit=async e=>{e.preventDefault();
  try{await api('market_share_save',{rune_id:id,restricted,accounts:[...people.keys()]});d.close();toast(T('‘{0}’ 공유 설정을 저장했어요.',VaderI18n.rune(id,r.name)));}
  catch(err){$('#shareMsg',d).textContent=err.message;}};
 draw();$('#shareFind',d).focus();
}
const heading=t=>'<div class="dialog-heading"><h2 id="settingsTitle">'+esc(t)+'</h2><button type="button" class="text-button" id="closeDialog">닫기</button></div>';
// 광장 목록 다시 서명 — 키 파일을 찾거나 이 창에 끌어 놓고, 암호를 넣어 한 번에 서명·올리기 (마스터 요청 2026-11-09).
// 암호는 화면에 남기지 않는다 — 서명이 끝나면 칸을 비운다.
let keyDrop=null;
function openResign() {
 let file=null;
 const d=modal(heading('광장 목록 다시 서명')+'<p class="dialog-copy">목록 키 파일과 암호로 오늘의 목록 정보를 서명해 광장에 올려요. 운영 서버에 발행자 계정으로 로그인돼 있어야 해요.</p>'
  +'<div id="keyBox" class="key-drop" tabindex="-1">'+icon('file')+'<span id="keyName">목록 키 파일을 여기에 끌어 놓거나</span>'+button('keyPick','파일 찾기','search','ghost')+'</div>'
  +'<label for="keyPass">목록 키 암호</label><input id="keyPass" type="password" autocomplete="off" spellcheck="false">'
  +'<p id="dialogError" role="alert"></p><div class="actions"><button type="button" class="button primary" id="keyGo" disabled>'+icon('confirm')+'서명하고 올리기</button></div>');
 api('catalog_publisher').catch(e=>dialogError(e.message));   // 서명할 수 없는 연결이면 파일·암호를 넣기 전에 알린다
 const ready=()=>{$('#keyGo').disabled=!file||!$('#keyPass').value;};
 const take=path=>{file=path;$('#keyName').textContent=path.split(/[\\/]/).pop();$('#keyBox').classList.add('chosen');ready();};
 $('[data-action="keyPick"]',d).onclick=async()=>{try{const path=await api('catalog_key_pick');if(path)take(path);}catch(e){dialogError(e.message);}};
 const box=$('#keyBox');
 box.ondragover=e=>{e.preventDefault();box.classList.add('over');};box.ondragleave=()=>box.classList.remove('over');
 box.ondrop=e=>{e.preventDefault();box.classList.remove('over');keyDrop={take,at:Date.now()};};   // 경로는 코어가 receiveDrop 으로 준다
 $('#keyPass').oninput=ready;$('#keyPass').onkeydown=e=>{if(e.key==='Enter'&&!$('#keyGo').disabled)$('#keyGo').click();};
 d.addEventListener('close',()=>{keyDrop=null;},{once:true});
 $('#keyGo').onclick=async e=>{const btn=e.currentTarget,pass=$('#keyPass');btn.disabled=true;btn.lastChild.textContent='서명하는 중…';
  try{const r=await api('catalog_resign',{key_file:file,passphrase:pass.value});pass.value='';catalog(r.catalog);render();d.close();toast('목록 정보를 서명해 올렸어요. '+r.lines.join(' · '));}
  catch(err){pass.value='';pass.focus();btn.lastChild.textContent='서명하고 올리기';ready();dialogError(err.message);}};
}
async function pushCatalog(a) {
 a.disabled=true;
 try{const r=await api('catalog_push');catalog(r.catalog);render();toast(r.lines.join(' · '));}catch(e){a.disabled=false;toast(e.message);}
}
const dialogError=m=>{const e=$('#dialogError');e.textContent=m;e.scrollIntoView({block:'nearest'});};
function openSeal() {
 const r=taskRunes[state.task];
 const d=modal('<form id="sealForm">'+heading(r.name+' · 실사용 승인')+'<p class="dialog-copy">승인하면 이 룬의 <b>실사용 실행</b>이 열립니다. 누가 언제 무엇을 확인했는지 기록으로 남고, 룬이 바뀌면 승인은 저절로 풀립니다.</p><label class="check"><input type="checkbox" required autofocus> 연습 실행으로 결과를 직접 확인했습니다</label><div class="settings-field"><label for="sealNote">무엇을 확인했는지</label><textarea id="sealNote" rows="3" placeholder="예: 9/28 원본으로 발주 135행 대조 일치"></textarea></div><p id="dialogError" role="alert"></p><div class="actions"><button type="submit" class="button primary">'+icon('confirm')+'승인</button></div></form>');
 $('#sealForm').onsubmit=async e=>{e.preventDefault();
  try{catalog(await api('seal',{rune_id:r.id,note:$('#sealNote').value.trim()}));d.close();
  const back=state.jobs.find(x=>x.id===state.returnTo);state.returnTo=null;if(back){state.job=back;navigate('result');}else render();toast(r.name+' 승인했어요. 이제 실사용 실행을 할 수 있어요.');}
  catch(err){dialogError(err.message);}};
}
function openUninstall() {
 const r=taskRunes[state.task];
 modal(heading(r.name+' 제거')+'<p class="dialog-copy">룬만 떼어냅니다. 설정·체크포인트·학습 데이터는 남아서, 다시 설치하면 그대로 씁니다.</p><p id="dialogError" role="alert"></p><div class="actions">'+'<button type="button" class="button primary" data-action="confirmUninstall">제거</button>'+'</div>');
}
async function confirmUninstall() {
 const r=taskRunes[state.task];
 try{catalog(await api('uninstall',{rune_id:r.id}));$('#settingsDialog').close();render();toast(r.name+' 제거했어요.');}
 catch(err){dialogError(err.message);}
}
// ── AI 연결 — Orca 에이전트처럼: 서비스 카드에 키를 넣으면 연결, 별 하나가 기본 AI (마스터 결정 2026-10-14) ──
// 역할 배정 화면은 없다 — 어느 일에 무엇을 쓸지는 코어가 정한다(기본 AI, 문서 읽기만 문서를 읽는 AI).
let aiv=null,aiOpen=null,aiMsg={},aiArmed=null,aiAddForm={src:'',model:''};
const AI_STATE={on:['연결됨','good'],fix:['조치 필요','warn'],off:['미연결','idle']};
async function openAssignments() {aiv=await api('ai_choice');aiv.clients=await api('client_status').catch(()=>[]);aiMsg={};aiAddForm={src:'',model:''};aiArmed=null;aiOpen=aiv.cards.some(c=>c.state!=='off')?null:aiv.cards[0]?.id;renderAi();}
// 카드별 한계 안내 (마스터 2026-11-19) — 오른쪽 위 동그라미 느낌표, 누르면 펼쳐진다.
const AI_NOTICE={agy_console:'<b>Antigravity 는 설치·로그인과 제작소(룬 만들기)에 쓸 수 있어요.</b> 문서 읽기는 아직 닫혀 있어요.'
 +'<br><br>Antigravity CLI 는 그림을 읽는 동안 이 PC 의 다른 파일(다른 문서·로그인 정보 등)까지 읽을 수 있고, 읽는 범위를 작업 폴더로 제한하는 방법이 아직 없어요. 환자 정보가 든 문서가 다른 자료와 섞여 나가지 않도록 막아 두었어요.'
 +'<br><br>문서 읽기는 Claude Code·Codex·Google Gemini 를 쓰세요. 범위를 제한할 수 있게 되면 다시 확인해서 열어요.'};
const aiNotice=id=>'<details class="ai-notice"><summary aria-label="사용 안내"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 7.5v6"/><circle cx="12" cy="16.6" r=".6" fill="currentColor"/></svg></summary><div class="ai-notice-pop" role="note">'+AI_NOTICE[id]+'</div></details>';
// 구독 사용량 (2026-11-27) — Codex 는 공식 app-server 에서 읽은 창만, Claude Code 는 '—' 와 /usage 안내. 없는 값을 0% 로 채우지 않는다.
const aiLeft=at=>{const m=Math.max(0,Math.round((at*1000-Date.now())/60000));return m>=1440?Math.floor(m/1440)+'d '+Math.floor(m%1440/60)+'h':Math.floor(m/60)+'h '+m%60+'m';};
const aiWindow=w=>w===300?T('5시간'):w===10080?T('주간'):w?T('{0}분',w):'—';
function aiUsage(u) {
 if(!u)return '';
 const parts=u.windows?u.windows.map(w=>aiWindow(w.window_mins)+' '+w.used_percent+'%'+(w.resets_at?' · '+T('{0} 뒤 초기화',aiLeft(w.resets_at)):'')):['—'];
 const ago=u.seen?Math.max(0,Math.round((Date.now()/1000-u.seen)/60)):null;
 return '<p class="small muted ai-usage">'+T('사용량')+' '+parts.join(' / ')+(ago!=null?' · '+T('마지막 호출 기준 {0}분 전',ago):'')+(u.note?' · '+T(u.note):'')+'</p>';
}
function renderAi() {
 state.todoAiTarget=undefined;window.VaderMessenger?.aiChanged?.();   // 기본 AI 가 바뀌었을 수 있다 — 할 일 안내·메신저 스위치의 '보내는 곳' 을 다시 읽는다
 window.VaderChat?.sync();   // AI 를 붙이거나 떼면 어느 화면에서든 대화 서랍이 바로 따라간다 (codex1 P2-3)
 const name=id=>aiv.cards.find(c=>c.id===id)?.label||id;
 const route=aiv.thinking?'<p class="ai-route"><span>판단·대화</span><b>'+esc(name(aiv.thinking))+'</b>'+(aiv.documents&&aiv.documents!==aiv.thinking?'<span>문서 처리</span><b>'+esc(name(aiv.documents))+'</b>':'')+'</p>'
  :'<p class="ai-route empty">'+icon('info')+'<span>'+(aiv.default?esc(name(aiv.default))+' (기본 AI) 를 지금 쓸 수 없어요. 아래 카드에서 조치하세요.':aiv.cards.some(c=>c.state==='on')?'기본 AI 가 없어요. 쓸 카드에서 ☆ 기본으로 를 눌러 고르세요.':'아직 쓸 AI 가 없어요. 아래에서 서비스 하나에 키를 넣어 연결하세요. AI 가 필요 없는 룬은 지금도 쓸 수 있어요.')+'</span></p>';
 const cards=aiv.cards.map(c=>{
  const [st,tone]=AI_STATE[c.state], open=aiOpen===c.id, m=aiMsg[c.id];
  const star=(c.keyless&&c.documents?'<span class="ai-star on" title="문서 읽기를 맡은 AI">문서 담당</span>':'')+(c.is_default?'<span class="ai-star on" title="기본 AI">★ 기본</span>':c.state==='on'&&(!c.keyless||c.can_default)?('<button type="button" class="ai-star" data-action="aiDefault" data-id="'+esc(c.id)+'">☆ 기본으로</button>'):'');
  const keyForm=(label)=>c.keyless?'<div class="ai-form">'+button('aiCheck',label==='연결'?'연결 확인':label,'link','primary').replace('data-action="aiCheck"','data-action="aiCheck" data-id="'+esc(c.id)+'"')+'</div>'+(c.key_help?'<p class="small muted">'+esc(c.key_help)+'</p>':''):'<div class="ai-form"><label class="sr-only" for="k-'+esc(c.id)+'">'+esc(c.service)+' 키</label><input id="k-'+esc(c.id)+'" type="password" data-ai-key="'+esc(c.id)+'" placeholder="API 키" autocomplete="off" spellcheck="false">'+button('aiConnect',label,'link','primary').replace('data-action="aiConnect"','data-action="aiConnect" data-id="'+esc(c.id)+'"')+'</div>'+(c.key_help?'<p class="small muted">'+esc(c.key_help)+'</p>':'');
  // 터미널 AI (2026-11-19) — 설치 안 됐으면 [설치], 로그인이 필요하면 [로그인] (제작소 개발 모드와 같은 흐름)
  const cl=c.keyless?(aiv.clients||[]).find(x=>x.id===c.id.replace('_console','')):null;
  const needLogin=c.keyless&&/로그인/.test((m&&m.text||'')+' '+(c.state==='fix'?c.state_text:''));
  const termBody=cl&&!cl.installed?'<p class="small muted">이 PC 에 설치되어 있지 않아요. Windows 앱 설치 관리자로 공식 패키지를 설치해요.</p><div class="ai-form">'+button('aiInstall','설치','up','primary').replace('data-action="aiInstall"','data-action="aiInstall" data-client="'+esc(cl.id)+'"')+'</div>'
   :needLogin?'<p class="small muted">새 창에서 '+esc(cl?.name||c.service)+' 에 로그인한 뒤 [연결 확인] 을 다시 눌러 주세요.</p><div class="ai-form">'+button('aiLogin','로그인','link','primary').replace('data-action="aiLogin"','data-action="aiLogin" data-client="'+esc(c.id.replace('_console',''))+'"')+button('aiCheck','연결 확인','link','ghost').replace('data-action="aiCheck"','data-action="aiCheck" data-id="'+esc(c.id)+'"')+'</div>':'';
  // 문서 읽기를 닫아 둔 카드(Antigravity) — 문서 읽기 연결 대신 로그인만, 이유는 ⓘ 안내에
  const closedDocs=c.id==='agy_console'&&cl&&cl.installed;
  const body=closedDocs?'<p class="small muted">문서 읽기는 아직 닫혀 있어요 · 오른쪽 위 안내를 눌러 보세요.</p><div class="ai-form">'+button('aiLogin','로그인','link','ghost').replace('data-action="aiLogin"','data-action="aiLogin" data-client="agy"')+'</div>'
   :termBody||(c.state==='off'
   ?(open?keyForm('연결'):button('aiOpenCard','연결하기','arrow','ghost').replace('data-action="aiOpenCard"','data-action="aiOpenCard" data-id="'+esc(c.id)+'"'))
   :(c.recommended&&c.state==='fix'?button('aiRecommend','권장 모델로 연결 확인','link','primary').replace('data-action="aiRecommend"','data-action="aiRecommend" data-id="'+esc(c.id)+'"')+'<p class="small muted">권장: '+esc(c.recommended)+'</p>':'')
    +'<details class="ai-advanced"'+(open?' open':'')+'><summary>고급</summary><div class="settings-field"><label for="m-'+esc(c.id)+'">모델</label><div><input id="m-'+esc(c.id)+'" data-ai-model="'+esc(c.id)+'" value="'+esc(c.model)+'" autocomplete="off" spellcheck="false"'+(c.models?.length?' list="ml-'+esc(c.id)+'" placeholder="'+T('비우면 프로그램 기본값')+'"':'')+'>'+(c.models?.length?'<datalist id="ml-'+esc(c.id)+'">'+c.models.map(x=>'<option value="'+esc(x)+'">').join('')+'</datalist>':'')+''+button('aiCheck','연결 확인','link','ghost').replace('data-action="aiCheck"','data-action="aiCheck" data-id="'+esc(c.id)+'"')+'</div></div><p class="small muted">키 바꾸기'+(c.shared>1?' — 이 키를 쓰는 모델 '+c.shared+'개에 함께 적용돼요':'')+'</p>'+(c.keyless?'':keyForm('새 키로 연결'))+'<button type="button" class="text-button danger" data-action="aiDisconnect" data-id="'+esc(c.id)+'">'+(c.shared>1?(aiArmed===c.id?'한 번 더 누르면 이 모델을 지워요 (키는 남아요)':'이 모델 지우기'):(aiArmed===c.id?'한 번 더 누르면 연결을 끊어요':'연결 끊기'))+'</button></details>');
  return (c.keyless?'\u0001':'')+'<article class="ai-svc '+c.state+(c.is_default?' default':'')+'"><div class="ai-svc-head"><span class="ai-svc-name"><strong>'+esc(c.service)+'</strong><small>'+(c.state==='off'?'':esc(c.model))+'</small></span>'+star+'<span class="chip '+tone+'">'+st+'</span>'+(AI_NOTICE[c.id]?aiNotice(c.id):'')+'</div><p class="ai-can">'+esc(c.id==='agy_console'?T('설치 · 로그인 · 룬 만들기'):c.can.map(x=>T(x)).join(' · '))+'</p>'+(c.state==='fix'?'<p class="small ai-bad">'+esc(c.state_text)+'</p>':c.state==='on'?'<p class="small muted">'+esc(c.state_text)+'</p>':'')+aiUsage(c.usage)+body+(m?'<p class="small '+(m.ok?'ai-ok':m.ok===false?'ai-bad':'muted')+'" role="status">'+esc(m.text)+'</p>':'')+'</article>';
 });
 // 터미널 AI(이 PC 에 로그인된 프로그램, 키 없음) / API AI(키) 를 나눠 보인다 (마스터 2026-11-19)
 const term=cards.filter(x=>x.startsWith('\u0001')).map(x=>x.slice(1)).join(''),apis=cards.filter(x=>!x.startsWith('\u0001')).join('')+aiAddCard();
 const cardsHtml='<h3 class="ai-section">터미널 AI <small>이 PC 에 설치·로그인된 프로그램 · 키 없이 문서 읽기·대화</small></h3><div class="ai-grid">'+term+'</div>'
  +'<h3 class="ai-section">API AI <small>키를 넣어 연결 · 판단·대화·문서 읽기</small></h3><div class="ai-grid">'+apis+'</div>';
 const d=modal(heading('AI 연결')+'<p class="dialog-copy"><b>터미널 AI</b> 는 이 PC 에 설치·로그인된 프로그램으로, <b>API AI</b> 는 키로 연결해요. <b>별을 찍은 기본 AI</b> 가 판단·대화를 맡고, 문서 읽기는 문서를 읽을 수 있는 AI 가 맡아요.</p>'+route+cardsHtml+'<p class="small muted ai-foot">연결 확인은 업무 자료 없이 짧은 요청 한 번이에요 · 요금이 날 수 있어요. 키는 이 PC 의 Windows 보호 저장소에 암호화해 두고 다시 보여 주지 않아요. 터미널 AI 는 그 프로그램의 로그인 계정 사용량을 써요.</p><p id="dialogError" role="alert"></p>',true);
 d.onclose=aiClosed;
}
// + AI 추가 — 이미 연결된 서비스(그 키)를 사람이 골라 모델을 더한다. 주소는 그 연결의 것, 키를 다시 묻지 않는다.
// 새 서비스는 위의 미연결 카드에서 연결한다. 주소 직접 입력은 아직 없다(주소 검사 규칙이 정해진 뒤).
function aiAddCard() {
 const seen=new Set(), conns=aiv.cards.filter(c=>c.can_add&&!seen.has(c.connection)&&seen.add(c.connection));
 const dup=s=>conns.filter(c=>c.service===s).length>1, m=aiMsg['+'];
 const body=conns.length
  ?'<div class="settings-field"><label for="ai-add-src">서비스</label><select id="ai-add-src">'+conns.map(c=>'<option value="'+esc(c.id)+'"'+(aiAddForm.src===c.id?' selected':'')+'>'+esc(c.service+(dup(c.service)?' ('+c.connection+')':''))+'</option>').join('')+'</select></div>'
   +'<div class="ai-form"><label class="sr-only" for="ai-add-model">모델 이름</label><input id="ai-add-model" value="'+esc(aiAddForm.model)+'" placeholder="모델 이름 (서비스 문서 그대로)" autocomplete="off" spellcheck="false">'+button('aiAdd','확인 후 추가','link','primary')+'</div>'
   +'<p class="small muted">그 서비스의 키를 그대로 써요. 더한 모델은 별을 옮겨야 쓰여요.</p>'
  :'<p class="small muted">먼저 위에서 서비스 하나를 키로 연결하세요. 연결된 서비스(지금은 NVIDIA 처럼 OpenAI 호환)에 모델을 더할 수 있어요.</p>';
 return '<article class="ai-svc add"><div class="ai-svc-head"><span class="ai-svc-name"><strong>+ AI 추가</strong><small>연결된 서비스에 모델 더하기</small></span></div>'+body+(m?'<p class="small '+(m.ok?'ai-ok':m.ok===false?'ai-bad':'muted')+'" role="status">'+esc(m.text)+'</p>':'')+'</article>';
}
async function aiAdd() {
 aiAddForm={src:$('#ai-add-src')?.value||'',model:$('#ai-add-model')?.value.trim()||''};
 if(!aiAddForm.model){aiMsg['+']={ok:false,text:'모델 이름을 적어 주세요.'};renderAi();return;}
 aiMsg['+']={ok:null,text:'연결을 확인하는 중…'};renderAi();
 try{const r=await api('ai_add',{provider:aiAddForm.src,model:aiAddForm.model});aiv=r;aiMsg['+']={ok:r.ok,text:r.message};if(r.ok)aiAddForm.model='';}
 catch(err){aiMsg['+']={ok:false,text:err.message};}
 state.aiReady=!!aiv.thinking;renderAi();
}
// 창을 닫으면 코어의 'AI 연결' 안내를 새로 그린다
function aiClosed() {if(['core','run','result'].includes(state.page))render();window.VaderChat?.sync();}
function aiOpenCard(a) {aiOpen=a.dataset.id;renderAi();$('[data-ai-key="'+CSS.escape(aiOpen)+'"]')?.focus();}
async function aiRun(id,args,busyText) {
 aiMsg[id]={ok:null,text:busyText};aiOpen=id;renderAi();
 try{const r=await api('ai_connect',{provider:id,...args});aiv=r;aiMsg[id]={ok:r.ok,text:r.message};if(r.ok)aiOpen=null;}
 catch(err){aiMsg[id]={ok:false,text:err.message};}
 state.aiReady=!!aiv.thinking;renderAi();
}
function aiConnect(a) {const id=a.dataset.id,key=$('[data-ai-key="'+CSS.escape(id)+'"]')?.value.trim();if(!key){aiMsg[id]={ok:false,text:'키를 넣어 주세요.'};renderAi();return;}return aiRun(id,{key,model:$('[data-ai-model="'+CSS.escape(id)+'"]')?.value.trim()||null},'연결을 확인하는 중…');}
function aiCheck(a) {const id=a.dataset.id;return aiRun(id,{model:$('[data-ai-model="'+CSS.escape(id)+'"]')?.value.trim()||null},'연결을 확인하는 중…');}
function aiRecommend(a) {const c=aiv.cards.find(x=>x.id===a.dataset.id);return aiRun(c.id,{model:c.recommended},'권장 모델로 확인하는 중…');}
async function aiDefault(a) {try{aiv=await api('ai_default',{provider:a.dataset.id});state.aiReady=!!aiv.thinking;renderAi();}catch(err){dialogError(err.message);}}
async function aiDisconnect(a) {
 const id=a.dataset.id;if(aiArmed!==id){aiArmed=id;renderAi();return;}
 try{aiv=await api('ai_disconnect',{provider:id});state.aiReady=!!aiv.thinking;aiArmed=null;aiMsg={};renderAi();}catch(err){dialogError(err.message);}
}
let mapping=null,edits={},picked=new Set(),deleteArmed=false,closeArmed=false,sortBy={key:'vendor',dir:1};
const FIELDS=['vendor','name','code'],FIELD_NAMES={vendor:'거래처',name:'품목명',code:'코드'};
async function openMapping() {mapping=await api('mapping',{rune_id:state.task});resetLearned();renderMapping();}
function resetLearned() {edits={};picked=new Set();deleteArmed=false;closeArmed=false;}
const learnedValue=(i,k)=>edits[i]?.[k]??mapping.learned[i][k];
const isChanged=i=>!!edits[i]&&FIELDS.some(k=>edits[i][k]!==mapping.learned[i][k]);
const changedRows=()=>mapping.learned.map((_,i)=>i).filter(isChanged);
const dialogStatus=m=>{const e=$('#dialogStatus');e.textContent=m;$('#dialogError').textContent='';e.scrollIntoView({block:'nearest'});};
function reviewTable(m) {
 return '<div class="table-wrap"><table class="data-table"><thead><tr><th><span class="sr-only">선택</span></th><th>검토</th><th>보험코드</th><th>품목명</th><th>현재 코드</th><th>저장 위치</th></tr></thead><tbody>'+m.review.map((x,i)=>'<tr><td><input type="radio" name="reviewRow" value="'+i+'" aria-label="'+esc(x.name)+'"></td>'+[x.review,x.insurance_code,x.name,x.current_code,x.scope==='insurance'?'보험코드':'품목명'].map(c=>'<td>'+esc(c)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
}
function learnedOrder() {
 const {key,dir}=sortBy;
 return mapping.learned.map((_,i)=>i).sort((a,b)=>dir*String(learnedValue(a,key)).localeCompare(String(learnedValue(b,key)),'ko',{numeric:true}));
}
function learnedSection() {
 const m=mapping;if(!m.learned.length)return '<p class="dialog-copy">학습된 품목이 없어요.</p>';
 const changed=changedRows().length;
 const sortHead=(key)=>'<th aria-sort="'+(sortBy.key===key?(sortBy.dir>0?'ascending':'descending'):'none')+'"><button type="button" class="sort" data-sort="'+key+'">'+FIELD_NAMES[key]+'<span class="sort-mark" aria-hidden="true">'+(sortBy.key===key?(sortBy.dir>0?'▲':'▼'):'↕')+'</span></button></th>';
 const rows=learnedOrder().map(i=>'<tr data-row="'+i+'"'+(isChanged(i)?' class="changed"':'')+'><td><input type="checkbox" class="pick" value="'+i+'"'+(picked.has(i)?' checked':'')+' aria-label="'+esc(m.learned[i].name)+' 선택"></td>'+FIELDS.map(k=>'<td><input class="cell" data-key="'+k+'" value="'+esc(learnedValue(i,k))+'" aria-label="'+FIELD_NAMES[k]+'" autocomplete="off"></td>').join('')+'</tr>').join('');
 const bulk='<div class="bulk-bar"'+(picked.size?'':' hidden')+'><span>선택 <b>'+picked.size+'</b>개</span><select id="bulkField" aria-label="한꺼번에 바꿀 항목"><option value="code">코드</option><option value="vendor">거래처</option><option value="name">품목명</option></select><input id="bulkValue" placeholder="선택한 행을 모두 이 값으로" autocomplete="off">'+button('bulkApply','적용','confirm','ghost')+'<button type="button" class="button ghost" data-action="bulkDelete">'+(deleteArmed?'한 번 더 누르면 삭제':'선택 삭제')+'</button></div>';
 return '<div class="table-wrap tall" id="learnedWrap"><table class="data-table editable"><thead><tr><th><input type="checkbox" id="pickAll" aria-label="모두 선택"'+(picked.size===m.learned.length?' checked':'')+'></th>'+FIELDS.map(sortHead).join('')+'</tr></thead><tbody>'+rows+'</tbody></table></div>'+bulk
  +'<div class="actions save-bar"><button type="button" class="button primary" data-action="learnedSave"'+(changed?'':' disabled')+'>'+icon('save')+'<span>변경 저장 <b id="changedCount">'+changed+'</b>건</span></button><button type="button" class="button ghost" data-action="learnedReset"'+(changed?'':' disabled')+'>되돌리기</button></div>';
}
// 입고 종류 기억 (eonm_ipgo 2026-11-04) — 같은 답 N번 연속이면 자동. 여기서 바꾸면 바로 자동, 지우면 다음에 다시 묻는다.
let deptArmed=null;
function deptSection() {
 const d=mapping.dept;if(!d)return '';
 const intro='<h3>입고 종류 기억</h3><p class="dialog-copy">명세서의 품목·상호명마다 고른 종류를 셉니다. 같은 답이 '+d.auto_streak+'번 연속이면 묻지 않고 그 부서로 입고합니다. 여기서 종류를 바꾸면 바로 자동이 됩니다. 지우기는 그 줄만 지웁니다 — 같은 명세서의 상호명이나 품목 기억이 남아 있으면 그것으로 계속 정해질 수 있으니, 잘못된 자동은 지우지 말고 종류를 바꾸세요.</p>';
 if(!d.rows.length)return intro+'<p class="dialog-copy">아직 기억한 것이 없어요.</p>';
 const kinds=r=>'<select class="dept-kind" data-key="'+esc(r.key)+'" aria-label="'+esc(r.name)+' 입고 종류">'+d.kinds.map(k=>'<option value="'+esc(k.kind)+'"'+(k.kind===r.kind?' selected':'')+'>'+esc(k.kind+' · '+k.dept)+'</option>').join('')+'</select>';
 const rows=d.rows.map(r=>'<tr><td>'+esc(r.scope)+'</td><td>'+esc(r.name)+'</td><td>'+kinds(r)+'</td><td>'+(r.auto?'<span class="chip good">자동</span>':'<span class="chip">제안 '+r.streak+'/'+d.auto_streak+'</span>')+'</td><td><button type="button" class="text-button danger" data-action="deptDelete" data-key="'+esc(r.key)+'">'+(deptArmed===r.key?'한 번 더 누르면 삭제':'지우기')+'</button></td></tr>').join('');
 return intro+'<div class="table-wrap"><table class="data-table"><thead><tr><th>구분</th><th>이름</th><th>입고 종류</th><th>상태</th><th aria-label="지우기"></th></tr></thead><tbody>'+rows+'</tbody></table></div>';
}
function deptCall(key,kind,done) {
 if(changedRows().length)return dialogError('학습 DB 의 변경을 먼저 저장하거나 되돌려 주세요.');
 deptArmed=null;return mappingCall('mapping_dept',{key,kind},()=>done);
}
function deptDelete(a) {
 const key=a.dataset.key;
 if(deptArmed!==key){deptArmed=key;$('#deptMemory').innerHTML=deptSection();return dialogError('이 줄의 기억만 지웁니다. 한 번 더 누르세요.');}
 return deptCall(key,'','이 줄의 기억을 지웠어요.');
}
function refreshLearned() {
 const top=$('#learnedWrap')?.scrollTop||0;$('#learned').innerHTML=learnedSection();
 if($('#learnedWrap'))$('#learnedWrap').scrollTop=top;
}
function renderMapping() {
 const r=taskRunes[state.task],m=mapping;
 const review=m.review.length?reviewTable(m)+'<div class="inline-form"><label for="correctCode">올바른 내부코드</label><input id="correctCode" autocomplete="off">'+button('mappingCorrect','교정 저장','save','primary')+'</div>':'<p class="dialog-copy">최근 작업에 고칠 항목이 없어요.</p>';
 const d=modal(heading(r.name+' · 매핑 관리')+'<h3>품목 매핑 교정</h3><p class="dialog-copy">최근 작업의 확인필요·AI추정 항목을 고칩니다. 보험코드 규칙에 저장할지 품목명 학습에 저장할지는 자동으로 정해집니다.</p>'+review
  +'<h3>학습 DB 정리</h3><p class="dialog-copy">칸을 눌러 거래처·품목명·코드를 바로 고칩니다. 여러 행을 체크하면 한꺼번에 바꾸거나 지울 수 있고, 머리글을 누르면 정렬됩니다. 고친 칸은 <b>변경 저장</b>을 눌러야 저장됩니다.</p><div id="learned">'+learnedSection()+'</div>'
  +'<div id="deptMemory">'+deptSection()+'</div>'
  +'<p id="dialogStatus" class="dialog-copy" role="status"></p><p id="dialogError" role="alert"></p>',true);
 // 저장하지 않은 변경을 닫기·Esc 로 잃지 않게 — 한 번 더 눌러야 닫힌다.
 const guard=e=>{const n=changedRows().length;if(!n||closeArmed){closeArmed=false;return d.close();}
  e?.preventDefault();closeArmed=true;dialogError('저장하지 않은 변경 '+n+'건이 있어요. 버리고 닫으려면 한 번 더 누르세요.');};
 $('#closeDialog').onclick=guard;d.oncancel=guard;
}
const chosen=name=>{const c=$('input[name="'+name+'"]:checked');return c?Number(c.value):null;};
async function mappingCall(method,args,done) {
 try{mapping=await api(method,{rune_id:state.task,...args});resetLearned();renderMapping();if(done)dialogStatus(done(mapping));}
 catch(err){dialogError(err.message);}
}
function mappingCorrect() {
 const i=chosen('reviewRow');if(i===null)return dialogError('교정할 항목을 선택해 주세요.');
 if(changedRows().length)return dialogError('학습 DB 의 변경을 먼저 저장하거나 되돌려 주세요.');
 return mappingCall('mapping_correct',{index:i,code:$('#correctCode').value},m=>m.scope==='insurance'?'보험코드 규칙에 저장했어요.':'품목명 학습에 저장했어요.');
}
function onLearnedInput(cell) {
 const i=Number(cell.closest('tr').dataset.row),key=cell.dataset.key;
 edits[i]={...Object.fromEntries(FIELDS.map(k=>[k,learnedValue(i,k)])),[key]:cell.value};
 cell.closest('tr').classList.toggle('changed',isChanged(i));
 const n=changedRows().length;$('#changedCount').textContent=n;closeArmed=false;
 document.querySelectorAll('[data-action="learnedSave"],[data-action="learnedReset"]').forEach(b=>b.disabled=!n);
}
function bulkApply() {
 const field=$('#bulkField').value,value=$('#bulkValue').value.trim();
 if(!value)return dialogError(FIELD_NAMES[field]+'에 넣을 값을 적어 주세요.');
 picked.forEach(i=>{edits[i]={...Object.fromEntries(FIELDS.map(k=>[k,learnedValue(i,k)])),[field]:value};});
 const n=picked.size;refreshLearned();
 dialogStatus('선택한 '+n+'개 행의 '+FIELD_NAMES[field]+'을(를) 바꿨어요. 변경 저장을 눌러야 저장됩니다.');
}
async function bulkDelete() {
 if(changedRows().length)return dialogError('바꾼 내용을 먼저 저장하거나 되돌린 뒤 지워 주세요.');
 // 되돌릴 수 없으니 한 번 더 누르게 한다.
 if(!deleteArmed){deleteArmed=true;refreshLearned();return dialogError('선택한 '+picked.size+'개 학습 행을 지웁니다. 한 번 더 누르세요.');}
 const items=[...picked].map(i=>({vendor:mapping.learned[i].vendor,name:mapping.learned[i].name}));
 return mappingCall('mapping_delete',{items},m=>m.count+'개 학습 행을 지웠어요.');
}
function learnedSave() {
 const list=changedRows().map(i=>({vendor:mapping.learned[i].vendor,name:mapping.learned[i].name,
  new_vendor:learnedValue(i,'vendor'),new_name:learnedValue(i,'name'),new_code:learnedValue(i,'code')}));
 return mappingCall('mapping_save',{edits:list},m=>m.count+'개 학습 행을 저장했어요.');
}
function learnedReset() {edits={};deleteArmed=false;refreshLearned();dialogStatus('바꾼 내용을 되돌렸어요.');}
async function answer(action) {
 const j=state.job,q=j?.request;if(!q)return;
 try{await api('respond',{job_id:j.id,request_id:q.id,action:action==='answer'?(['correct_value','edit_rows'].includes(q.kind)?'corrected':'ok'):action,value:q.kind==='edit_rows'?JSON.stringify(editDraft.rows):$('#answerValue')?.value??null});}
 catch(e){if($('#responseError'))$('#responseError').textContent=e.message;else toast(e.message);}
}
const actions={core:()=>navigate('core'),trySamples:()=>start(taskRunes[state.task].samples,{...intent(),dry_run:true,samples:true}),runes:()=>navigate('runes'),run:()=>navigate('run'),result:()=>navigate('result'),
 aiInstall:async a=>{const id=a.dataset.client;a.disabled=true;a.textContent='설치하는 중… 창을 닫지 마세요';
  try{await api('install_client',{client:id});await openAssignments();}catch(err){a.disabled=false;a.textContent='다시 시도';dialogError(err.message);}},
 aiLogin:async a=>{try{await api('client_login',{client:a.dataset.client});toast('새 창에서 로그인해 주세요. 끝나면 [연결 확인] 을 눌러요.');}catch(err){dialogError(err.message);}},
 back:()=>navigate('core'),confirmResult,settings:openSettings,install:()=>installRune(),devMode,openClient,installClient,connectClient,stopConsole,reviewTest,allowTest,confirmInstall,aiOpenCard,aiConnect,aiCheck,aiRecommend,aiDefault,aiDisconnect,aiAdd,openAi:()=>openAssignments().catch(e=>toast(e.message)),installProject,editRune,linkSource,importRecording,attachFile,recordOpen,recordSave,recordTell,recordDiscard,recordAgain,editWith,closeDialog:()=>$('#settingsDialog').close(),connect,
 retry:()=>retry(),retryPick:async()=>{const p=await api('pick');if(p.length)await retry(p);},
 promote,dismissPromote:()=>{state.dismissed.add(state.job.id);render();},
 graphHome,runSearch,todoSnooze,todoDelete,todoRunePick,
 todoRuneOpen:a=>{const id=a.dataset.rune;if(!taskRunes[id]){toast(T('룬 없음'));render();return;}api('todo_rune_opened',{card_id:a.dataset.id,rune_id:id}).catch(()=>{});state.task=id;if(taskRunes[id].locked)state.dryRun=true;navigate('runes');},
 todoRuneUnlink:a=>todoRuneSet(a.dataset.id,''),
 todoUnshare:async a=>{const c=(state.todos||[]).find(x=>x.id===a.dataset.id),to=shareTargets().filter(x=>(c?.shared_with||[]).includes(x));if(!to.length)return;try{state.todos=await api('todo_unshare',{card_id:a.dataset.id,account_ids:to});loadTodos.last=JSON.stringify(state.todos)+ymd(new Date());render();$('[data-action="todoShare"][data-id="'+a.dataset.id+'"]')?.focus();toast('공유를 취소했어요. 받은 쪽 목록에서도 빠져요.');}catch(e){toast(e.message);}},
 todoShare:async a=>{const to=shareTargets();if(!to.length)return;try{state.todos=await api('todo_share',{card_id:a.dataset.id,account_ids:to});loadTodos.last=JSON.stringify(state.todos)+ymd(new Date());render();toast('공유했어요 ('+todoMemberName()+'). 메신저가 연결돼 있으면 바로, 아니면 연결될 때 보내요.');}catch(e){toast(e.message);}},
 noteOpen:async a=>{if(state.note?.card===a.dataset.id)return noteFold();await noteSave();await noteLoad(a.dataset.id,'');},
 noteGo:async a=>{await noteSave();await noteLoad(state.note.card,a.dataset.node,null,a.dataset.dir==='out'||state.note.path.some(p=>p.node===a.dataset.node)?'out':'in');},
 noteClose:()=>noteFold(),
 noteToggle:async a=>{const n=state.note;try{state.note=await api('todo_note_done',{card_id:n.card,item:a.dataset.item,undo:a.getAttribute('aria-checked')==='true'});render();$('[data-action="noteToggle"][data-item="'+a.dataset.item+'"]')?.focus();}catch(e){toast(e.message);}},
 noteDelete:async a=>{const n=state.note,item=a.dataset.item;
  if(state.noteArmed!==item){state.noteArmed=item;a.textContent='한 번 더 누르면 안쪽까지 지워요';setTimeout(()=>{if(state.noteArmed===item){state.noteArmed=null;if(a.isConnected)a.textContent='지우기';}},4000);return;}
  try{state.note=await api('todo_note_delete',{card_id:n.card,item});state.noteArmed=null;render();$('#noteItem')?.focus();}catch(e){toast(e.message);}},
 todoTab:a=>{state.todoTab=a.dataset.tab;render();},
 todoShareRoom:a=>todoSet('todo_share_room',{card_id:a.dataset.id,room:a.dataset.room}),todoUnshareRoom:a=>todoSet('todo_unshare_room',{card_id:a.dataset.id}),archMore:()=>loadArchive(true),archExport,archImport,
 archImported:()=>{const a=archState();a.imported=!a.imported;a.data=null;a.error='';loadArchive();render();},   // 바꾸는 즉시 새 요청 번호로 (회신 2 P2-2)
 importAll:()=>{state.importPick=new Set(state.importPre.items.filter(x=>!x.already).map(x=>x.i));importDialog();},importNone:()=>{state.importPick=new Set();importDialog();},
 todoAsk,todoSuggest,
 todoFoundAll:a=>{const x=archState();Object.assign(x,{start:a.dataset.start,end:a.dataset.end,q:a.dataset.q,imported:false,data:null,error:''});state.todoTab='archive';loadArchive();window.scrollTo(0,0);},   // 새 요청 번호로 바로 — 늦게 온 옛 답은 버린다 (비서 회신 2 P1)
 todoPrivacy:()=>modal(heading('저장·전송 안내')+'<p class="dialog-copy">할 일은 이 PC 에 저장해요. 내용은 암호화하고 날짜·알림 시각은 그대로 저장해요.</p><p class="dialog-copy">연결한 폰에는 할 일을 보내요. [비서에게 묻기]를 누를 때만 적은 말과 열린 할 일의 제목·날짜·알림 시각을 연결한 AI에 보내요. 끝낸 기록은 AI에 보내지 않고 이 PC 에서 찾아 보여 줘요. AI 제안은 버튼을 눌러야 하나씩 바뀌어요.</p>'),
 phoneOpen:()=>{phoneOpen.count=null;return phoneOpen();},todoDone:a=>todoSet('todo_done',{card_id:a.dataset.id,undo:a.getAttribute('aria-checked')==='true'}),resign:openResign,pushCatalog,
 approveFromResult:()=>{state.returnTo=state.job.id;state.task=state.job.rune_id;navigate('runes');openSeal();},
 seal:openSeal,uninstall:openUninstall,toggleRune,confirmUninstall,assignments:openAssignments,marketInstall,marketConfirm,marketPick:a=>{state.marketPick=a.dataset.id;render();},marketShare:a=>openShare(a.dataset.id),marketRefresh:()=>loadListing(true),
 mapping:openMapping,mappingCorrect,deptDelete,bulkApply,bulkDelete,learnedSave,learnedReset,
 cancel:()=>api('cancel',{job_id:state.job.id}),answer:()=>answer('answer'),skip:()=>answer('skip'),abort:()=>answer('abort'),
 editAddRow:()=>{const c=state.job.request.context;editDraft.rows.push(Object.fromEntries(c.columns.map(x=>[x[0],x[0]==='page'&&c.pages?c.pages[0]:''])));render();},
 editDelRow:a=>{editDraft.rows.splice(Number(a.dataset.i),1);render();}};
document.addEventListener('click',async e=>{
 try {
  const a=e.target.closest('[data-action]');if(a){if(a.disabled)return;a.disabled=true;try{await actions[a.dataset.action]?.(a);}finally{a.disabled=false;}return;}
  const pr=e.target.closest('[data-project]');if(pr){state.project=pr.dataset.project;render();return;}
  const star=e.target.closest('[data-run]');if(star){toast(star.title);return;}
  const ring=e.target.closest('[data-ring]');if(ring){ringTo(ring.dataset.ring);return;}
  const friend=e.target.closest('[data-friend]');if(friend){window.VaderMessenger?.show(friend.dataset.friend||null);return;}
  const td=e.target.closest('[data-todo]');if(td){navigate('todo');return;}
  const t=e.target.closest('[data-task]');if(t){state.task=t.dataset.task;if(taskRunes[state.task].locked)state.dryRun=true;render();return;}
  const artifact=e.target.closest('[data-artifact]');if(artifact){await api('open_artifact',{job_id:state.job.id,index:Number(artifact.dataset.artifact)});return;}
  const job=e.target.closest('[data-job]');if(job){state.job=state.jobs.find(j=>j.id===job.dataset.job);navigate(state.job.terminal?'result':'run');return;}
  const pick=e.target.closest('[data-pick-field]');if(pick){const paths=await api('pick',{kind:pick.dataset.kind});if(paths[0])$('#'+pick.dataset.pickField).value=paths[0];}
 }catch(err){toast(err.message);}
});
addEventListener('resize',()=>{if(state.page==='core'){placeGraph();fitGraph();}});
document.addEventListener('submit',e=>{if(e.target.matches('.graph-search')){e.preventDefault();runSearch();}else if(e.target.id==='todoAdd'){e.preventDefault();todoAdd();}
 else if(e.target.id==='noteAdd'){e.preventDefault();const v=$('#noteItem').value.trim(),n=state.note;if(!v||!n)return;
  noteSave().then(()=>api('todo_note_add',{card_id:n.card,node:n.node,title:v})).then(got=>{state.note=got;render();$('#noteItem')?.focus();}).catch(err=>toast(err.message));}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&e.target.id==='graphQ'&&state.lit){e.target.value='';runSearch();}});
document.addEventListener('keydown',e=>{if(!e.target.closest?.('.orbit')||!['ArrowLeft','ArrowRight'].includes(e.key))return;e.preventDefault();spinRing(e.key==='ArrowLeft'?-1:1);});
document.addEventListener('input',e=>{if(e.target.classList.contains('cell'))onLearnedInput(e.target);});
['input','change'].forEach(t=>document.addEventListener(t,e=>{const x=e.target;if(!x.classList.contains('edit-cell'))return;
 editDraft.rows[Number(x.dataset.i)][x.dataset.k]=x.value;const s=$('#editSum');if(s&&state.job?.request){const [text,ok]=editSum(state.job.request.context);s.textContent=text;s.className='edit-sum '+(ok?'ok':'off');}}));
document.addEventListener('click',e=>{const h=e.target.closest('[data-sort]');if(!h)return;
 const key=h.dataset.sort;sortBy=sortBy.key===key?{key,dir:-sortBy.dir}:{key,dir:1};refreshLearned();});
document.addEventListener('change',e=>{
 if(e.target.classList.contains('dept-kind'))deptCall(e.target.dataset.key,e.target.value,'입고 종류를 바꿨어요. 다음 입고부터 묻지 않고 이 부서로 넣습니다.');
 if(e.target.name==='reviewRow')$('#correctCode').value=mapping.review[e.target.value].current_code;
 if(e.target.classList.contains('pick')){e.target.checked?picked.add(Number(e.target.value)):picked.delete(Number(e.target.value));deleteArmed=false;refreshLearned();}
 if(e.target.id==='pickAll'){picked=e.target.checked?new Set(mapping.learned.map((_,i)=>i)):new Set();deleteArmed=false;refreshLearned();}
 if(e.target.id==='dryRun'){state.dryRun=e.target.checked;render();}
 if(e.target.id==='startPage'){state.startPage=Math.max(1,Math.floor(Number(e.target.value)||1));e.target.value=state.startPage;}
});
document.addEventListener('dragover',e=>e.preventDefault());document.addEventListener('drop',e=>e.preventDefault());
$('#aiButton').onclick=()=>openAssignments().catch(e=>toast(e.message));
// ── 앱 로그인: 머리의 버튼이 로그인 상태를 보여 준다. 토큰은 코어(DPAPI)에만, 화면은 이름만 안다 ──
// 로그인은 문(gate.html)에서만. 여기서는 계정을 보고 로그아웃만 — 로그아웃하면 앱이 닫힌다 (2단계, 작업공간은 프로세스마다 하나).
let acct={signed_in:false};
const acctLabel=()=>{const t=acct.account?acct.account.display_name||'로그인됨':acct.signed_in?'로그인 확인 필요':'로그인';$('#accountButton .rail-label').textContent=t;$('#accountButton .rail-label').toggleAttribute('data-raw',!!acct.account?.display_name);$('#accountButton').title=acct.account?VaderI18n.t('{0} · 계정',t):'웹 계정으로 이 PC 를 연결합니다';};
async function refreshAccount() {try{acct=await api('account_status');}catch{acct={signed_in:false};}acctLabel();}
async function openAccount() {
 const a=acct.account;let auto=true;try{auto=(await api('auto_online')).on;}catch{}
 const d=modal(heading('계정')+(a?'<p class="dialog-copy">'+VaderI18n.t('{0} 계정으로 이 PC 가 연결되어 있어요.','<b data-raw>'+esc(a.display_name||VaderI18n.t('이름 없음'))+'</b>')+(a.providers&&a.providers.length?' '+VaderI18n.t('로그인 방법:')+' '+esc(a.providers.map(p=>p==='kakao'?VaderI18n.t('카카오'):'Google').join(' · ')):'')+'</p><p class="small muted">'+VaderI18n.t('자동화 패키지 받기 · {0}',VaderI18n.t(a.catalog_access?'허용됨':'아직 허용되지 않았어요 (운영자에게 요청)'))+'</p>'
  :acct.error?'<p class="dialog-copy">'+esc(acct.error)+'</p>'
  :'<p class="dialog-copy">계정 연결을 확인하지 못했어요. 인터넷 연결을 확인해 주세요.</p>')
  // 자동 온라인 확인 끄기 — 개인정보 처리방침의 '끄는 방법' (SignPath 조건, 2026-11-24)
  +'<p class="dialog-copy"><label><input type="checkbox" id="autoOnline"'+(auto?' checked':'')+'> 자동 온라인 확인</label></p><p class="small muted">켜 두면 앱 새 버전과 광장 목록(회수·만료 안내 포함)을 알아서 확인해요. 끄면 앱 업데이트는 받지 않고, 광장은 새로 고침을 누를 때만 확인해요 — 그 사이 회수된 패키지는 모르고 실행될 수 있어요.</p>'
  // 표시 언어 (2026-11-24 영문판) — 기기 설정에 두고 화면을 다시 그린다. 처음 값은 Windows 표시 언어
  +'<p class="dialog-copy"><label>'+VaderI18n.t('표시 언어')+' <select id="appLang" class="select"><option value="ko" lang="ko">한국어</option><option value="en" lang="en">English</option></select></label></p>'
  +'<p class="small muted">로그아웃하면 앱이 닫혀요. 다른 계정은 앱을 다시 켠 뒤 로그인하세요.</p><p id="dialogError" role="alert"></p><div class="actions"><button type="button" class="button" id="acctWeb">로그인 방법 관리 (웹)</button><button type="button" class="button ghost" id="acctOut">로그아웃</button></div>');
 $('#appLang').value=VaderI18n.lang;
 $('#appLang').onchange=async e=>{try{await api('set_app_lang',{lang:e.currentTarget.value});location.reload();}catch(err){dialogError(err.message);}};
 $('#autoOnline').onchange=async e=>{const box=e.currentTarget;try{box.checked=(await api('set_auto_online',{on:box.checked})).on;}catch(err){box.checked=!box.checked;dialogError(err.message);}};
 $('#acctWeb').onclick=()=>api('account_web').catch(err=>dialogError(err.message));   // 구글·카카오 연결, 계정 합치기
 $('#acctOut').onclick=async e=>{const btn=e.currentTarget;btn.disabled=true;btn.textContent='로그아웃하는 중…';try{await api('account_logout');}catch(err){btn.disabled=false;btn.textContent='로그아웃';dialogError(err.message);}};
}
$('#accountButton').onclick=openAccount;
window.addEventListener('hashchange',()=>navigate(location.hash.slice(1)));
matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change',e=>{state.motion=e.matches;applyMotion();});
window.VaderUI={notice:toast,recordingEnded,resumeGraph,receiveDrop(paths){
 const key=keyDrop;keyDrop=null;
 if(key&&Date.now()-key.at<10000){if(paths.length)key.take(paths[0]);return;}
 const onConsole=consoleDrop;consoleDrop=null;
 if(onConsole&&Date.now()-onConsole.at<10000){if(paths.length)consoleInsert(onConsole.session,quotePaths(paths));return;}
 const drop=pendingDrop;pendingDrop=null;
 if(!drop||Date.now()-drop.at>10000)return;
 if(!paths.length){toast('파일 경로를 읽지 못했어요. 메인 가운데를 눌러 파일을 선택하세요.');return;}
 const {at,...selected}=drop;start(paths,selected);
}};
render();
// 만드는 프로그램 창에서 돌아오면 시험 요청을 다시 본다 — 따로 묻지 않아도 요청이 보이게.
window.addEventListener('focus',async()=>{if(!state.devMode||!state.connected||$('#settingsDialog').open)return;try{catalog(await api('catalog'));render();}catch(e){}});
VaderI18n.ready.then(connect);   // i18n.js 가 코어 연결(pywebviewready)과 언어를 확인한 뒤 한 번
// 화면 자동화 승인 카드 — AI 가 제안한 조작 하나. 글자는 textContent 로만, 승인은 이 창에서만 (codex1 화면 2단계 계약 3)
let screenAsk=null,screenDone=null;
async function watchScreen() {
 try{
  const d=$('#screenDialog'),p=state.connected?await api('screen_pending'):null;
  if(!p||p.id===screenDone){if(d.open&&screenAsk)d.close();screenAsk=null;}
  else{
   if(screenAsk?.id!==p.id){screenAsk=p;
    for(const k of ['app','label','action','value','plan'])d.querySelector('[data-f="'+k+'"]').textContent=p[k]??'';
    d.querySelectorAll('[data-v]').forEach(e=>e.hidden=p.value==null);
    d.querySelectorAll('[data-p]').forEach(e=>e.hidden=p.plan==null);
    if(!d.open)d.showModal();}
   d.querySelector('[data-f="left"]').textContent=p.left+'초 안에 정하지 않으면 버려져요.';
  }
 }catch(e){}
 setTimeout(watchScreen,700);
}
// 누른 동안은 같은 카드를 다시 띄우지 않는다. 앱이 못 받았으면(오류) 숨김을 풀어 — 아직 대기 중이면 카드가 다시 뜬다.
// 다시 보내지는 않는다: 다음 폴링이 그 제안이 아직 대기 중인지부터 본다 (codex1 2단계 r1 P2-3)
async function decideScreen(approve) {
 const p=screenAsk;if(!p)return;screenAsk=null;screenDone=p.id;$('#screenDialog').close();
 try{const r=await api('screen_decide',{id:p.id,digest:p.digest,approve});toast(r.message);}
 catch(err){if(screenDone===p.id)screenDone=null;toast(err.message);}
}
$('#screenApprove').onclick=()=>decideScreen(true);
$('#screenReject').onclick=()=>decideScreen(false);
$('#screenDialog').oncancel=e=>{e.preventDefault();decideScreen(false);};   // Esc = 거절
watchScreen();
// 화면 묶음 계획 — 승인 카드(단계 표는 textContent 로만)와 진행 중 띠([중단])
let planAsk=null,planDone=null;
async function watchPlan() {
 try{
  const d=$('#planDialog'),bar=$('#planBar'),c=state.connected?await api('screen_plan_card'):null;
  const ask=c?.kind==='ask'&&c.id!==planDone?c:null;
  if(!ask){if(d.open&&planAsk)d.close();planAsk=null;}
  else{
   if(planAsk?.id!==ask.id){planAsk=ask;d.querySelector('[data-f="app"]').textContent=T('앱: {0} · {1}분 동안',ask.app,ask.minutes);d.querySelector('[data-f="app"]').dataset.raw='';
    const ol=d.querySelector('.plan-steps');ol.replaceChildren();
    for(const s of ask.steps){const li=document.createElement('li');
     li.dataset.raw='';li.textContent=s.target+' · '+s.action+(s.value!=null?' '+T('· 값: {0}',s.value):'')+' '+T(s.confirm?'· 다시 확인':'· 바로 진행');ol.append(li);}
    if(!d.open)d.showModal();}
   d.querySelector('[data-f="left"]').textContent=ask.left+'초 안에 정하지 않으면 버려져요.';
  }
  bar.hidden=c?.kind!=='active';
  if(c?.kind==='active')bar.querySelector('[data-f="text"]').textContent='화면 묶음 계획 진행 중 · '+c.next+'/'+c.total+' 단계 끝 · 남은 '+Math.ceil(c.left/60)+'분';
 }catch(e){}
 setTimeout(watchPlan,700);
}
async function decidePlan(approve) {
 const a=planAsk;if(!a)return;planAsk=null;planDone=a.id;$('#planDialog').close();
 try{const r=await api('screen_plan_decide',{id:a.id,digest:a.digest,approve});toast(r.message);}
 catch(err){if(planDone===a.id)planDone=null;toast(err.message);}
}
$('#planApprove').onclick=()=>decidePlan(true);
$('#planReject').onclick=()=>decidePlan(false);
$('#planDialog').oncancel=e=>{e.preventDefault();decidePlan(false);};
$('#planStop').onclick=async()=>{try{await api('screen_plan_stop');toast('계획을 멈췄어요. 이미 보낸 단계는 화면에서 확인하세요.');}catch(err){toast(err.message);}};
watchPlan();
