"""폰에서 같은 펫을 만난다. Phase 15.

**들어오는 문이다.** 지금까지 문은 전부 나가는 쪽이었다(`http.py`, 제공자
SDK). 여기는 반대라 규칙도 반대다 — 나가는 문은 "무엇을 보내나" 를 지키고
들어오는 문은 "누구를 들이나" 를 지킨다.

    끕니다가 기본        `기기 연결…` 을 누른 동안만 산다
    같은 네트워크        계정 서버는 안 만든다 (`PLAN.md`)
    토큰 없으면 404      있는 것도 안 알려 준다

## 404 로 답하는 이유

토큰이 없을 때 401 을 주면 "여기 뭔가 있다" 를 알려 주는 셈이다. 이 서버는
사내망 어딘가에 떠 있고, 붙을 사람은 이미 코드를 받은 사람뿐이다.
**모르는 사람에게는 아무것도 없어야 한다.**

## 여기서 안 하는 것

- **룬을 실행하지 않는다.** 원격 명령은 Phase 13 이고, 승인·감사·멱등성이
  거기 전제로 붙어 있다. 지금 대화만 되는 것이 약속이다
- **관찰을 안 보낸다.** `observe`·`character` 를 import 하지 않고
  `tests/test_data_map.py` 가 ast 로 강제한다
- **설정을 스스로 못 바꾼다.** 폰이 설정 변경을 시키면 승인 창은
  **데스크톱에서** 뜬다. 데스크톱 창이 닫혀 있으면 그냥 거절된다

## 무엇이 나가나

펫의 이름·말투와 대화 내용. `data-map.md` 가 둘 다 "된다" 로 둔 것들이다 —
사용자가 만들었고 행동에서 파생되지 않았다. 그 밖의 것은 안 나간다.
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from vader.core import conversation, pairing, pet as pet_module

__all__ = ["PetServer", "DEFAULT_PORT", "lan_address"]

DEFAULT_PORT = 8756

#: 요청 본문 상한. 대화 한 마디에 이보다 클 이유가 없고, 상한이 없으면
#: 아무나 메모리를 채울 수 있다.
MAX_BODY = 64 * 1024


class PetServer:
    """데스크톱이 켜 두는 작은 서버. **창을 닫으면 같이 죽는다.**"""

    def __init__(self, *, port: int = DEFAULT_PORT, approve=None, log=None) -> None:
        self.port = port
        self.approve = approve
        self.log = log or (lambda level, message, **fields: None)
        #: 화면에 떠 있는 코드. 없으면 아무도 새로 못 붙는다.
        self.pending: pairing.Pending | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ── 켜고 끄기 ───────────────────────────────────────────────

    def start(self) -> int:
        """돌려주는 것은 **실제로 잡은 포트**다. 0 을 주면 빈 포트를 고른다."""
        if self._httpd is not None:
            return self._httpd.server_address[1]
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), handler)
        # 창을 닫는 순간 붙어 있던 폰의 연결이 남아 앱 종료를 막지 않게 한다.
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self.port = self._httpd.server_address[1]
        self.log("info", "기기 연결을 열었습니다", port=self.port)
        return self.port

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        self._thread = None
        # 코드도 같이 죽인다. 창을 닫았는데 코드가 도는 것이 더 이상하다.
        self.pending = None
        self.log("info", "기기 연결을 닫았습니다")

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def show_code(self) -> pairing.Pending:
        """새 코드를 띄운다. 앞의 코드는 그 자리에서 죽는다."""
        self.pending = pairing.new_code()
        return self.pending

    def url(self) -> str:
        return f"http://{lan_address()}:{self.port}/"

    # ── 요청 처리 ───────────────────────────────────────────────

    def handle(self, method: str, route: str, token: str, body: dict) -> tuple[int, object]:
        """라우팅. **HTTP 를 모르게 짜서 테스트가 창 없이 돈다.**"""
        if method == "GET" and route == "/":
            return 200, PAGE
        if method == "POST" and route == "/pair":
            token = pairing.claim(
                self.pending, str(body.get("code", "")), str(body.get("name", ""))
            )
            if not token:
                self.log("warning", "페어링 실패")
                return 403, {"error": "코드가 맞지 않습니다"}
            # 성공한 코드는 죽었다. 화면에서도 지운다.
            self.pending = None
            self.log("info", "새 기기가 붙었습니다", name=str(body.get("name", "")))
            return 200, {"token": token}

        device = pairing.device_for(token)
        if device is None:
            # 있는 것도 안 알려 준다.
            return 404, {"error": "없습니다"}

        if method == "GET" and route == "/api/pet":
            pet = pet_module.load()
            # **온라인 상태가 곧 이 응답이다.** 따로 심장 박동을 만들지
            # 않는다 — 답이 오면 켜져 있는 것이다.
            return 200, {"name": pet.name, "tone": pet.tone, "online": True}
        if method == "GET" and route == "/api/history":
            talk = conversation.session(approve=self.approve, log=self.log)
            return 200, {
                "messages": [
                    {"role": m.role, "content": m.content} for m in talk.messages
                ]
            }
        if method == "POST" and route == "/api/say":
            text = str(body.get("text", "")).strip()
            if not text:
                return 400, {"error": "할 말이 없습니다"}
            try:
                answer = conversation.say(text, approve=self.approve, log=self.log)
            except Exception as exc:  # noqa: BLE001 — 폰에 스택을 보내지 않는다
                self.log("error", "폰에서 온 말에 답하지 못했습니다", kind=type(exc).__name__)
                return 502, {"error": "지금은 답하지 못합니다"}
            return 200, {"answer": answer}
        return 404, {"error": "없습니다"}


def lan_address() -> str:
    """같은 네트워크에서 이 기계를 부르는 주소.

    **패킷은 안 나간다.** UDP 소켓은 connect 해도 아무것도 안 보내고, 커널이
    "이 목적지로 가려면 어느 인터페이스냐" 만 정해 준다. `gethostbyname` 은
    기계에 따라 127.0.0.1 을 돌려줘서 폰에서 못 붙는다.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def _make_handler(server: PetServer):
    class Handler(BaseHTTPRequestHandler):
        # 기본 구현은 stderr 로 한 줄씩 찍는다. 콘솔 없는 배포에서 그건
        # 그냥 소음이다.
        def log_message(self, *args) -> None:
            pass

        def do_GET(self) -> None:
            self._respond(*server.handle("GET", self._route(), self._token(), {}))

        def do_POST(self) -> None:
            self._respond(*server.handle("POST", self._route(), self._token(), self._body()))

        def _route(self) -> str:
            # 쿼리는 버린다. 라우트는 정확히 일치하는 것만 있고, 그래서
            # `..` 같은 경로가 낄 자리가 없다.
            return self.path.split("?", 1)[0]

        def _token(self) -> str:
            head = self.headers.get("Authorization", "")
            return head[7:] if head.startswith("Bearer ") else ""

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return {}
            if length <= 0 or length > MAX_BODY:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeError):
                return {}

        def _respond(self, status: int, payload: object) -> None:
            if isinstance(payload, str):
                data = payload.encode("utf-8")
                kind = "text/html; charset=utf-8"
            else:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                kind = "application/json; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            # 다른 사이트가 이 서버를 부르게 두지 않는다.
            self.send_header("Access-Control-Allow-Origin", "null")
            self.end_headers()
            self.wfile.write(data)

    return Handler


