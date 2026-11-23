// Persistent terminal: polling appends actual log lines without replaying history.
class LiveLogTerminal {
 constructor() {
  this.jobId=null;this.lines=[];this.follow=true;this.unseen=0;this.position=0;
  this.element=document.createElement('section');this.element.id='jobLogs';this.element.className='log-terminal';
  this.element.setAttribute('aria-label','실시간 진행 기록');
  this.element.innerHTML='<div class="terminal-header"><h3><span class="terminal-light" aria-hidden="true"></span>진행 기록</h3><span class="terminal-status"></span><button type="button" id="followLogs" class="text-button" aria-pressed="true">최신 로그 따라가는 중</button></div><div id="logViewport" class="terminal-scroll" tabindex="0" aria-label="진행 로그, 위로 스크롤하여 이전 기록 읽기"><div class="terminal-lines" role="log" aria-label="룬 실행 로그" aria-live="off"></div><div class="terminal-prompt"><span aria-hidden="true">›</span><span class="terminal-message"></span><span class="terminal-cursor" aria-hidden="true"></span></div></div><span class="sr-only terminal-announcement" role="status" aria-live="polite"></span>';
  this.viewport=this.element.querySelector('.terminal-scroll');this.list=this.element.querySelector('.terminal-lines');
  this.button=this.element.querySelector('#followLogs');
  this.viewport.addEventListener('scroll',()=>{
   if(!this.element.isConnected)return;
   this.position=this.viewport.scrollTop;
   this.follow=this.viewport.scrollHeight-this.viewport.clientHeight-this.position<12;
   if(this.follow)this.unseen=0;
   this.updateButton();
  });
  this.button.onclick=()=>{this.follow=true;this.unseen=0;this.viewport.scrollTop=this.viewport.scrollHeight;this.position=this.viewport.scrollTop;this.updateButton();};
  this.resizeObserver=new ResizeObserver(()=>{
   if(this.element.isConnected&&this.follow){this.viewport.scrollTop=this.viewport.scrollHeight;this.position=this.viewport.scrollTop;}
  });
  this.resizeObserver.observe(this.viewport);
 }
 updateButton() {
  this.button.textContent=this.follow?'최신 로그 따라가는 중':(this.unseen?'새 로그 '+this.unseen+'줄 · ':'')+'최신으로 이동';
  this.button.setAttribute('aria-pressed',String(this.follow));
 }
 detach() {
  if(this.element.isConnected)this.position=this.viewport.scrollTop;
  this.element.remove();
 }
 mount(target,job) {
  if(!target||!job)return;
  const fresh=job.id!==this.jobId;
  if(fresh){this.jobId=job.id;this.lines=[];this.list.replaceChildren();this.follow=true;this.unseen=0;this.position=0;}
  if(target!==this.element)target.replaceWith(this.element);
  const incoming=job.logs.map(String);
  // The backend retains 500 lines. Match the surviving tail, including repeats.
  let overlap=Math.min(this.lines.length,incoming.length);
  while(overlap>0&&!this.lines.slice(-overlap).every((line,i)=>line===incoming[i]))overlap--;
  const removed=this.lines.length-overlap;
  const before=this.list.offsetHeight;
  for(let i=0;i<removed;i++)this.list.firstElementChild?.remove();
  const removedHeight=before-this.list.offsetHeight;
  const added=incoming.slice(overlap),fragment=document.createDocumentFragment();
  for(const text of added){
   const line=document.createElement('div');line.className='terminal-line';
   if(!fresh&&this.follow&&!job.terminal&&added.length<=8)line.classList.add('arriving');
   if(/\[error\]/i.test(text))line.classList.add('log-error');
   else if(/\[warn(?:ing)?\]/i.test(text))line.classList.add('log-warning');
   line.textContent=text;fragment.append(line);
  }
  this.list.append(fragment);this.lines=incoming;
  if(!this.follow)this.unseen+=added.length;
  this.element.dataset.live=String(!job.terminal);
  this.element.querySelector('.terminal-status').textContent=job.terminal?'기록 완료':job.state==='waiting_user'?'확인 대기':'실시간';
  this.element.querySelector('.terminal-message').textContent=job.terminal?'기록이 끝났습니다.':job.state==='waiting_user'?'응답을 기다리고 있습니다.':'다음 로그를 기다리고 있습니다.';
  this.viewport.scrollTop=this.follow?this.viewport.scrollHeight:Math.max(0,this.position-removedHeight);
  this.position=this.viewport.scrollTop;this.updateButton();
  if(added.length&&!fresh)this.element.querySelector('.terminal-announcement').textContent=added.length+'개의 새 로그가 도착했습니다.';
 }
 setConnected(connected) {
  this.element.dataset.offline=String(!connected);
  if(!connected){this.element.querySelector('.terminal-status').textContent='연결 확인 중';this.element.querySelector('.terminal-message').textContent='로그 연결을 다시 확인하고 있습니다.';}
 }
}
