// 메신저 서랍 (3단계 3d). 대화 서랍과 같은 자리를 쓰고, 하나가 열리면 다른 하나는 닫힌다.
// 받은 글은 HTML 로 넣지 않는다 — textContent 로만. 이 창은 Python 다리가 붙은 창이다.
// 화면이 열려(잠기지 않은) 동안 몇 초마다 받아 온다. 잠기면 api() 가 문(gate.html)으로 보낸다.
// 상대를 바꾸면 epoch 가 오른다 — 늦게 온 답(기록·안전 번호·보내기)은 그 상대의 화면일 때만 쓴다 (codex1 3d P1-2).
// 초안은 상대마다 따로 둔다 — A 에게 쓰던 글이 B 에게 가지 않게 (codex1 3d P1-1).
(()=>{
const box=$('#messenger'),stream=$('#msgStream'),form=$('#msgForm'),input=$('#msgInput'),send=$('#msgSend'),title=$('#msgTitle'),line=$('#msgState'),
 back=$('#msgBack'),avatar=$('#msgAvatar'),more=$('#msgMore'),toggle=$('#msgToggle'),badge=$('#msgBadge');
const docked=matchMedia('(min-width:1180px)');
const ms={state:null,handle:null,error:null,friends:[],groups:[],renaming:null,groupsOpen:false,peer:null,history:[],safety:null,found:null,busy:false,armed:null,epoch:0};
const drafts={};
let timer=0,delay=5000,ticks=0,lastPublished='',running=null;
$('#msgClose').innerHTML=icon('close');back.innerHTML=icon('back');send.insertAdjacentHTML('afterbegin',icon('send'));
line.setAttribute('role','status');

// 어디까지 읽었나 — 이 PC·이 계정의 메신저 저장소(코어)에. 화면에 실제로 그린 받은 메시지의 시각까지만 읽음으로 (codex1 3d P2-3).
// 화면 저장소(localStorage)는 앱을 끄면 지워져 다시 켤 때마다 '새 메시지' 로 돌아왔다 (2026-11-09). seen 은 답을 기다리는 동안의 사본.
let seen={};
function loadSeen(){seen={};}
function markShown(peer){
 const at=Math.max(0,...ms.history.filter(m=>!m.outgoing&&m.state==='received').map(m=>m.at));
 if(!at||at<=seenAt(peer))return;seen[peer]=at;api('messenger_seen',{account_id:peer,at}).catch(()=>{});publish();
}
const friend=id=>ms.friends.find(f=>f.account_id===id);
const seenAt=id=>Math.max(seen[id]||0,friend(id)?.seen||0);
const unread=f=>!!f.last&&f.last>Math.max(seen[f.account_id]||0,f.seen||0);
const live=f=>f.state==='accepted'||f.state==='pending';

// 메인 그래프에 알린다 — 친구·상태가 바뀔 때만 다시 그린다. render() 는 포커스를 id 로 되돌린다.
function publish(){
 const fs=ms.friends.filter(live).map(f=>({...f,unread:unread(f)})),n=fs.filter(f=>f.unread||f.state==='pending'&&!f.mine).length;
 badge.hidden=!n;badge.textContent=n?String(n):'';toggle.setAttribute('aria-label',n?'메신저 · 확인할 것 '+n+'개':'메신저');
 const next=JSON.stringify([ms.state==='ready',fs,ms.groups]);if(next===lastPublished)return;lastPublished=next;
 state.friends=fs;state.msgGroups=ms.groups;state.msgReady=ms.state==='ready';
 if(state.page==='core'&&!document.activeElement?.closest?.('.orbit,.execution-options'))render();
}

function open(show,focus=true){
 if(show)window.VaderChat?.close();
 box.hidden=!show;document.body.classList.toggle('msg-open',show);toggle.setAttribute('aria-expanded',String(show));
 if(show){draw();if(focus)focusHere();if(ms.peer)refreshTalk(ms.epoch);else if(ms.state!=='ready')tick();}
 else if(focus)toggle.focus();
 publish();
}
const focusHere=()=>(ms.peer?(input.disabled?stream:input):($('#msgFind')||stream)).focus();
function leave(){if(ms.peer)drafts[ms.peer]=input.value;ms.epoch++;ms.history=[];ms.safety=null;ms.armed=null;}
async function show(id){
 leave();ms.peer=id||null;input.value=id&&drafts[id]||'';
 const ep=ms.epoch;open(true,false);
 if(id)await refreshTalk(ep);
 if(ep===ms.epoch&&!box.hidden)focusHere();
}
// 상태 줄은 [data-raw] — 여기서 한 번만 번역한다. 이름이 낀 문장은 부르는 쪽이 VaderI18n.t 로 만들고 ready=true (codex1 2단계 4차)
function status(text,bad=false,ready=false){line.textContent=ready?text:VaderI18n.word(text);line.classList.toggle('bad',bad);}

// 대화 다시 읽기 — 이 epoch 의 상대일 때만 화면에 쓴다.
async function refreshTalk(ep){
 const peer=ms.peer;if(!peer)return;
 try{
  const h=await api('messenger_history',{account_id:peer});if(ep!==ms.epoch)return;
  ms.history=h;const f=friend(peer);
  if(f?.ready&&!f.verified&&ms.safety?.peer!==peer){const s=await api('messenger_safety',{account_id:peer});if(ep!==ms.epoch)return;ms.safety={peer,number:s.number};}
  draw();if(!box.hidden&&!document.hidden)markShown(peer);
 }catch(e){if(ep===ms.epoch)status(e.message,true);}
}

// ── 그리기 — 찾기 칸의 글·포커스와 읽던 스크롤 자리는 지킨다 (codex1 3d P2-4) ──
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;};
function btn(text,m,cls='button ghost',data={}){const b=el('button',cls,text);b.type='button';b.dataset.m=m;Object.assign(b.dataset,data);return b;}
function draw(){
 const f=ms.peer&&friend(ms.peer),view=ms.peer||'list',same=stream.dataset.view===view;
 const find=$('#msgFind'),findValue=find?.value,findFocus=document.activeElement===find;
 const top=stream.scrollTop,atEnd=stream.scrollHeight-top-stream.clientHeight<48;
 back.hidden=!ms.peer;more.hidden=!(f&&f.state==='accepted');
 title.textContent=f?'@'+f.handle:ms.peer?'대화':'메신저';
 avatar.innerHTML=f?'':icon('people');if(f)avatar.textContent=(f.handle||'?')[0].toUpperCase();
 avatar.classList.toggle('verified',!!f?.verified);
 stream.textContent='';stream.dataset.view=view;
 if(ms.peer)drawTalk(f);else drawList();
 const again=$('#msgFind');if(again&&findValue!=null){again.value=findValue;if(findFocus)again.focus();}
 stream.scrollTop=ms.peer?(!same||atEnd?stream.scrollHeight:top):(same?top:0);
 syncCompose();
}
// 친구 줄 오른쪽의 그룹 고르기 (마스터 2026-11-19) — 목록에서 바로 넣고 뺀다. 바꾸면 바로 저장.
function groupPick(f){
 const sel=el('select','select msg-group-select');sel.dataset.peer=f.account_id;
 sel.setAttribute('aria-label','@'+(f.handle||'')+' 그룹');
 sel.append(new Option('그룹 없음',''));ms.groups.forEach(g=>{const o=new Option(g.name,g.id);o.dataset.raw='';sel.append(o);});
 sel.value=ms.groups.some(g=>g.id===f.group)?f.group:'';
 sel.onchange=async()=>{try{await call('messenger_group_assign',{account_id:f.account_id,gid:sel.value});const g=ms.groups.find(x=>x.id===sel.value);
  await loadFriends();status(g?VaderI18n.t('@{0} 님을 {1} 에 넣었어요',f.handle,g.name):VaderI18n.t('@{0} 님을 그룹에서 뺐어요',f.handle),false,true);$('.msg-group-select[data-peer="'+f.account_id+'"]',stream)?.focus();}catch(e){}};
 return sel;
}
function drawList(){
 stream.removeAttribute('role');form.hidden=true;
 status(ms.state==='ready'?(ms.handle?VaderI18n.t('{0} 으로 대화해요','@'+ms.handle):''):'');
 if(ms.state!=='ready'){stream.append(gateNote());return;}
 const find=el('form','msg-find');find.innerHTML='<label for="msgFind">아이디로 친구 찾기</label><div class="chat-box"><input id="msgFind" autocomplete="off" spellcheck="false" maxlength="21" placeholder="@아이디" aria-describedby="msgFindHelp"><button class="button ghost" type="submit">찾기</button></div><p id="msgFindHelp" class="chat-note">아이디를 정확히 알아야 찾을 수 있어요. 목록이나 자동완성은 없어요.</p>';
 find.onsubmit=e=>{e.preventDefault();findHandle($('#msgFind').value);};
 stream.append(find);
 if(ms.found){const r=el('div','msg-found');r.setAttribute('role','status');
  if(ms.found.found){const known=friend(ms.found.account_id);r.append(el('span','msg-handle','@'+ms.found.handle));
   r.append(known&&live(known)?el('span','muted small',known.state==='accepted'?'이미 친구예요':'요청이 있어요'):btn('친구 요청','request','button primary',{id:ms.found.account_id}));}
  else r.append(el('span','muted','찾지 못했어요. 아이디를 정확히 입력했는지 확인해 주세요.'));
  stream.append(r);}
 // 멤버 그룹 (2026-11-18) — 친구는 자기 그룹 밑에, 그룹 없는 친구는 '친구' 밑에. 그룹은 이 PC 에만 있다.
 const named=new Set(ms.groups.map(g=>g.id)),inGroup=g=>f=>f.state==='accepted'&&f.group===g.id;
 const groups=[['받은 요청',f=>f.state==='pending'&&!f.mine],...ms.groups.map(g=>[g.name,inGroup(g),true]),
  ['친구',f=>f.state==='accepted'&&!named.has(f.group)],['보낸 요청',f=>f.state==='pending'&&f.mine],
  ['지난 대화',f=>f.state==='ended'],['차단',f=>f.state==='blocked']];
 let any=false;
 for(const [label,pick,mine] of groups){const fs=ms.friends.filter(pick);if(!fs.length)continue;any=true;
  // 그룹 이름은 사용자가 지은 것 — 번역하지 않는다
  const head=el('h3','msg-group',label);if(mine)head.dataset.raw='';stream.append(head);const list=el('ul','msg-list');
  fs.forEach(f=>list.append(row(f)));stream.append(list);}
 if(!any)stream.append(el('p','chat-note','아직 친구가 없어요. 상대의 아이디로 찾아 요청을 보내 보세요.'));
 stream.append(groupManager());
}
// 그룹 관리 — 접어 두는 칸. 만들기·이름 바꾸기·지우기(두 번 눌러). 친구를 넣는 곳은 대화 머리의 그룹 고르기.
function groupManager(){
 const d=el('details','msg-groups');d.open=ms.groupsOpen;d.ontoggle=()=>{ms.groupsOpen=d.open;};
 const s=el('summary',null,'그룹 관리');if(ms.groups.length)s.append(el('span','muted',String(ms.groups.length)));d.append(s);
 const add=el('form','msg-group-add');
 add.innerHTML='<label class="sr-only" for="msgGroupNew">새 그룹 이름</label><div class="chat-box"><input id="msgGroupNew" maxlength="20" autocomplete="off" placeholder="새 그룹 이름 (예: 구매팀)"><button class="button ghost" type="submit">만들기</button></div>';
 add.onsubmit=async e=>{e.preventDefault();const v=$('#msgGroupNew').value;if(!v.trim())return;
  try{ms.groups=await call('messenger_group_save',{name:v});ms.groupsOpen=true;draw();status('그룹을 만들었어요. 친구 목록에서 각 친구 오른쪽의 그룹을 고르면 들어가요.');$('#msgGroupNew')?.focus();}catch(e){}};
 d.append(add);
 if(ms.groups.length){const ul=el('ul','msg-list msg-group-list');
  ms.groups.forEach(g=>{const li=el('li','msg-row');
   if(ms.renaming===g.id){const f=el('form','msg-group-rename');
    f.innerHTML='<label class="sr-only" for="msgGroupName">그룹 이름</label><div class="chat-box"><input id="msgGroupName" maxlength="20" autocomplete="off"><button class="button ghost" type="submit">저장</button></div>';
    f.onsubmit=async e=>{e.preventDefault();try{ms.groups=await call('messenger_group_save',{name:$('#msgGroupName').value,gid:g.id});ms.renaming=null;await loadFriends();}catch(e){}};
    li.append(f,btn('취소','groupRenameCancel','text-button'));ul.append(li);
    queueMicrotask(()=>{const i=$('#msgGroupName');if(i&&!i.value){i.value=g.name;i.focus();i.select();}});return;}
   // 그룹을 누르면 소속 친구가 펼쳐진다 (마스터 2026-11-19). 친구마다 [빼기], 아래에 이름 바꾸기·지우기.
   const members=ms.friends.filter(f=>f.group===g.id&&f.state!=='blocked'),dd=el('details','msg-group-item');
   dd.open=ms.groupShown===g.id;dd.ontoggle=()=>{if(dd.open)ms.groupShown=g.id;else if(ms.groupShown===g.id)ms.groupShown=null;};
   const sum=el('summary');const gn=el('span','msg-handle',g.name);gn.dataset.raw='';sum.append(gn,el('small','muted','친구 '+members.length));dd.append(sum);
   const box=el('div','msg-group-body');
   if(members.length){const ml=el('ul','msg-members');
    members.forEach(f=>{const m=el('li');m.append(el('span','friend-orb',(f.handle||'?')[0].toUpperCase()),el('span','msg-handle','@'+(f.handle||'알 수 없음')),btn('빼기','groupKick','text-button',{id:f.account_id,gid:g.id}));ml.append(m);});
    box.append(ml);}
   else box.append(el('p','muted small msg-group-empty','아직 친구가 없어요. 위 친구 목록에서 친구 오른쪽의 그룹을 고르면 들어와요.'));
   const acts=el('div','msg-group-acts');acts.append(btn('이름 바꾸기','groupRename','text-button',{gid:g.id}),btn('지우기','groupDelete','text-button',{gid:g.id}));
   box.append(acts);dd.append(box);li.append(dd);
   ul.append(li);});
  d.append(ul);}
 return d;
}
function row(f){
 const li=el('li','msg-row'),handle='@'+(f.handle||'알 수 없음');
 if(f.state==='accepted'||f.state==='ended'){
  const b=btn('','open','msg-person',{id:f.account_id});b.append(el('span','friend-orb',(f.handle||'?')[0].toUpperCase()),el('span','msg-handle',handle));
  const t=f.state==='ended'?'친구가 아니에요 · 기록만 볼 수 있어요':f.changed?'키 바뀜':!f.ready?'상대가 메신저를 열면 대화할 수 있어요':!f.verified?'안전 번호 확인 필요':unread(f)?'새 메시지':'';
  if(t)b.append(el('small',f.changed?'warn':unread(f)?'new':'muted',t));li.append(b);
  if(f.state==='accepted'&&ms.groups.length)li.append(groupPick(f));return li;}
 li.append(el('span','msg-handle',handle));
 if(f.state==='blocked')li.append(btn('차단 해제','unblock','text-button',{id:f.account_id}));
 else if(!f.mine)li.append(btn('수락','accept','button primary',{id:f.account_id}),btn('거절','remove','button ghost',{id:f.account_id}));
 else li.append(el('small','muted','답을 기다리는 중'),btn('취소','remove','text-button',{id:f.account_id}));
 return li;
}
function gateNote(){
 const p=el('div','chat-gate msg-gate');
 p.append(el('span',null,{not_joined:'메신저를 쓰려면 웹 계정 화면에서 만 14세 이상 확인 후 @아이디를 정해 주세요. 정한 뒤 아래 \'다시 확인\' 을 누르세요.',
  other_pc:'다른 PC 에서 이 계정의 메신저를 쓰고 있어요. 이 PC 에서 쓰려면 웹 계정 화면에서 그 PC 의 메신저를 해제해 주세요.',
  off:(ms.error?ms.error+' ':'')+'로그인과 인터넷 연결을 확인해 주세요.'}[ms.state]||'메신저에 연결하는 중…'));
 if(ms.state&&ms.state!=='ready')p.append(btn('다시 확인','retry'));
 return p;
}
function drawTalk(f){
 stream.setAttribute('role','log');form.hidden=false;
 if(!f){stream.append(el('p','chat-note','이 대화는 더 이상 열 수 없어요.'));status('');return;}
 status(f.state!=='accepted'?'친구가 아니에요 · 기록만 볼 수 있어요':!f.ready?'상대가 아직 메신저를 열지 않았어요':f.verified?'안전 번호 확인됨':f.changed?'상대의 키가 바뀌었어요':'안전 번호를 확인해 주세요',f.changed);
 const showSafety=f.state==='accepted'&&f.ready&&(!f.verified||ms.safety?.open);
 if(f.state==='accepted'&&f.verified&&!showSafety)stream.append(btn('안전 번호 다시 보기','safety','text-button msg-safety-link'));
 if(showSafety)stream.append(safetyPanel(f));
 if(!ms.history.length&&f.verified)stream.append(el('p','chat-note msg-empty','아직 주고받은 메시지가 없어요.'));
 ms.history.forEach(m=>stream.append(bubble(m)));
}
function safetyPanel(f){
 const s=el('section','msg-safety'+(f.changed?' changed':''));s.setAttribute('aria-labelledby','safetyTitle');
 const h=el('h3',null);h.id='safetyTitle';h.innerHTML=icon('shield');
 h.append(f.verified?'안전 번호':f.changed?'안전 번호를 다시 확인해 주세요':'처음 대화하기 전에 안전 번호를 확인해요');s.append(h);
 if(f.changed){const w=el('p','warn','@'+f.handle+' 님의 키가 바뀌었어요. PC 를 바꿨을 수 있어요. 다시 확인하기 전에는 보내지 않아요.');w.setAttribute('role','alert');s.append(w);}
 s.append(el('p',null,'@'+f.handle+' 님 화면에도 같은 숫자가 보이는지 전화나 직접 만나서 확인하세요. 서버를 거치지 않는 방법이어야 해요.'));
 const number=ms.safety?.peer===f.account_id?ms.safety.number:null;
 const grid=el('div','safety-grid');grid.setAttribute('aria-label','안전 번호');
 if(number)number.split(' ').forEach(g=>grid.append(el('span',null,g)));else grid.append(el('span','muted safety-wait','번호를 만드는 중…'));
 s.append(grid);
 if(!f.verified){const b=btn('번호가 같아요','verify','button primary',{id:f.account_id,number:number||''});b.disabled=!number;s.append(b);}
 else s.append(btn('닫기','safetyClose','text-button'));
 return s;
}
const STATES={queued:'보내는 중',failed:'보내지 못했어요',unreadable:'읽을 수 없는 메시지'};
// 할 일 공유 글(코어 todo.SHARE_MARK)은 말풍선에 '할 일' 로 — 글자 그대로 보이지 않게.
const SHARE='\u2063vader-todo:';
function shareText(m){
 if(!m.text.startsWith(SHARE))return m.text;
 try{const s=JSON.parse(m.text.slice(SHARE.length));
  return s.op==='share'?VaderI18n.t(m.outgoing?'할 일을 공유했어요 ·':'할 일을 공유받았어요 ·')+' '+s.title:VaderI18n.t(s.undo?'공유한 할 일을 다시 열었어요':'공유한 할 일을 끝냈어요');}
 catch{return '할 일 공유';}
}
function bubble(m){
 const d=el('div','msg '+(m.outgoing?'me':'ai')+(m.state==='failed'?' failed':'')+(m.state==='unreadable'?' unreadable':''));
 const body=el('div','msg-body');body.append(el('span','sr-only',m.outgoing?'나: ':'상대: '));
 const said=el('span');said.dataset.raw='';said.append(m.text==null?STATES.unreadable:shareText(m));body.append(m.text==null?STATES.unreadable:said);d.append(body);if(m.text?.startsWith(SHARE))d.classList.add('msg-todo');
 const note=STATES[m.state]&&m.text!=null?STATES[m.state]:!m.outgoing&&m.state==='received'&&!m.trusted?'안전 번호 확인 전에 받음':'';
 d.append(el('small','msg-meta',(note?note+' · ':'')+new Date(m.at*1000).toLocaleTimeString(VaderI18n.lang==='en'?'en-US':'ko-KR',{hour:'2-digit',minute:'2-digit'})));
 return d;
}
function syncCompose(){
 const f=ms.peer&&friend(ms.peer),ok=!!f&&f.state==='accepted'&&f.verified&&!ms.busy;
 input.disabled=!ok;send.disabled=!ok;
 input.placeholder=!f||f.state!=='accepted'?'':!f.verified?'안전 번호 확인 후 보낼 수 있어요':'메시지';
}
function merge(m){const i=ms.history.findIndex(x=>x.id===m.id);if(i<0)ms.history.push(m);else ms.history[i]=m;}

// ── 동작 ──
async function call(method,args){try{return await api(method,args);}catch(e){status(e.message,true);throw e;}}
// 친구 목록을 새로 받는다. 열린 대화 상대의 관계·신원이 바뀌었으면 번호를 버리고 대화를 다시 그린다 (codex1 3d P2-1).
async function loadFriends(list){
 const key=f=>f?[f.state,f.ready,f.verified,f.changed].join():'';
 const before=key(ms.peer&&friend(ms.peer));
 ms.friends=list||await api('messenger_friends');ms.groups=await api('messenger_groups').catch(()=>ms.groups);publish();
 if(box.hidden)return;
 if(!ms.peer)return draw();
 if(key(friend(ms.peer))!==before){ms.safety=null;await refreshTalk(ms.epoch);}
}
async function findHandle(h){
 h=String(h).trim();if(!h)return;
 try{ms.found=await call('messenger_find',{handle:h});draw();($('[data-m="request"]',stream)||$('#msgFind')).focus();}catch(e){}
}
async function act(b){
 const m=b.dataset.m,id=b.dataset.id||ms.peer;
 if(m==='open')return show(id);
 if(m==='groupKick'){const f=friend(b.dataset.id);try{await call('messenger_group_assign',{account_id:b.dataset.id,gid:''});ms.groupShown=b.dataset.gid;await loadFriends();status(VaderI18n.t('@{0} 님을 그룹에서 뺐어요',f?.handle||''),false,true);}catch(e){}return;}
 if(m==='groupRename'){ms.renaming=b.dataset.gid;return draw();}
 if(m==='groupRenameCancel'){ms.renaming=null;return draw();}
 if(m==='groupDelete'){const g=ms.groups.find(x=>x.id===b.dataset.gid);if(!g)return;
  if(ms.armed!=='del:'+g.id){ms.armed='del:'+g.id;b.textContent='한 번 더 누르면 지워요';setTimeout(()=>{if(ms.armed==='del:'+g.id){ms.armed=null;b.textContent='지우기';}},4000);return;}
  ms.armed=null;try{ms.groups=await call('messenger_group_delete',{gid:g.id});await loadFriends();status(VaderI18n.t('{0} 그룹을 지웠어요. 친구 관계는 그대로예요.',g.name),false,true);}catch(e){}return;}
 if(m==='retry'){ms.state=null;ms.error=null;draw();return tick();}
 if(m==='safety'){const peer=ms.peer,ep=ms.epoch;try{const s=await api('messenger_safety',{account_id:peer});if(ep===ms.epoch){ms.safety={peer,number:s.number,open:true};draw();}}catch(e){status(e.message,true);}return;}
 if(m==='safetyClose'){ms.safety=null;return draw();}
 if(m==='verify'){
  // 화면에 그린 그 상대·그 번호로만 확인한다
  if(!b.dataset.number||id!==ms.peer)return;
  try{await call('messenger_verify',{account_id:id,number:b.dataset.number});ms.safety=null;await loadFriends();if(id===ms.peer){draw();input.focus();status('안전 번호를 확인했어요. 이제 보낼 수 있어요.');}}
  catch(e){ms.safety=null;await loadFriends().catch(()=>{});await refreshTalk(ms.epoch);}
  return;}
 if(m==='remove'||m==='block'){
  const f=friend(id),inTalk=!!b.closest('#msgMore');
  if(inTalk&&ms.armed!==m){ms.armed=m;b.textContent=m==='block'?'한 번 더 누르면 차단 (목록에서 풀 수 있어요)':'한 번 더 누르면 끊어요';
   setTimeout(()=>{if(ms.armed===m){ms.armed=null;b.textContent=m==='block'?'차단':'친구 끊기';}},4000);return;}
  ms.armed=null;
  try{const list=await call(m==='block'?'messenger_block':'messenger_friend',{account_id:id,op:m==='block'?'block':'remove'});
   if(ms.peer===id){leave();ms.peer=null;}await loadFriends(list);draw();status(m==='block'?'@'+(f?.handle||'')+' 님을 차단했어요. 목록의 차단에서 풀 수 있어요.':'친구를 끊었어요');}catch(e){}
  return;}
 if(m==='unblock'){try{await loadFriends(await call('messenger_block',{account_id:id,op:'unblock'}));draw();status('차단을 풀었어요. 다시 친구가 되려면 요청을 보내세요.');}catch(e){}return;}
 if(m==='request'||m==='accept'){
  try{const list=await call('messenger_friend',{account_id:id,op:m});ms.found=null;await loadFriends(list);draw();status(m==='request'?'요청을 보냈어요':'친구가 됐어요');}catch(e){}
 }
}
async function say(){
 const peer=ms.peer,ep=ms.epoch,text=input.value.trim();if(!text||ms.busy||!peer)return;
 ms.busy=true;syncCompose();
 try{const m=await api('messenger_send',{account_id:peer,text});
  drafts[peer]='';
  if(ep===ms.epoch){if(input.value.trim()===text)input.value='';merge(m);draw();input.focus();}
 }catch(e){if(ep===ms.epoch)status(e.message,true);}
 finally{ms.busy=false;syncCompose();}
}

// ── 받아 오기: 한 번에 하나, 5초마다, 실패하면 1분까지 늘린다 (codex1 3d P2-2) ──
function tick(){
 if(running)return running;
 clearTimeout(timer);
 running=(async()=>{
  if(document.hidden||window.vaderTray)return;   // 트레이에 숨은 동안은 코어가 받아 알린다 (host._watch_messenger)
  try{
   if(ms.state!=='ready'){
    const c=await api('messenger_connect');ms.state=c.state;ms.error=null;
    if(ms.handle!==(c.handle||null)){ms.handle=c.handle||null;loadSeen();}
    if(c.state==='ready')await loadFriends();else publish();
    if(!box.hidden&&!ms.peer)draw();
   }
   if(ms.state==='ready'){
    const r=await api('messenger_sync');ticks++;
    if(r.received||ticks%6===0)await loadFriends();
    if(ms.peer&&!box.hidden&&(r.received||ms.history.some(m=>m.state==='queued')))await refreshTalk(ms.epoch);
   }
   delay=5000;
  }catch(e){
   delay=Math.min(delay*2,60000);ms.error=e.message;
   // 연결된 줄 알았는데 실패했다 — 다음 번에 connect 부터 (다시 묶기·해제 감지). 기기를 지우는 판단은 코어가 서버 상태로만 한다.
   ms.state=ms.state==='ready'?null:'off';publish();
   if(!box.hidden){if(ms.peer)status('메신저에 연결하지 못했어요 · '+e.message,true);else draw();}
  }
 })().finally(()=>{running=null;timer=setTimeout(tick,ms.state==='ready'?delay:ms.state==null?Math.min(delay,15000):60000);});
 return running;
}

toggle.onclick=()=>open(box.hidden);
$('#msgClose').onclick=()=>open(false);
back.onclick=()=>{leave();ms.peer=null;input.value='';draw();($('#msgFind')||stream).focus();};
box.addEventListener('click',e=>{const b=e.target.closest('[data-m]');if(!b||b.disabled)return;b.disabled=true;Promise.resolve(act(b)).finally(()=>{b.disabled=false;syncCompose();});});
form.onsubmit=e=>{e.preventDefault();say();};
input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();say();}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!box.hidden&&!docked.matches&&!$('#settingsDialog').open){e.preventDefault();open(false,box.contains(document.activeElement));}});
box.addEventListener('focusout',e=>{const to=e.relatedTarget;if(!docked.matches&&to&&!box.contains(to)&&to!==toggle&&!to.closest?.('[data-friend]'))open(false,false);});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){tick();if(ms.peer&&!box.hidden)refreshTalk(ms.epoch);}});
const start=()=>tick();
VaderI18n.ready.then(start);
// 트레이에서 다시 열면: 숨은 동안 코어가 받아 둔 메시지를 친구 목록·대화에 바로 보인다
const wake=async()=>{await tick();if(ms.state==='ready')await loadFriends().catch(()=>{});if(ms.peer&&!box.hidden)refreshTalk(ms.epoch);};
window.VaderMessenger={show,wake,close:()=>{if(!box.hidden)open(false,false);}};
})();
