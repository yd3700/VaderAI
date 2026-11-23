"""화면 자동화 중개 창구 — 엔진은 앱(일꾼) 프로세스만 띄운다. MCP 는 여기로 요청만 보낸다.

앱이 잠그면 screen.sweep 이 이 프로세스의 엔진을 전부 끝내고 지운다 — 다른 프로세스의 엔진이 없으니
'정리했다' 가 참이 된다 (codex1 화면 r3 P1-2). 앱이 죽으면 엔진은 Job Object 로 같이 끝난다.

잠금 경계는 여기(서버)에 있다 (codex1 r4 P1-1): 요청마다 앱 메모리의 지금 세대(screen.current_gen)와
요청에 적힌 세대·기한을 처리 직전과 답하기 직전에 대조한다. 잠기면 None, 다시 열면 새 세대라 잠금 전 요청은
어느 쪽이든 거절된다. 'apps' 도 예외가 아니다.

통로: Windows 이름 있는 파이프. 인증은 직접 — 서버가 무작위 nonce 를 보내고 클라이언트는 HMAC(키, nonce+본문)을
본문과 함께 한 번에 보낸다. 모든 단계에 기한(poll)이 있어 멈춘 연결이 창구를 붙잡지 못한다 (r4 P2-1).
동시 연결은 4개까지, 처리는 하나씩(_work). 연결마다 수명(CONN_LIFE) 이 지나면 서버가 닫는다 — 답을 안 읽어
막힌 송신도 깨어난다. 요청 기한(until)은 유한한 숫자·최대 60초, monotonic 으로 바꿔 작업 대기·엔진 호출마다·결과
게시·답 공개 전에 확인한다 (codex1 r5). 주고받는 것은 JSON 바이트뿐 — pickle 없음.
주소·키는 HOME/screen.json. ponytail: 같은 사용자 권한의 프로그램은 그 파일을 읽을 수 있다 — 여기서 받는 요청은
늘 '비신뢰 요청' 이고 허용 앱·라벨 정제·(2단계) 사람 승인은 앱 안에서 판단한다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import threading
import time
import uuid
from multiprocessing.connection import Client, Listener

from vader.core import config, screen

MAX_REQUEST = 16 * 1024
MAX_REPLY = 4 * 1024 * 1024
STEP = 5                 # 한 단계(nonce 받기·요청 받기) 기한
REPLY_WAIT = 60          # 요청 하나의 전체 기한 — 이것을 넘긴 요청은 서버가 실행하지 않는다
MAX_LIFE = 60            # 서버가 받아 주는 최대 기한(초) — 키를 가진 요청도 비신뢰라 서버가 상한을 정한다
CONN_LIFE = MAX_LIFE + 2 * STEP   # 연결 하나의 수명 — 넘으면 서버가 닫아 막힌 송신도 깨운다 (codex1 r5 P2-1)
MAX_CONN = 4

_listener = None
_key = b""
_conns: set = set()
_state = threading.Lock()
_work = threading.Lock()


def _file():
    return config.HOME / "screen.json"


def _mac(key: bytes, nonce: bytes, body: bytes) -> bytes:
    return hmac.new(key, nonce + body, hashlib.sha256).digest()


def _closed(kind=screen.REFUSED) -> dict:
    return {"ok": False, "refused": "VaderAI 가 잠겨 있어요.", "result": kind}


def _deadline(until) -> float | None:
    """요청의 until(벽시계) → monotonic 기한. bool·NaN·무한·지남·너무 먼 미래는 None."""
    if isinstance(until, bool) or not isinstance(until, (int, float)) or not math.isfinite(until):
        return None
    left = until - time.time()
    if left <= 0 or left > MAX_LIFE:
        return None
    return time.monotonic() + left


def _handle(request, deadline: float | None = None) -> dict:
    if not isinstance(request, dict) or not isinstance(request.get("gen"), str):
        return {"ok": False, "refused": "잘못된 요청이에요.", "result": screen.REFUSED}
    gen = request["gen"]
    deadline = deadline or _deadline(request.get("until"))
    if deadline is None or time.monotonic() >= deadline:
        return {"ok": False, "refused": "요청 기한이 맞지 않아요.", "result": screen.REFUSED}
    if screen.current_gen() != gen:                  # 앱 메모리 기준 — 잠김·다른 열림이면 처리하지 않는다
        return _closed()
    op = request.get("op")
    try:
        if op == "apps":
            reply = {"ok": True, "apps": sorted(screen.apps())}
        elif op == "look" and isinstance(request.get("app"), str):
            reply = {"ok": True, **screen.look(request["app"], deadline)}
        elif op == "act" and isinstance(request.get("app"), str):    # 제안만 — 실행은 앱 창에서 사람이 승인할 때
            reply = {"ok": True, **screen.propose(request["app"], request.get("ref"), request.get("action"),
                                                  request.get("value"), request.get("via"))}
        elif op == "table" and isinstance(request.get("app"), str):    # 표 읽기 — 설정이 켠 앱에서 카드 없이
            reply = {"ok": True, **screen.table(request["app"], request.get("table"), deadline)}
        elif op == "diagnose" and all(isinstance(request.get(k), str) for k in ("app", "rune_id", "recording")):
            reply = {"ok": True, **screen.diagnose(request["app"], request["rune_id"], request["recording"], deadline)}
        elif op == "plan" and isinstance(request.get("app"), str):        # 계획 카드만 — 승인은 앱 창에서
            reply = {"ok": True, **screen.plan_propose(request["app"], request.get("steps"), request.get("minutes"))}
        elif op == "step" and isinstance(request.get("plan"), str):
            reply = {"ok": True, **screen.step(request["plan"], request.get("step"), deadline)}
        elif op == "result" and isinstance(request.get("id"), str):
            reply = {"ok": True, **screen.outcome(request["id"])}
        else:
            return {"ok": False, "refused": "모르는 요청이에요.", "result": screen.REFUSED}
    except screen.ScreenError as exc:            # 문장은 중개가 만든 것 — 화면 글자가 없다
        reply = {"ok": False, "refused": str(exc), "result": exc.kind}
    except Exception:
        reply = {"ok": False, "refused": "화면을 읽지 못했어요.", "result": screen.UNKNOWN}
    if time.monotonic() >= deadline:              # 기한을 넘겼다 — 결과를 내지 않는다
        return {"ok": False, "refused": "요청 기한이 지났어요. 화면에서 확인해 주세요.", "result": screen.UNKNOWN}
    return reply if screen.current_gen() == gen else _closed(screen.UNKNOWN)   # 답하기 직전에도


def _close(conn) -> None:
    try:
        conn.close()                 # 대기 중인 송신·수신도 깨어나 오류로 끝난다
    except Exception:                # 수명 정리와 처리 끝이 동시에 닫으면 두 번째는 None 핸들 — 무시
        pass


def _serve_one(conn, key: bytes) -> None:
    reaper = threading.Timer(CONN_LIFE, _close, (conn,))   # 연결 수명 — 답을 안 읽는 상대가 슬롯을 못 잡는다
    reaper.daemon = True
    reaper.start()
    try:
        nonce = os.urandom(32)
        conn.send_bytes(nonce)
        if not conn.poll(STEP):
            return
        raw = conn.recv_bytes(MAX_REQUEST + 32)
        mac, body = raw[:32], raw[32:]
        if len(raw) < 32 or not hmac.compare_digest(mac, _mac(key, nonce, body)):
            return                                # 키를 모르는 상대 — 아무 답도 하지 않는다
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            request = None
        deadline = _deadline(request.get("until")) if isinstance(request, dict) else None
        if deadline is None:
            reply = _handle(request)
        elif not _work.acquire(timeout=max(0.0, deadline - time.monotonic())):   # 기다림도 기한 안에서만
            reply = {"ok": False, "refused": "앱이 바빠 기한 안에 처리하지 못했어요.", "result": screen.REFUSED}
        else:
            try:
                reply = _handle(request, deadline)
            finally:
                _work.release()
        conn.send_bytes(json.dumps(reply, ensure_ascii=False).encode("utf-8"))
    except Exception:            # 수명이 다해 닫힌 연결 등 — 이 연결만 끝낸다
        pass
    finally:
        reaper.cancel()
        with _state:
            _conns.discard(conn)
        _close(conn)


def start() -> None:
    """앱이 뜰 때 한 번."""
    global _listener, _key
    if _listener is not None or os.name != "nt":
        return
    _key = os.urandom(32)
    address = r"\\.\pipe\vaderai-screen-" + uuid.uuid4().hex
    listener = _listener = Listener(address, family="AF_PIPE")   # 인증은 _serve_one 이 기한을 걸고 직접
    key = _key
    config.HOME.mkdir(parents=True, exist_ok=True)
    _file().write_text(json.dumps({"address": address, "key": key.hex(), "pid": os.getpid()}), encoding="utf-8")

    def loop():
        while True:
            try:
                conn = listener.accept()
            except Exception:
                if _listener is not listener:
                    return
                continue
            with _state:
                busy = len(_conns) >= MAX_CONN
                if not busy:
                    _conns.add(conn)
            if busy:
                conn.close()
                continue
            threading.Thread(target=_serve_one, args=(conn, key), daemon=True).start()
    threading.Thread(target=loop, name="screen-broker", daemon=True).start()


def stop() -> None:
    """새 연결을 받지 않고, 열린 연결을 닫는다(처리 중인 것은 세대가 바뀌어 답을 버린다)."""
    global _listener
    listener, _listener = _listener, None
    try:
        _file().unlink(missing_ok=True)
    except OSError:
        pass
    if listener is not None:
        try:
            listener.close()
        except OSError:
            pass
    with _state:
        conns = list(_conns)
    for conn in conns:
        try:
            conn.close()
        except OSError:
            pass


def call(request: dict) -> dict:
    """MCP 쪽: 지금 세대와 기한을 붙여 앱에 요청 하나. 앱이 없으면 닫힌 것으로."""
    closed = {"ok": False, "refused": "VaderAI 앱을 켜고 잠금을 푼 뒤 다시 요청하세요.", "result": screen.REFUSED}
    gen = screen.current_gen()
    if gen is None:
        return closed
    until = time.time() + REPLY_WAIT                 # 연결부터 답까지 한 기한
    end = time.monotonic() + REPLY_WAIT
    try:
        info = json.loads(_file().read_text(encoding="utf-8"))
        key = bytes.fromhex(info["key"])
        conn = Client(info["address"], family="AF_PIPE")
    except Exception:
        return closed
    try:
        if not conn.poll(min(STEP, max(0.0, end - time.monotonic()))):
            return closed
        nonce = conn.recv_bytes(64)
        body = json.dumps({**request, "gen": gen, "until": until}, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_REQUEST:                  # 서버는 큰 요청에 답하지 않는다 — 보내기 전에 분명히 거절
            return {"ok": False, "refused": "요청이 너무 커요. 단계나 값을 줄여 주세요.", "result": screen.REFUSED}
        conn.send_bytes(_mac(key, nonce, body) + body)
        if not conn.poll(max(0.0, end - time.monotonic()) + 1):
            return {"ok": False, "refused": "앱이 제때 답하지 않았어요. 화면에서 확인해 주세요.", "result": screen.UNKNOWN}
        reply = json.loads(conn.recv_bytes(MAX_REPLY).decode("utf-8"))
        return reply if isinstance(reply, dict) else closed
    except Exception:
        return {"ok": False, "refused": "앱과의 연결이 끊겼어요. 화면에서 확인해 주세요.", "result": screen.UNKNOWN}
    finally:
        conn.close()
