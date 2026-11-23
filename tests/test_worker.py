"""Rust 껍데기 ↔ 파이썬 일꾼 줄 규약 (vader/desktop/worker.py)."""
import io
import json
import queue
import threading
import time

import pytest

from vader.desktop import host, worker


class Out(io.StringIO):
    """일꾼이 쓴 줄을 하나씩 꺼낸다."""

    def __init__(self, broken=False):
        super().__init__()
        self.q, self.broken = queue.Queue(), broken

    def write(self, s):
        if self.broken:
            raise BrokenPipeError
        for line in s.splitlines():
            self.q.put(json.loads(line))
        return len(s)

    def next(self):
        return self.q.get(timeout=5)


class Api:
    _controller = None
    _quitting = False
    _hidden = False
    _open = True

    def __init__(self):
        self.locked = 0
        self.closing = False
        self.gate = threading.Event()

    def call(self, method, args=None):
        if method == 'pick':
            return {'ok': True, 'data': self._window.pick(folder=True)}
        if method == 'logout':
            threading.Thread(target=self._window.destroy, daemon=True).start()
            return {'ok': True, 'data': {'closing': True}}
        if method == 'late_pick':
            self.gate.wait(5)
            return {'ok': True, 'data': self._window.pick()}
        if method == 'slow':
            self.gate.wait(5)
            return {'ok': True, 'data': 'slow done', 'open_when_done': self._open}
        return {'ok': True, 'data': [method, args]}

    def chat_off(self, reason):
        self.off = reason

    def begin_shutdown(self):
        self.closing = True

    def lock(self):
        self._open = False
        self.locked += 1


def run(lines, api=None, out=None, hello=True):
    out, inbox = out or Out(), queue.Queue()
    shell = worker.Shell(out)
    api = api or Api()
    window, tray = worker.Window(shell), worker.Tray(shell)
    api._window = window

    def feed():
        while (line := inbox.get()) is not None:
            yield line
    box = {}
    t = threading.Thread(target=lambda: box.update(safe=worker.serve(api, shell, feed(), window, tray)), daemon=True)
    t.start()
    for line in ([{'ev': 'hello', 'tray': True}] if hello else []) + lines:
        inbox.put(json.dumps(line))
    return out, inbox, t, box


def test_a_call_is_answered_with_its_id():
    out, inbox, t, _ = run([{'id': 7, 'call': 'todo_list', 'args': {'a': 1}}])
    assert out.next() == {'id': 7, 'result': {'ok': True, 'data': ['todo_list', {'a': 1}]}}
    inbox.put(None)
    t.join(5)
    assert not t.is_alive()


def test_a_dialog_asks_the_shell_and_other_calls_keep_flowing():
    out, inbox, t, _ = run([{'id': 1, 'call': 'pick'}])
    ask = out.next()
    assert ask == {'ask': 1, 'ui': 'pick', 'folder': True, 'multiple': False, 'types': []}
    inbox.put(json.dumps({'id': 2, 'call': 'todo_list'}))          # 대화상자가 떠 있어도 다른 호출은 돈다
    assert out.next()['id'] == 2
    inbox.put(json.dumps({'re': ask['ask'], 'result': ['C:/가나메디칼']}))
    assert out.next() == {'id': 1, 'result': {'ok': True, 'data': ['C:/가나메디칼']}}
    inbox.put(None)
    t.join(5)


def test_a_dead_shell_releases_a_waiting_dialog_and_locks_the_door():
    api = Api()
    out, inbox, t, box = run([{'id': 1, 'call': 'pick'}], api=api)
    out.next()
    inbox.put(None)                                                # stdin 닫힘
    assert out.next() == {'id': 1, 'result': {'ok': True, 'data': []}}
    t.join(5)
    assert not t.is_alive() and box['safe'] is False
    assert api.locked == 1 and api.off == '창을 닫는 중이에요.'


