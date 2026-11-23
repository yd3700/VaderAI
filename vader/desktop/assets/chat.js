// 대화 서랍 — main 렌더와 따로 산다. 화면을 옮기거나 poll 이 다시 그려도 초안·스크롤·진행 중인 턴이 남는다 (codex1).
// 모델 답은 HTML 로 넣지 않는다: 글자 그대로 + **굵게** 만. 주소도 글자로만 둔다 — 이 창은 Python 다리가 붙은 창이다.
(()=>{
const box=$('#chat'),stream=$('#chatStream'),input=$('#chatInput'),send=$('#chatSend'),toggle=$('#chatToggle'),line=$('#chatState'),gate=$('#chatGate'),forget=$('#chatForget');
const docked=matchMedia('(min-width:1180px)');
const ASK=['처음 쓰는 룬은 어떻게 시작해요?','승인은 언제 해야 해요?','광장에서 받은 룬은 어떻게 써요?'];
let loaded=false,loading=false,waiting=false,deleting=false,armed=null,retry=null;
$('#chatClose').innerHTML=icon('close');send.insertAdjacentHTML('afterbegin',icon('send'));

const store=v=>{try{localStorage.setItem('vader.chat',v?'open':'');}catch(e){}};
function open(show,focus=true){
 if(show)window.VaderMessenger?.close();   // 오른쪽 서랍은 하나만
 box.hidden=!show;document.body.classList.toggle('chat-open',show);toggle.setAttribute('aria-expanded',String(show));store(show);
 if(show){sync();if(!loaded)load();if(focus&&!input.disabled)input.focus();}else if(focus)toggle.focus();
}
function status(text,bad=false){line.textContent=text||'화면 사용법을 물어보세요';line.classList.toggle('bad',bad);}
const nearEnd=()=>stream.scrollHeight-stream.scrollTop-stream.clientHeight<48;
function bubble(role,text,cls=''){
 const m=document.createElement('div');m.className='msg '+role+(cls?' '+cls:'');
 const who=document.createElement('span');who.className='sr-only';who.textContent=role==='me'?'나: ':'VaderAI: ';
 const body=document.createElement('div');body.className='msg-body';body.append(who);
 String(text).split(/\*\*(.+?)\*\*/s).forEach((part,i)=>{if(!part)return;if(i%2){const b=document.createElement('b');b.textContent=part;body.append(b);}else body.append(part);});
 m.append(body);const stick=nearEnd();stream.append(m);if(stick||role==='me')stream.scrollTop=stream.scrollHeight;return m;
}
function intro(){
 const d=document.createElement('div');d.className='chat-intro';
 d.innerHTML='<p>화면 사용법과 설치된 룬을 안내해요. 실행 기록이나 업무 파일은 이 대화에서 보지 못해요.</p>';
 ASK.forEach(q=>{const b=document.createElement('button');b.type='button';b.className='chat-ask';b.textContent=q;b.onclick=()=>say(q);d.append(b);});
 stream.append(d);
}
// 이력을 다 그리기 전에는 보내기·지우기를 막는다 — 늦게 온 이력이 새 말풍선이나 지운 화면을 덮지 않게 (codex1 P2-2).
async function load(){
 if(loading||loaded)return;loading=true;clearTimeout(retry);
 try{const h=await api('chat_history');
  if(h.busy){status('앞 질문의 답을 기다리는 중…');retry=setTimeout(()=>{if(!box.hidden)load();},1500);return;}
  loaded=true;stream.textContent='';
  if(h.messages.length){const d=document.createElement('p');d.className='chat-divider';d.textContent='이전 대화';stream.append(d);}
  h.messages.forEach(m=>bubble(m.role==='user'?'me':'ai',m.content));
  if(!h.messages.length)intro();
  stream.scrollTop=stream.scrollHeight;status('');
 }catch(e){status(e.message,true);}
 finally{loading=false;sync();}
}
// AI 가 없으면 이력은 보여 주되 보내기는 막고, 연결 버튼을 둔다.
function sync(){
 const ready=state.aiReady!==false;box.classList.toggle("thinking",waiting);gate.hidden=ready;const off=!ready||waiting||deleting||!loaded;input.disabled=off;send.disabled=off;forget.disabled=waiting||deleting||!loaded;
 if(!ready&&!gate.firstChild){gate.innerHTML='<span>대화에 쓸 AI 가 연결되지 않았어요.</span>';const b=document.createElement('button');b.type='button';b.className='button ghost';b.innerHTML=icon('link')+'AI 연결';b.onclick=()=>openAssignments().catch(e=>toast(e.message));gate.append(b);}
}
async function say(text){
 text=String(text).trim();if(!text||waiting||deleting||!loaded||state.aiReady===false)return;
 waiting=true;sync();input.value='';$('.chat-intro',stream)?.remove();
 const mine=bubble('me',text),pending=bubble('ai','답을 쓰는 중…','pending');pending.setAttribute('aria-busy','true');status('답을 쓰는 중…');
 try{const answer=await api('chat_say',{text});pending.remove();bubble('ai',answer);status('');}
 catch(e){pending.remove();mine.remove();                 // 서버도 답 못 받은 말을 이력에서 뺀다
  const err=document.createElement('p');err.className='chat-error';err.setAttribute('role','alert');err.textContent='보내지 못했어요 · '+e.message;stream.append(err);stream.scrollTop=stream.scrollHeight;
  if(!input.value)input.value=text;status('보내지 못했어요',true);}  // 그새 쓴 초안은 덮지 않는다
 finally{waiting=false;sync();if(box.contains(document.activeElement)||document.activeElement===document.body)input.focus();}
}
async function wipe(){
 if(!loaded||waiting||deleting)return;
 if(armed){clearTimeout(armed);armed=null;forget.textContent='대화 지우기';
  // 지우는 답이 올 때까지 보내기를 막는다 — 늦게 온 삭제가 그새 보낸 말풍선을 지우지 않게 (codex1)
  deleting=true;sync();
  try{await api('chat_forget');stream.textContent='';intro();status('대화를 지웠어요');}catch(e){status(e.message,true);}
  finally{deleting=false;sync();}return;}
 forget.textContent='한 번 더 누르면 지워요';armed=setTimeout(()=>{armed=null;forget.textContent='대화 지우기';},4000);
}
toggle.onclick=()=>open(box.hidden);
$('#chatClose').onclick=()=>open(false);
forget.onclick=wipe;
$('#chatForm').onsubmit=e=>{e.preventDefault();say(input.value);};
input.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();say(input.value);}});
// 좁은 창에서는 업무 화면 위에 겹친다 — Escape 나 서랍 밖으로 포커스가 나가면 닫아 가려진 곳에 포커스가 숨지 않게 (codex1)
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!box.hidden&&!docked.matches&&!$('#settingsDialog').open){e.preventDefault();open(false,box.contains(document.activeElement));}});
box.addEventListener('focusout',e=>{const to=e.relatedTarget;if(!docked.matches&&to&&!box.contains(to)&&to!==toggle)open(false,false);});
// 넓은 창에서 열어 두고 닫았으면 다시 열어 둔다 — 코어 다리가 붙은 뒤에
const restore=()=>{try{if(docked.matches&&localStorage.getItem('vader.chat')==='open')open(true,false);}catch(e){}};
if(window.pywebview?.api)restore();else window.addEventListener('pywebviewready',restore,{once:true});
window.VaderChat={sync,close:()=>{if(!box.hidden)open(false,false);}};
})();
