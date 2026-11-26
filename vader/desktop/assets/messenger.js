// 메신저 서랍 (3단계 3d). 대화 서랍과 같은 자리를 쓰고, 하나가 열리면 다른 하나는 닫힌다.
// 받은 글은 HTML 로 넣지 않는다 — textContent 로만. 이 창은 Python 다리가 붙은 창이다.
// 화면이 열려(잠기지 않은) 동안 몇 초마다 받아 온다. 잠기면 api() 가 문(gate.html)으로 보낸다.
// 상대를 바꾸면 epoch 가 오른다 — 늦게 온 답(기록·안전 번호·보내기)은 그 상대의 화면일 때만 쓴다 (codex1 3d P1-2).
// 초안은 상대마다 따로 둔다 — A 에게 쓰던 글이 B 에게 가지 않게 (codex1 3d P1-1).
(()=>{
const box=$('#messenger'),stream=$('#msgStream'),form=$('#msgForm'),input=$('#msgInput'),send=$('#msgSend'),title=$('#msgTitle'),line=$('#msgState'),
 back=$('#msgBack'),avatar=$('#msgAvatar'),more=$('#msgMore'),toggle=$('#msgToggle'),badge=$('#msgBadge');
const docked=matchMedia('(min-width:1180px)');
const ms={state:null,handle:null,error:null,friends:[],groups:[],renaming:null,groupsOpen:false,peer:null,history:[],safety:null,found:null,busy:false,armed:null,epoch:0,
 rooms:[],room:null,making:false,pick:new Set(),membersOpen:false};   // 그룹 채팅방 (2026-11-26) — room 이 있으면 방 대화
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
 const fs=ms.friends.filter(live).map(f=>({...f,unread:unread(f)})),n=fs.filter(f=>f.unread||f.state==='pending'&&!f.mine).length+ms.rooms.filter(r=>r.unread||r.state==='invited').length;
 state.msgRooms=ms.rooms.filter(r=>r.state==='joined');
 badge.hidden=!n;badge.textContent=n?String(n):'';toggle.setAttribute('aria-label',n?'메신저 · 확인할 것 '+n+'개':'메신저');
 const next=JSON.stringify([ms.state==='ready',fs,ms.groups,state.msgRooms]);if(next===lastPublished)return;lastPublished=next;
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
const focusHere=()=>(ms.peer||ms.room?(input.disabled?stream:input):($('#msgFind')||stream)).focus();
const draftKey=()=>ms.room?'room:'+ms.room:ms.peer;
function leave(){if(ms.shots?.length)dropShots();photoCache.clear();if(draftKey())drafts[draftKey()]=input.value;ms.epoch++;ms.history=[];ms.safety=null;ms.armed=null;}
const roomOf=id=>ms.rooms.find(r=>r.id===id);
async function showRoom(id){
 leave();ms.peer=null;ms.room=id;input.value=drafts['room:'+id]||'';
 const ep=ms.epoch;open(true,false);await refreshTalk(ep);
 if(ep===ms.epoch&&!box.hidden)focusHere();
}
async function show(id){
 leave();ms.room=null;ms.peer=id||null;input.value=id&&drafts[id]||'';
 const ep=ms.epoch;open(true,false);
 if(id)await refreshTalk(ep);
 if(ep===ms.epoch&&!box.hidden)focusHere();
}
// 상태 줄은 [data-raw] — 여기서 한 번만 번역한다. 이름이 낀 문장은 부르는 쪽이 VaderI18n.t 로 만들고 ready=true (codex1 2단계 4차)
function status(text,bad=false,ready=false){line.textContent=ready?text:VaderI18n.word(text);line.classList.toggle('bad',bad);}

// 대화 다시 읽기 — 이 epoch 의 상대일 때만 화면에 쓴다.
async function refreshTalk(ep){
 if(ms.room){const room=ms.room;
  try{const h=await api('messenger_room_history',{room});if(ep!==ms.epoch)return;ms.history=h;draw();
   if(!box.hidden&&!document.hidden&&h.some(m=>!m.outgoing))api('messenger_room_read',{room}).then(moved=>{if(moved)loadRooms();}).catch(()=>{});}
  catch(e){if(ep===ms.epoch)status(e.message,true);}
  return;}
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
 const f=ms.peer&&friend(ms.peer),r=ms.room&&roomOf(ms.room),view=ms.room?'room:'+ms.room:ms.peer||'list',same=stream.dataset.view===view;
 const find=$('#msgFind'),findValue=find?.value,findFocus=document.activeElement===find;
 const top=stream.scrollTop,atEnd=stream.scrollHeight-top-stream.clientHeight<48;
 back.hidden=!(ms.peer||ms.room);more.hidden=!(f&&f.state==='accepted');
 title.textContent=r?r.name:f?'@'+f.handle:ms.peer||ms.room?'대화':'메신저';
 if(r)title.dataset.raw='';else delete title.dataset.raw;
 avatar.innerHTML=f?'':icon('people');if(f)avatar.textContent=(f.handle||'?')[0].toUpperCase();
 avatar.classList.toggle('verified',!!f?.verified);
 stream.textContent='';stream.dataset.view=view;
 if(ms.room)drawRoom(r);else if(ms.peer)drawTalk(f);else drawList();
 const again=$('#msgFind');if(again&&findValue!=null){again.value=findValue;if(findFocus)again.focus();}
 stream.scrollTop=ms.peer||ms.room?(!same||atEnd?stream.scrollHeight:top):(same?top:0);
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
 stream.append(roomSection());
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
// ── 그룹 채팅방 (2026-11-26) — 방 초대만으로 대화. 확인 안 된 멤버는 경고만, 방 이름은 이 PC 에만 ──
function roomSection(){
 const wrap=el('section','msg-rooms'),head=el('div','msg-rooms-head');
 head.append(el('h3','msg-group','방'),btn(ms.making?'닫기':'방 만들기','roomNew','text-button'));wrap.append(head);
 if(ms.making)wrap.append(roomForm());
 const rs=ms.rooms.filter(r=>r.state!=='ended'||r.last);
 if(!rs.length&&!ms.making){wrap.append(el('p','chat-note','친구 여럿과 함께 이야기하려면 방을 만들어요.'));return wrap;}
 const ul=el('ul','msg-list');
 rs.forEach(r=>{const li=el('li','msg-row');
  if(r.state==='invited'){const who=el('span','msg-handle',VaderI18n.t('@{0} 님의 방 초대',r.invited_by_handle||''));who.dataset.raw='';
   li.append(who,el('small','muted',VaderI18n.t('{0}명',r.joined)),btn('수락','roomAccept','button primary',{room:r.id}),btn('거절','roomDecline','button ghost',{room:r.id}));ul.append(li);return;}
  const b=btn('','roomOpen','msg-person',{room:r.id}),name=el('span','msg-handle',r.name);name.dataset.raw='';
  b.append(el('span','friend-orb room-orb',String(r.members.filter(x=>x.state==='joined').length)),name);
  const t=r.state==='ended'?'나온 방 · 기록만 볼 수 있어요':r.unread?VaderI18n.t('새 메시지 {0}',r.unread):'';
  if(t)b.append(el('small',r.unread?'new':'muted',t));li.append(b);ul.append(li);});
 wrap.append(ul);return wrap;
}
function roomForm(){
 const f=el('form','msg-room-new'),fs=ms.friends.filter(x=>x.state==='accepted');
 f.append(el('p','chat-note','초대할 친구를 고르세요. 방에 들어온 멤버끼리는 친구가 아니어도 이야기할 수 있어요.'));
 if(!fs.length){f.append(el('p','muted small','먼저 친구를 추가해 주세요.'));return f;}
 const ul=el('ul','msg-room-pick');
 fs.forEach(x=>{const li=el('li'),l=el('label'),c=document.createElement('input');c.type='checkbox';c.value=x.account_id;c.checked=ms.pick.has(x.account_id);
  c.onchange=()=>{c.checked?ms.pick.add(x.account_id):ms.pick.delete(x.account_id);const go=$('[data-m="roomCreate"]',f);if(go)go.disabled=!ms.pick.size;};
  l.append(c,el('span','msg-handle','@'+(x.handle||'')));li.append(l);ul.append(li);});
 f.append(ul);
 const n=el('div','chat-box');n.innerHTML='<label class="sr-only" for="roomName">방 이름</label><input id="roomName" maxlength="30" autocomplete="off" placeholder="방 이름 (이 PC 에만 보여요 · 비우면 멤버 이름)">';
 const go=btn('방 만들기','roomCreate','button primary');go.disabled=!ms.pick.size;n.append(go);f.append(n);
 f.onsubmit=e=>{e.preventDefault();go.click();};
 return f;
}
function drawRoom(r){
 stream.setAttribute('role','log');form.hidden=false;
 if(!r){stream.append(el('p','chat-note','이 방은 더 이상 열 수 없어요.'));status('');return;}
 const joined=r.members.filter(x=>x.state==='joined'),unchecked=joined.filter(x=>!x.me&&!x.verified),old=joined.filter(x=>!x.me&&!x.ready);
 status(r.state!=='joined'?VaderI18n.word('나온 방이에요 · 기록만 볼 수 있어요'):VaderI18n.t('{0}명',joined.length)+(unchecked.length?' · '+VaderI18n.t('안전 번호 확인 안 된 멤버 {0}',unchecked.length):''),!!unchecked.length,true);
 if(r.state==='joined')stream.append(roomPanel(r));
 if(r.state==='joined'&&unchecked.length){const w=el('p','msg-room-warn','안전 번호를 확인하지 않은 멤버가 있어요. 방 초대만으로 대화하므로 민감한 내용은 확인한 뒤에 보내세요.');stream.append(w);}
 if(r.state==='joined'&&old.length)stream.append(el('p','chat-note',VaderI18n.t('{0}명은 앱을 업데이트해야 이 방 글을 받아요',old.length)));
 if(!ms.history.length)stream.append(el('p','chat-note msg-empty','아직 주고받은 메시지가 없어요.'));
 ms.history.forEach(m=>stream.append(bubble(m,true)));
}
function roomPanel(r){
 const d=el('details','msg-room-panel');d.open=ms.membersOpen;d.ontoggle=()=>{ms.membersOpen=d.open;};
 const joined=r.members.filter(x=>x.state==='joined');
 d.append(el('summary',null,VaderI18n.t('멤버 {0} · 방 설정',joined.length)));
 const ul=el('ul','msg-members');
 r.members.forEach(x=>{const li=el('li');li.append(el('span','friend-orb',(x.handle||'?')[0].toUpperCase()),el('span','msg-handle','@'+(x.handle||'알 수 없음')));
  const tags=[x.me?'나':'',x.account_id===r.owner?'방장':'',x.state==='invited'?'초대 중':!x.me&&!x.verified?'확인 안 됨':''].filter(Boolean);
  if(tags.length)li.append(el('small',tags.includes('확인 안 됨')?'warn':'muted',tags.map(t=>VaderI18n.word(t)).join(' · ')));
  if(r.mine&&!x.me)li.append(btn('내보내기','roomKick','text-button',{room:r.id,id:x.account_id}));
  ul.append(li);});
 d.append(ul);
 const rn=el('form','msg-room-rename');
 rn.innerHTML='<label class="sr-only" for="roomRename">방 이름</label><div class="chat-box"><input id="roomRename" maxlength="30" autocomplete="off" placeholder="방 이름"><button class="button ghost" type="submit">이름 저장</button></div><p class="chat-note">이름은 이 PC 에만 보여요. 메신저를 다른 PC 로 옮기면 멤버 이름으로 돌아가요.</p>';
 queueMicrotask(()=>{const i=$('#roomRename');if(i&&!i.value&&r.named)i.value=r.name;});
 rn.onsubmit=async e=>{e.preventDefault();try{ms.rooms=await call('messenger_room_rename',{room:r.id,name:$('#roomRename').value});publish();draw();status('방 이름을 바꿨어요');}catch(e){}};
 d.append(rn);
 if(r.mine){const out=ms.friends.filter(f=>f.state==='accepted'&&!r.members.some(x=>x.account_id===f.account_id));
  if(out.length){const inv=el('div','chat-box msg-room-invite'),sel=el('select','select');sel.id='roomInvitePick';sel.setAttribute('aria-label','초대할 친구');
   out.forEach(f=>sel.append(new Option('@'+(f.handle||''),f.account_id)));inv.append(sel,btn('초대','roomInvite','button ghost',{room:r.id}));d.append(inv);}}
 d.append(btn('방 나가기','roomLeave','text-button danger',{room:r.id}));
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
  other_pc:'다른 PC 에서 이 계정의 메신저를 쓰고 있었어요. 이 PC 로 옮기면 그 PC 의 지난 대화는 이 PC 로 오지 않고, 아직 받지 못한 메시지는 사라져요. 친구들은 안전 번호를 다시 확인해야 해요.',
  off:(ms.error?ms.error+' ':'')+'로그인과 인터넷 연결을 확인해 주세요.'}[ms.state]||'메신저에 연결하는 중…'));
 if(ms.state==='other_pc')p.append(btn('이 PC 로 메신저 옮기기','move','button primary'));   // 안내를 읽고 두 번 눌러 확인 (2026-11-25)
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
// 사진 (2026-11-25) — 받은 사진의 상태. ok 만 그린다. 그림은 코어가 다시 만든 JPEG(data URL)만 받는다.
const PHOTO={waiting:'사진 받는 중…',expired:'기한이 지나 받지 못한 사진',broken:'손상돼 열 수 없는 사진',unchecked:'안전 번호 확인 전에 받은 사진은 열지 않아요'};
const photoCache=new Map();
function photo(m){
 const f=el('figure','msg-photo-frame');
 if(m.image.state!=='ok'){f.append(el('span','msg-photo-note',PHOTO[m.image.state]||PHOTO.broken));return f;}
 const img=document.createElement('img');img.alt=m.outgoing?'보낸 사진':'받은 사진';
 if(m.image.w&&m.image.h)img.style.aspectRatio=m.image.w+' / '+m.image.h;
 const show=src=>{img.src=src;img.onclick=()=>viewPhoto(src,img.alt);};
 if(photoCache.has(m.id))show(photoCache.get(m.id));
 else api('messenger_image',{message_id:m.id}).then(r=>{photoCache.set(m.id,r.src);if(photoCache.size>40)photoCache.delete(photoCache.keys().next().value);show(r.src);}).catch(()=>{f.replaceChildren(el('span','msg-photo-note',PHOTO.waiting));});
 img.tabIndex=0;img.onkeydown=e=>{if(e.key==='Enter'&&img.src)viewPhoto(img.src,img.alt);};
 f.append(img);return f;
}
function viewPhoto(src,alt){
 const v=el('div','msg-viewer');v.setAttribute('role','dialog');v.setAttribute('aria-label',alt);
 const img=document.createElement('img');img.src=src;img.alt=alt;v.append(img,el('span','msg-viewer-hint','누르거나 Esc 로 닫기'));
 const close=()=>{v.remove();document.removeEventListener('keydown',esc);};const esc=e=>{if(e.key==='Escape')close();};
 v.onclick=close;document.addEventListener('keydown',esc);document.body.append(v);
}
// 보내기 전: 코어가 다시 만든 미리보기만 보인다(원본은 화면에 안 남김). 확인한 사진만 보낸다.
const shots=$('#msgShots'),photoBtn=$('#msgPhoto'),fileIn=$('#msgFile');
ms.shots=[];
const PHOTO_MAX=20*1024*1024,SHOTS_MAX=10;
function canPhoto(){if(ms.room)return roomOf(ms.room)?.state==='joined'&&!ms.busy;const f=ms.peer&&friend(ms.peer);return !!f&&f.state==='accepted'&&f.verified&&!ms.busy;}
async function addPhotos(files){
 const f=ms.peer&&friend(ms.peer);
 if(!canPhoto())return status(ms.room?'들어가 있는 방에만 보낼 수 있어요':!f||f.state!=='accepted'?'친구에게만 보낼 수 있어요':'안전 번호를 확인한 뒤에 보낼 수 있어요',true);
 if(!ms.room&&!f.images)return status('상대가 아직 사진을 받을 수 없는 버전이에요. 상대가 앱을 업데이트하면 보낼 수 있어요.',true);
 const peer=draftKey();
 for(const file of [...files]){
  if(ms.shots.length>=SHOTS_MAX){status(VaderI18n.t('사진은 한 번에 {0}장까지 보낼 수 있어요',SHOTS_MAX),true);break;}
  if(!['image/jpeg','image/png'].includes(file.type)){status('JPG·PNG 사진만 보낼 수 있어요.',true);continue;}
  if(file.size>PHOTO_MAX){status('사진은 20MB 까지 보낼 수 있어요.',true);continue;}
  try{
   const data=await new Promise((ok,bad)=>{const r=new FileReader();r.onload=()=>ok(String(r.result).split(',',2)[1]||'');r.onerror=()=>bad(new Error('사진을 읽지 못했어요.'));r.readAsDataURL(file);});
   const p=await api('messenger_prepare_image',{data});
   if(draftKey()!==peer){api('messenger_drop_image',{token:p.token}).catch(()=>{});return;}
   ms.shots.push(p);drawShots();
  }catch(e){status(e.message,true);}
 }
}
function dropShots(){for(const s of ms.shots)api('messenger_drop_image',{token:s.token}).catch(()=>{});ms.shots=[];drawShots();}
function drawShots(){
 shots.hidden=!ms.shots.length;shots.replaceChildren();if(!ms.shots.length)return;
 const row=el('div','msg-shot-row');
 ms.shots.forEach((s,i)=>{const t=el('div','msg-shot');const img=document.createElement('img');img.src=s.preview;img.alt=VaderI18n.t('보낼 사진 {0}',i+1);
  const x=btn('×','shotRemove','msg-shot-x',{i:String(i)});x.setAttribute('aria-label',VaderI18n.t('사진 {0} 빼기',i+1));t.append(img,x);row.append(t);});
 const f=friend(ms.peer),r=ms.room&&roomOf(ms.room);
 const label=r?VaderI18n.t('{0} 에 사진 {1}장 보내기',r.name,ms.shots.length):VaderI18n.t('@{0} 님에게 사진 {1}장 보내기',f?.handle||'',ms.shots.length);
 shots.append(row,el('p','msg-shot-warn','사진 속 환자 이름·등록번호·얼굴은 가려지지 않아요. 기관에서 허용한 자료만 보내세요.'),
  (()=>{const a=el('div','msg-shot-acts'),go=btn(label,'shotSend','button primary');if(r)go.dataset.raw='';a.append(go,btn('취소','shotCancel','button ghost'));return a;})());
}
async function sendShots(){
 const peer=ms.peer,room=ms.room,ep=ms.epoch;if(!(peer||room)||ms.busy||!ms.shots.length)return;
 ms.busy=true;syncCompose();
 // 응답을 기다리는 사이 다른 대화로 갔으면 그 결과를 지금 대화에 넣지 않는다 (codex1 사진 회신 2 P1-3)
 try{while(ms.shots.length&&ep===ms.epoch){const s=ms.shots[0];
   const m=await api(room?'messenger_room_send_image':'messenger_send_image',room?{room,token:s.token}:{account_id:peer,token:s.token});
   ms.shots=ms.shots.filter(x=>x.token!==s.token);if(ep!==ms.epoch)break;merge(m);drawShots();draw();}}
 catch(e){if(ep===ms.epoch)status(e.message,true);}
 finally{ms.busy=false;syncCompose();drawShots();}
}
photoBtn.innerHTML=icon('image');
photoBtn.onclick=()=>{if(canPhoto())fileIn.click();else addPhotos([]);};
fileIn.onchange=()=>{const files=[...fileIn.files];fileIn.value='';addPhotos(files);};
// 끌어다 놓기는 대화창 안에서만 — 룬 실행용 드롭(메인)과 섞이지 않게 여기서 멈춘다
const hasFiles=e=>[...(e.dataTransfer?.types||[])].includes('Files');
box.addEventListener('dragover',e=>{if(!(ms.peer||ms.room)||!hasFiles(e))return;e.preventDefault();e.stopPropagation();box.classList.add('dropping');});
box.addEventListener('dragleave',e=>{if(!box.contains(e.relatedTarget))box.classList.remove('dropping');});
box.addEventListener('drop',e=>{box.classList.remove('dropping');if(!(ms.peer||ms.room)||!hasFiles(e))return;e.preventDefault();e.stopPropagation();addPhotos(e.dataTransfer.files);});
input.addEventListener('paste',e=>{const files=[...(e.clipboardData?.files||[])].filter(f=>f.type.startsWith('image/'));if(files.length){e.preventDefault();addPhotos(files);}});
// 할 일 공유 글(코어 todo.SHARE_MARK)은 말풍선에 '할 일' 로 — 글자 그대로 보이지 않게.
const SHARE='\u2063vader-todo:';
function shareText(m){
 if(!m.text.startsWith(SHARE))return m.text;
 try{const s=JSON.parse(m.text.slice(SHARE.length));
  return s.op==='share'?VaderI18n.t(m.outgoing?'할 일을 공유했어요 ·':'할 일을 공유받았어요 ·')+' '+s.title:VaderI18n.t(s.undo?'공유한 할 일을 다시 열었어요':'공유한 할 일을 끝냈어요');}
 catch{return '할 일 공유';}
}
function roomMeta(m){
 if(!m.outgoing)return m.state==='received'&&!m.trusted?'확인 안 된 멤버':'';
 return [m.unread?VaderI18n.t('안 읽음 {0}',m.unread):'',m.missed?VaderI18n.t('{0}명 못 받음',m.missed):''].filter(Boolean).join(' · ');
}
function bubble(m,room=false){
 const d=el('div','msg '+(m.outgoing?'me':'ai')+(m.state==='failed'?' failed':'')+(m.state==='unreadable'?' unreadable':''));
 if(room&&!m.outgoing){const who=el('span','msg-from','@'+(m.handle||'알 수 없음'));d.append(who);}
 if(m.image){d.classList.add('msg-image');const body=el('div','msg-body');body.append(el('span','sr-only',m.outgoing?'나: ':'상대: '),photo(m));d.append(body);
  const note=STATES[m.state]||(room?roomMeta(m):!m.outgoing&&!m.trusted?'안전 번호 확인 전에 받음':'');
  d.append(el('small','msg-meta',(note?VaderI18n.word(note)+' · ':'')+new Date(m.at*1000).toLocaleTimeString(VaderI18n.lang==='en'?'en-US':'ko-KR',{hour:'2-digit',minute:'2-digit'})));return d;}
 const body=el('div','msg-body');body.append(el('span','sr-only',m.outgoing?'나: ':'상대: '));
 const said=el('span');said.dataset.raw='';said.append(m.text==null?STATES.unreadable:shareText(m));body.append(m.text==null?STATES.unreadable:said);d.append(body);if(m.text?.startsWith(SHARE))d.classList.add('msg-todo');
 const note=STATES[m.state]&&m.text!=null?STATES[m.state]:room?roomMeta(m):!m.outgoing&&m.state==='received'&&!m.trusted?'안전 번호 확인 전에 받음':'';
 d.append(el('small','msg-meta',(note?VaderI18n.word(note)+' · ':'')+new Date(m.at*1000).toLocaleTimeString(VaderI18n.lang==='en'?'en-US':'ko-KR',{hour:'2-digit',minute:'2-digit'})));
 if(room&&m.outgoing&&m.state==='failed'&&m.text!=null)d.append(btn('다시 보내기','roomResend','text-button msg-resend',{id:m.id}));
 return d;
}
const NOTE_ONE='Enter 보내기 · Shift+Enter 줄바꿈 · 두 PC 사이에서만 풀리게 암호화돼요. 환자 정보는 보내지 마세요.',
 NOTE_ROOM='Enter 보내기 · Shift+Enter 줄바꿈 · 방 멤버의 PC 에서만 풀리게 암호화돼요. 환자 정보는 보내지 마세요.';
function syncCompose(){
 const note=$('#msgNote');if(note)note.textContent=VaderI18n.word(ms.room?NOTE_ROOM:NOTE_ONE);
 if(ms.room){const r=roomOf(ms.room),ok=r?.state==='joined'&&!ms.busy;input.disabled=!ok;send.disabled=!ok;photoBtn.disabled=!ok;input.placeholder=ok?'메시지':'';return;}
 const f=ms.peer&&friend(ms.peer),ok=!!f&&f.state==='accepted'&&f.verified&&!ms.busy;
 input.disabled=!ok;send.disabled=!ok;photoBtn.disabled=!ok;
 input.placeholder=!f||f.state!=='accepted'?'':!f.verified?'안전 번호 확인 후 보낼 수 있어요':'메시지';
}
function merge(m){const i=ms.history.findIndex(x=>x.id===m.id);if(i<0)ms.history.push(m);else ms.history[i]=m;}

// ── 동작 ──
async function call(method,args){try{return await api(method,args);}catch(e){status(e.message,true);throw e;}}
// 친구 목록을 새로 받는다. 열린 대화 상대의 관계·신원이 바뀌었으면 번호를 버리고 대화를 다시 그린다 (codex1 3d P2-1).
async function loadFriends(list){
 const key=f=>f?[f.state,f.ready,f.verified,f.changed].join():'';
 const before=key(ms.peer&&friend(ms.peer));
 ms.friends=list||await api('messenger_friends');ms.groups=await api('messenger_groups').catch(()=>ms.groups);
 ms.rooms=(await api('messenger_rooms').catch(()=>ms.rooms))||[];publish();
 if(box.hidden)return;
 if(ms.room)return draw();
 if(!ms.peer)return draw();
 if(key(friend(ms.peer))!==before){ms.safety=null;await refreshTalk(ms.epoch);}
}
async function loadRooms(){try{ms.rooms=(await api('messenger_rooms'))||[];publish();if(!box.hidden&&!ms.peer)draw();}catch(e){}}
async function findHandle(h){
 h=String(h).trim();if(!h)return;
 try{ms.found=await call('messenger_find',{handle:h});draw();($('[data-m="request"]',stream)||$('#msgFind')).focus();}catch(e){}
}
async function act(b){
 const m=b.dataset.m,id=b.dataset.id||ms.peer;
 if(m==='open')return show(id);
 if(m==='roomOpen')return showRoom(b.dataset.room);
 if(m==='roomNew'){ms.making=!ms.making;ms.pick=new Set();draw();return;}
 if(m==='roomCreate'){if(!ms.pick.size)return;
  try{const got=await call('messenger_room_create',{account_ids:[...ms.pick],name:$('#roomName')?.value||''});ms.rooms=got.rooms;ms.making=false;ms.pick=new Set();publish();
   status('방을 만들었어요. 초대한 친구가 수락하면 함께 이야기할 수 있어요.');await showRoom(got.id);}catch(e){}return;}
 if(m==='roomAccept'||m==='roomDecline'){try{ms.rooms=await call(m==='roomAccept'?'messenger_room_accept':'messenger_room_decline',{room:b.dataset.room});publish();
   if(m==='roomAccept')return showRoom(b.dataset.room);draw();}catch(e){}return;}
 if(m==='roomInvite'){const who=$('#roomInvitePick')?.value;if(!who)return;
  try{ms.rooms=await call('messenger_room_invite',{room:b.dataset.room,account_ids:[who]});publish();draw();status('초대했어요. 수락하면 방에 들어와요.');}catch(e){}return;}
 if(m==='roomKick'||m==='roomLeave'){const key=m+':'+(b.dataset.id||b.dataset.room);
  if(ms.armed!==key){ms.armed=key;const was=b.textContent;b.textContent=m==='roomLeave'?'한 번 더 누르면 나가요 (기록은 남아요)':'한 번 더 누르면 내보내요';setTimeout(()=>{if(ms.armed===key){ms.armed=null;b.textContent=was;}},4000);return;}
  ms.armed=null;
  try{ms.rooms=await call(m==='roomLeave'?'messenger_room_leave':'messenger_room_remove',m==='roomLeave'?{room:b.dataset.room}:{room:b.dataset.room,account_id:b.dataset.id});publish();
   if(m==='roomLeave'){leave();ms.room=null;status('방에서 나왔어요');}draw();}catch(e){}return;}
 if(m==='roomResend'){const ep=ms.epoch;try{const got=await call('messenger_room_resend',{message_id:b.dataset.id});if(ep===ms.epoch){ms.history=ms.history.filter(x=>x.id!==b.dataset.id);merge(got);draw();}}catch(e){}return;}
 if(m==='groupKick'){const f=friend(b.dataset.id);try{await call('messenger_group_assign',{account_id:b.dataset.id,gid:''});ms.groupShown=b.dataset.gid;await loadFriends();status(VaderI18n.t('@{0} 님을 그룹에서 뺐어요',f?.handle||''),false,true);}catch(e){}return;}
 if(m==='groupRename'){ms.renaming=b.dataset.gid;return draw();}
 if(m==='groupRenameCancel'){ms.renaming=null;return draw();}
 if(m==='groupDelete'){const g=ms.groups.find(x=>x.id===b.dataset.gid);if(!g)return;
  if(ms.armed!=='del:'+g.id){ms.armed='del:'+g.id;b.textContent='한 번 더 누르면 지워요';setTimeout(()=>{if(ms.armed==='del:'+g.id){ms.armed=null;b.textContent='지우기';}},4000);return;}
  ms.armed=null;try{ms.groups=await call('messenger_group_delete',{gid:g.id});await loadFriends();status(VaderI18n.t('{0} 그룹을 지웠어요. 친구 관계는 그대로예요.',g.name),false,true);}catch(e){}return;}
 if(m==='retry'){ms.state=null;ms.error=null;draw();return tick();}
 if(m==='shotSend')return sendShots();
 if(m==='shotCancel')return dropShots();
 if(m==='shotRemove'){const s=ms.shots.splice(Number(b.dataset.i),1)[0];if(s)api('messenger_drop_image',{token:s.token}).catch(()=>{});return drawShots();}
 if(m==='move'){
  if(ms.armed!=='move'){ms.armed='move';b.textContent='한 번 더 누르면 옮겨요 (지난 대화·안 받은 메시지는 오지 않아요)';setTimeout(()=>{if(ms.armed==='move'){ms.armed=null;b.textContent='이 PC 로 메신저 옮기기';}},6000);return;}
  ms.armed=null;b.disabled=true;
  try{const c=await call('messenger_move');ms.state=c.state;ms.error=null;if(ms.handle!==(c.handle||null)){ms.handle=c.handle||null;loadSeen();}
   if(c.state==='ready'){await loadFriends();status('메신저를 이 PC 로 옮겼어요. 친구와 안전 번호를 다시 확인해 주세요.',false,true);}draw();}
  catch(e){b.disabled=false;}
  return;}
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
 const peer=ms.peer,room=ms.room,key=draftKey(),ep=ms.epoch,text=input.value.trim();if(!text||ms.busy||!(peer||room))return;
 ms.busy=true;syncCompose();
 try{const m=room?await api('messenger_room_send',{room,text}):await api('messenger_send',{account_id:peer,text});
  drafts[key]='';
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
    if((ms.peer||ms.room)&&!box.hidden&&(r.received||r.images||ticks%6===0||ms.history.some(m=>m.state==='queued')))await refreshTalk(ms.epoch);
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
back.onclick=()=>{leave();ms.peer=null;ms.room=null;input.value='';draw();($('#msgFind')||stream).focus();};
box.addEventListener('click',e=>{const b=e.target.closest('[data-m]');if(!b||b.disabled)return;b.disabled=true;Promise.resolve(act(b)).finally(()=>{b.disabled=false;syncCompose();});});
form.onsubmit=e=>{e.preventDefault();say();};
input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();say();}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!box.hidden&&!docked.matches&&!$('#settingsDialog').open){e.preventDefault();open(false,box.contains(document.activeElement));}});
box.addEventListener('focusout',e=>{const to=e.relatedTarget;if(!docked.matches&&to&&!box.contains(to)&&to!==toggle&&!to.closest?.('[data-friend]'))open(false,false);});
document.addEventListener('visibilitychange',()=>{if(!document.hidden){tick();if((ms.peer||ms.room)&&!box.hidden)refreshTalk(ms.epoch);}});
const start=()=>tick();
VaderI18n.ready.then(start);
// 트레이에서 다시 열면: 숨은 동안 코어가 받아 둔 메시지를 친구 목록·대화에 바로 보인다
const wake=async()=>{await tick();if(ms.state==='ready')await loadFriends().catch(()=>{});if((ms.peer||ms.room)&&!box.hidden)refreshTalk(ms.epoch);};
window.VaderMessenger={show,showRoom,wake,close:()=>{if(!box.hidden)open(false,false);}};
})();