def test_a_late_ask_after_the_shell_is_gone_does_not_wait():
    shell = worker.Shell(Out())
    shell.disconnect()
    assert worker.Window(shell).pick() == []


def test_closing_waits_for_calls_in_flight_and_refuses_new_ones():
    """초기화 같은 진행 중인 호출이 끝난 뒤에야 문을 잠근다 — 늦게 끝난 호출이 다시 여는 일이 없게 (codex1 r1 P1-2)."""
    api = Api()
    out, inbox, t, box = run([{'id': 1, 'call': 'slow'}], api=api, hello=False)   # 트레이 없음 → X 는 닫기
    inbox.put(json.dumps({'ev': 'close'}))
    assert out.next() == {'ui': 'exit'}
    inbox.put(json.dumps({'id': 2, 'call': 'todo_list'}))
    assert out.next() == {'id': 2, 'result': worker._CLOSING}      # 새 호출은 거절
    for _ in range(200):                                           # 끝내기 스레드가 곧 문을 닫는다
        if api.closing:
            break
        time.sleep(.01)
    assert api.closing and t.is_alive() and api.locked == 0       # 문은 바로 닫고, 잠금은 호출 1 이 끝난 뒤
    api.gate.set()
    first = out.next()
    assert first['id'] == 1 and first['result']['data'] == 'slow done'
    t.join(5)
    assert api.locked == 1 and box['safe'] is True


def test_a_broken_pipe_counts_as_the_shell_going_away():
    out, inbox, t, box = run([{'id': 1, 'call': 'todo_list'}], out=Out(broken=True))
    t.join(5)
    assert not t.is_alive() and box['safe'] is False


@pytest.mark.parametrize('raw', ['null', '3', '[1]', '{"re": [1]}', '{"id": "x", "call": "a"}', '{"id": 1, "call": "a", "args": [1]}', '{"ev": "nope"}', '{oops'])
def test_malformed_frames_are_dropped(raw):
    out, inbox, t, _ = run([])
    inbox.put(raw)
    inbox.put(json.dumps({'id': 5, 'call': 'todo_list'}))
    assert out.next()['id'] == 5
    inbox.put(None)
    t.join(5)


def test_x_hides_to_the_tray_and_quit_closes():
    api = Api()
    out, inbox, t, box = run([{'ev': 'close'}], api=api)
    got = [out.next(), out.next(), out.next()]
    assert got[:2] == [{'ui': 'eval', 'js': 'window.vaderTray=true'}, {'ui': 'hide'}]
    assert got[2]['ui'] == 'notify'                                # 처음 숨길 때 한 번 알린다
    assert api._hidden and t.is_alive()
    inbox.put(json.dumps({'ev': 'quit'}))
    assert out.next() == {'ui': 'exit'}
    t.join(5)
    assert not t.is_alive() and box['safe'] is True


def test_without_a_tray_x_closes_instead_of_hiding():
    out, inbox, t, box = run([{'ev': 'close'}], hello=False)
    assert out.next() == {'ui': 'exit'}
    t.join(5)
    assert box['safe'] is True


def test_a_busy_job_blocks_closing_and_shows_the_window():
    api = Api()
    api._controller = type('C', (), {'begin_close': lambda self: False})()
    out, inbox, t, _ = run([{'ev': 'quit'}], api=api)
    seen = [out.next()['ui'] for _ in range(4)]
    assert seen == ['show', 'restore', 'eval', 'eval']
    assert t.is_alive()
    api._controller = None
    inbox.put(json.dumps({'ev': 'quit'}))
    assert out.next() == {'ui': 'exit'}


def test_the_badge_must_really_open_before_recording_goes_on():
    out = Out()
    shell = worker.Shell(out)
    window = worker.Window(shell)
    box = {}
    t = threading.Thread(target=lambda: box.update(err=_try(lambda: window.badge_open(1))))
    t.start()
    ask = out.next()
    assert ask['ui'] == 'badge_open'
    shell.answer(ask['ask'], False)
    t.join(5)
    assert isinstance(box['err'], RuntimeError)


