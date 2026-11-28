"""Rust 껍데기 뒤의 파이썬 일꾼 (Rust 전환, 2026-11-12).

창·트레이·파일 대화상자는 껍데기(Rust, shell/)가, 업무는 여기 DesktopAPI 가 한다. stdin/stdout 의 JSON 한 줄씩.

껍데기 → 일꾼
  {"ev": "hello", "tray": bool}                  시작 직후 한 번 — 트레이를 만들었는지 (없으면 X 는 숨기지 않고 닫는다)
  {"id": n, "call": "<method>", "args": {...}}   화면의 api(method,args). 답 {"id": n, "result": {...}}
  {"re": k, "result": ...}                       일꾼이 물은 것(ask)의 답
  {"ev": "close"} · {"ev": "quit"} · {"ev": "show"}   창의 X · 트레이 [종료] · 트레이 [열기]
일꾼 → 껍데기
  {"ui": "eval", "js": "..."} · {"ui": "hide|show|restore|minimize"} · {"ui": "badge_close", "rec": n}
  {"ui": "exit"}                                  닫기가 승인됐다 — 창을 내린다. 일꾼은 뒷정리를 한다
  {"ui": "done", "safe": bool, "update": sha256|null}  뒷정리 끝. safe 이고 캐시 패키지가 그 sha256 일 때만 업데이트를 적용한다
  {"ask": k, "ui": "pick", "folder": bool, "multiple": bool, "types": [...]}  → 경로 목록
  {"ask": k, "ui": "badge_open", "rec": n}        → 녹화 배지를 실제로 띄웠으면 true. 닫히면 call record_end {"rec": n}
  {"ask": k, "ui": "notify", "title": "...", "text": "..."}  → 알림 API 가 받았으면 true

stdout 은 이 줄들만 — 다른 출력(print·자식 프로세스)은 stderr 로 돌린다.
끝나는 길은 하나(`stop`): 닫기 승인(X·트레이 종료·로그아웃) · stdin 닫힘 · 쓰기 실패. 끝나면 바로 문을 닫고
(새 호출·잠금 해제·MCP 표식), 대화상자를 풀고, 들어온 호출을 기다린 뒤 잠그고, 작업을 안전 지점까지 멈춘다.
safe 는 닫기 승인 + 작업·콘솔까지 정말 끝남일 때만 — 그때만 껍데기가 **닫을 때** 업데이트를 건다.
(다음 시작 때 Velopack 이 받아 둔 업데이트를 먼저 적용하는 것은 별도 정책 — 파이썬 판과 같은 기본 동작이다.)
"""
from __future__ import annotations

import json
import os
import queue
import sys
import time
from threading import Condition, Event, Lock, Thread

MAX_LINE = 32 * 1024 * 1024   # 붙여 넣은 그림(console_paste_image)까지
_CLOSING = {'ok': False, 'error': '앱을 닫는 중이에요.', 'locked': True}


class Shell:
    """보내기는 큐 + 쓰는 스레드 하나 — 껍데기가 읽기를 멈춰 파이프가 차도 호출·닫기는 막히지 않는다.
    큐가 넘치거나 쓰기가 실패하면 껍데기가 사라진 것으로 친다 (codex1 r1 P2-3, Rust r2 P1-4, r3 C)."""

    QUEUE = 4096

    def __init__(self, out, on_gone=None):
        self._out, self._state = out, Lock()
        self._asks, self._n, self.gone, self.asking = {}, 0, False, True
        self.on_gone = on_gone   # 쓰기 실패를 끝내기 길로 — 읽기가 막혀 있어도
        self._q = queue.Queue(self.QUEUE)
        self._writer = Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    def send(self, **msg):
        try:
            self._q.put_nowait(json.dumps(msg, ensure_ascii=False, default=str) + '\n')
        except queue.Full:
            self.disconnect()

    def _write_loop(self):
        while (line := self._q.get()) is not None:
            try:
                self._out.write(line)
                self._out.flush()
            except (OSError, ValueError):
                self.disconnect()
                return

    def flush(self, timeout=5):
        """끝날 때 — 큐에 남은 줄(마지막 done)을 쓸 때까지만 기다린다. 막혀 있으면 포기한다."""
        try:
            self._q.put(None, timeout=timeout)
        except queue.Full:
            return
        self._writer.join(timeout)

    def ask(self, **msg):
        """답이 올 때까지 이 스레드만 기다린다. 끝나는 중이거나 껍데기가 없으면 바로 None."""
        done, box = Event(), {}
        with self._state:
            if not self.asking:
                return None
            self._n += 1
            k = self._n
            self._asks[k] = (done, box)
        self.send(ask=k, **msg)
        done.wait()
        return box.get('result')

    def answer(self, k, result):
        with self._state:
            slot = self._asks.pop(k, None)
        if slot:
            slot[1]['result'] = result
            slot[0].set()

    def disconnect(self, gone=True):
        """기다리는 물음을 '답 없음' 으로 풀고 새 물음을 받지 않는다(끝나는 중에 대화상자를 새로 열지 않는다,
        codex1 Rust r2 P1-2). gone 이면 껍데기가 사라진 것 — 끝내기 길을 깨운다."""
        with self._state:
            first = gone and not self.gone
            self.gone, self.asking = self.gone or gone, False
            slots, self._asks = list(self._asks.values()), {}
        for done, _ in slots:
            done.set()
        if first and self.on_gone:
            self.on_gone()


