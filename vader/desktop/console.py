"""제작소 안 콘솔 — Claude Code·Codex 를 VaderAI 화면 안의 가상 터미널(ConPTY)에서 띄운다.

마스터 결정 (2026-10-14): 별도 콘솔 창 대신 제작소 안에. codex1 합의 (`wiki/handoff/vader-pages-codex.md`):

- 띄우는 것은 `clients.console_command` 가 만든 두 프로그램의 고정된 모양뿐이다 — 화면이 명령을 조립하지 못한다.
- **권한 장치가 아니다.** 안에 띄운다고 Claude Code·Codex 가 할 수 있는 일이 줄지 않는다.
- 출력은 화면이 다 그린 뒤에 다음 조각을 가져간다(긴 대기 읽기). 버퍼가 차면 읽기를 멈춰 프로그램 쪽이 기다리게 한다
  — 무한히 쌓지 않는다. 출력은 디스크에 남기지 않는다(키가 섞일 수 있다).
- 한 번에 한 세션. 화면을 옮겨도 세션은 산다. 닫을 때는 Ctrl+C 두 번 → 기다림 → 프로세스 트리 강제 종료.
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import threading
import time
import uuid

from vader.core import clients, config, workshop

#: 화면이 아직 가져가지 않은 출력의 상한(글자). 넘으면 읽기를 멈춘다.
MAX_BUFFER = 1 << 20
#: 한 번에 받는 입력의 상한(글자) — 붙여넣기 포함.
MAX_INPUT = 1 << 20
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: 붙여넣은 이미지 한 장의 상한(바이트)과, 앱이 만든 그 파일을 남겨 두는 날 수 — 대화를 이어 갈 때 다시 읽을 수 있게.
MAX_IMAGE = 20 << 20
KEEP_DAYS = 7
PNG_MAGIC = bytes.fromhex("89504e470d0a1a0a")


class ConsoleError(ValueError):
    """사람에게 그대로 보여 줄 수 있는 거절."""


class Console:
    def __init__(self):
        self._cond = threading.Condition()
        self._pty = None
        self._id = None
        self._client = None
        self._rune = None
        self._chunks: list[str] = []
        self._size = 0
        self._ended = False

    def status(self) -> dict:
        with self._cond:
            return {"session": self._id, "client": self._client, "rune_id": self._rune, "alive": self._alive()}

    def start(self, client: str, cols: int, rows: int, rune_id: str | None = None) -> dict:
        from winpty import PTY

        cols, rows = _size(cols, rows)
        with self._cond:
            if self._alive():
                raise ConsoleError("이미 열린 콘솔이 있습니다. 닫은 뒤 다시 여세요.")
            app, cmdline = clients.console_command(client, rune_id)
            # 고치는 룬이면 그 룬 폴더에서 연다 — AI 가 고치는 곳과 검사·설치하는 곳이 같아야 한다 (codex1 지적)
            home = workshop.package_dir(rune_id) if rune_id else workshop.root()
            home.mkdir(parents=True, exist_ok=True)
            pty = PTY(cols, rows)
            pty.spawn(app, cmdline=cmdline, cwd=str(home))
            self._pty, self._id, self._client, self._rune = pty, uuid.uuid4().hex, client, rune_id
            _sweep_pastes()
            self._chunks, self._size, self._ended = [], 0, False
            threading.Thread(target=self._pump, args=(pty, self._id), daemon=True,
                             name=f"console-{client}").start()
            return {"session": self._id, "client": client, "alive": True}

    def read(self, session: str, timeout: float = 0.25) -> dict:
        """화면이 다 그린 뒤 부른다. 새 출력이 생기거나 `timeout` 이 지나면 돌아온다."""
        with self._cond:
            self._check(session)
            if not self._chunks and not self._ended:
                self._cond.wait(min(float(timeout), 1.0))
            if not self._chunks and not self._ended and not _alive(self._pty):
                # ConPTY 는 프로그램이 끝나도 출력 통로를 열어 둔다 — 읽기 스레드가 알아채지 못하니 여기서 끝낸다.
                self._end()
            data, self._chunks, self._size = "".join(self._chunks), [], 0
            self._cond.notify_all()   # 버퍼가 비었다 — 멈춰 있던 읽기를 깨운다
            return {"data": data, "alive": not self._ended}

    def write(self, session: str, data: str) -> bool:
        if not isinstance(data, str) or len(data) > MAX_INPUT:
            raise ConsoleError("입력이 너무 깁니다.")
        with self._cond:
            self._check(session)
            if not self._ended:
                self._pty.write(data)
        return True

    def save_image(self, session: str, data: str) -> str:
        """붙여넣은 이미지(PNG, base64)를 콘솔 전용 폴더에 두고 절대 경로를 돌려준다 — AI 는 경로로 이미지를 읽는다.

        편집 중인 룬 폴더 밖이다 — 연결한 원본(git 저장소)에 스크린샷이 섞이지 않게 (codex1 지적).
        """
        raw = b""
        if isinstance(data, str) and len(data) <= MAX_IMAGE * 4 // 3 + 4:
            try:
                raw = base64.b64decode(data, validate=True)
            except ValueError:
                pass
        if not raw.startswith(PNG_MAGIC):
            raise ConsoleError("붙여넣을 수 있는 이미지가 아닙니다 (PNG, 20MB 이하).")
        with self._cond:
            self._check(session)
        folder = _paste_root() / session
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{uuid.uuid4().hex[:12]}.png"
        path.write_bytes(raw)
        return str(path)

    def resize(self, session: str, cols: int, rows: int) -> bool:
        cols, rows = _size(cols, rows)
        with self._cond:
            self._check(session)
            if not self._ended:
                self._pty.set_size(cols, rows)
        return True

    def stop(self, session: str | None = None, grace: float = 3.0) -> bool:
        """Ctrl+C 두 번으로 정상 종료를 청하고, 안 끝나면 그 프로세스 트리만 끈다.
        돌려주는 값: 콘솔 프로그램이 **정말** 끝났는가 — OS 프로세스와 taskkill 결과로 본다(화면의 '끝남' 표시가 아니라).
        켜진 것이 없으면 True. 업데이트를 걸지 정할 때 쓴다 (codex1 Rust r3 B)."""
        with self._cond:
            if session is not None:
                self._check(session)
            pty = self._pty
        if pty is None:
            return True
        try:
            for _ in range(2):
                pty.write("\x03")
                time.sleep(0.4)
        except Exception:
            pass
        deadline = time.time() + grace
        while time.time() < deadline and _alive(pty):
            time.sleep(0.1)
        killed = True
        if _state(pty) is not False:   # 살아 있거나 모르면 — 끈다 (모름을 '끝남' 으로 치지 않는다, codex1 Rust r4 P2-1)
            # 이름이 아니라 이 PID 의 트리만 — 같은 이름의 다른 프로그램을 끄지 않는다.
            try:
                done = subprocess.run(["taskkill", "/T", "/F", "/PID", str(pty.pid)], capture_output=True,
                                      creationflags=NO_WINDOW, timeout=10, stdin=subprocess.DEVNULL)
                killed = done.returncode == 0
            except (OSError, subprocess.SubprocessError):
                killed = False
            deadline = time.time() + 2
            while time.time() < deadline and _state(pty) is not False:
                time.sleep(0.1)
        gone = _state(pty) is False
        with self._cond:
            if self._pty is pty:
                self._end()
        # ponytail: 뿌리 프로세스 생존과 taskkill /T 결과로만 본다 — 트리 전체 종료의 증명은 아니다(손자는 세지 않는다).
        return gone and killed

    # ── 안쪽 ──

    def _pump(self, pty, session):
        while True:
            try:
                data = pty.read(blocking=True)
            except Exception:
                data = None
            with self._cond:
                if self._id != session:
                    return
                if not data:
                    if not _alive(pty):
                        self._ended = True
                        self._cond.notify_all()
                        return
                    continue
                while self._size > MAX_BUFFER and self._id == session:
                    self._cond.wait(0.5)   # 화면이 따라올 때까지 프로그램을 기다리게 한다
                self._chunks.append(data)
                self._size += len(data)
                self._cond.notify_all()

    def _end(self):
        """끝났다고 표시하고, 막혀 있는 읽기 스레드를 풀어 준다. 잠금 안에서만 부른다."""
        self._ended = True
        try:
            self._pty.cancel_io()
        except Exception:
            pass
        self._cond.notify_all()

    def _alive(self) -> bool:
        return self._pty is not None and not self._ended and _alive(self._pty)

    def _check(self, session):
        if not session or session != self._id:
            raise ConsoleError("콘솔 세션이 바뀌었습니다. 다시 열어 주세요.")


def _paste_root():
    return config.data_dir() / "console-paste"


def _sweep_pastes() -> None:
    """앱이 만든 붙여넣기 이미지 중 오래된 세션 폴더만 지운다. 사람이 첨부한 원본 파일은 건드리지 않는다."""
    cutoff = time.time() - KEEP_DAYS * 86400
    for folder in _paste_root().glob("*") if _paste_root().is_dir() else []:
        if folder.is_dir() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)


def _state(pty) -> bool | None:
    """안전 종료 판정용 — 살아 있음 True, 끝남 False, 확인 못 함 None."""
    try:
        return bool(pty.isalive())
    except Exception:
        return None


def _alive(pty) -> bool:
    try:
        return bool(pty.isalive())
    except Exception:
        return False


def _size(cols, rows) -> tuple[int, int]:
    """숨긴 화면의 0 크기 같은 값은 받지 않는다."""
    if type(cols) is not int or type(rows) is not int or not (10 <= cols <= 500 and 4 <= rows <= 300):
        raise ConsoleError("콘솔 크기가 올바르지 않습니다.")
    return cols, rows