def _try(fn):
    try:
        fn()
    except Exception as exc:
        return exc


def test_the_desktop_api_no_longer_imports_pywebview_outside_main():
    """일꾼(Rust 껍데기 뒤)에는 pywebview 가 없다 — DesktopAPI 는 창 모양(pick·badge_*)만 쓴다."""
    import inspect
    src = inspect.getsource(host.DesktopAPI)
    assert 'import webview' not in src and 'create_file_dialog' not in src


def test_the_shell_process_is_also_ours_for_recording(monkeypatch):
    pytest.importorskip("pywinauto", reason="pip install .[eonm]")
    from vader.core import recorder
    monkeypatch.setattr(recorder, 'SHELL_PID', 4242)
    monkeypatch.setattr(recorder, '_pid_of', lambda hwnd: 4242)
    assert recorder.probe_at(1, 1, skip_pid=1) is None


def test_logout_alone_ends_the_worker():
    """로그아웃의 window.destroy 도 같은 끝내기 길 — 트레이 [종료] 를 또 누르지 않아도 끝난다 (codex1 Rust r2 P1-3)."""
    api = Api()
    out, inbox, t, box = run([{'id': 1, 'call': 'logout'}], api=api)
    got = [out.next(), out.next()]
    assert {'ui': 'exit'} in got
    t.join(5)
    assert not t.is_alive() and box['safe'] is True and api.locked == 1


def test_an_ask_made_after_closing_started_is_cancelled_not_waited_for():
    """ask 직전에 멈춘 호출 → 닫기 승인 → 그 뒤 ask 는 바로 '안 고름' (codex1 Rust r2 P1-2)."""
    api = Api()
    out, inbox, t, box = run([{'id': 1, 'call': 'late_pick'}], api=api, hello=False)
    inbox.put(json.dumps({'ev': 'close'}))
    assert out.next() == {'ui': 'exit'}
    api.gate.set()
    got = out.next()
    if 'ask' in got:      # 닫기 정리보다 먼저 물었으면 물음이 나가고 곧 풀린다(껍데기는 exit 뒤의 물음을 띄우지 않는다)
        got = out.next()
    assert got == {'id': 1, 'result': {'ok': True, 'data': []}}
    t.join(5)
    assert not t.is_alive() and box['safe'] is True


def test_shutdown_closes_the_door_at_once_even_while_unlock_holds_it(monkeypatch):
    """긴 초기화가 문(_door)을 잡고 있어도 바로 닫힌다 — 새 호출 거절·늦게 끝난 잠금 해제는 열지 않음 (codex1 Rust r2 P1-1)."""
    from unittest.mock import MagicMock
    from vader.core import gate
    marks = []
    monkeypatch.setattr(gate, 'mark', lambda folder, opened: marks.append(opened))
    api = host.DesktopAPI(controller=MagicMock())
    api._folder = 'x'
    with api._door:
        api.begin_shutdown()
    assert marks == [False]
    assert api.call('todo_list')['locked'] and api.call('gate_status')['locked']
    api._controller = None
    with pytest.raises(ValueError):
        api._unlock('x')


def test_a_failed_notification_does_not_use_up_the_reminder():
    calls = []

    class A:
        _hidden = False

        def call(self, method, args=None):
            calls.append(method)
            return {'ok': True, 'data': ['c1'] if method == 'todo_due' else {}}
    tray = type('T', (), {'notify': lambda self, text, title: False})()
    ticks = iter([None])
    with pytest.raises(StopIteration):
        host._watch_messenger(A(), tray, sleep=lambda s: next(ticks))
    assert 'todo_due' in calls and 'todo_ack' not in calls


