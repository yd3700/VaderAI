"""화면 자동화 엔진 중개 — agent-desktop(vercel-labs, Apache-2.0)을 이 모듈만 부른다.

룬·AI·MCP 는 엔진을 직접 실행하지 않는다. 여기서 명령·대상·승인·잠금·정보 반출을 검사한 뒤
정해진 argv 로 한 번씩 부른다 (codex1 agent-desktop 검토 P1-1, 마스터 2026-11-12 4단계 계획).

엔진 (codex1 0단계 P1-1)
- 설치본은 앱 옆 `agent-desktop.exe`, 개발은 `tools/agent-desktop/`. PATH·npm 은 찾지 않는다.
- 실행 전에 그 파일의 SHA-256 을 `screen_pin.py`(빌드가 만든다, 앱 묶음 안에 들어간다) 값과 대조한다.
  exe 옆의 해시 파일은 믿지 않는다. 링크·다른 경로면 거절. ponytail: 같은 사용자 권한의 악성 프로세스가
  묶음까지 바꾸는 것은 못 막는다 — OS 격리가 필요하면 비관리자 AI + 권한 broker 로.

수명 (codex1 0단계 P1-2, r2 P1-1)
- 엔진 호출은 프로세스 안에서 한 번에 하나(_run_lock). 호출마다 자기 상태 폴더(불변 경로)를 받는다.
- 호출은 시작 때 gate 세대와 정리 회차(_epoch)를 잡는다. sweep 은 회차를 올리고 등록된 엔진을 끝낸다.
  sweep 뒤에 뜬 엔진은 등록하려다 회차가 바뀐 것을 보고 스스로 끝낸다.
- 끝난 뒤 세대·회차가 바뀌었으면: 엔진이 정말 끝났는지 확인 → 그 호출의 폴더를 지움 → '모름'.
- 지우지 못하면(또는 엔진이 안 끝나면) `screen.dirty` 표식 — 어느 프로세스든 표식이 있으면 새 호출을 거절,
  앱이 다음에 다 지우면 표식을 뗀다. host 는 sweep 의 False 를 '깨끗하지 않음' 으로 다룬다.
- 엔진은 앱(일꾼) 프로세스만 띄운다(screen_broker). 멈춘 채 만들어 KILL_ON_JOB_CLOSE Job 에 넣고 깨운다 —
  앱이 죽으면 OS 가 엔진도 끝낸다. 열림 상태는 앱 메모리(authority)가 파일 맥박보다 우선.

데이터 저장 지점 (codex1 0단계 요청 5)
| 곳 | 원문 | 지우기 |
| 작업공간/screen/<호출 폴더> (엔진 ref·스냅샷) | 화면 이름·값 원문 | 엔진이 제 권한(DACL)으로 만든다. 세대가 바뀐 호출은 즉시, 새 관찰은 이전 관찰 폴더를, 앱 잠금·끝·시작은 전부 |
| 작업공간/screen.dirty | 없음(빈 표식) | 다 지운 sweep 이 뗀다 |
| HOME/screen.json (창구 주소·키·PID) | 화면 정보 없음. 키는 같은 사용자면 읽힘 | 앱이 끝날 때 지움 |
| 창구 요청·응답 | 정제된 결과만(값·제목 없음) | 메모리, 저장 안 함 |
| 자식 stdout/stderr | 원문 | 메모리에서만, 상한 4MB/64KB, 버린다. 로그·예외에 안 넣는다 |
| 오류 문장 | 엔진 오류 코드(알려진 것만) | — |
| 작업공간/screen_tables/*.tsv (표 읽기) | 표 원문 | 룬이 읽으면 바로. 아니면 만든 지 24시간 넘은 것을 계정 연 때·잠금·끝·표 읽기·한 시간마다 지운다(앱이 꺼진 동안은 다음 실행 때) |
| 일지 | 'screen_look'·앱 key·노드 수 | 일지 보존 규칙 |
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from vader.core import config, gate, journal

ENGINE_VERSION = "1.2.0"
ENVELOPE = "2.6"
TIMEOUT = 20
MAX_OUTPUT = 4 * 1024 * 1024
MAX_ERR = 64 * 1024
MAX_NODES = 1000          # 실제 EMR 메인 메뉴가 요소 648개 (2026-11-14 실PC)
SNAP_MS = 15000            # 엔진 관찰·ref 찾기 기한 상한 — 기본(3초·5초)이면 큰 메뉴 창을 다 못 훑는다(실PC 7~12초).
                           # 실제로는 남은 기한에서 여유를 뺀 값(_engine_ms) — 부모가 먼저 끊지 않게
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SUSPENDED = 0x00000004 if os.name == "nt" else 0

# 이 단계까지 열린 명령만 (단계마다 codex1 검증 뒤 늘린다). 세대 검사 없이 돌 수 있는 것은 진단 둘뿐
COMMANDS = {"version", "status", "list-windows", "snapshot", "click", "check", "uncheck", "set-value"}
UNGUARDED = {"version", "status"}
# 엔진 오류 코드 — 이것만 밖에 보인다 (response/error_code.rs)
CODES = {"PERM_DENIED", "ELEMENT_NOT_FOUND", "APP_NOT_FOUND", "ACTION_FAILED", "ACTION_NOT_SUPPORTED", "STALE_REF",
         "AMBIGUOUS_TARGET", "WINDOW_NOT_FOUND", "PLATFORM_NOT_SUPPORTED", "TIMEOUT", "INVALID_ARGS",
         "NOTIFICATION_NOT_FOUND", "SNAPSHOT_NOT_FOUND", "SANDBOX_NOT_FOUND", "POLICY_DENIED", "APP_UNRESPONSIVE",
         "INTERNAL"}
ACTIONS = {"Click", "SetFocus", "SetValue", "Toggle", "Select", "Expand", "Collapse", "Scroll", "ScrollIntoView",
           "Invoke", "Check", "Uncheck", "DoubleClick", "RightClick"}
_REF = re.compile(r"^@[a-z0-9]{1,40}:e\d{1,6}$")
# 엔진의 Windows 역할 이름 전부 (crates/windows/src/tree/roles.rs) — 이 밖의 값은 모양이 틀린 답
ROLES = {"button", "cell", "checkbox", "column", "combobox", "dialog", "document", "grid", "group", "handle", "image",
         "incrementor", "link", "list", "listbox", "menu", "menubutton", "menuitem", "option", "outline", "progressbar",
         "radiobutton", "row", "scrollbar", "separator", "slider", "statictext", "status", "switch", "tab", "table",
         "tablist", "textfield", "toolbar", "tooltip", "treeitem", "unknown", "window", "windows"}
MAX_VISIT = 5000

# 결과 분류 — 업무에서 '안 됐다' 와 '됐는지 모른다' 를 섞지 않는다 (codex1 §3)
REFUSED = "refused"      # 보내기 전에 거절 — 화면은 그대로
DONE = "done"            # 엔진이 보냈고 성공이라 답함 — 업무 결과는 따로 다시 읽어 확인
UNKNOWN = "unknown"      # 시간 초과·죽음·알 수 없는 답·그 사이 잠김 — 보냈을 수도 있다. 다시 하지 않는다


class ScreenError(Exception):
    def __init__(self, message: str, kind: str = REFUSED):
        super().__init__(message)
        self.kind = kind


def _pin() -> str | None:
    try:
        from vader.core import screen_pin
    except ImportError:
        return None
    return getattr(screen_pin, "SHA256", None)


def engine() -> Path:
    if config.FROZEN:
        path = Path(sys.executable).parent / "agent-desktop.exe"
    else:
        path = Path(__file__).parents[2] / "tools" / "agent-desktop" / "agent-desktop.exe"
    pin = _pin()
    if not pin or not path.is_file() or path.is_symlink() or os.path.realpath(path) != os.path.abspath(path):
        raise ScreenError("화면 자동화 엔진이 설치되어 있지 않아요.")
    if hashlib.sha256(path.read_bytes()).hexdigest() != pin:
        raise ScreenError("화면 자동화 엔진이 설치본과 달라요. 다시 설치해 주세요.")
    return path


# 앱(일꾼)이 자기 열림 상태를 알려 준다 — 파일 맥박(최대 15초 늦음)보다 우선. 없으면(다른 프로세스) gate 파일.
authority = None


def current_gen() -> str | None:
    if authority is not None:
        gen = authority()
        return gen if isinstance(gen, str) and gen else None
    return gate.session_gen()


_lock = threading.Lock()          # 아래 상태
_run_lock = threading.Lock()      # 엔진 호출은 한 번에 하나
_ghost_proc: subprocess.Popen | None = None
_ghost_q: "queue.Queue" = None   # (좌표, 열림) 하나만 — 차 있으면 새 그림은 버린다
_ghost_lock = threading.Lock()
_procs: set[subprocess.Popen] = set()
_epoch = 0                        # sweep 회차
_dirty = False                    # 이 프로세스가 정리 실패를 봤다 — 표식 파일을 못 써도 잊지 않는다


def _root() -> Path:
    return config.data_dir() / "screen"


def _dirty_mark() -> Path:
    return config.data_dir() / "screen.dirty"


def new_home() -> Path:
    """호출(관찰) 하나의 상태 폴더 경로. 만들지 않는다 — 엔진이 자기만 읽는 권한(DACL)으로 만들고,
    다른 권한으로 이미 있으면 거절한다 (실기 2026-11-12: 'private file DACL grants an untrusted principal')."""
    return _root() / uuid.uuid4().hex


def _remove(path: Path) -> bool:
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


def _set_dirty(on: bool) -> bool:
    """정리 실패 표식. 켜기는 메모리 latch 를 먼저 — 파일을 못 써도 이 프로세스는 거절한다.
    끄기는 파일을 정말 뗐을 때만 latch 를 푼다. 반환: 원하는 상태가 됐는가."""
    global _dirty
    if on:
        _dirty = True
    try:
        if on:
            _dirty_mark().parent.mkdir(parents=True, exist_ok=True)
            _dirty_mark().touch()
        else:
            _dirty_mark().unlink(missing_ok=True)
    except OSError:
        return False
    if not on:
        _dirty = False
    return True


_job = None


def _in_job(proc: subprocess.Popen) -> None:
    """KILL_ON_JOB_CLOSE Job 에 넣는다 — 이 프로세스가 비정상 종료해도 엔진이 남지 않는다.
    엔진은 멈춘 채(CREATE_SUSPENDED) 만들어졌다 — 넣은 뒤에야 깨운다(_resume). 그래서 Job 밖에서 도는 틈이 없다."""
    global _job
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    if _job is None:
        class Basic(ctypes.Structure):
            _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                        ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD),
                        ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", ctypes.c_uint64 * 6), ("p", ctypes.c_size_t),
                        ("j", ctypes.c_size_t), ("pp", ctypes.c_size_t), ("jp", ctypes.c_size_t)]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise ScreenError("화면 자동화 엔진을 묶지 못했어요.")
        info = Extended()
        info.Basic.LimitFlags = 0x2000                     # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle(job)
            raise ScreenError("화면 자동화 엔진을 묶지 못했어요.")
        _job = job
    if not k32.AssignProcessToJobObject(_job, wintypes.HANDLE(int(proc._handle))):
        raise ScreenError("화면 자동화 엔진을 묶지 못했어요.")


def _resume(proc: subprocess.Popen) -> None:
    """멈춘 채 만든 엔진을 깨운다 (Popen 이 주 스레드 핸들을 닫아 프로세스 단위로)."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    nt = ctypes.windll.ntdll
    nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
    nt.NtResumeProcess.restype = ctypes.c_long
    if nt.NtResumeProcess(wintypes.HANDLE(int(proc._handle))) != 0:
        raise ScreenError("화면 자동화 엔진을 시작하지 못했어요.")


def _left(deadline: float | None) -> float:
    """남은 시간(초). 기한이 없으면 TIMEOUT."""
    return TIMEOUT if deadline is None else deadline - time.monotonic()


def _stop(proc: subprocess.Popen) -> bool:
    """엔진을 끝내고 정말 끝났는지. 안 끝났으면 False — 그 폴더를 깨끗하다고 하지 않는다."""
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(5)
        return True
    except subprocess.TimeoutExpired:
        return False


def sweep() -> bool:
    """잠금·끝·시작: 회차를 올리고, 이 프로세스가 띄운 엔진을 끝내고, 모든 상태 폴더를 지운다.
    다 끝내고 다 지웠으면 True(표식도 뗀다). 아니면 표식을 남기고 False — 새 호출은 거절된다."""
    global _epoch
    with _lock:
        _epoch += 1
        live = list(_procs)
        _issued.clear()
        _clear_pending()
    stopped = all([_stop(p) for p in live])
    clean = stopped and _remove(_root())
    expire_tables()
    return _set_dirty(not clean) and clean


def _drain(proc, stream, limit: int, out: list, over: threading.Event) -> None:
    got = 0
    while chunk := stream.read(65536):
        got += len(chunk)
        if got > limit:            # 받는 중에 끊는다 — 다 받은 뒤 재지 않는다 (codex1 0단계 P2-1)
            over.set()
            proc.kill()
            break
        out.append(chunk)
    stream.close()