#: 폰이 여는 화면. **한 파일에 다 넣는다** — 바깥에서 아무것도 안 받아온다.
#: 폰트도 스크립트도 CDN 에서 끌어오면 그 순간 이 기계 밖으로 요청이 나간다.
PAGE = """<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>내 펫</title>
<style>
 :root { color-scheme: light dark; }
 body { margin:0; font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
 header { padding:12px 16px; border-bottom:1px solid #8883; font-weight:600; }
 #log { padding:16px; }
 .m { margin:0 0 12px; padding:10px 12px; border-radius:12px; max-width:80%; white-space:pre-wrap; }
 .user { margin-left:auto; background:#2f6fed; color:#fff; }
 .pet { background:#8882; }
 .src { font-size:12px; opacity:.6; margin:-6px 0 12px; }
 form { position:sticky; bottom:0; display:flex; gap:8px; padding:12px;
        border-top:1px solid #8883; background:Canvas; }
 input,button { font:inherit; padding:10px; border-radius:10px; border:1px solid #8886; }
 input { flex:1; min-width:0; }
</style></head><body>
<header id="who">연결 중…</header>
<div id="log"></div>
<form id="f"><input id="t" placeholder="말 걸기" autocomplete="off"><button>보내기</button></form>
<script>
const K='vader-token';
const $=s=>document.querySelector(s);
function add(cls,text){const p=document.createElement('p');p.className='m '+cls;p.textContent=text;$('#log').appendChild(p);window.scrollTo(0,document.body.scrollHeight);}
async function api(path,opt={}){const t=localStorage.getItem(K);
 return fetch(path,{...opt,headers:{'Content-Type':'application/json','Authorization':'Bearer '+t}});}
async function pair(){
 const code=prompt('데스크톱 화면의 여섯 자리 코드');
 if(!code)return false;
 const r=await fetch('/pair',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({code:code,name:navigator.userAgent.slice(0,40)})});
 if(!r.ok){alert('코드가 맞지 않습니다');return false;}
 localStorage.setItem(K,(await r.json()).token);return true;}
async function boot(){
 let r=await api('/api/pet');
 if(r.status===404){ if(!await pair())return; r=await api('/api/pet'); }
 if(!r.ok){$('#who').textContent='연결하지 못했습니다';return;}
 $('#who').textContent=(await r.json()).name;
 const h=await (await api('/api/history')).json();
 for(const m of h.messages) add(m.role==='user'?'user':'pet',m.content);}
$('#f').addEventListener('submit',async e=>{
 e.preventDefault();const text=$('#t').value.trim();if(!text)return;
 $('#t').value='';add('user',text);
 const r=await api('/api/say',{method:'POST',body:JSON.stringify({text:text})});
 const d=await r.json();
 if(!r.ok){add('pet',d.error||'답하지 못했습니다');return;}
 add('pet',d.answer);});
boot();
</script></body></html>
"""