class Life:
    """닫기와 호출 접수를 한 잠금에 — 닫기가 시작되면 새 호출을 받지 않고, 들어온 호출이 끝나길 기다린다 (codex1 r1 P1-2)."""

    def __init__(self):
        self._c, self.closing, self._active = Condition(), False, 0

    def enter(self):
        with self._c:
            if self.closing:
                return False
            self._active += 1
            return True

    def leave(self):
        with self._c:
            self._active -= 1
            self._c.notify_all()

    def close(self):
        with self._c:
            self.closing = True

    def drain(self):
        with self._c:
            self._c.wait_for(lambda: self._active == 0)


class Window:
    """DesktopAPI·host 가 쓰는 창 모양을 껍데기 명령으로."""

    def __init__(self, shell):
        self._shell, self.on_destroy = shell, None

    def evaluate_js(self, js):
        self._shell.send(ui='eval', js=js)

    def pick(self, folder=False, multiple=False, types=()):
        return self._shell.ask(ui='pick', folder=folder, multiple=multiple, types=list(types)) or []

    def badge_open(self, rec):
        # 띄운 것을 확인한 뒤에만 녹화를 이어 간다 — 끝내기 버튼 없는 수집을 만들지 않는다 (codex1 r1 P2-1).
        # 배지는 녹화 번호를 들고 있다가 닫힐 때 record_end(rec) 로 돌려준다 (r3 E).
        if self._shell.ask(ui='badge_open', rec=rec) is not True:
            raise RuntimeError('녹화 표시 창을 띄우지 못했어요.')

    def badge_close(self, rec):
        self._shell.send(ui='badge_close', rec=rec)   # 그 번호의 배지만 닫는다 — 늦은 옛 닫기가 새 배지를 닫지 않게 (codex1 Rust r6 P2-3)

    def destroy(self):
        """닫기 승인(close_requested)·로그아웃. 같은 끝내기 길로 (codex1 Rust r2 P1-3). 승인이 먼저 — 보내기에 기대지 않는다."""
        if self.on_destroy:
            self.on_destroy()
        self._shell.send(ui='exit')

    def __getattr__(self, name):
        if name not in ('hide', 'show', 'restore', 'minimize'):
            raise AttributeError(name)
        return lambda: self._shell.send(ui=name)


class Tray:
    """껍데기가 hello 로 트레이가 있다고 알려야 참 — 없으면 X 가 숨기지 않고 닫는다 (codex1 r1 P2-4)."""

    def __init__(self, shell):
        self._shell, self.available = shell, False

    def __bool__(self):
        return self.available

    def notify(self, text, title):
        """알림 API 가 받았으면 True — 아니면 할 일 다시 알림을 '알렸음' 으로 치지 않는다 (codex1 Rust r2 P2-4)."""
        return self._shell.ask(ui='notify', title=title, text=text) is True


def _frame(raw):
    """한 줄을 검사한다. 모양이 틀리면 None — 버린다."""
    if len(raw) > MAX_LINE:
        return None
    try:
        msg = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    if 're' in msg:
        return msg if type(msg['re']) is int else None
    if 'call' in msg:
        ok = type(msg.get('id')) is int and isinstance(msg['call'], str) and isinstance(msg.get('args') or {}, dict)
        return msg if ok else None
    if msg.get('ev') in ('hello', 'close', 'quit', 'show'):
        return msg
    return None


def serve(api, shell, lines, window, tray, life=None):
    """줄을 읽는 스레드를 띄우고, 끝내기 길(stop)이 열릴 때까지 기다렸다가 정리한다.
    돌려주는 값: 닫기가 승인됐는가 (stdin 닫힘·쓰기 실패는 False)."""
    from vader.desktop import host
    life = life or Life()
    stop, approved = Event(), Event()

    def approve():
        approved.set()
        stop.set()
    window.on_destroy = approve
    shell.on_gone = stop.set

    def call(msg):
        if not life.enter():
            shell.send(id=msg['id'], result=_CLOSING)
            return
        try:
            result = api.call(msg['call'], msg.get('args'))
        finally:
            life.leave()
        shell.send(id=msg['id'], result=result)

    def read():
        try:
            for raw in lines:
                msg = _frame(raw)
                if msg is None:
                    continue
                if 're' in msg:
                    shell.answer(msg['re'], msg.get('result'))
                elif stop.is_set():               # 끝나는 중 — 답(re)만 받고, 호출은 바로 거절(껍데기가 기다리지 않게)
                    if 'call' in msg:
                        shell.send(id=msg['id'], result=_CLOSING)
                elif 'call' in msg:
                    Thread(target=call, args=(msg,), daemon=True).start()
                elif msg['ev'] == 'hello':
                    tray.available = msg.get('tray') is True
                elif msg['ev'] == 'show':
                    host._show(api, window)
                else:
                    if msg['ev'] == 'quit':
                        api._quitting = True
                    if host.close_requested(api, window, tray):
                        window.destroy()                  # exit 를 알리고 approve
        finally:
            shell.disconnect()                    # stdin 닫힘 = 껍데기가 사라짐

    Thread(target=read, daemon=True).start()
    stop.wait()
    return _finish(api, shell, life, approved.is_set())