def run(args: list[str], home: Path | None = None, deadline: float | None = None,
        value: bytes | None = None, go=None, menu_path: bool = False) -> dict | list:
    """엔진 한 번. args 는 이 모듈이 만든 목록만, 첫째가 열린 명령. home 은 이 호출의 상태 폴더(없으면 새로).
    version·status 말고는 앱이 열려 있어야 하고, 끝난 뒤에도 같은 열림·같은 정리 회차여야 결과를 낸다.
    value 는 엔진 stdin 으로만 (argv·로그에 안 남는다) — 엔진이 깨어난 뒤 같은 기한 안에서 쓴다.
    go 는 _lock 을 쥔 채 부르는 확인(잠금을 잡지 않는다) — 띄우기 전·깨우기 직전에 거짓이면 보내지 않는다(계획 중단)."""
    if not args or not all(isinstance(a, str) for a in args) or args[0] not in COMMANDS:
        raise ScreenError("허용되지 않은 화면 자동화 요청이에요.")
    guard = args[0] not in UNGUARDED
    owned = home is None              # 남길 필요 없는 호출(창 목록·진단) — 끝나면 바로 지운다
    home = home or new_home()
    if not _run_lock.acquire(timeout=max(0.0, min(TIMEOUT, _left(deadline)))):
        raise ScreenError("요청 기한 안에 시작하지 못했어요.")
    try:
        return _run_locked(args, home, owned, guard, deadline, value, go, menu_path)
    finally:
        _run_lock.release()


def _write(proc, value: bytes) -> None:
    try:                        # 엔진이 끝나면(기한·잠금) 파이프가 깨져 여기서도 끝난다
        proc.stdin.write(value)
    except OSError:
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def _run_locked(args, home, owned, guard, deadline, value=None, go=None, menu_path=False):
    if _left(deadline) <= 0:                 # 기한이 지난 요청은 엔진을 띄우지 않는다 (codex1 r5 P2-2)
        raise ScreenError("요청 기한이 지났어요.")
    if _dirty or _dirty_mark().exists():
        raise ScreenError("지난 화면 정보를 아직 지우지 못했어요. VaderAI 를 잠갔다 다시 열어 주세요.")
    gen = current_gen() if guard else None
    if guard and gen is None:
        raise ScreenError("VaderAI 가 잠겨 있어요.")
    exe = engine()
    with _lock:
        epoch = _epoch
    env = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA",
                                      "APPDATA", "PATH") if k in os.environ}
    env["AGENT_DESKTOP_HOME"] = str(home)
    if menu_path is True:   # 엔진 VaderAI 패치: 고유 id 없는 메뉴 항목을 메뉴 경로로 식별 — 앱 설정이 켠 창에서만
        env["AGENT_DESKTOP_MENU_PATH_IDENTITY"] = "1"
    if _left(deadline) <= 0:                 # 엔진 검사·준비 사이에 기한이 지났다 — 띄우지 않는다 (codex1 r6 P2-2)
        raise ScreenError("요청 기한이 지났어요.")
    if go is not None:
        with _lock:
            if not go():
                raise ScreenError("계획이 멈춰 보내지 않았어요.")
    try:
        proc = subprocess.Popen([str(exe), *args], stdin=subprocess.DEVNULL if value is None else subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, cwd=str(exe.parent),
                                creationflags=NO_WINDOW | SUSPENDED)
    except OSError:
        raise ScreenError("화면 자동화 엔진을 실행하지 못했어요.") from None
    out, err, over = [], [], threading.Event()
    readers = [threading.Thread(target=_drain, args=(proc, proc.stdout, MAX_OUTPUT, out, over), daemon=True),
               threading.Thread(target=_drain, args=(proc, proc.stderr, MAX_ERR, err, over), daemon=True)]
    if value is not None:
        readers.append(threading.Thread(target=_write, args=(proc, value), daemon=True))
    late = timed_out = expired = halted = False
    try:                                   # Popen 뒤의 모든 실패는 아래 finally 하나로 정리한다
        with _lock:
            late = _epoch != epoch          # 띄우는 사이 정리됐다 — 깨우지 않고 끝낸다
            _procs.add(proc)                # 멈춘 채라도 등록 — sweep 이 끝낼 수 있게
        for r in readers[:2]:
            r.start()
        if not late:
            _in_job(proc)
            with _lock:                      # 깨우기 직전에 다시 — 지났거나 계획이 멈췄으면 멈춘 채로 끝낸다.
                expired = _left(deadline) <= 0     # 확인과 깨우기가 한 걸음 — 중단은 이 앞이거나(안 보냄) 뒤(보냄)
                halted = not expired and go is not None and not go()
                if not expired and not halted:
                    _resume(proc)
            if not expired and not halted:
                for r in readers[2:]:
                    r.start()
                try:
                    proc.wait(max(0.0, min(TIMEOUT, _left(deadline))))   # 요청 기한까지만
                except subprocess.TimeoutExpired:
                    timed_out = True
        if late or expired or halted or timed_out:
            _stop(proc)                     # 기한·정리 — 엔진을 먼저 끝내고 나서 출력을 거둔다 (r6 P2-1)
        for r in readers:
            if r.ident is not None:
                r.join(5)
    finally:
        ended = _stop(proc)
        with _lock:
            if ended:
                _procs.discard(proc)        # 안 끝났으면 추적을 남긴다 — 다음 sweep 이 다시 끝낸다
            moved = _epoch != epoch
        if not ended:
            _set_dirty(True)
        if owned and not (ended and _remove(home)):
            _set_dirty(True)
        if late or moved or (guard and current_gen() != gen):
            # 그 사이 잠겼다(다시 열렸어도) — 엔진이 끝난 뒤 이 호출이 쓴 화면 정보까지 지운다
            if not (ended and _remove(home)):
                _set_dirty(True)
            raise ScreenError("VaderAI 가 잠겨 결과를 버렸어요. 화면에서 확인해 주세요.", UNKNOWN)
    if expired:
        raise ScreenError("요청 기한이 지났어요.")
    if halted:
        raise ScreenError("계획이 멈춰 보내지 않았어요.")
    if timed_out:
        raise ScreenError("화면 자동화가 제때 끝나지 않았어요. 화면에서 결과를 확인해 주세요.", UNKNOWN)
    if over.is_set() or any(r.is_alive() for r in readers):
        raise ScreenError("화면 정보가 너무 커요. 대상 창을 좁혀 주세요.", UNKNOWN)
    return _envelope(args[0], proc.returncode, b"".join(out))


def _envelope(command: str, code: int, raw: bytes) -> dict | list:
    """봉투 검사 — 버전·명령·ok/error·종료 코드·명령별 data 모양이 맞아야 한다. 아니면 '모름' (codex1 0단계 P2-2)."""
    try:
        env = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ScreenError("화면 자동화 엔진의 답을 알아보지 못했어요.", UNKNOWN) from None
    bad = ScreenError("화면 자동화 엔진의 답이 맞지 않아요.", UNKNOWN)
    if not isinstance(env, dict) or env.get("version") != ENVELOPE or env.get("command") != command:
        raise bad
    if env.get("ok") is True and env.get("error") is None and code == 0:
        data = env.get("data")
        if not _shaped(command, data):
            raise bad
        return data
    error = env.get("error")
    if env.get("ok") is not False or not isinstance(error, dict) or env.get("data") is not None or code == 0:
        raise bad
    raw_code = error.get("code")
    name = raw_code if isinstance(raw_code, str) and raw_code in CODES else "UNKNOWN_CODE"
    disposition = error.get("disposition") if isinstance(error.get("disposition"), dict) else {}
    # 엔진이 '안 보냈다' 고 한 것만 거절. 오류 문장은 화면 글자를 담을 수 있어 코드만 쓴다 (P1-3)
    raise ScreenError(f"화면 자동화가 거절했어요 ({name}{_checks(error.get('details'))}).",
                      REFUSED if disposition.get("delivery") == "not_delivered" else UNKNOWN)


def _checks(details) -> str:
    """엔진 actionability 보고에서 걸린 검사 이름·상태·가린 것의 역할만 — 고정 낱말만 통과(이름·이유·좌표는 버림)."""
    word = re.compile(r"[a-z_]{1,40}")
    out = []
    for c in (details.get("checks") if isinstance(details, dict) else None) or []:
        if not isinstance(c, dict) or c.get("status") not in ("fail", "unknown") \
                or not isinstance(c.get("check"), str) or not word.fullmatch(c["check"]):
            continue
        role = (c.get("occluder") or {}).get("role") if isinstance(c.get("occluder"), dict) else None
        out.append(f"{c['check']}={c['status']}" + (f"(가림:{role})" if isinstance(role, str) and word.fullmatch(role) else ""))
    return (": " + ", ".join(out[:5])) if out else ""


def _shaped(command: str, data) -> bool:
    if command in ("version", "status"):
        return isinstance(data, dict) and data.get("version") == ENGINE_VERSION and \
            (command == "version" or isinstance(data.get("state_root"), str))
    if command == "list-windows":
        return isinstance(data, list) and all(
            isinstance(w, dict) and isinstance(w.get("id"), str) and isinstance(w.get("pid"), int)
            and isinstance(w.get("title"), str) and isinstance(w.get("visible"), bool)
            and isinstance(w.get("minimized"), bool) for w in data)
    if command in ACT.values():
        disposition = data.get("disposition") if isinstance(data, dict) else None
        return (isinstance(disposition, dict) and data.get("action") == command
                and disposition.get("delivery") in DELIVERY)
    if command == "snapshot":
        return (isinstance(data, dict) and isinstance(data.get("snapshot_id"), str)
                and re.fullmatch(r"[a-z0-9]{1,40}", data["snapshot_id"]) is not None
                and isinstance(data.get("tree"), dict) and isinstance(data.get("window"), dict)
                and isinstance(data.get("complete"), bool))
    return False


# ── 1단계: 읽기 (허용 앱 하나의 창 구조만, 승인된 정적 라벨 외 화면 글자는 내보내지 않는다) ──

# 허용 앱 — 설정 screen.apps 로 바꾼다(사람이 관리하는 설정 — 화면·AI 가 정하지 않는다). 기본은 시험용 합성 앱
# (tests/fixtures/screen_app.ps1) 하나. labels 는 사람이 확인한 '정확한 정적 라벨' — 역할로 짐작하지 않는다 (P1-A).
DEFAULT_APPS = {
    "시험 화면": {
        "exe": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "title_prefix": "VaderAI 시험 화면",
        "labels": ["이름", "주민번호", "확인", "저장", "시스템", "최소화", "최대화", "닫기"],
        # 대상 프로필 (3단계) — 사람이 확인한 이름→셀렉터. aid 는 정적 automation_id, label 은 위 승인 라벨 중 하나.
        "targets": {
            "이름 칸": {"role": "textfield", "aid": "nameBox", "batch": ["set_value"]},
            "확인 체크": {"role": "checkbox", "label": "확인", "batch": ["check", "uncheck"]},
            "저장 버튼": {"role": "button", "label": "저장", "batch": []},
        },
    },
}
# 우리 자신(앱·일꾼·엔진)은 대상이 아니다 — 승인 화면을 AI 가 누르지 못하게
_SELF = {"vaderai.exe", "vaderai-worker.exe", "vaderai-cli.exe", "agent-desktop.exe"}
_issued: dict[str, dict] = {}     # 이 프로세스가 낸 마지막 스냅샷 → 대상·폴더·세대. 2단계 ref 검사에 쓴다


def apps() -> dict:
    got = config.get("screen.apps")
    return got if isinstance(got, dict) and got else DEFAULT_APPS


def _exe_of(pid) -> str | None:
    """PID 의 실행 파일 전체 경로 (엔진의 app 이름은 파일 이름뿐이라 따로 본다)."""
    if not isinstance(pid, int) or os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        return buf.value if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else None
    finally:
        k32.CloseHandle(handle)


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _windows(spec: dict, deadline: float | None = None) -> list[dict]:
    """허용 exe 의 보이는 창들 (PID·process_instance·실제 exe 경로까지 맞는 것만, 펼친 메뉴 목록 창은 뺀다)."""
    found, menus = [], {}
    for w in run(["list-windows"], None, deadline):
        if not w["visible"] or w["minimized"] or not isinstance(w.get("process_instance"), str):
            continue
        exe = _exe_of(w["pid"])
        if not exe or os.path.basename(exe).lower() in _SELF or w["pid"] in (os.getpid(), os.getppid()):
            continue
        if _same_file(exe, spec["exe"]) and w["title"].startswith(str(spec.get("title_prefix") or "")):
            found.append({"id": w["id"], "pid": w["pid"], "process": w["process_instance"], "exe": exe})
            if not w["title"]:
                menus[w["id"]] = _popup_owner(w["id"])
    # 펼친 메뉴 목록(WinForms ToolStripDropDown, 실PC 2026-11-16)은 따로 창으로 잡히지만 비어 있고 항목은 주인 창 트리에
    # 있다. 제목 없는 WS_POPUP·주인(GW_OWNER)이 같은 프로세스 후보 창·주인 켜짐(모달 아님)이고, 그 창을 직접 읽어
    # 다 봤고 뿌리가 menu 이고 ref 가 하나도 없을 때만 창으로 세지 않는다 — 그 메뉴 스냅샷에 조작할 요소가 없다는
    # 증거까지만(화면에 보이는 것 전부가 없다는 뜻은 아니다, codex1 P1·회신 2).
    # HWND 를 못 읽거나 주인을 모르면 None 은 짝으로 쓰지 않는다 — 그 창은 그대로 센다(codex1 P2).
    owners = {(h, x["pid"], x["process"]) for x in found if (h := _hwnd(x["id"])) is not None}
    return [x for x in found if not (menus.get(x["id"]) is not None
                                     and (menus[x["id"]], x["pid"], x["process"]) in owners
                                     and _empty_menu(x, deadline))]