def test_a_heartbeat_in_progress_cannot_reopen_the_marker_after_shutdown(monkeypatch):
    """열림 표식 쓰기 도중에 끝내기가 오면, 닫힘이 마지막 표식이 된다 (codex1 Rust r3 A)."""
    from unittest.mock import MagicMock
    from vader.core import gate
    marks, writing, release = [], threading.Event(), threading.Event()

    def mark(folder, opened, gen=""):
        if opened:
            writing.set()
            release.wait(5)
        marks.append(opened)
    monkeypatch.setattr(gate, 'mark', mark)
    api = host.DesktopAPI(controller=MagicMock())
    api._folder = 'x'
    t = threading.Thread(target=lambda: api._unlock('x'))
    t.start()
    writing.wait(5)                                   # 열림 표식을 쓰는 중
    s = threading.Thread(target=api.begin_shutdown)
    s.start()
    time.sleep(.05)
    assert marks == []                                # 끝내기는 그 쓰기가 끝나길 기다린다
    release.set()
    t.join(5)
    s.join(5)
    assert marks == [True, False]


def test_a_console_that_will_not_die_is_not_clean(monkeypatch):
    """화면의 '끝남' 이 아니라 OS 프로세스로 본다 — 안 죽으면 업데이트를 걸지 않는다 (codex1 Rust r3 B)."""
    from vader.desktop import console as console_mod
    con = console_mod.Console.__new__(console_mod.Console)
    con._cond, con._pty, con._ended = threading.Condition(), type('P', (), {'pid': 1, 'write': lambda self, x: None})(), False
    monkeypatch.setattr(console_mod, '_alive', lambda pty: True)
    monkeypatch.setattr(console_mod.subprocess, 'run', lambda *a, **k: type('R', (), {'returncode': 128})())
    monkeypatch.setattr(console_mod.time, 'sleep', lambda s: None)
    monkeypatch.setattr(con, '_end', lambda: None)
    assert con.stop(grace=0) is False


def test_an_old_badge_closing_late_does_not_end_the_new_recording():
    """늦게 온 옛 배지의 닫힘(rec 1)은 새 녹화(rec 2)를 끝내지 않는다 (codex1 Rust r3 E)."""
    from unittest.mock import MagicMock
    api = host.DesktopAPI(controller=MagicMock())
    api._window = MagicMock()
    api._badge = 2
    assert api._record_end(rec=1) is True and api._badge == 2
    api._record_end(rec=2)
    assert api._badge is None


def test_a_stuck_pipe_does_not_block_closing():
    """껍데기가 읽기를 멈춰도(쓰기가 안 돌아옴) 보내기는 막히지 않고, 큐가 넘치면 사라진 것으로 친다 (codex1 Rust r3 C)."""
    stuck = threading.Event()

    class Stuck(io.StringIO):
        def write(self, s):
            stuck.wait(10)
            return len(s)
    gone = threading.Event()
    shell = worker.Shell(Stuck(), on_gone=gone.set)
    for _ in range(worker.Shell.QUEUE + 2):
        shell.send(ui='eval', js='x')               # 막히지 않고 돌아온다
    assert gone.is_set() and shell.gone
    stuck.set()


def test_a_console_whose_state_cannot_be_read_is_not_clean(monkeypatch):
    """생존 조회가 실패하면(모름) '끝남' 으로 치지 않는다 (codex1 Rust r4 P2-1)."""
    from vader.desktop import console as console_mod

    class P:
        pid = 1

        def write(self, x):
            pass

        def isalive(self):
            raise OSError('조회 실패')
    con = console_mod.Console.__new__(console_mod.Console)
    con._cond, con._pty, con._ended = threading.Condition(), P(), False
    monkeypatch.setattr(console_mod.subprocess, 'run', lambda *a, **k: type('R', (), {'returncode': 0})())
    monkeypatch.setattr(console_mod.time, 'sleep', lambda s: None)
    monkeypatch.setattr(con, '_end', lambda: None)
    assert con.stop(grace=0) is False


