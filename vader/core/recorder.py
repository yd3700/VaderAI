"""시연을 기록한다. **말로 안 나오는 것을 손이 알려 준다.**

마스터 제안 (2026-08-11):

> 내가 녹화 버튼을 눌러 가이드 하면 너가 그 정보를 자동화하고, 사용자가
> 최종 확인해서 그걸 룬화 하는건 어때?

이온엠 메뉴를 세 번 고쳤고 세 번 다 빗나갔다. 원인은 하나다 — **화면을 못
보면서 셀렉터를 썼다.** 시연에는 그 정보가 그냥 들어 있다.

계획서가 "말로 요청" 이라고 쓴 자리다. 마스터 말이 더 정확하다 — 창 제목·
입력 순서·팝업은 **말로 안 나오고 시연에는 있다.** Phase 12 가 "남는 80%"
라고 부른 것이 정확히 그것이다.

## 클릭과 **이동 키**를 기록한다. 글자는 안 한다

마스터 요청 (2026-08-12):

> 방향키 입력을 별도로 설정에 빼놓으면 녹화의 효율이 떨어지면서 적당한 곳에
> 적용할 수 없어. 그러니 방향키 입력 또한 녹화 기능에 추가해서 클릭을
> 녹화하든 **키로그도 같이 녹화**를 하자.

맞다. 키 순서를 설정에 따로 빼면 **시연의 어느 지점에 끼는지**를 표현할
방법이 없다. 메뉴는 클릭으로 열고 항목은 방향키로 고르는 식이 흔하다.

**그래도 글자는 안 담는다.** 담는 순간 키로거이고, 이 기계에서 마스터는
병원 ERP 비밀번호를 친다.

```
담는다    ↑ ↓ ← → · Tab · Enter · Esc · Alt · F1~F12 · Home/End/PgUp/PgDn
안 담는다  글자 · 숫자 · 그 밖의 모든 것
```

**비밀번호는 글자다.** 이 선은 목록으로 지켜지고 테스트가 목록에 글자 키가
들어오는 것을 막는다. 값이 필요한 자리는 지금도 통로가 따로 있다 —
문서에서 뽑거나(OCR) 설정에서 받는다(`RuneField`).

## 무엇을 적어야 다음에도 찾나

좌표로 적으면 창 크기가 바뀌는 순간 깨진다. 그래서 **컨트롤의 정체**를
적는다 — `automation_id` · 이름 · 종류. 좌표는 그 셋으로도 못 찾을 때의
마지막 수단으로만 남긴다. 그것도 픽셀이 아니라 **창 대비 비율**이다.
"""
from __future__ import annotations

import ctypes
import json
import os
import tempfile
import threading
import time
from ctypes.wintypes import POINT, RECT
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

import re

from vader.core import config

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

__all__ = ["Step", "Recorder", "save", "load", "path_for", "recordings", "PURPOSES"]

#: 녹화가 **실제로 쓰이는 이름**과 그 뜻.
#:
#: 마스터가 `eonm_guide` 로 저장했는데 룬은 `navigation` 만 재생한다. 그래서
#: **조용히 아무 일도 안 일어났다** (2026-08-11). 이름을 자유 입력으로 받으면
#: 사용자는 저장했다고 믿고 우리는 안 쓴다.
#:
#: 새 자리가 생기면 여기 한 줄을 더한다. 그 한 줄이 "이 녹화를 누가 쓰나" 를
#: 묻는 자리가 된다.
PURPOSES = [
    ("navigation", "로그인 뒤 입고 화면까지 (eonm_ipgo)"),
]

#: 마우스 왼쪽 버튼.
_VK_LBUTTON = 0x01

#: 녹화하는 **이동 키**. 가상 키 코드 → `send_keys` 표기.
#:
#: **여기에 글자·숫자 키를 넣으면 키로거가 된다.** 0x30~0x5A(0-9, A-Z)는
#: 한 개도 들어오면 안 되고, `tests/test_recorder.py` 가 그걸 막는다.
NAV_KEYS = {
    0x09: "{TAB}", 0x0D: "{ENTER}", 0x1B: "{ESC}",
    0x25: "{LEFT}", 0x26: "{UP}", 0x27: "{RIGHT}", 0x28: "{DOWN}",
    0x21: "{PGUP}", 0x22: "{PGDN}", 0x23: "{END}", 0x24: "{HOME}",
    0x12: "%",  # Alt — 메뉴 막대로 들어가는 열쇠
    **{0x70 + n: "{F%d}" % (n + 1) for n in range(12)},
}