def _finish(api, shell, life, approved):
    """바로 문을 닫고(새 호출·잠금 해제·MCP 표식) → 대화상자를 풀고 → 들어온 호출이 끝나길 기다린 뒤 →
    잠그고(녹화·배지) → 작업을 안전 지점까지 멈춘다 (codex1 Rust r1 P1-1, r2 P1-1·2)."""
    life.close()
    api.begin_shutdown()
    api.chat_off('창을 닫는 중이에요.')
    shell.disconnect(gone=not approved)
    life.drain()
    api.lock()
    controller = api._controller
    if controller is not None:
        # ponytail: 취소를 확인하지 않는 외부 호출에 막히면 여기서 계속 기다린다 — 저장 중인 RPA 를 끊지 않는 쪽을 택했다.
        # 문은 이미 닫혀 있다(begin_shutdown). 업데이트도 안 건다(일꾼이 끝나야 껍데기가 판단).
        while not controller.begin_close():
            if controller.active:
                try:
                    controller.cancel(controller.active)
                except Exception:
                    pass
            time.sleep(2)
    return approved


def _std_in_to_nul():
    """프로세스의 표준 입력을 NUL 로 — 줄은 복제한 핸들로만 읽는다.
    읽는 스레드가 파이프에서 기다리는 동안 Windows 는 그 파이프의 다른 동기 호출을 줄 세운다. 네이티브 DLL(numpy 등)은
    로드될 때 표준 입력을 조회하므로, 그대로 두면 다음 줄이 올 때까지 import 가 멈춘다(2026-11-12 PIN 열기 멈춤).
    자식 프로세스도 NUL 을 물려받아 규약 줄을 가로채지 못한다."""
    nul = os.open(os.devnull, os.O_RDONLY)
    os.dup2(nul, 0)
    os.close(nul)
    sys.stdin = open(os.devnull, encoding='utf-8')
    if os.name == 'nt':
        import ctypes
        import msvcrt
        ctypes.windll.kernel32.SetStdHandle(-10, msvcrt.get_osfhandle(0))   # STD_INPUT_HANDLE


def main():
    out = os.fdopen(os.dup(1), 'w', encoding='utf-8', newline='\n')
    os.dup2(2, 1)                 # 남은 fd 1 쓰기(자식 프로세스 포함)는 stderr 로 — 줄 규약을 깨지 않게. import 보다 먼저
    inp = os.fdopen(os.dup(0), 'rb')
    _std_in_to_nul()
    sys.stdout = sys.stderr
    from vader.core import recorder
    from vader.core.crash import install_exception_hooks
    from vader.desktop import host
    install_exception_hooks()
    from vader.core import diag
    diag.start()                  # 진단 기록 — 환경·파이썬 밖 충돌 위치 (2026-11-28, 이 PC 에만)
    # 껍데기 창도 녹화에서 뺀다 — 껍데기가 환경변수로 준 자기 PID(개발 실행은 부모가 venv 런처라). 웹 요청이 정하지 못한다
    recorder.SHELL_PID = int(os.environ.get('VADER_SHELL_PID') or os.getppid())
    shell = Shell(out)
    if not host.claim_app():
        shell.send(ui='exit')
        shell.send(ui='done', safe=False)
        shell.flush()
        return
    api = host.DesktopAPI()
    window, tray = Window(shell), Tray(shell)
    api._window = window
    host.start(api, tray)
    approved = clean = False
    try:
        approved = serve(api, shell, (line.decode('utf-8', 'replace') for line in inp), window, tray)
    finally:
        # 정리는 늘 해 본다. 업데이트는 껍데기가 — 승인 + 작업·콘솔 자식까지 정말 끝났을 때만 safe (codex1 r1, r2 P2-5, r3 D)
        try:
            clean = host.shutdown(api, apply_update=False)
        finally:
            from vader.core import update
            shell.send(ui='done', safe=approved and clean, update=update.verified_sha())
            shell.flush()


if __name__ == '__main__':
    main()