def test_a_late_record_end_cannot_clear_a_newer_recording():
    """번호 확인과 비우기가 한 걸음 — 옛 번호로 온 끝내기는 새 녹화를 비우지 못한다 (codex1 Rust r4 P2-2)."""
    from unittest.mock import MagicMock
    api = host.DesktopAPI(controller=MagicMock())
    api._window = MagicMock()
    api._badge = 3
    results = []
    ts = [threading.Thread(target=lambda r=r: results.append(api._record_end(rec=r))) for r in (1, 2, 1, 2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)
    assert api._badge == 3 and api._window.badge_close.call_count == 0


def test_the_pywebview_badge_button_carries_its_recording_number():
    calls = []
    bridge = host._BadgeApi(type('A', (), {'call': lambda self, m, a=None: calls.append((m, a))})(), 7)
    bridge.call('record_end')
    bridge.call('todo_list')
    assert calls == [('record_end', {'rec': 7})]


def test_end_pressed_while_the_badge_is_still_opening_ends_that_recording():
    """배지가 뜨는 중(ask 답 전)에 누른 끝내기도 받는다 — 녹화가 되살아나거나 큰 창이 내려가지 않는다 (codex1 Rust r5 P2-2)."""
    from unittest.mock import MagicMock
    api = host.DesktopAPI(controller=MagicMock())
    api._open = True
    win = api._window = MagicMock()
    win.badge_open.side_effect = lambda rec: api._record_end(rec=rec)   # 띄우자마자 끝내기
    api._record_start('s', 'navigation')
    assert api._badge is None and win.badge_close.called and not win.minimize.called


def test_end_between_the_check_and_minimize_leaves_the_window_up():
    """확인 뒤·내리기 전에 끝내면 다시 올린다 — 마지막 창 명령은 restore (codex1 Rust r6 P2-2)."""
    from unittest.mock import MagicMock
    api = host.DesktopAPI(controller=MagicMock())
    api._open = True
    win = api._window = MagicMock()
    win.minimize.side_effect = lambda: api._record_end(rec=api._badge)   # 내리는 순간 끝내기가 끼어든다
    api._record_start('s', 'navigation')
    names = [c[0] for c in win.method_calls if c[0] in ('minimize', 'restore')]
    assert api._badge is None and names[-1] == 'restore'
    win.badge_close.assert_called_once_with(1)


def test_badge_close_names_its_recording():
    sent = []
    w = worker.Window(type('S', (), {'send': lambda self, **m: sent.append(m)})())
    w.badge_close(4)
    assert sent == [{'ui': 'badge_close', 'rec': 4}]


def test_an_old_pywebview_badge_close_cannot_take_the_new_badge():
    """옛 번호의 닫기는 새 배지를 떼어 내지 못한다 (codex1 Rust r7 P2-1)."""
    from unittest.mock import MagicMock
    pw = host._PyWindow(MagicMock(), MagicMock())
    new = MagicMock()
    pw._badge = (2, new)
    pw.badge_close(1)
    assert pw._badge == (2, new) and not new.destroy.called
    pw.badge_close(2)
    assert pw._badge is None and new.destroy.called


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows 파이프 동기 I/O 줄 세우기')
def test_the_process_stdin_is_nul_so_dll_loads_do_not_wait_on_the_pipe():
    """읽는 스레드가 파이프에서 기다리는 동안 DLL 이 표준 입력을 조회하면 멈춘다 — 표준 입력은 NUL 이어야 한다 (2026-11-12)."""
    import subprocess
    import sys
    code = ("import ctypes; from vader.desktop import worker; worker._std_in_to_nul(); "
            "k = ctypes.windll.kernel32; k.GetStdHandle.restype = ctypes.c_void_p; "
            "print(k.GetFileType(ctypes.c_void_p(k.GetStdHandle(-10))))")
    r = subprocess.run([sys.executable, '-c', code], stdin=subprocess.PIPE, capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == '2', r.stderr   # FILE_TYPE_CHAR(NUL) — 파이프(3)가 아니다