#: 버튼 상태를 얼마나 자주 볼까(초). 후킹 대신 폴링을 쓴다 — 전역 훅은
#: 메시지 루프가 필요하고, 그 루프가 tkinter 와 같은 스레드에서 엉킨다.
#
# ponytail: 아주 빠른 연타는 놓칠 수 있다. 시연은 사람 속도라 문제가 안 된다.
POLL_SECONDS = 0.02

#: 같은 자리를 두 번 잡지 않기 위한 최소 간격(초).
MIN_GAP_SECONDS = 0.25

#: 창을 앞으로 가져온 뒤 누르기 전에 기다리는 시간. 활성화가 끝나기 전에 누르면 같은 일이 난다.
#: 기계마다 다를 수 있는 값이라 상수로 둔다.
FOCUS_SETTLE_SECONDS = 0.2


@dataclass(frozen=True)
class Step:
    """시연에서 누른 것 하나."""

    at: str
    #: 눌린 창의 제목. 어느 화면에서 한 일인지 사람이 알아보는 열쇠다.
    window_title: str = ""
    control_type: str = ""
    name: str = ""
    automation_id: str = ""
    class_name: str = ""
    #: 이 단계가 **키 입력**이면 `send_keys` 표기가 들어간다 (`{DOWN 3}`).
    #: 비어 있으면 클릭이다. **글자는 절대 안 들어온다** — `NAV_KEYS` 참조.
    keys: str = ""
    #: 위로 못 찾을 때의 마지막 수단. **픽셀이 아니라 창 대비 비율이다** —
    #: 창 크기가 바뀌어도 대략 같은 자리를 가리킨다.
    rel_x: float = 0.0
    rel_y: float = 0.0
    #: **컨트롤 안에서 어디를 눌렀나** (비율). 기본은 한가운데.
    #:
    #: 콤보는 `[1002.구매과   |▼]` 모양이라 **▼ 를 눌러야 목록이 열린다.**
    #: 정체로 찾은 다음 가운데를 누르면 글자칸이 눌리고 아무 일도 안 난다 —
    #: 마스터가 화면으로 잡아 준 그 버그다 (2026-08-12).
    #:
    #: 창이 움직이거나 커져도 **컨트롤 안 비율은 같다.** 옛 녹화에는 이 값이
    #: 없어 0.5(가운데)로 읽히고, 그건 전과 같은 동작이다.
    ctrl_x: float = 0.5
    ctrl_y: float = 0.5

    def label(self) -> str:
        """사람이 확인 화면에서 읽을 한 줄. **여기서 사람이 거른다.**"""
        where = self.window_title or "(창 모름)"
        if self.keys:
            return f"키 {self.keys}  ·  {where}"
        what = self.name or self.automation_id or "(이름 없음)"
        return f"{what}  ·  {self.control_type or '?'}  ·  {where}"

    def findable(self) -> bool:
        """다음에도 찾을 수 있는 단서가 있나.

        이름도 `automation_id` 도 없으면 남는 것은 좌표뿐이고, 그건 창 크기가
        바뀌면 깨진다. **확인 화면이 이걸 사용자에게 알려 준다.**

        **키는 늘 참이다.** 좌표도 UIA 도 안 쓰므로 창이 움직여도 같다.
        """
        return bool(self.keys or self.name or self.automation_id)


#: Rust 껍데기(창·녹화 배지)의 프로세스 — 일꾼이 시작할 때 부모 PID 로 넣는다(웹 요청이 정하지 못한다).
#: 껍데기 창도 '우리 창' 이라 녹화에 담지 않는다 (Rust 전환 2단계, codex1 P2-2).
SHELL_PID = 0