def _empty_menu(w: dict, deadline: float | None) -> bool:
    """그 창을 읽어 보니 같은 창의 답이고(id·주인), _nodes 검사를 통과했고, 다 봤고, 뿌리 역할이 menu 이고,
    ref 가 하나도 없다 — '그 메뉴 스냅샷에 조작할 요소가 없다' 까지만의 증거. 실패·모름·틀린 모양이면 False (codex1 회신 2)."""
    try:
        data = run(["snapshot", "--window-id", w["id"], "-i", "--compact", "--timeout-ms", _engine_ms(deadline)],
                   None, deadline)
        win = data["window"]
        if win.get("id") != w["id"] or not all(win.get(k) in (None, w[v]) for k, v in (("pid", "pid"), ("process_instance", "process"))):
            return False
        nodes, full = _nodes(data["tree"], data["snapshot_id"], set())
    except (ScreenError, _Bad):
        return False
    return data["complete"] is True and full and not nodes and data["tree"].get("role") == "menu"


def _hwnd(window_id: str) -> int | None:
    """엔진 창 id 'w-<HWND>' 의 HWND."""
    try:
        return int(window_id.split("-", 1)[1], 0) if window_id.startswith("w-") else None
    except (ValueError, IndexError):
        return None


def _popup_owner(window_id: str) -> int | None:
    """창이 WS_POPUP 이고 주인 창이 있고 그 주인이 켜져 있으면 주인 HWND. 아니면(모달·모름) None — 그 창은 그대로 센다."""
    hwnd = _hwnd(window_id)
    if hwnd is None or os.name != "nt":
        return None
    import ctypes
    u32 = ctypes.windll.user32
    u32.GetWindow.restype = ctypes.c_void_p
    u32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u32.IsWindowEnabled.argtypes = [ctypes.c_void_p]
    u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    if not u32.GetWindowLongPtrW(hwnd, -16) & 0x80000000:          # GWL_STYLE, WS_POPUP
        return None
    owner = u32.GetWindow(hwnd, 4)                                     # GW_OWNER
    return owner if owner and u32.IsWindowEnabled(owner) else None


def _target(key: str, deadline: float | None = None) -> dict:
    spec = apps().get(key)
    if not isinstance(spec, dict) or not isinstance(spec.get("exe"), str):
        raise ScreenError("허용된 앱이 아니에요.")
    found = _windows(spec, deadline)
    if len(found) != 1:      # 없거나 여럿이면 고르지 않는다 — 다른 창을 잡지 않게
        raise ScreenError("그 앱의 창을 하나로 찾지 못했어요." if not found else "그 앱의 창이 여러 개예요. 하나만 열어 주세요.")
    return found[0]


_look_lock = threading.RLock()   # 관찰 하나가 끝나 게시될 때까지 (계획 단계는 잡은 채 look 을 부른다) — 늦은 관찰이 새 것을 덮지 않게 (P2-3)


class _Bad(Exception):
    """스냅샷 모양이 틀렸다 — 모름으로."""


def _nodes(tree: dict, snap: str, labels: set, meta: dict | None = None) -> tuple[list[dict], bool]:
    """정제한 노드와 '다 봤는가'. 겹치는 ref·모르는 역할·틀린 타입·다른 스냅샷 ref 는 _Bad.
    meta 를 주면 ref → {aid, exact} 를 채운다 — 대상 프로필 비교용, 밖으로 안 나간다 (codex1 3단계 P1-4)."""
    nodes, seen, refs, cut, visits = [], {}, set(), False, 0
    stack = [(tree, 0, None)]                  # (노드, 깊이, 가장 가까운 ref 있는 조상)
    while stack:
        n, depth, up = stack.pop()
        visits += 1
        if visits > MAX_VISIT or depth > 64 or len(nodes) >= MAX_NODES:
            cut = True
            break
        if not isinstance(n, dict):
            raise _Bad
        role, ref, children, actions = n.get("role"), n.get("ref_id"), n.get("children", []), n.get("available_actions", [])
        if not isinstance(role, str) or role not in ROLES or not isinstance(children, list) or not isinstance(actions, list):
            raise _Bad
        if ref is not None:
            if not isinstance(ref, str) or not _REF.match(ref) or not ref.startswith("@" + snap + ":") or ref in refs:
                raise _Bad
            refs.add(ref)
            seen[role] = seen.get(role, 0) + 1
            name = n.get("name")
            exact = name if isinstance(name, str) and name in labels else None
            nodes.append({"ref": ref, "role": role, "actions": [a for a in actions if isinstance(a, str) and a in ACTIONS],
                          "label": exact or f"{role}#{seen[role]}"})
            if meta is not None:
                nid = n.get("native_id")
                # parent_automation_id = 엔진 VaderAI 패치의 입력칸 '부모/자기' id(이온엠 m_pTextBox 쌍둥이).
                # kind 까지 같이 둔다 — plain 'a/b' 와 복합 'a/b' 는 다른 칸이다.
                aid = ((nid.get("kind"), nid.get("value")) if isinstance(nid, dict) and nid.get("kind") in AID_KINDS
                       and isinstance(nid.get("value"), str) else None)
                b = n.get("bounds")
                at = (round(b["x"] + b["width"] / 2), round(b["y"] + b["height"] / 2)) if isinstance(b, dict) and all(
                    isinstance(b.get(k), (int, float)) and math.isfinite(b[k]) for k in ("x", "y", "width", "height")) else None
                meta[ref] = {"aid": aid, "exact": exact, "at": at, "up": up}
        stack.extend((c, depth + 1, ref if ref is not None else up) for c in reversed(children))
    return nodes, not cut


def _menu_path(key: str) -> bool:
    """앱 설정 `"menu_path": true` — 같은 이름 메뉴 항목을 메뉴 경로로 구분한다. 사람이 그 창에서 확인하고 켠다:
    엔진은 메뉴 구조를 위→아래로 읽어 경로를 만들고 누르기 직전엔 아래→위로 읽는데, 둘이 같다는 건 WinForms 메뉴
    (이온엠·시험 창)에서만 실측했다. WPF 는 펼친 메뉴에서 둘이 달라 켜면 항목을 못 찾는다(거절)."""
    spec = apps().get(key)
    return isinstance(spec, dict) and spec.get("menu_path") is True


def _observe(key: str, w: dict, home: Path, deadline: float | None, menu_path: bool = False):
    """창 w 를 한 번 읽어 정제 — (원 답, 공개 노드, 내부 메타, 다 봤나). 읽기 앞뒤 창 주인이 같아야 한다."""
    data = run(["snapshot", "--window-id", w["id"], "-i", "--compact", "--include-bounds", "--timeout-ms", _engine_ms(deadline)], home, deadline,
               menu_path=menu_path)
    after = [x for x in _windows(apps()[key], deadline) if x["id"] == w["id"]]
    # 읽는 동안 창 주인이 바뀌지 않았다 — 같은 창 id·PID·생성 식별·exe (HWND 재사용을 거절)
    win = data["window"]
    # 엔진 1.2.0 스냅샷의 window 는 id·title 만 준다 — pid·process_instance 가 오면 그것도 같아야 한다
    owner_ok = all(win.get(k) in (None, w[v]) for k, v in (("pid", "pid"), ("process_instance", "process")))
    if win.get("id") != w["id"] or not owner_ok or after != [w]:
        raise ScreenError("읽는 사이 창이 바뀌었어요. 다시 요청해 주세요.", UNKNOWN)
    meta: dict = {}
    try:
        nodes, full = _nodes(data["tree"], data["snapshot_id"],
                             {x for x in apps()[key].get("labels") or [] if isinstance(x, str)}, meta)
    except _Bad:
        raise ScreenError("화면 정보를 알아보지 못했어요.", UNKNOWN) from None
    return data, nodes, meta, full


def _name_targets(key: str, nodes: list[dict], meta: dict, complete: bool) -> None:
    """완전한 관찰에서 '역할#번호' 로만 보이던 노드가 사람이 정한 대상과 딱 하나 맞으면 라벨을 그 대상 이름으로.
    대상 이름은 사람이 설정에 쓴 고정 문구라 승인 라벨과 같은 급이다(개인정보를 넣지 않는다). 프로필 형식이 틀리면
    이름을 붙이지 않지만, 잘못 적은 id 가 다른 칸 하나에 맞으면 그 칸에 붙는다 — 카드에서 사람이 확인한다 (codex1)."""
    if not complete:
        return
    try:
        targets = profile(key)
    except ScreenError:
        return
    named: dict = {}
    for name, t in targets.items():
        hits = _hits(t, nodes, meta)
        if len(hits) == 1:
            named.setdefault(hits[0], []).append(name)
    for n in nodes:
        # 승인 라벨이 이미 있으면 그대로, 두 대상이 같은 칸이면 어느 이름도 붙이지 않는다
        if meta[n["ref"]]["exact"] is None and len(named.get(n["ref"], [])) == 1:
            n["label"] = named[n["ref"]][0]


def look(key: str, deadline: float | None = None) -> dict:
    """허용 앱 창의 조작 가능한 요소 목록. 나가는 것: ref·역할·할 수 있는 동작·라벨(승인 목록에 정확히 있을 때만,
    사람이 정한 대상과 딱 하나 맞으면 그 대상 이름, 아니면 '역할#번호'). 값·창 제목·설명·native_id·앱 이름은 내보내지 않는다.
    complete=False 면 일부만 본 것."""
    with _look_lock:
        gen = current_gen()
        with _lock:
            epoch = _epoch
        w = _target(key, deadline)
        home = new_home()
        try:
            menu_path = _menu_path(key)      # 한 번만 읽는다 — 관찰과 그 관찰로 하는 조작이 같은 값 (codex1 회신 6)
            data, nodes, meta, full = _observe(key, w, home, deadline, menu_path)
            _name_targets(key, nodes, meta, data["complete"] and full)
            with _lock:
                fresh = _epoch == epoch and current_gen() == gen and gen is not None and _left(deadline) > 0
                if fresh:
                    old = list(_issued.values())
                    _issued.clear()
                    _issued[data["snapshot_id"]] = {"app": key, "pid": w["pid"], "process": w["process"],
                                                    "exe": w["exe"], "window": w["id"], "gen": gen, "home": home,
                                                    "complete": data["complete"] and full,
                                                    "menu_path": menu_path,   # 이 관찰에 실제로 쓴 값
                                                    "nodes": {n["ref"]: dict(n) for n in nodes}, "meta": meta}
            if not fresh:
                raise ScreenError("VaderAI 가 잠겨 결과를 버렸어요.", UNKNOWN)
        except BaseException:
            if not _remove(home):
                _set_dirty(True)
            raise
    for o in old:                # 조작할 수 있는 관찰은 마지막 것 하나 — 이전 폴더(원문)는 지운다
        if not _remove(o["home"]):
            _set_dirty(True)
    journal.record(journal.Entry(at=journal.now(), job_id="", rune_id="screen", level="info",
                                 message="screen_look", fields={"app": key, "count": len(nodes)}))
    return {"app": key, "complete": data["complete"] and full, "count": len(nodes), "nodes": nodes}


# ── 표 읽기: 화면 읽기 통로에 셀을 내놓지 않는 표(이온엠 FarPoint Spread — 빈 칸 하나로만 보인다)를 복사로 읽는다 ──
# 마스터 결정 2026-11-17: 앱 설정 `"tables": "<표 id 접두어>"` 를 넣은 앱에서는 승인 카드 없이 돈다. 표 내용은 작업공간
# 파일로만 가고 AI 에는 줄·칸 수만 준다(첫 줄이 제목이라는 보장이 없어 글자는 하나도 안 준다, codex1 회신 1 P1).
# 실제 마우스·키보드는 쓰지 않는다(회신 1 — 좌표 클릭은 맨 위 창에 떨어진다). 개발자 이름으로 찾은 그 표 창(HWND)에만
# '모서리 칸 클릭(= 전체 선택)' 과 Ctrl+Insert(복사) 를 메시지로 보낸다. 시험 창 실측: 모서리 클릭 없이는 활성 칸 하나만
# 복사되고, Ctrl 없이 늦게 처리된 Insert 는 아무것도 하지 않는다(C 는 글자로 들어갈 수 있어 쓰지 않는다).
# 모서리 클릭은 표 선택을 바꾼다 — 선택에 반응하는 화면이면 그 반응이 일어나고, 머리글이 숨은 표면 셀 클릭이 된다(남은 위험).

