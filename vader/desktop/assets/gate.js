'use strict';
// 문 — 로그인 → (처음이면 PIN 정하기) → PIN 해제 → 업무 화면. 판정은 코어가 한다(host.py gate_*). 화면은 상태만 그린다.
(()=>{
 const $=s=>document.querySelector(s);
 const form=$('#gateForm'),pin=$('#pin'),again=$('#again'),action=$('#gateAction'),error=$('#gateError');
 let current=null,busy=false;
 async function call(method,args={}){
  const r=await window.pywebview.api.call(method,args);
  if(!r.ok)throw Error(r.error);
  return r.data;
 }
 const VIEWS={
  // 첫 실행 — 무엇과 통신하는지 알리고 자동 확인을 끌 수 있게 (SignPath 조건). 이 선택은 [계정] 창에서 바꾼다.
  notice:()=>['처음 쓰기 전에','업무 처리는 이 PC 에서 해요. AI 연결·메신저·휴대폰 연결처럼 고른 기능을 쓸 때만 그 내용이 해당 서비스로 가요. 로그인하면 계정 상태를 주기적으로 확인하고, 광장은 VaderAI 서버와, 자동 업데이트는 GitHub 과 통신해요. 자동 온라인 확인을 끄면 새 버전과 광장 목록을 알아서 확인하지 않아요 — 회수된 패키지도 광장에서 새로 고침을 누르기 전까지 모르고 실행될 수 있어요.',{label:'시작하기',wait:'준비하는 중…',run:()=>call('gate_notice',{auto:$('#autoOnline').checked})}],
  login:()=>['로그인','Google 또는 카카오 계정으로 브라우저에서 로그인해요. 비밀번호는 이 앱에 입력하지 않아요.',{label:'브라우저에서 로그인',wait:'브라우저에서 연결을 기다리는 중…',run:()=>call('gate_login')}],
  pin_setup:s=>[s.name?VaderI18n.t('{0} 님, PIN 을 정해 주세요',s.name):'PIN 을 정해 주세요','PC 를 한동안 안 쓰거나 Windows 가 잠기면 앱도 잠겨요. 숫자 6자리로 풀어요.'],
  pin:s=>[s.name||'잠겨 있어요','PIN 6자리를 입력하세요.'],
  // 한 계정 한 PC (2026-11-25) — 다른 PC 에서 같은 계정으로 로그인해 이 PC 가 끊겼다
  replaced:()=>['다른 PC 에서 로그인했어요','이 계정으로 다른 PC 에서 로그인해 이 PC 는 로그아웃됐어요. 여기서 다시 로그인하면 다른 PC 가 로그아웃돼요.',{label:'이 PC 에서 다시 로그인',wait:'브라우저에서 연결을 기다리는 중…',run:()=>call('gate_login')}],
  offline:()=>['인터넷에 연결해 주세요','서버에서 계정을 다시 확인해야 해요. 마지막 확인 뒤 7일이 지났거나 PC 시계가 바뀌었어요.',{label:'다시 확인',wait:'확인하는 중…',run:()=>call('gate_status')}],
  claiming:()=>['데이터 옮기기를 마무리해 주세요','이 PC 의 기존 데이터를 옮기다 멈췄어요. 앱을 닫고 옮기기 명령을 --apply 로 다시 실행하면 이어서 끝나요.'],
  restart:s=>['앱을 다시 켜 주세요',VaderI18n.t('{0} 계정으로 들어가려면 앱을 닫고 다시 켠 뒤 PIN 을 입력하세요. 이 창은 처음 연 계정 전용이에요.',s.name||VaderI18n.t('다른'))],
 };
 function show(s){
  current=s;
  if(s.state==='open'){location.replace('index.html');return;}
  const [title,lead,act]=(VIEWS[s.state]||VIEWS.login)(s);
  $('#gateTitle').textContent=title;$('#gateTitle').toggleAttribute('data-raw',s.state==='pin'&&!!s.name);$('#gateLead').textContent=lead;
  const asking=s.state==='pin'||s.state==='pin_setup';
  form.hidden=!asking;$('#gateNotice').hidden=s.state!=='notice';$('#againField').hidden=s.state!=='pin_setup';
  $('#gateSubmit').textContent=s.state==='pin_setup'?'시작하기':'열기';
  $('#gateForgot').hidden=s.state!=='pin';$('#gateNote').hidden=s.state!=='pin_setup';
  action.hidden=!act;if(act){action.textContent=act.label;action.onclick=()=>run(act.wait,act.run);}
  pin.value=again.value='';
  if(s.wait)error.textContent=s.wait+'초 뒤에 다시 입력해 주세요.';
  (asking?pin:$('#gateTitle')).focus();
 }
 // 한 번에 하나만. 기다리는 동안 버튼 글자로 무엇을 기다리는지 보인다.
 async function run(wait,work,keep){
  if(busy)return;busy=true;error.textContent=action.hidden?wait:'';document.body.classList.add('busy');
  const label=action.textContent;if(!action.hidden){action.disabled=true;action.textContent=wait;}
  $('#gateSubmit').disabled=true;
  try{show(await work());}
  catch(e){error.textContent=e.message;if(keep)keep();}
  finally{busy=false;document.body.classList.remove('busy');action.disabled=false;if(action.textContent===wait)action.textContent=label;$('#gateSubmit').disabled=false;}
 }
 form.onsubmit=e=>{
  e.preventDefault();
  if(current.state==='pin_setup')run('',()=>call('gate_pin_setup',{pin:pin.value,again:again.value}),()=>{pin.value=again.value='';pin.focus();});
  else run('',()=>call('gate_unlock',{pin:pin.value}),()=>{pin.value='';pin.focus();});
 };
 // 숫자만. 해제는 여섯 자리가 차면 바로 — 정할 때는 두 칸을 다 채우고 누른다.
 [pin,again].forEach(x=>x.addEventListener('input',()=>{x.value=x.value.replace(/\D/g,'').slice(0,6);
  if(current?.state==='pin'&&x===pin&&pin.value.length===6)form.requestSubmit();}));
 // 로그인을 취소·실패해도 같은 화면에서 다시 할 수 있게 — 상태를 새로 받아 그린 뒤 오류를 보인다 (codex1 NEW-P2-2)
 const recover=work=>async()=>{try{return await work();}catch(e){show(await call('gate_status'));throw e;}};
 $('#gateForgot').onclick=()=>run('브라우저에서 같은 계정으로 다시 로그인하세요…',recover(()=>call('gate_forgot')));
 $('#gatePrivacy').onclick=()=>call('gate_privacy').catch(e=>{error.textContent=e.message;});
 const start=()=>run('',()=>call('gate_status'));
 VaderI18n.ready.then(start);   // i18n.js 가 코어 준비(pywebviewready)를 기다려 언어를 정한 뒤
})();