class Recorder:
    """왼쪽 클릭을 모은다. **우리 창은 안 담는다.**

    녹화 창의 `중지` 버튼까지 기록되면 재생할 때 그것도 누른다.
    """

    def __init__(self, *, probe=None, skip_pid: int | None = None) -> None:
        #: 좌표를 받아 `Step` 을 만드는 함수. 테스트가 갈아끼운다 —
        #: 마우스 없이 이 클래스 전체를 검증할 수 있어야 한다.
        self._probe = probe or probe_at
        self._skip_pid = os.getpid() if skip_pid is None else skip_pid
        self.steps: list[Step] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        """멈춘다. **정말 멈췄을 때만** 참 — 늦은 UIA 조회가 멈춘 뒤 단계를 붙이지 않게 (codex1 지적 2026-10-27)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                return False
        self._thread = None
        return True

    def capture(self, x: int, y: int) -> Step | None:
        """한 지점을 기록한다. 폴링 루프와 테스트가 같이 쓴다."""
        step = self._probe(x, y, skip_pid=self._skip_pid)
        if step is None or self._stop.is_set():
            return None
        if self.steps and _too_close(self.steps[-1], step):
            return None
        self.steps.append(step)
        return step

    def capture_key(self, token: str) -> Step | None:
        """이동 키 하나를 기록한다. **같은 키가 이어지면 횟수로 접는다.**

        `↓` 를 여덟 번 누른 것은 `{DOWN 8}` 한 줄이어야 한다. 여덟 줄이면
        확인 화면을 사람이 못 읽는다.
        """
        foreground = ctypes.windll.user32.GetForegroundWindow()
        if self._stop.is_set() or (self._skip_pid and _pid_of(foreground) in {self._skip_pid, SHELL_PID} - {0}):
            return None   # 우리 창에서 누른 키 — 녹화 끝내기를 Enter 로 눌러도 담지 않는다
        if self.steps and self.steps[-1].keys:
            last = self.steps[-1]
            base, count = _split_count(last.keys)
            if base == token:
                self.steps[-1] = replace(last, keys=_join_count(base, count + 1))
                return self.steps[-1]
        step = Step(
            at=datetime.now().isoformat(timespec="seconds"),
            window_title=_title_of(foreground),
            keys=token,
        )
        self.steps.append(step)
        return step

    def _loop(self) -> None:
        pressed = False
        keys_down: set = set()
        last = 0.0
        while not self._stop.is_set():
            down = bool(ctypes.windll.user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000)
            now = time.monotonic()
            if down and not pressed and now - last > MIN_GAP_SECONDS:
                last = now
                point = POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
                try:
                    self.capture(point.x, point.y)
                except Exception:
                    # 시연 중에 예외로 멈추면 사용자는 녹화가 도는 줄 안다.
                    pass
            pressed = down

            # **이동 키만 본다.** 글자 키는 이 목록에 없다 — 비밀번호를
            # 담지 않는 것이 그 하나로 지켜진다.
            for code, token in NAV_KEYS.items():
                held = bool(ctypes.windll.user32.GetAsyncKeyState(code) & 0x8000)
                if held and code not in keys_down:
                    keys_down.add(code)
                    try:
                        self.capture_key(token)
                    except Exception:
                        pass
                elif not held:
                    keys_down.discard(code)
            time.sleep(POLL_SECONDS)


def probe_at(x: int, y: int, *, skip_pid: int = 0) -> Step | None:
    """그 자리에 있는 컨트롤의 정체를 읽는다. **읽기만 한다.**

    고전 메뉴(`#32768`)도 UIA 는 `MenuItem` 으로 보여 준다. 이온엠 메뉴가
    안 눌렸던 자리가 여기서 정직하게 드러난다.
    """
    from pywinauto.uia_defines import IUIA
    from pywinauto.uia_element_info import UIAElementInfo

    point = POINT(int(x), int(y))
    hwnd = ctypes.windll.user32.WindowFromPoint(point)
    root = ctypes.windll.user32.GetAncestor(hwnd, 2)  # GA_ROOT
    # 우리 창이다. 녹화 창의 버튼까지 담으면 재생할 때 그것도 누른다.
    # 화면 안쪽(WebView2)은 다른 프로세스의 자식 창이라 바깥 창의 프로세스도 본다.
    if skip_pid and {skip_pid, SHELL_PID} & {_pid_of(hwnd), _pid_of(root)} - {0}:
        return None

    info = UIAElementInfo(IUIA().iuia.ElementFromPoint(point))
    return Step(
        at=datetime.now().isoformat(timespec="seconds"),
        window_title=_title_of(root),
        control_type=str(info.control_type or ""),
        name=str(info.name or ""),
        automation_id=str(info.automation_id or ""),
        class_name=str(info.class_name or ""),
        **_relative(root, x, y),
        **_inside(info, x, y),
    )


def _inside(info, x: int, y: int) -> dict:
    """컨트롤 **안에서** 어디를 눌렀나. 못 재면 한가운데.

    콤보의 ▼ 처럼 **컨트롤의 어느 부분이냐가 뜻을 바꾸는** 자리가 있다.
    """
    try:
        box = info.rectangle
        width = max(1, box.right - box.left)
        height = max(1, box.bottom - box.top)
    except Exception:
        return {}
    return {
        "ctrl_x": round(min(1.0, max(0.0, (x - box.left) / width)), 4),
        "ctrl_y": round(min(1.0, max(0.0, (y - box.top) / height)), 4),
    }


def _relative(hwnd: int, x: int, y: int) -> dict:
    rect = RECT()
    if not hwnd or not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return {"rel_x": 0.0, "rel_y": 0.0}
    width = max(1, rect.right - rect.left)
    height = max(1, rect.bottom - rect.top)
    return {
        "rel_x": round((x - rect.left) / width, 4),
        "rel_y": round((y - rect.top) / height, 4),
    }


def _pid_of(hwnd: int) -> int:
    pid = ctypes.c_ulong(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _title_of(hwnd: int) -> str:
    if not hwnd:
        return ""
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _split_count(token: str) -> tuple[str, int]:
    """`{DOWN 3}` → `({DOWN}, 3)`. 횟수가 없으면 1."""
    inner = token[1:-1] if token.startswith("{") and token.endswith("}") else token
    head, _, tail = inner.rpartition(" ")
    if head and tail.isdigit():
        return "{" + head + "}", int(tail)
    return token, 1


def _join_count(base: str, count: int) -> str:
    if count <= 1:
        return base
    inner = base[1:-1] if base.startswith("{") else base
    return "{%s %d}" % (inner, count)


def _too_close(previous: Step, step: Step) -> bool:
    """같은 컨트롤을 연달아 잡은 것인가. 한 번의 클릭이 두 줄이 되면 안 된다."""
    return (
        previous.automation_id == step.automation_id
        and previous.name == step.name
        and previous.window_title == step.window_title
        and abs(previous.rel_x - step.rel_x) < 0.002
        and abs(previous.rel_y - step.rel_y) < 0.002
    )


# ── 저장 ────────────────────────────────────────────────────────

def path_for(name: str) -> Path:
    return config.data_dir() / "recordings" / f"{name}.json"


def recordings() -> list[str]:
    try:
        return sorted(p.stem for p in (config.data_dir() / "recordings").glob("*.json"))
    except OSError:
        return []


def save(name: str, steps: list[Step], target: Path | None = None) -> Path:
    """사람이 확인한 것만 저장한다. **확인 없이 저장하는 길은 없다.** `target` 은 룬 안 녹화 파일."""
    target = Path(target) if target is not None else path_for(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent,
        prefix=f".{name}.", suffix=".tmp", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        json.dump(
            {"format": "vader-recording/1", "steps": [asdict(s) for s in steps]},
            stream, ensure_ascii=False, indent=2,
        )
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    return target


def load(name: str, rune: Path | None = None) -> list[Step]:
    """**읽기 실패는 빈 목록이다.** 녹화를 못 읽는 것이 실행을 막지 않는다.

    `rune` 을 주면 그 룬 안 `recordings/<name>.json` 을 읽는다 (2026-10-15 — 녹화는 룬의 것).
    그때는 **엄격하다**: 이름이 경로를 벗어나거나, 한 줄이라도 못 읽으면 통째로 빈 목록 — 일부만 누르지 않는다.
    """
    if rune is not None:
        if not _NAME.fullmatch(name or ""):
            return []
        path = Path(rune) / "recordings" / f"{name}.json"
    else:
        path = path_for(name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    out = []
    for row in data.get("steps", []) if isinstance(data, dict) else []:
        try:
            out.append(Step(**{k: v for k, v in row.items() if k in Step.__annotations__}))
        except (TypeError, AttributeError):
            if rune is not None:
                return []
            continue
    return out


# ── 재생 ────────────────────────────────────────────────────────

def replay(step: Step, window, log=None) -> bool:
    """한 단계를 다시 누른다. **정체로 먼저 찾고, 좌표는 마지막이다.**

    좌표부터 쓰면 창 크기가 바뀐 날 조용히 엉뚱한 곳을 누른다. 그래서
    `automation_id` → 이름+종류 → 이름 → 좌표 순으로 내려간다. 어느 것으로
    찾았는지 남긴다 — 좌표까지 내려갔다면 그건 다음에 깨질 신호다.
    """
    log = log or (lambda level, message, **fields: None)
    _bring_forward(window)
    if step.keys:
        # **좌표도 UIA 도 안 쓴다.** 창이 움직여도 같다 (마스터 제안).
        from pywinauto.keyboard import send_keys

        try:
            send_keys(step.keys, pause=0.08)
        except Exception as exc:
            log("error", "키를 보내지 못했습니다", what=step.label(), error=str(exc))
            return False
        log("info", "키를 보냈습니다", what=step.label())
        return True

    attempts = []
    if step.automation_id:
        attempts.append(("automation_id", {"auto_id": step.automation_id}))
    if step.name and step.control_type:
        attempts.append(
            ("name+type", {"title": step.name, "control_type": step.control_type})
        )
    if step.name:
        attempts.append(("name", {"title": step.name}))

    time.sleep(FOCUS_SETTLE_SECONDS)
    problems = []
    for how, kwargs in attempts:
        try:
            _click_inside(window.child_window(**kwargs), step)
            log("info", "단계를 재생했습니다", how=how, what=step.label())
            return True
        except Exception as exc:
            # **왜 못 찾았는지 남긴다.** 조용히 좌표로 떨어지면 원인을 못 본다 —
            # 마스터 로그에 "좌표로 눌렀습니다" 만 있고 이유가 없었다.
            problems.append(f"{how}: {type(exc).__name__} {exc}")

    if step.rel_x or step.rel_y:
        try:
            _click_relative(window, step)
            log(
                "warning",
                "이름으로 못 찾아 좌표로 눌렀습니다. **창 크기가 바뀌면 "
                "깨집니다** — 다시 녹화하세요",
                what=step.label(),
                tried=problems,
            )
            return True
        except Exception as exc:
            log("error", "좌표로도 못 눌렀습니다", what=step.label(), error=str(exc))
            return False

    log("error", "단계를 재생하지 못했습니다", what=step.label())
    return False


def _bring_forward(window) -> None:
    """누르기 전에 그 창을 앞으로 — **그 프로그램이 이미 앞에 있으면 건드리지 않는다.**

    뒤에 있는 창의 첫 클릭은 창을 앞으로 가져오는 데만 쓰이고 메뉴가 안 열린다 —
    이어서 쓴 이온엠에서 '재고관리' 에 마우스만 가고 메뉴가 안 열렸다 (마스터 2026-10-12).

    그런데 부서 콤보를 누르면 뜨는 목록(`Form6`)은 **앞으로 가져오는 순간 닫힌다.** 그래서
    모든 단계에 앞으로 가져오기를 했더니 '7001.공급실' 을 못 눌러 녹화 끝이 빠졌다
    (2026-10-12 오후부터, 마스터 2026-10-15). 앞 창이 같은 프로그램이면 이미 앞에 있는 것이다.
    """
    try:
        handle = window.handle
    except Exception:
        handle = None   # 못 알아보면 전처럼 앞으로 가져온다
    try:
        if handle and _pid_of(ctypes.windll.user32.GetForegroundWindow()) == _pid_of(handle):
            return
        window.set_focus()
    except Exception:
        pass


def _click_inside(control, step: Step) -> None:
    """컨트롤을 찾은 뒤 **녹화된 자리**를 누른다.

    가운데를 누르면 콤보의 글자칸이 눌리고 목록이 안 열린다. 정체로 찾는
    것과 **어디를 누르는지**는 다른 문제다 (마스터 2026-08-12).
    """
    if step.ctrl_x == 0.5 and step.ctrl_y == 0.5:
        # 옛 녹화이거나 정말 한가운데다. 전과 같이 동작한다.
        control.click_input()
        return
    box = control.rectangle()
    width = max(1, box.right - box.left)
    height = max(1, box.bottom - box.top)
    control.click_input(
        coords=(int(step.ctrl_x * width), int(step.ctrl_y * height))
    )


def _click_relative(window, step: Step) -> None:
    rect = window.rectangle()
    width = max(1, rect.right - rect.left)
    height = max(1, rect.bottom - rect.top)
    window.click_input(
        coords=(int(step.rel_x * width), int(step.rel_y * height))
    )