TABLE_ID = re.compile(r"[A-Za-z0-9_]{1,80}")
TABLE_CORNER = 8                # 표 창 안쪽 좌표(화소) — 행·열 머리글이 만나는 모서리 칸
TABLE_IDLE = 1.5                # 사람 입력이 이만큼(초) 없을 때만 Ctrl 을 켠다
TABLE_COPY_WAIT = 10.0          # 앱이 클립보드에 쓰기까지 기다리는 시간(초) — 이 안에서만 지켜본다(뒤에 남는 감시 없음)
TABLE_UIA = 10.0                # 표 찾기(UIA) 기한(초)
MAX_TABLE_CHILDREN = 20000
TABLE_KEEP = 24 * 3600          # 저장한 표 파일 보관(초) — 룬이 읽으면 바로, 아니면 이만큼 뒤 지운다 (마스터 2026-11-18)


def tables_dir() -> Path | None:
    root = config.workspace()
    return None if root is None else root / "screen_tables"


def _plain(path: Path, folder: bool = False) -> bool:
    """링크·junction(재분석 지점)이 아닌 진짜 폴더/일반 파일인가 — 따라가지 않고 본다(lstat)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:   # FILE_ATTRIBUTE_REPARSE_POINT
        return False
    return stat.S_ISDIR(st.st_mode) if folder else stat.S_ISREG(st.st_mode)


def own_table(path) -> bool:
    """표 읽기가 저장한 파일인가: 이 계정 screen_tables(진짜 폴더) 바로 아래의 일반 .tsv. 경로는 글자 그대로 비교한다
    (resolve 하지 않는다 — 링크를 따라간 경로가 같다고 우리 것이 아니다, codex1 회신 1 P1).
    ponytail: 확인과 지우기 사이에 같은 사용자 권한으로 링크를 바꿔치는 경쟁은 못 막는다 — 같은 사용자 악성 프로세스는 범위 밖."""
    folder, path = tables_dir(), Path(path)
    return (folder is not None and path.is_absolute() and path.parent == folder and path.suffix.lower() == ".tsv"
            and _plain(folder, folder=True) and _plain(path))


def expire_tables() -> None:
    """만든 지 TABLE_KEEP 이 지난 표 파일을 지운다. 계정을 연 때·잠금·끝·표 읽기·한 시간마다 부른다 —
    앱이 꺼진 동안은 못 지우고 다음 실행 때 지운다. 못 지운 것은 다음에 다시."""
    folder = tables_dir()
    if folder is None or not _plain(folder, folder=True):
        return
    old = time.time() - TABLE_KEEP
    try:
        names = list(folder.iterdir())
    except OSError:
        return
    for f in names:
        try:
            st = f.stat()
            if own_table(f) and getattr(st, "st_birthtime", st.st_ctime) < old:   # 만든 시각 — 고쳐도 늘지 않는다
                f.unlink()
        except OSError:
            pass


_VK_CTRL = (0x11, 0xA2)         # VK_CONTROL·VK_LCONTROL


def _table_prefix(key: str) -> str:
    spec = apps().get(key)
    prefix = spec.get("tables") if isinstance(spec, dict) else None
    if not isinstance(prefix, str) or not TABLE_ID.fullmatch(prefix):
        raise ScreenError("이 앱은 표 읽기가 켜져 있지 않아요.")
    return prefix


def _u32():
    """(ctypes, wintypes, user32, kernel32) — 표 읽기가 쓰는 Win32 함수의 형을 맞춘 것. 테스트가 바꿔 끼운다."""
    import ctypes
    from ctypes import wintypes
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    vp = ctypes.c_void_p
    for f in ("GetClipboardOwner", "GetClipboardData", "GetAncestor"):
        getattr(u32, f).restype = vp
    u32.GetAncestor.argtypes = [vp, ctypes.c_uint]
    u32.GetWindowThreadProcessId.argtypes = [vp, ctypes.POINTER(wintypes.DWORD)]
    u32.PostMessageW.argtypes = [vp, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    u32.EnumChildWindows.argtypes = [vp, vp, ctypes.c_ssize_t]
    u32.GetClassNameW.argtypes = [vp, ctypes.c_wchar_p, ctypes.c_int]
    u32.IsWindowVisible.argtypes = u32.IsWindow.argtypes = u32.IsHungAppWindow.argtypes = [vp]
    k32.GlobalLock.restype = vp
    k32.GlobalLock.argtypes = k32.GlobalUnlock.argtypes = [vp]
    return ctypes, wintypes, u32, k32


def _pid_of(w32, hwnd) -> int:
    ctypes, wintypes, u32, _ = w32
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _host(w32, h, top: int, pid: int) -> bool | None:
    """h 가 그 앱 창(top) 아래 보이는 같은 프로세스의 WinForms 창인가. 클래스·PID·뿌리를 못 읽었으면 None(모름)."""
    ctypes, _, u32, _ = w32
    cls = ctypes.create_unicode_buffer(256)
    if not u32.IsWindow(h):
        return False
    pid_h, root = _pid_of(w32, h), u32.GetAncestor(h, 2)                 # GA_ROOT
    if not u32.GetClassNameW(h, cls, 256) or not pid_h or not root:
        return None
    return cls.value.startswith("WindowsForms10.") and bool(u32.IsWindowVisible(h)) and pid_h == pid and root == top


_uia_busy: threading.Thread | None = None    # 기한을 넘겨 아직 안 끝난 UIA 읽기 — 끝나기 전엔 새로 읽지 않는다


def _in_mta(fn, deadline: float | None):
    """UIA 는 따로 MTA 스레드에서, 요청 기한과 TABLE_UIA 중 짧은 쪽까지만(UI Automation threading 권고, 회신 2·3 P2).
    넘기면 그 스레드를 기억해 두고, 끝날 때까지 다음 표 읽기를 거절한다 — 멈춘 읽기가 쌓이지 않게. 취소는 못 한다."""
    global _uia_busy
    import comtypes.client  # noqa: F401  처음 import 는 부르는 스레드에서 — comtypes 가 제 방식으로 COM 을 켠다
    if _uia_busy is not None and _uia_busy.is_alive():
        raise ScreenError("앞의 표 찾기가 아직 끝나지 않았어요. 잠시 뒤 다시 요청해 주세요.")
    box: dict = {}

    def work():
        import ctypes
        hr = ctypes.windll.ole32.CoInitializeEx(None, 0)             # COINIT_MULTITHREADED
        if hr not in (0, 1):
            return                                                   # 못 켰다 — 읽지 않는다(아래에서 거절)
        try:
            box["ok"] = fn()
        except Exception as exc:                                     # noqa: BLE001 — 아래에서 거절로
            box["error"] = exc
        finally:
            import gc
            gc.collect()                                             # COM 객체를 놓고 나서 끈다
            ctypes.windll.ole32.CoUninitialize()
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(max(0.0, min(TABLE_UIA, _left(deadline))))
    if t.is_alive():
        _uia_busy = t
    if t.is_alive() or "ok" not in box:
        raise ScreenError("표를 제때 찾지 못했어요. 다시 요청해 주세요.")
    return box["ok"]


def _uia():
    import comtypes.client
    comtypes.client.GetModule("UIAutomationCore.dll")
    from comtypes.gen import UIAutomationClient as UIA
    return comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)


def _grids(top: int, pid: int, prefix: str, deadline: float | None = None) -> tuple[dict, bool]:
    """그 앱 창 아래 표 후보 → HWND 들, 그리고 '다 봤나'. 후보 = _host 이고 UIA AutomationId(= 개발자가 붙인 컨트롤
    이름)가 접두어로 시작. 열거가 상한에 닿았거나, 후보인지 못 읽은 창이 있거나, 후보 이름을 못 읽었으면 다 본 것이
    아니다(회신 2·3 P1)."""
    w32 = _u32()
    ctypes, wintypes, u32, _ = w32
    kids = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_ssize_t)(
        lambda h, _: kids.append(h) or len(kids) <= MAX_TABLE_CHILDREN)
    u32.EnumChildWindows(top, proc, 0)
    seen = {h: _host(w32, h, top, pid) for h in kids[:MAX_TABLE_CHILDREN]}
    hosts = [h for h, ok in seen.items() if ok]

    def read():
        uia, names = _uia(), {}
        for h in hosts:
            try:
                names[h] = uia.ElementFromHandle(h).CurrentAutomationId
            except Exception:                                        # noqa: BLE001
                names[h] = None
        return names
    names = _in_mta(read, deadline)
    out: dict = {}
    for h, aid in names.items():
        if isinstance(aid, str) and aid.startswith(prefix) and TABLE_ID.fullmatch(aid):
            out.setdefault(aid, []).append(h)
    return out, (len(kids) <= MAX_TABLE_CHILDREN and None not in seen.values()
                 and all(isinstance(a, str) for a in names.values()))


def _still(grid: int, top: int, pid: int, name: str, deadline: float | None = None) -> bool:
    """보내기 직전에 다시: 같은 앱 창 아래 같은 PID 의 그 이름 창인가 (열거 뒤 파괴·재사용·옮김, 회신 2 P1). 프로세스
    생성 식별·exe 는 다시 보지 않는다 — 앱 창(top) 이 _target 때와 같은 HWND 라는 것까지만."""
    w32 = _u32()
    if _host(w32, grid, top, pid) is not True:
        return False
    try:
        return _in_mta(lambda: _uia().ElementFromHandle(grid).CurrentAutomationId, deadline) == name
    except ScreenError:
        return False


def _idle(w32) -> bool:
    ctypes, _, u32, k32 = w32

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    return bool(u32.GetLastInputInfo(ctypes.byref(info))) and \
        ((k32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) >= TABLE_IDLE * 1000


def _open(w32) -> bool:
    _, _, u32, _ = w32
    for _ in range(20):
        if u32.OpenClipboard(None):
            return True
        time.sleep(0.05)
    return False


def _take(w32, start: int, pid: int) -> str | None:
    """클립보드를 연 채로 한 번에: 보낸 뒤 바뀌었고 주인이 그 앱이면 글자를 읽고, 누가 썼든 비운다(회신 2 P1 — 검사와 읽기가
    같은 상태). 못 열면 ScreenError(남았을 수 있음)."""
    ctypes, _, u32, k32 = w32
    if not _open(w32):
        raise ScreenError("클립보드를 열지 못했어요. 클립보드에 표 내용이 남았을 수 있어요.", UNKNOWN)
    try:
        text = None
        owner = u32.GetClipboardOwner()
        if u32.GetClipboardSequenceNumber() != start and owner and _pid_of(w32, owner) == pid:
            handle = u32.GetClipboardData(13)                         # CF_UNICODETEXT
            ptr = k32.GlobalLock(handle) if handle else None
            if ptr:
                try:
                    text = ctypes.wstring_at(ptr)
                finally:
                    k32.GlobalUnlock(handle)
        if not u32.EmptyClipboard():
            raise ScreenError("클립보드를 비우지 못했어요. 클립보드에 표 내용이 남았을 수 있어요.", UNKNOWN)
        return text
    finally:
        u32.CloseClipboard()


def _ctrl(w32, keys, on: bool) -> bool:
    """공유 키 상태를 지금 다시 읽어 Ctrl 두 바이트만 바꿔 쓴다 — 그 사이 바뀐 다른 키는 덮지 않는다(회신 3 P2)."""
    _, _, u32, _ = w32
    if not u32.GetKeyboardState(keys):
        return False
    for vk in _VK_CTRL:
        keys[vk] = 0x80 if on else 0
    return bool(u32.SetKeyboardState(keys))


def _copy(grid: int, top: int, pid: int, name: str, go, deadline: float | None = None) -> str:
    """그 표 창에 모서리 클릭 + Ctrl+Insert 를 메시지로 보내고, 클립보드가 바뀌고 주인이 그 앱일 때의 글자.
    메시지마다 직전에 go() — 첫 메시지 전에 멈추면 '안 보냄', 그 뒤 멈춤·실패·시간 초과는 '모름'(UNKNOWN).
    끝날 때는 어느 길로든: Ctrl 끄기·떼기를 하고, 하나라도 보냈으면 클립보드를 읽고(그 앱 것만) 비운다(회신 3 P1)."""
    w32 = _u32()
    ctypes, _, u32, k32 = w32
    if u32.IsHungAppWindow(top) or not _still(grid, top, pid, name, deadline):
        raise ScreenError("그 표를 화면에서 하나로 찾지 못했어요.")
    if not _idle(w32):                                           # UIA 확인 뒤에 — 붙이기 바로 전의 값
        raise ScreenError("지금 키보드·마우스를 쓰고 있어요. 잠시 손을 뗀 뒤 다시 요청해 주세요.")
    me, them = k32.GetCurrentThreadId(), u32.GetWindowThreadProcessId(grid, None)
    if not them or not go() or not u32.AttachThreadInput(me, them, True):
        raise ScreenError("그 표에 복사를 보내지 못했어요.")
    keys, start, sent, problem, text = (ctypes.c_ubyte * 256)(), u32.GetClipboardSequenceNumber(), 0, None, None
    late = False                                                 # 다 보냈는데 기다림 안에 클립보드가 안 바뀜 — 정리 전에 정한다
    at = (TABLE_CORNER << 16) | TABLE_CORNER
    try:
        if not _ctrl(w32, keys, True):
            problem = ScreenError("그 표에 복사를 보내지 못했어요.")
        for msg, wp, lp in ((0x0201, 1, at), (0x0202, 0, at),                   # 왼쪽 눌렀다 뗌 — 모서리 칸
                            (0x0100, 0x2D, 0x01520001)):                       # Insert 누름 (Ctrl 켠 채)
            if problem is not None:
                break
            if not go() or not u32.PostMessageW(grid, msg, wp, lp):
                problem = ScreenError("그 표에 복사를 보내지 못했어요." if not sent else
                                      "표에 보내다 멈췄어요. 표의 선택이 바뀌었을 수 있어요.", REFUSED if not sent else UNKNOWN)
                break
            sent += 1
        if problem is None:
            end = time.monotonic() + max(0.0, min(TABLE_COPY_WAIT, _left(deadline)))   # 요청 기한을 넘겨 Ctrl 을 쥐지 않는다
            while u32.GetClipboardSequenceNumber() == start:
                if time.monotonic() >= end or not go():
                    late = True
                    break
                time.sleep(0.02)
            if not late:
                time.sleep(0.1)                                  # 앱이 여러 형식을 다 쓸 틈
    finally:
        released = _ctrl(w32, keys, False)
        detached = bool(u32.AttachThreadInput(me, them, False))
        cleaned = None
        if sent:
            try:
                text = _take(w32, start, pid)                    # 그 앱 것이면 읽고, 누구 것이든 비운다
            except ScreenError as exc:
                cleaned = exc
    for issue in (cleaned,
                  None if released and detached else
                  ScreenError("키 상태를 되돌리지 못했을 수 있어요. 그 앱에서 Ctrl 키를 한 번 눌렀다 떼 주세요.", UNKNOWN),
                  problem,
                  ScreenError("복사가 제때 오지 않았어요. 늦게 오면 클립보드에 표 내용이 남을 수 있어요.", UNKNOWN) if late else None,
                  ScreenError("그 표에서 복사되지 않았어요. 화면에서 확인해 주세요.", UNKNOWN) if not text else None):
        if issue is not None:
            raise issue                                          # 사람이 먼저 알아야 할 것부터: 남은 표 내용 > Ctrl > 나머지
    return text


def table(key: str, name=None, deadline: float | None = None) -> dict:
    """name 없이: 지금 그 앱 창의 표 id 목록과 '다 봤나'(아무것도 보내지 않는다). name 이 있으면 그 표를 통째로 복사해
    작업공간 screen_tables/ 에 그대로 쓰고 줄 수·칸 수·파일 경로만 돌려준다 — 표 글자는 돌려주지 않는다."""
    prefix = _table_prefix(key)
    if name is not None and not (isinstance(name, str) and TABLE_ID.fullmatch(name) and name.startswith(prefix)):
        raise ScreenError("그 이름의 표는 읽을 수 없어요.")
    if os.name != "nt":
        raise ScreenError("표 읽기는 Windows 에서만 돼요.")
    with _look_lock:
        gen, root = current_gen(), config.workspace()
        with _lock:
            epoch = _epoch

        def go() -> bool:                       # 같은 열림·같은 정리 회차·기한 안 — 보내기·저장 직전·뒤에 본다
            with _lock:
                return gen is not None and current_gen() == gen and _epoch == epoch and _left(deadline) > 0
        w = _target(key, deadline)
        top = _hwnd(w["id"])
        if top is None or not go():
            raise ScreenError("그 앱의 창을 하나로 찾지 못했어요.")
        found, complete = _grids(top, w["pid"], prefix, deadline)
        if name is None:
            return {"app": key, "complete": complete, "tables": sorted(found)}
        if not complete or len(found.get(name, [])) != 1:
            raise ScreenError("그 표를 화면에서 하나로 찾지 못했어요.")
        if hold is None:
            raise ScreenError("지금은 화면 조작을 할 수 없어요.")
        expire_tables()
        folder = tables_dir()
        if folder is None:
            raise ScreenError("VaderAI 가 잠겨 있어요.")
        folder.mkdir(exist_ok=True)
        if not _plain(folder, folder=True):       # 링크로 바꿔 놓은 폴더에는 쓰지 않는다 — 복사도 안 한다
            raise ScreenError("표 저장 폴더가 일반 폴더가 아니라서 읽지 않았어요.")
        try:
            with hold():                        # 작업·녹화와 겹치지 않게 — 못 잡으면 안 보낸다
                text = _copy(found[name][0], top, w["pid"], name, go, deadline)
        except ValueError as exc:               # 실행권을 못 잡았다 — 보내지 않았다
            raise ScreenError(str(exc)) from None
        if root is None or not go():
            raise ScreenError("VaderAI 가 잠겨 결과를 버렸어요.", UNKNOWN)
        path = folder / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.tsv"
        with open(path, "x", encoding="utf-8", newline="") as f:   # 고친 것 없이 받은 그대로
            f.write(text)
        if not go():                            # 쓰는 사이 잠겼다 — 이 계정 화면이 아닐 수 있는 답은 내지 않는다
            path.unlink(missing_ok=True)
            raise ScreenError("VaderAI 가 잠겨 결과를 버렸어요.", UNKNOWN)
    lines = text.replace("\r\n", "\n").rstrip("\n").split("\n")
    journal.record(journal.Entry(at=journal.now(), job_id="", rune_id="screen", level="info",
                                 message="screen_table", fields={"app": key, "table": name, "lines": len(lines)}))
    # 줄·칸 수는 표 모양의 짐작일 뿐 — 머리글 줄이 있는지, 셀 안 줄바꿈이 있는지는 모른다(파일을 읽는 룬이 판단)
    return {"app": key, "table": name, "lines": len(lines), "columns": lines[0].count("\t") + 1, "file": str(path)}


# ── 2단계: 사람 승인 단일 조작 (codex1 r4 의 7항목 계약) ──
# AI 는 '제안' 만 한다(propose). 실행은 앱 메인 창의 승인 카드에서 사람이 누를 때 한 번(decide).
# 묶는 것: 세대·창 주인(창·PID·생성 식별·exe)·스냅샷·ref·동작·값 해시·60초·한 번. 값은 AI 가 정한 글자만 —
# 저장된 비밀값(룬 설정 비밀번호 등)을 화면에 넣는 길은 이 단계에 없다. 끝나면(무슨 결과든) 그 관찰을 버린다 —
# 다음 조작은 새로 읽고 다시 승인. 실패·모름을 다시 시도하지 않는다 — 사람이 화면에서 확인한다.

# AI 가 부르는 동작 → 엔진 명령. toggle 은 없다(누를 때마다 뒤집혀 결과를 모른다) — check/uncheck 로.
ACT = {"click": "click", "check": "check", "uncheck": "uncheck", "set_value": "set-value"}
ACT_KO = {"click": "누르기", "check": "체크하기", "uncheck": "체크 풀기", "set_value": "값 넣기"}
DELIVERY = {"delivered_verified", "delivered_unverified", "not_delivered"}
APPROVAL = 60                   # 승인 유효 시간(초)
RUN_AFTER = 30                  # 승인한 뒤 엔진이 끝나야 하는 기한(초) — 지나면 끊고 모름. 큰 창(EMR 메뉴)은 창 재확인+ref 찾기 ≈10초+
_UNNAMED = re.compile(r"[a-z]+#\d+")   # 사람이 정하지 않은 이름 '역할#번호'
MAX_VALUE = 1000                # 값 글자 수 — 카드에 다 보여야 한다


def _fits(node: dict, action: str) -> bool:
    have = node["actions"]
    if action == "click":
        return "Click" in have or "Invoke" in have
    if action in ("check", "uncheck"):
        return node["role"] in ("checkbox", "switch") and "Toggle" in have
    return action == "set_value" and node["role"] == "textfield" and "SetValue" in have


_pending: dict | None = None    # 승인 기다리는 제안 하나 — 새 제안이 오면 이전 것은 무효
_running: dict | None = None    # 승인돼 보내는 중인 제안 — 이 동안 결과는 running (codex1 2단계 r1 P2-2)
_outcome: dict | None = None    # 마지막 결과 하나 (같은 세대에서만 보인다)
hold = None                     # 앱이 넣는다: 실행권(작업·녹화와 겹치지 않게) — contextmanager, 못 잡으면 ValueError


def _digest(value) -> str:
    return hashlib.sha256(("" if value is None else value).encode("utf-8")).hexdigest()


MAX_VIA = 3                     # 메뉴 경로 — 먼저 누를 상위 메뉴 수


def _under(meta: dict, child: str, ancestor: str) -> bool:
    """관찰한 트리에서 ancestor 가 child 의 조상(ref 있는 노드끼리)인가 — 메뉴 경로의 증거 (codex1 메뉴 경로 P1)."""
    up = meta.get(child, {}).get("up")
    for _ in range(64):
        if up is None:
            return False
        if up == ancestor:
            return True
        up = meta.get(up, {}).get("up")
    return False


def _menu_chain(seen: dict, ref: str) -> list[str]:
    """ref 가 메뉴 항목이면 맨 위 메뉴부터 자기까지의 ref 들(관찰 트리의 메뉴 항목 조상), 아니면 [ref]."""
    meta, nodes = seen.get("meta") or {}, seen.get("nodes") or {}
    chain = [ref]
    for _ in range(64):
        up = meta.get(chain[0], {}).get("up")
        if (nodes.get(chain[0]) or {}).get("role") != "menuitem" or (nodes.get(up) or {}).get("role") != "menuitem":
            break
        chain.insert(0, up)
    return chain


def _menu_top(seen: dict, ref: str) -> str:
    """닫힌 메뉴 속 항목의 위치는 의미가 없다(실PC 2026-11-16) — 가상 커서는 그 항목이 달린 맨 위 메뉴(메뉴바의 보이는 칸)를 가리킨다."""
    return _menu_chain(seen, ref)[0]


def propose(key: str, ref: str, action: str, value=None, via=None) -> dict:
    """제안을 기록하고 id 를 준다. 실행하지 않는다. 마지막 관찰이 공개한 ref·그 노드가 할 수 있는 동작만.
    via: 먼저 차례로 누를 상위 메뉴 ref 들(click 만). 승인 클릭에 포커스가 넘어가면 WinForms 펼침 메뉴가 닫혀서
    (실PC 2026-11-16), 펼치기도 승인 뒤에 한 번에 한다. 카드엔 '원무 › 수가별발생조회' 처럼 경로 전체가 보이고,
    경로의 모든 칸은 사람이 정한 이름(승인 라벨·대상 이름)이어야 한다 — '역할#번호' 는 무엇인지 몰라 거절."""
    global _pending
    via = [] if via is None else via
    if action not in ACT or not isinstance(ref, str) or not _REF.match(ref):
        raise ScreenError("허용되지 않은 조작이에요.")
    if not isinstance(via, list) or len(via) > MAX_VIA or (via and action != "click")             or not all(isinstance(r, str) and _REF.match(r) and r[1:].split(":")[0] == ref[1:].split(":")[0] for r in via)             or len(set(via + [ref])) != len(via) + 1:
        raise ScreenError(f"메뉴 경로는 같은 관찰의 서로 다른 ref {MAX_VIA}개까지, 누르기에만 붙어요.")
    via = list(via)                                 # 승인할 순서는 복사해 둔다 (codex1 D)
    if action == "set_value":
        if not isinstance(value, str) or len(value) > MAX_VALUE or "\x00" in value:
            raise ScreenError(f"넣을 값은 {MAX_VALUE}자 이하 글자여야 해요.")
    elif value is not None:
        raise ScreenError("이 조작에는 값을 넣지 않아요.")
    gen = current_gen()
    gone = None
    try:
        with _lock:
            if _running is not None:     # 보내는 중인 관찰을 다른 제안의 만료·거절 정리가 지우지 않게
                raise ScreenError("앞의 조작을 보내는 중이에요. 끝난 뒤 다시 읽고 요청하세요.")
            sid = ref[1:].split(":")[0]
            gone = _stop_plan("다른 조작 요청이 들어왔어요.")   # 계획 중간 개입 → 계획 끝 (3b)
            seen = _issued.get(sid)
            if gen is None or not seen or seen["gen"] != gen or seen["app"] != key:
                raise ScreenError("지금 관찰한 화면이 아니에요. screen_look 으로 다시 읽어 주세요.")
            if not seen["complete"]:
                raise ScreenError("화면을 일부만 읽어서 조작할 수 없어요. 대상 창을 좁혀 다시 읽어 주세요.")
            node = seen["nodes"].get(ref)
            if node is None or not _fits(node, action):
                raise ScreenError("그 요소에는 그 조작을 할 수 없어요.")
            path = [seen["nodes"].get(r) for r in via]
            # 카드엔 via 와 상관없이 관찰 트리의 메뉴 경로 전체 — 같은 이름 항목이 여러 메뉴에 있고(이온엠 78개),
            # via 가 중간 메뉴를 건너뛰어도 사람이 어느 명령인지 구분해야 한다 (codex1 메뉴 경로 신원 P2)
            shown = [seen["nodes"].get(r) for r in _menu_chain(seen, ref)[:-1]] if node["role"] == "menuitem" else path
            if via and not all(n is not None and n["role"] == "menuitem" and _fits(n, "click") for n in path + [node]):
                raise ScreenError("메뉴 경로에는 누를 수 있는 메뉴 항목만 올 수 있어요.")
            if via and not all(_under(seen["meta"], b, a) for a, b in zip(via, via[1:] + [ref])):
                raise ScreenError("메뉴 경로가 화면의 메뉴 계층과 달라요 — 앞 항목 아래에 다음 항목이 있어야 해요.")
            # 메뉴 항목은 닫힌 메뉴 속(안 보이는 것)도 엔진이 누를 수 있다 — 무엇인지 모르는 '역할#번호' 는 거절
            if (via or node["role"] == "menuitem") and any(_UNNAMED.fullmatch(n["label"]) for n in path + shown + [node]):
                raise ScreenError("메뉴 항목과 그 위 메뉴들은 모두 사람이 정한 이름이어야 해요. 이름을 승인 목록에 넣어 주세요.")
            label = " › ".join([n["label"] for n in shown] + [node["label"]])
            _pending = {"id": uuid.uuid4().hex, "gen": gen, "snapshot": sid, "seen": seen, "ref": ref, "action": action,
                        "value": value, "digest": _digest(value), "label": label, "role": node["role"], "via": via,
                        "app": key, "until": time.monotonic() + APPROVAL}
            pid = _pending["id"]
    finally:
        _drop(gone)                  # 떼어 낸 관찰은 거절돼도 정리 (3b r1 P2-1)
    journal.record(journal.Entry(at=journal.now(), job_id="", rune_id="screen", level="info", message="screen_propose",
                                 fields={"app": key, "action": action, "label": label}))
    return {"pending": pid, "expires_in": APPROVAL,
            "next": "사람이 VaderAI 화면에서 승인해야 실행돼요. screen_result 로 결과를 확인하세요."}


def _expire() -> None:
    """승인 기한이 지난 제안을 끝난 상태(refused)로 — 카드만 숨기지 않는다 (codex1 2단계 r1 P2-1)."""
    global _pending
    with _lock:
        p = _pending
        if not p or time.monotonic() < p["until"]:
            return
        _pending = None
        gone = _detach(p)            # 같은 걸음에 관찰도 떼어 — 그 사이 같은 관찰로 새 제안을 받지 않는다 (r2 NEW-P2-1)
        _settle(p, REFUSED, "60초 안에 승인하지 않아 보내지 않았어요.")   # 결과도 같은 걸음에 (r3 NEW-P2-1)
    _drop(gone)                      # 파일 정리·일지만 잠금 밖
    _log(p, REFUSED)


def pending() -> dict | None:
    """앱 메인 창의 승인 카드에 보일 것 (값 포함 — 사람이 본 그대로 승인한다)."""
    _expire()
    gen = current_gen()
    with _lock:
        p = _pending
        if not p or p["gen"] != gen or time.monotonic() >= p["until"]:
            return None
        return {"id": p["id"], "app": p["app"], "label": p["label"], "role": p["role"],
                # 메뉴 항목은 '보이는 곳' 이 아니라 그 메뉴 명령을 실행한다 — 닫혀 있어도 (codex1 메뉴 명령 D)
                "action": "메뉴 명령 실행 (메뉴가 닫혀 있어도)" if p["role"] == "menuitem" and p["action"] == "click"
                else ACT_KO[p["action"]], "value": p["value"], "digest": p["digest"],
                "left": int(p["until"] - time.monotonic()),
                "plan": f"묶음 {p['step'] + 1}/{len(p['plan']['steps'])} 단계" if p.get("plan") else None}


def _settle(p: dict, kind: str, message: str) -> None:
    """_lock 안에서: 결과 확정. 만료도 제안을 꺼낸 같은 걸음에 — 늦은 정리가 뒤 결과를 덮지 않게 (r3 NEW-P2-1)."""
    global _outcome, _running
    _outcome = {"id": p["id"], "gen": p["gen"], "result": kind, "message": message}
    if _running is p:
        _running = None
    _plan_after(p, kind, message)


def _log(p: dict, kind: str) -> None:
    extra = {"plan": p["plan"]["id"], "step": p["step"]} if p.get("plan") else {}
    journal.record(journal.Entry(at=journal.now(), job_id="", rune_id="screen", level="info", message="screen_act",
                                 fields={"app": p["app"], "action": p["action"], "label": p["label"], "result": kind,
                                         **extra}))


def _done(p: dict, kind: str, message: str) -> dict:
    with _lock:
        _settle(p, kind, message)
    _log(p, kind)
    return {"result": kind, "message": message}


def decide(pid: str, digest: str, approve: bool) -> dict:
    """사람의 승인·거절 — 앱 메인 창에서만 온다. 제안은 여기서 한 번 소비된다(승인이든 거절이든)."""
    global _pending, _running
    if not _look_lock.acquire(timeout=RUN_AFTER):   # 실행 중엔 새 관찰이 그 폴더(ref)를 지우지 못하게
        raise ScreenError("화면을 읽는 중이에요. 잠시 뒤 다시 눌러 주세요.")
    try:
        _plan_expire()
        gen = current_gen()
        with _lock:
            q = _pending
        same_profile = not (q and q.get("plan")) or _profile_digest(q["app"]) == q["plan"]["profile"]
        with _lock:
            p = _pending
            if not p or p["id"] != pid:
                raise ScreenError("이미 처리됐거나 바뀐 요청이에요.")
            _pending = None                         # 한 번만 — 같은 걸음에 '보내는 중' 으로
            _running = p
            live = (p["gen"] == gen and gen is not None and time.monotonic() < p["until"]
                    and _issued.get(p["snapshot"]) is p["seen"] and p["digest"] == digest)
            pl = p.get("plan")
            if pl is not None:          # 계획 단계 — 계획이 아직 이 단계 확인을 기다리고, 프로필이 같아야 (3b)
                live = live and same_profile and _plan is pl and pl["state"] == "confirming" and pl["at"] == p["step"]
                if live:
                    pl["state"] = "executing"
            end = min(time.monotonic() + RUN_AFTER, pl["until"] if pl else math.inf)
        try:
            if approve is not True:
                return _done(p, REFUSED, "사람이 거절했어요.")
            if not live or hold is None:
                return _done(p, REFUSED, "승인 기한이 지났거나 화면이 바뀌었어요. 다시 읽고 요청하세요.")
            with hold():                            # 작업·녹화와 겹치지 않게 — 못 잡으면 안 보낸다
                return _act(p, end)
        except ScreenError as exc:
            return _done(p, exc.kind, str(exc))
        except ValueError as exc:                   # 실행권을 못 잡았다 — 보내지 않았다
            return _done(p, REFUSED, str(exc))
        finally:
            _forget(p)
    finally:
        _look_lock.release()


def _detach(p: dict) -> dict | None:
    """_lock 안에서: 그 제안의 관찰이 아직 게시돼 있을 때만(같은 객체) 뗀다. 새 관찰은 건드리지 않는다 (r2 NEW-P2-2)."""
    if _issued.get(p["snapshot"]) is p["seen"]:
        return _issued.pop(p["snapshot"])
    return None


def _drop(seen: dict | None) -> None:
    if seen and not _remove(seen["home"]):
        _set_dirty(True)


def _forget(p: dict) -> None:
    """그 관찰을 버린다 — 조작했든 안 했든 다음은 새로 읽는다."""
    with _lock:
        gone = _detach(p)
    _drop(gone)


def _act(p: dict, deadline: float) -> dict:
    seen = p["seen"]
    w = {"id": seen["window"], "pid": seen["pid"], "process": seen["process"], "exe": seen["exe"]}
    spec = apps().get(p["app"])
    if not isinstance(spec, dict) or [x for x in _windows(spec, deadline) if x["id"] == w["id"]] != [w]:
        raise ScreenError("창이 바뀌었어요. 보내지 않았어요.")
    if current_gen() != p["gen"]:
        raise ScreenError("VaderAI 가 잠겼어요. 보내지 않았어요.")
    value = p["value"].encode("utf-8") if p["action"] == "set_value" else None
    sent = False                                    # 메뉴 경로에서 앞 클릭이 전달됐(을 수 있)다
    partial = "앞 메뉴 클릭은 전달됐어요(펼쳐졌는지는 확인 못 함). 끝 항목은 확실하지 않아요"
    try:
        for r in p.get("via") or []:                # 메뉴 경로 — 실패·미전달이면 그 아래는 누르지 않는다
            _ghost((seen.get("meta") or {}).get(r, {}).get("at"), p["gen"])
            got = run(["click", r, "--timeout-ms", _engine_ms(deadline)], seen["home"], deadline, None, p.get("go"),
                      menu_path=seen.get("menu_path") is True)
            if got["disposition"]["delivery"] == "not_delivered":
                if sent:
                    raise ScreenError(f"{partial} — 다음 메뉴를 보내지 않았어요. 화면을 확인해 주세요.", UNKNOWN)
                raise ScreenError("메뉴를 보내지 않았어요. 그 아래는 누르지 않았어요. 화면에서 확인하세요.", REFUSED)
            sent = True
        _ghost((seen.get("meta") or {}).get(p["ref"] if p.get("via") else _menu_top(seen, p["ref"]), {}).get("at"), p["gen"])
        args = [ACT[p["action"]], p["ref"], "--timeout-ms", _engine_ms(deadline), *(["--stdin"] if value is not None else [])]
        data = run(args, seen["home"], deadline, value, p.get("go"), menu_path=seen.get("menu_path") is True)
    except ScreenError as exc:
        if sent and not str(exc).startswith(partial):   # 앞 메뉴는 이미 보냈다 — '안 보냈다' 가 아니다
            raise ScreenError(f"{partial} ({exc}). 화면을 확인해 주세요.", UNKNOWN) from exc
        raise
    delivery = p["delivery"] = data["disposition"]["delivery"]
    if delivery == "not_delivered" and sent:
        return _done(p, UNKNOWN, f"{partial} — 끝 항목은 보내지 않았어요. 화면을 확인해 주세요.")
    if delivery == "not_delivered":
        return _done(p, REFUSED, "보내지 않았어요 (이미 그 상태일 수 있어요). 화면에서 확인하세요.")
    return _done(p, DONE, "보냈어요. 결과는 화면을 다시 읽어 확인하세요." if delivery == "delivered_unverified"
                 else "보냈고 엔진이 바뀐 것을 확인했어요.")


def _engine_ms(deadline: float | None) -> str:
    """엔진 --timeout-ms — SNAP_MS 이하, 남은 기한에서 2초(띄우기·답 받기) 뺀 만큼. 최소 1초."""
    return str(int(max(1.0, min(SNAP_MS / 1000, _left(deadline) - 2)) * 1000))


def _ghost(at, gen) -> None:
    """가상 커서(vader.desktop.ghost)에 '누를 예정 위치' 를 넘긴다 — 그림뿐, 보냈다는 증거가 아니다.
    막지 않는다: 큐에 하나만 넣고(차 있으면 버림) 별도 스레드가 보낸다. gen 은 그 조작의 열림 — 잠긴 뒤엔 안 그린다."""
    global _ghost_q
    if not at or gen is None:
        return
    with _ghost_lock:
        if _ghost_q is None:
            _ghost_q = queue.Queue(maxsize=1)
            threading.Thread(target=_ghost_loop, daemon=True).start()
    try:
        _ghost_q.put_nowait((at, gen))
    except queue.Full:
        pass


def _ghost_child(gen) -> subprocess.Popen | None:
    """엔진과 같은 수명 규칙: 멈춘 채 만들고 → _procs·Job 에 넣고 → 아직 그 열림이면 깨운다. 아니면 끝낸다.
    만들기는 _lock 밖 — 느려도 조작·중단·정리를 막지 않는다. 등록만 짧게 _lock 안."""
    proc = subprocess.Popen(config.module_command("vader.desktop.ghost"), stdin=subprocess.PIPE,
                            creationflags=NO_WINDOW | SUSPENDED)
    with _lock:
        _procs.add(proc)
    try:
        _in_job(proc)
        if current_gen() != gen:
            raise ScreenError("잠겼어요.")
        _resume(proc)
        return proc
    except (OSError, ScreenError):
        if _stop(proc):                       # 못 끝냈으면 추적을 남긴다 — 다음 sweep 이 다시 끝낸다
            with _lock:
                _procs.discard(proc)
        return None


def _ghost_loop() -> None:
    """커서 프로세스도 _procs·Job — sweep(잠금·끝)이 끝내고, 앱이 죽어도 남지 않는다. 막혀도 이 스레드만."""
    global _ghost_proc
    while True:
        (x, y), gen = _ghost_q.get()
        try:
            if current_gen() != gen:
                continue
            proc = _ghost_proc
            if proc is None or proc.poll() is not None:
                proc = _ghost_proc = _ghost_child(gen)
                if proc is None:
                    continue
            if current_gen() == gen:
                proc.stdin.write(f"{x} {y}\n".encode())
                proc.stdin.flush()
        except (OSError, ValueError):
            pass


def outcome(pid: str) -> dict:
    """AI 가 묻는 결과 — 같은 세대에서만. 기다리는 중이면 waiting. 계획 id 면 계획 상태."""
    _expire()
    _plan_expire()
    gen = current_gen()
    with _lock:
        status = _plan_status(pid, gen)
        if status is not None:
            return status
        if _pending and _pending["id"] == pid and _pending["gen"] == gen:
            return {"result": "waiting", "message": "사람의 승인을 기다리는 중이에요."}
        if _running and _running["id"] == pid and _running["gen"] == gen:
            return {"result": "running", "message": "승인돼 보내는 중이에요. 잠시 뒤 다시 확인하세요."}
        o = _outcome
    if not o or o["id"] != pid or o["gen"] != gen:
        raise ScreenError("그 요청의 결과가 없어요.")
    return {"result": o["result"], "message": o["message"]}


def _clear_pending() -> None:
    global _pending, _outcome, _running, _plan, _plan_ask
    _pending = _outcome = _running = _plan = _plan_ask = None


# ── 3단계 공통: 대상 프로필 — 사람이 정한 이름→셀렉터. AI·녹화의 임의 문자열을 화면과 비교하지 않는다 ──

BATCH_OK = {"check", "uncheck", "set_value"}     # 묶음에서 카드 없이 돌 수 있는 동작 — click 은 늘 다시 승인


AID_KINDS = ("automation_id", "parent_automation_id")   # 대상 kind — 없으면 automation_id


def profile(key: str) -> dict:
    """그 앱의 대상 프로필 (검사한 것). 틀리면 ScreenError — 어설픈 프로필로 비교하지 않는다."""
    spec = apps().get(key)
    if not isinstance(spec, dict):
        raise ScreenError("허용된 앱이 아니에요.")
    labels = {x for x in spec.get("labels") or [] if isinstance(x, str)}
    raw = spec.get("targets") or {}
    if not isinstance(raw, dict):
        raise ScreenError("대상 프로필이 맞지 않아요.")
    out = {}
    for name, t in raw.items():
        ok = (isinstance(name, str) and 0 < len(name) <= 40 and isinstance(t, dict) and t.get("role") in ROLES
              and isinstance(t.get("batch", []), list) and set(t.get("batch", [])) <= BATCH_OK
              and (("aid" in t) != ("label" in t)) and t.get("kind", "automation_id") in AID_KINDS
              and ("kind" not in t or "aid" in t)
              and (isinstance(t.get("aid"), str) and 0 < len(t["aid"]) <= 200 if "aid" in t else t.get("label") in labels))
        if not ok:
            raise ScreenError("대상 프로필이 맞지 않아요.")
        out[name] = {"role": t["role"], "aid": (t.get("kind", "automation_id"), t["aid"]) if "aid" in t else None, "label": t.get("label"), "batch": list(t.get("batch", []))}
    return out


def _hits(target: dict, nodes: list[dict], meta: dict) -> list[str]:
    """완전한 관찰에서 그 대상에 맞는 ref 들 — 역할 같고 (aid 같음 | 화면 이름이 그 승인 라벨과 정확히 같음)."""
    return [n["ref"] for n in nodes if n["role"] == target["role"] and (
        meta[n["ref"]]["aid"] == target["aid"] if target["aid"] else meta[n["ref"]]["exact"] == target["label"])]


# ── 3a 셀렉터 진단 (제작소, 읽기만) ──

# 녹화(pywinauto UIA control_type) → 엔진 역할
_CONTROL = {"Button": "button", "Edit": "textfield", "CheckBox": "checkbox", "ComboBox": "combobox",
            "RadioButton": "radiobutton", "Hyperlink": "link", "TabItem": "tab", "ListItem": "option",
            "MenuItem": "menuitem", "TreeItem": "treeitem", "DataItem": "row", "Document": "document"}
MAX_RECORDING = 256 * 1024
MAX_STEPS = 500
MAX_FIELD = 500


def _recording(rune_id: str, name: str) -> list[dict]:
    """룬의 녹화 하나를 엄격하게 — 경로는 그 룬 recordings 폴더 안(링크·정션으로 밖에 나가지 않음), 크기·형식·필드."""
    from dataclasses import fields
    from vader.core import recorder, workshop
    if not isinstance(name, str) or not recorder._NAME.fullmatch(name):
        raise ScreenError("녹화 이름이 맞지 않아요.")
    if not workshop.ID_PATTERN.fullmatch(rune_id or ""):
        raise ScreenError("룬을 찾지 못했어요.")
    # 연결 정보(project.json)도 제작소 폴더 자리 그대로인 것만 믿는다 — 정션 밖 메타의 source 로 넘어가지 않게
    # (codex1 3a r2 P1-1). package_dir 를 쓰지 않고 검사한 메타를 직접 읽는다.
    proj = Path(os.path.realpath(workshop.root())) / rune_id
    meta = proj / workshop.META
    if os.path.normcase(os.path.realpath(proj)) != os.path.normcase(str(proj)) or not proj.is_dir():
        raise ScreenError("룬을 찾지 못했어요.")
    source = None
    if os.path.lexists(meta):
        if os.path.normcase(os.path.realpath(meta)) != os.path.normcase(str(meta)) or not meta.is_file() \
                or meta.stat().st_size > 64 * 1024:
            raise ScreenError("룬 연결 정보가 맞지 않아요.")
        try:
            got = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ScreenError("룬 연결 정보가 맞지 않아요.") from None
        source = got.get("source") if isinstance(got, dict) else None
        if source is not None and not (isinstance(source, str) and os.path.isabs(source)):
            raise ScreenError("룬 연결 정보가 맞지 않아요.")
    # 기준 뿌리: 사람이 link_source 로 연결한 원본이면 그 폴더, 아니면 제작소 사본 <id>/<id>
    base = Path(os.path.realpath(source)) if source else proj / rune_id
    path = base / "recordings" / f"{name}.json"
    # 뿌리 아래 경로(룬 폴더·recordings·파일)가 재분석 지점이면 실제 경로가 달라진다 — 그 자리 그대로여야 한다
    if os.path.normcase(os.path.realpath(path)) != os.path.normcase(str(path)) or not path.is_file():
        raise ScreenError("그 룬 안의 녹화를 찾지 못했어요.")
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_RECORDING + 1)
            opened = os.fstat(f.fileno())
        now = os.stat(path, follow_symlinks=False)
    except OSError:
        raise ScreenError("녹화를 읽지 못했어요.") from None
    # 검사와 열기 사이에 바뀌지 않았다 — 연 파일이 지금 그 자리의 (링크 아닌) 그 파일
    if (opened.st_dev, opened.st_ino) != (now.st_dev, now.st_ino) or \
            os.path.normcase(os.path.realpath(path)) != os.path.normcase(str(path)):
        raise ScreenError("녹화를 읽는 사이 파일이 바뀌었어요.")
    if len(raw) > MAX_RECORDING:
        raise ScreenError("녹화 파일이 너무 커요.")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ScreenError("녹화 파일 형식이 맞지 않아요.") from None
    kinds = {f.name: f.type for f in fields(recorder.Step)}
    steps = doc.get("steps") if isinstance(doc, dict) and doc.get("format") == "vader-recording/1" else None
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise ScreenError("녹화 파일 형식이 맞지 않아요.")
    for st in steps:
        if not isinstance(st, dict) or not set(st) <= set(kinds) or not all(
                (isinstance(v, str) and len(v) <= MAX_FIELD) if kinds[k] in (str, "str") else
                (isinstance(v, (int, float)) and not isinstance(v, bool)) for k, v in st.items()):
            raise ScreenError("녹화 파일 형식이 맞지 않아요.")
    return steps


def _pick(step: dict, targets: dict) -> str | None:
    """녹화 단계 → 프로필 대상 이름. 녹화 글자는 프로필과만 비교(화면과 안 함). 둘 이상이면 고르지 않는다.
    녹화에는 부모 id 가 없어 kind parent_automation_id 대상은 고르지 않는다 — 알려진 한계."""
    role = _CONTROL.get(step.get("control_type", ""))
    got = [n for n, t in targets.items() if t["role"] == role and (
        t["aid"] == ("automation_id", step.get("automation_id")) if t["aid"] else t["label"] == step.get("name"))]
    return got[0] if len(got) == 1 else None


def diagnose(key: str, rune_id: str, name: str, deadline: float | None = None) -> dict:
    """녹화 단계마다 프로필 대상과 지금 화면의 후보 수. 관찰은 게시하지 않고 끝나면 지운다.
    지금 화면 기준이라 앞 단계 뒤에 나타날 칸은 0 일 수 있다. 일부만 읽었으면 matches 는 전부 null."""
    targets = profile(key)
    steps = _recording(rune_id, name)
    gen = current_gen()
    with _lock:
        epoch = _epoch
    w = _target(key, deadline)
    home = new_home()
    try:
        data, nodes, meta, full = _observe(key, w, home, deadline)
    finally:
        if not _remove(home):
            _set_dirty(True)
    with _lock:
        fresh = _epoch == epoch and current_gen() == gen and gen is not None and _left(deadline) > 0
    if not fresh:
        raise ScreenError("VaderAI 가 잠겨 결과를 버렸어요.", UNKNOWN)
    complete = data["complete"] and full
    out = []
    for i, st in enumerate(steps):
        if st.get("keys"):
            out.append({"step": i, "kind": "key", "target": None, "matches": None})
            continue
        target = _pick(st, targets)
        hits = len(_hits(targets[target], nodes, meta)) if target and complete else None
        out.append({"step": i, "kind": "click", "target": target,
                    "matches": None if hits is None else hits if hits < 2 else "many"})
    journal.record(journal.Entry(at=journal.now(), job_id="", rune_id="screen", level="info", message="screen_diagnose",
                                 fields={"app": key, "steps": len(out), "matched": sum(1 for x in out if x["matches"] == 1)}))
    return {"app": key, "complete": complete, "steps": out,
            "note": "지금 화면 기준이에요 — 앞 단계 뒤에 나타날 칸은 0 일 수 있어요."}


# ── 3b 묶음 승인 — 사람이 정한 대상 프로필의 대상만, 정해진 순서로. click·batch 밖 동작은 그 단계에서 다시 카드 ──
# 상태: (카드) → active{next} → executing{at} | confirming{at} → active{next+1} | complete | stopped{reason}.
# 진행·중단은 _lock 안에서만 (결과 확정 _settle 과 같은 걸음). 자동 재개·재시도 없음. complete 는 '계획 실행 완료' —
# 업무 성공이 아니다. 중간 개입(단일 제안·룬 작업·녹화·새 계획·잠금·창 주인/프로필 바뀜)이면 stopped.

MAX_PLAN = 10
PLAN_VALUE = 500                # 단계 하나 값 글자
PLAN_VALUES = 2000              # 계획 전체 값 글자
PLAN_MINUTES = 5
PLAN_APPS = {"시험 화면"}         # 묶음을 쓸 수 있는 앱 — 이번 단계는 합성 시험 앱만

_plan_ask: dict | None = None   # 사람이 볼 계획 카드 (60초)
_plan: dict | None = None       # 승인된 계획 하나 (끝나도 같은 세대 동안 결과 조회용으로 남는다)
_plan_rev = 0                   # 계획 회차 — 개입·새 계획·중단마다 오른다. 승인 확정은 소비 때와 같은 회차일 때만 (3b r1 P1-1)


def _profile_digest(key: str) -> str:
    try:
        return hashlib.sha256(json.dumps(profile(key), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    except ScreenError:
        return ""


def _role_fits(role: str, action: str) -> bool:
    if action in ("check", "uncheck"):
        return role in ("checkbox", "switch")
    return action == "click" or (action == "set_value" and role == "textfield")


def _stop_plan(reason: str) -> dict | None:
    """_lock 안에서: 활성 계획을 끝낸다. 확인 카드가 떠 있으면 그 카드(그 관찰)도 — 떼어 낸 관찰을 돌려준다.
    회차를 올리고 승인 대기 계획 카드도 버린다 — 확정 중인 옛 승인이 이 개입 뒤에 살아나지 않게."""
    global _pending, _plan_rev, _plan_ask
    _plan_rev += 1
    _plan_ask = None
    pl = _plan
    if not pl or pl["state"] not in ("active", "confirming", "executing"):
        return None
    pl["state"], pl["reason"] = "stopped", reason
    p = _pending
    if p is not None and p.get("plan") is pl:
        _pending = None
        return _detach(p)
    return None


def stop_plan(reason: str = "사람이 중단했어요.") -> bool:
    """앱(사람 [중단]·룬 작업·녹화 시작)이 부른다. 이미 보냈을 수 있는 단계는 취소됐다고 하지 않는다."""
    with _lock:
        was = bool(_plan and _plan["state"] in ("active", "confirming", "executing"))
        gone = _stop_plan(reason)
    _drop(gone)
    return was


def _plan_expire() -> None:
    global _plan_ask
    gone = None
    with _lock:
        if _plan_ask and time.monotonic() >= _plan_ask["until"]:
            _plan_ask = None
        if _plan and _plan["state"] in ("active", "confirming") and time.monotonic() >= _plan["until"]:
            gone = _stop_plan("계획 시간이 지났어요.")
    _drop(gone)


def _plan_after(p: dict, kind: str, message: str) -> None:
    """_lock 안에서 (_settle): 그 단계 결과를 계획에 적고 진행하거나 끝낸다. 옛 계획·옛 단계는 건드리지 않는다."""
    pl = p.get("plan")
    if pl is None or _plan is not pl or pl["at"] != p["step"] or len(pl["results"]) != p["step"]:
        return
    delivery = p.get("delivery")
    pl["results"].append({"step": p["step"], "result": kind, "delivery": delivery, "message": message})
    if pl["state"] not in ("executing", "confirming"):
        return                       # 그 사이 중단됐다 — 결과만 남기고 진행하지 않는다
    ok = kind == DONE and (delivery == "delivered_verified" or
                           (p["action"] == "click" and delivery == "delivered_unverified"))
    if ok:
        pl["next"] += 1
        pl["state"] = "complete" if pl["next"] == len(pl["steps"]) else "active"
    else:
        pl["state"], pl["reason"] = "stopped", message


def _plan_status(pid: str, gen) -> dict | None:
    """_lock 안에서: 계획 id 의 상태 (아니면 None)."""
    if _plan_ask and _plan_ask["id"] == pid and _plan_ask["gen"] == gen:
        return {"result": "waiting", "message": "사람의 계획 승인을 기다리는 중이에요."}
    pl = _plan
    if not pl or pl["id"] != pid or pl["gen"] != gen:
        return None
    out = {"result": pl["state"], "next": pl["next"], "total": len(pl["steps"]),
           "left": max(0, int(pl["until"] - time.monotonic())), "steps": [dict(r) for r in pl["results"]]}
    if pl["state"] == "stopped":
        out["reason"] = pl["reason"]
    return out


def _plan_allowed(key: str) -> bool:
    """이번 단계 묶음은 내장 합성 시험 앱 설정 그대로일 때만 — 진단 프로필이 있다고 묶음이 열리지 않는다.
    일반 업무 앱은 실PC 검증 뒤 별도 범위 변경으로 (codex1 3b r1 P2-2)."""
    return key in PLAN_APPS and apps().get(key) == DEFAULT_APPS[key]


def plan_propose(key: str, steps, minutes) -> dict:
    """계획 카드를 만든다. 실행하지 않는다. 이전 활성 계획은 끝난다."""
    global _plan_ask
    if not _plan_allowed(key):
        raise ScreenError("이 앱은 아직 묶음 계획을 쓸 수 없어요 (한 번에 하나씩 승인하는 조작은 돼요).")
    targets = profile(key)
    if not isinstance(steps, list) or not 0 < len(steps) <= MAX_PLAN:
        raise ScreenError(f"계획은 1~{MAX_PLAN}단계예요.")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) \
            or not 0 < minutes <= PLAN_MINUTES:
        raise ScreenError(f"계획 시간은 {PLAN_MINUTES}분 이하로 정해 주세요.")
    clean, total = [], 0
    for st in steps:
        if not isinstance(st, dict) or not set(st) <= {"target", "action", "value"}:
            raise ScreenError("계획 단계 모양이 맞지 않아요.")
        t, action, value = st.get("target"), st.get("action"), st.get("value")
        if t not in targets or action not in ACT or not _role_fits(targets[t]["role"], action):
            raise ScreenError("프로필에 없는 대상이거나 그 대상에 맞지 않는 동작이에요.")
        if action == "set_value":
            if not isinstance(value, str) or len(value) > PLAN_VALUE or "\x00" in value:
                raise ScreenError(f"넣을 값은 단계마다 {PLAN_VALUE}자 이하 글자여야 해요.")
            total += len(value)
        elif value is not None:
            raise ScreenError("이 조작에는 값을 넣지 않아요.")
        clean.append({"target": t, "action": action, "value": value})
    if total > PLAN_VALUES:
        raise ScreenError(f"계획 전체 값은 {PLAN_VALUES}자 이하여야 해요.")
    gen = current_gen()
    if gen is None:
        raise ScreenError("VaderAI 가 잠겨 있어요.")
    ask = {"id": uuid.uuid4().hex, "gen": gen, "app": key, "steps": clean, "minutes": minutes,
           "profile": _profile_digest(key), "until": time.monotonic() + APPROVAL,
           "digest": hashlib.sha256(json.dumps([key, clean, minutes], ensure_ascii=False).encode("utf-8")).hexdigest()}
    with _lock:
        gone = _stop_plan("새 계획이 들어왔어요.")     # 회차도 오른다 — 확정 중인 옛 승인은 무효
        _plan_ask = ask
    _drop(gone)
    journal.record(journal.Entry(at=journal.now(), job_id="", rune_id="screen", level="info", message="screen_plan",
                                 fields={"app": key, "steps": len(clean), "result": "proposed"}))
    return {"plan": ask["id"], "expires_in": APPROVAL,
            "next": "사람이 VaderAI 화면에서 계획을 승인하면 screen_step(plan, 0) 부터 한 단계씩 부르세요."}


def plan_card() -> dict | None:
    """앱 메인 창: 승인 기다리는 계획 카드, 아니면 진행 중 계획 띠."""
    _plan_expire()
    gen = current_gen()
    with _lock:
        a = _plan_ask
        if a and a["gen"] == gen:
            targets = {}
            try:
                targets = profile(a["app"])
            except ScreenError:
                pass
            return {"kind": "ask", "id": a["id"], "app": a["app"], "digest": a["digest"], "minutes": a["minutes"],
                    "left": int(a["until"] - time.monotonic()),
                    "steps": [{"target": st["target"], "role": targets.get(st["target"], {}).get("role", ""),
                               "action": ACT_KO[st["action"]], "value": st["value"],
                               "confirm": st["action"] not in targets.get(st["target"], {}).get("batch", [])}
                              for st in a["steps"]]}
        pl = _plan
        if pl and pl["gen"] == gen and pl["state"] in ("active", "confirming", "executing"):
            return {"kind": "active", "id": pl["id"], "app": pl["app"], "next": pl["next"], "total": len(pl["steps"]),
                    "left": max(0, int(pl["until"] - time.monotonic()))}
    return None


def plan_decide(pid: str, digest: str, approve: bool) -> dict:
    """사람의 계획 승인·거절 — 앱 메인 창에서만. 카드는 한 번 소비. 승인하면 그때의 창 주인을 고정한다."""
    global _plan_ask, _plan
    gen = current_gen()
    with _lock:
        a = _plan_ask
        if not a or a["id"] != pid:
            raise ScreenError("이미 처리됐거나 바뀐 계획이에요.")
        _plan_ask = None
        rev = _plan_rev
        live = a["gen"] == gen and gen is not None and time.monotonic() < a["until"] and a["digest"] == digest
    if approve is not True or not live or _profile_digest(a["app"]) != a["profile"]:
        return {"result": REFUSED, "message": "사람이 거절했어요." if approve is not True
                else "승인 기한이 지났거나 프로필이 바뀌었어요. 다시 요청하세요."}
    w = _target(a["app"], time.monotonic() + RUN_AFTER)
    with _lock:
        if current_gen() != a["gen"]:
            return {"result": REFUSED, "message": "VaderAI 가 잠겼어요."}
        if _plan_rev != rev:                    # 창을 확인하는 사이 새 계획·개입·중단이 있었다 — 되살리지 않는다
            return {"result": REFUSED, "message": "그 사이 다른 요청이 들어와 이 계획은 버렸어요."}
        gone = _stop_plan("새 계획이 승인됐어요.")
        _plan = {"id": a["id"], "gen": a["gen"], "app": a["app"], "steps": a["steps"], "profile": a["profile"],
                 "owner": w, "until": time.monotonic() + a["minutes"] * 60, "next": 0, "at": None,
                 "state": "active", "results": [], "reason": None}
    _drop(gone)
    journal.record(journal.Entry(at=journal.now(), job_id="", rune_id="screen", level="info", message="screen_plan",
                                 fields={"app": a["app"], "steps": len(a["steps"]), "result": "approved"}))
    return {"result": DONE, "message": f"계획을 승인했어요 ({len(a['steps'])}단계, {a['minutes']}분)."}


def step(pid: str, k, deadline: float | None = None) -> dict:
    """계획의 k 번째 단계 — AI 는 ref 를 주지 않는다. 앱이 새로 완전하게 읽어 그 대상의 유일 후보를 찾아 실행한다.
    k < next 면 기록된 결과(다시 안 보냄), 보내는 중이면 running, 확인 카드가 떠 있으면 confirming."""
    global _running, _pending
    if type(k) is not int or k < 0:
        raise ScreenError("단계 번호가 맞지 않아요.")
    _plan_expire()
    gen = current_gen()
    with _lock:
        pl = _plan
        if not pl or pl["id"] != pid or pl["gen"] != gen or gen is None:
            raise ScreenError("그 계획이 없어요.")
        if k < len(pl["results"]):
            return {"result": pl["results"][k]["result"], **pl["results"][k]}
        if pl["state"] in ("executing", "confirming") and pl["at"] == k:
            return {"result": "running" if pl["state"] == "executing" else "confirming",
                    "message": "보내는 중이에요." if pl["state"] == "executing" else "사람의 확인을 기다리는 중이에요."}
        if pl["state"] != "active":
            return _plan_status(pid, gen)
        if k != pl["next"]:
            raise ScreenError(f"지금은 {pl['next']}번 단계 차례예요.")
        if _running is not None or _pending is not None:
            raise ScreenError("다른 조작이 진행 중이에요.")
        pl["state"], pl["at"] = "executing", k           # 예약 — 같은 단계가 두 번 시작되지 않는다
    try:
        return _step(pl, k, deadline)
    except BaseException:
        with _lock:                                      # 뜻밖의 실패 — 계획을 끝낸다(다시 시도하지 않는다)
            if _plan is pl and pl["state"] == "executing" and pl["at"] == k:
                pl["state"], pl["reason"] = "stopped", "단계를 진행하지 못했어요."
        raise


def _step(pl: dict, k: int, deadline: float | None) -> dict:
    global _running, _pending
    end = min(deadline if deadline is not None else math.inf, pl["until"])
    st = pl["steps"][k]

    def stop(reason: str) -> dict:
        with _lock:
            if _plan is pl and pl["state"] == "executing" and pl["at"] == k:
                pl["state"], pl["reason"] = "stopped", reason
        return _plan_status(pl["id"], pl["gen"]) or {"result": "stopped", "reason": reason}

    if _profile_digest(pl["app"]) != pl["profile"] or not _plan_allowed(pl["app"]):
        return stop("대상 프로필이 바뀌었어요.")
    target = profile(pl["app"])[st["target"]]
    if hold is None:
        return stop("앱 화면에서만 진행할 수 있어요.")
    if not _look_lock.acquire(timeout=max(0.0, min(RUN_AFTER, end - time.monotonic()))):
        return stop("화면을 읽는 중이라 기한 안에 진행하지 못했어요.")
    p = None
    try:
        try:
            look(pl["app"], end)                        # 새 완전한 관찰 (이 스레드가 _look_lock 을 쥔 채 — 끼어들 관찰 없음)
        except ScreenError as exc:
            return stop(str(exc))
        with _lock:
            seen = next(iter(_issued.values()), None)
        if not seen or seen["gen"] != pl["gen"] or not seen["complete"]:
            return stop("화면을 다 읽지 못했어요.")
        owner = {"id": seen["window"], "pid": seen["pid"], "process": seen["process"], "exe": seen["exe"]}
        if owner != pl["owner"]:
            return stop("승인할 때의 창이 아니에요.")
        nodes = list(seen["nodes"].values())
        hits = _hits(target, nodes, seen["meta"])
        if len(hits) != 1:
            return stop("그 대상을 화면에서 하나로 찾지 못했어요." if not hits else "그 대상이 화면에 여럿이에요.")
        node = seen["nodes"][hits[0]]
        if not _fits(node, st["action"]):
            return stop("그 대상에는 그 조작을 할 수 없어요.")
        sid = hits[0][1:].split(":")[0]
        p = {"id": uuid.uuid4().hex, "gen": pl["gen"], "snapshot": sid, "seen": seen, "ref": hits[0],
             "action": st["action"], "value": st["value"], "digest": _digest(st["value"]), "label": st["target"],
             "role": node["role"], "app": pl["app"], "plan": pl, "step": k,
             # _lock 안에서: 아직 이 계획·이 단계를 보내는 중인가 (중단되면 거짓 — 보내지 않는다, 3b r1 P1-2)
             "go": lambda: _plan is pl and pl["state"] == "executing" and pl["at"] == k}
        if st["action"] not in target["batch"]:          # click·batch 밖 → 이 단계만 다시 사람 확인 (2단계 카드)
            with _lock:
                if not (_plan is pl and pl["state"] == "executing" and pl["at"] == k):
                    return _plan_status(pl["id"], pl["gen"]) or {"result": "stopped"}
                p["until"] = min(time.monotonic() + APPROVAL, pl["until"])   # 카드가 계획 만료를 늘리지 않는다
                _pending, pl["state"] = p, "confirming"
            keep, p = p, None                           # 관찰은 카드가 쥔다 — 여기서 버리지 않는다
            return {"result": "confirming", "pending": keep["id"],
                    "message": "이 단계는 사람이 VaderAI 화면에서 한 번 더 확인해야 해요. 같은 단계를 다시 물어 결과를 보세요."}
        with _lock:
            if not (_plan is pl and pl["state"] == "executing" and pl["at"] == k) or current_gen() != pl["gen"]:
                return _plan_status(pl["id"], pl["gen"]) or {"result": "stopped"}
            _running = p
        try:
            with hold():
                _act(p, min(end, time.monotonic() + RUN_AFTER))
        except ScreenError as exc:
            _done(p, exc.kind, str(exc))
        except ValueError as exc:                        # 실행권 없음 — 보내지 않았다
            _done(p, REFUSED, str(exc))
        with _lock:
            return {**pl["results"][k]} if len(pl["results"]) > k else (_plan_status(pl["id"], pl["gen"]) or {})
    finally:
        if p is not None:
            _forget(p)
        _look_lock.release()
