"""
룬에 주입하는 실제 RuneContext.

이 객체는 **워커 스레드에서만** 호출된다. 룬이 부르는 모든 메서드가 여기 있고,
UI 객체는 하나도 들어 있지 않다 — 룬이 위젯에 닿을 수 있으면 스레드 규칙이
룬 작성자의 책임이 되고, 그건 지켜지지 않는다.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from queue import Queue
from typing import Any, Protocol, runtime_checkable

from vader.core import config, journal
from vader.core.events import JobEvent, LogLine, Progress, StateChanged, UserActionRequested
from vader.core.job import Job, JobState
from vader.sdk import (
    CheckpointKey,
    Input,
    RuneCancelled,
    UserActionRequest,
    UserActionResponse,
)


@runtime_checkable
class CheckpointStore(Protocol):
    """체크포인트 저장소의 형태. 구현은 vader/core/checkpoint.py 가 갖는다.

    Protocol 로 받는 것은 러너가 저장소 구현을 몰라도 되게 하려는 것이고,
    테스트가 디스크 없이 돌 수 있게 하려는 것이기도 하다.

    `runtime_checkable` 인 이유: 시그니처를 넓혔을 때 테스트 대역이 따라오지
    않는 일이 세 번 있었다 (`_FakeContext` 의 `dry_run`, `GeminiProvider` 의
    `complete`, 그리고 `rune_version` 을 더한 이번). 준수 테스트가 있으면 대역이
    뒤처지는 것이 즉시 걸린다. Protocol 은 메서드 유무만 보고 인자는 못 보지만,
    대역을 잊는 것이 대부분이라 그것만으로도 값이 있다.
    """

    def load(self, key: CheckpointKey, *, rune_version: str = "") -> dict | None: ...
    def save(self, key: CheckpointKey, data: dict, *, rune_version: str = "") -> None: ...
    def clear(self, key: CheckpointKey) -> None: ...


def stable_part(title: str) -> str:
    """제목에서 **다시 켜도 안 변하는 조각**을 고른다.

    녹화한 제목에는 기동 시각이 들어 있다.

        "이영돈 []   Version=1.0.0.6 - … Version = 20260211 오전 11:15:31"

    이걸 통째로 찾으면 이온엠을 다시 켠 순간 **영원히 못 찾는다.** 실제로
    그래서 마스터의 녹화가 적용이 안 됐다 (2026-08-11).

    변하는 부분에서 자르고 **가장 긴 조각**을 쓴다. `view.coerce_field` 가
    창 제목 설정에서 같은 것을 막고 있었는데, 녹화 쪽에는 그 검사가 없었다.
    """
    import re

    from vader.core.view import _VOLATILE_TITLE

    # `오전`·`오후` 도 자른다. 아침에 녹화하고 오후에 돌리면 그 한 낱말이
    # 달라진다 — 시각을 잘라 놓고 이걸 남기면 반나절짜리 폭탄이 된다.
    #
    # `[…]` 안도 자른다. **거기 지금 열린 화면 이름이 들어간다** —
    # 녹화할 때 `이영돈 []`, 실행할 때 `이영돈 [고정자산]`. 그래서 녹화한
    # 창을 영영 못 찾았다 (마스터 로그 2026-08-12).
    #
    # 붙여 쓴 날짜 `20260211` 도 자른다. 이온엠 빌드마다 바뀐다 — 마스터 PC 는
    # `20260211`, 시험 PC 는 `20260306` 이라 녹화가 한 단계도 안 눌렸다 (2026-10-28).
    # 기관코드 `(10000001)` 는 20 으로 시작하지 않아 남는다.
    pattern = re.compile(_VOLATILE_TITLE.pattern + r"|(?<!\d)20\d{6}(?!\d)|오전|오후|\[[^\]]*\]")
    chunks = [chunk.strip() for chunk in pattern.split(str(title))]
    return max(chunks, key=len, default="") if chunks else ""


#: `_window_titled` 가 **같은 제목의 창이 여럿** 일 때 돌려준다. `None`(없음) 과 다르다 —
#: 없는 창은 "아직 안 뜬 준비 동작" 으로 넘길 수 있지만, 여럿은 넘기면 안 된다.
#: 넘겼더니 메뉴·부서 선택이 통째로 조용히 빠졌다 (2026-10-12, 연습 뒤 이온엠이 둘).
AMBIGUOUS_WINDOW = object()


def _top_windows() -> list[tuple[int, str]]:
    """보이고 최소화되지 않은 최상위 창의 (핸들, 제목).

    **UIA 가 아니라 Win32 로 본다** (2026-11-07). 설치본 앱 안 작업 스레드에서 `Desktop(backend="uia")
    .windows()` 는 이온엠 창을 못 봤다 — 같은 코드가 앱 밖(개발 파이썬·설치본 파이썬)에서는 찾았고,
    같은 실행의 Win32 창 목록(`driver.find_windows`)은 그 창을 보여 줬다. 녹화 재생이 8단계를
    1초 만에 '창 없음' 으로 건너뛴 이유다. UIA 는 찾은 창에 붙을 때만 쓴다(드라이버가 앱 안에서 쓰는 길).
    """
    import ctypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    def visit(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length and user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            found.append((int(hwnd), buf.value))
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(visit), 0)
    return found


def _window_titled(title: str):
    """제목이 맞는 창에 붙는다. 없으면 `None`, 여럿이면 `AMBIGUOUS_WINDOW`.

    `pywinauto` 를 여기서 import 한다 — 코어가 그것 없이도 부팅해야 한다.
    RPA 룬을 안 쓰는 사용자에게 설치를 강요하지 않는다. 붙지 못하면 예외 그대로 — 부르는 쪽이 오류로 남긴다.
    """
    needle = stable_part(title)
    if not needle:
        return None
    # 최소화된 창은 후보가 아니다 — 키가 안 들어간다.
    found = [hwnd for hwnd, text in _top_windows() if needle in text]
    # **여럿이면 안 고른다** (codex). 제목 부분 일치라 동명 창이 있으면
    # 엉뚱한 세션에 입력한다 — 이 프로젝트가 이미 두 번 만난 실패다.
    if len(found) > 1:
        return AMBIGUOUS_WINDOW
    if not found:
        return None
    try:
        from pywinauto.application import Application
    except ImportError:
        return None
    # 핸들로 열어 **`WindowSpecification`** 을 받는다 — `child_window` 는 그것에만 있다
    # (UIAWrapper 를 넘겼더니 이름 찾기가 매번 실패하고 좌표로 떨어졌다, 마스터 로그 2026-08-12).
    return Application(backend="uia").connect(handle=found[0], timeout=5).window(handle=found[0])


class _PendingAsk:
    """사람에게 물어놓고 답을 기다리는 한 건.

    응답과 취소 둘 중 먼저 오는 것이 워커를 깨운다. 취소만 있고 응답이 영영
    안 오는 경우에 워커가 매달려 있으면 앱이 종료되지 않는다.
    """

    def __init__(self) -> None:
        self.done = threading.Event()
        self.response: UserActionResponse | None = None


class JobContext:
    """RuneContext 구현. Protocol 을 상속하지 않는다 — 구조적 타이핑으로 충분하다."""

    def __init__(
        self,
        job: Job,
        events: Queue[JobEvent],
        cancel: threading.Event,
        params: dict[str, Any],
        checkpoints: CheckpointStore,
        checkpoint_key: CheckpointKey,
        rune_version: str = "",
        dry_run: bool = False,
        datadir: Path | None = None,
    ) -> None:
        self._job = job
        self._events = events
        self._cancel = cancel
        self._params = params
        self._checkpoints = checkpoints
        self._key = checkpoint_key
        self._rune_version = rune_version
        self._dry_run = dry_run
        # 룬 전용 영속 폴더. 러너가 만들어 넘긴다.
        self._datadir = datadir if datadir is not None else job.workdir
        #: 역할마다 한 번만 고른다. **Job 이 도는 동안 안 바뀐다** — 자동
        #: 배정이 도중에 흔들리면 한 작업의 앞뒤가 다른 모델에서 나온다.
        self._assignments: dict[str, Any] = {}
        self._asks: dict[str, _PendingAsk] = {}
        self._asks_lock = threading.Lock()

    # ── 룬이 쓰는 것 ────────────────────────────────────────────

    @property
    def workdir(self) -> Path:
        return self._job.workdir

    @property
    def params(self) -> dict[str, Any]:
        return self._params

    @property
    def datadir(self) -> Path:
        return self._datadir

    @property
    def dry_run(self) -> bool:
        """참이면 룬이 외부 시스템의 쓰기 동작을 건너뛰어야 한다."""
        return self._dry_run

    def log(self, level: str, message: str, **fields: Any) -> None:
        # 화면과 디스크 둘 다에 남긴다. 화면 로그는 500줄 상한이고 앱을 닫으면
        # 사라진다 — 어제 실행을 오늘 설명하려면 디스크에 있어야 한다.
        journal.record(
            journal.Entry(
                at=journal.now(),
                job_id=self._job.id,
                rune_id=self._job.rune_id,
                level=level,
                message=message,
                fields=fields,
            )
        )
        self._events.put(LogLine(self._job.id, level, message, fields))

    def report_progress(self, current: int, total: int | None, message: str = "") -> None:
        self._events.put(Progress(self._job.id, current, total, message))

    def request_user_action(self, request: UserActionRequest) -> UserActionResponse:
        """사람에게 묻고 답을 기다린다. 워커 스레드를 블록한다.

        비동기로 바꾸면 룬의 복구 로직이 전부 콜백으로 뒤집힌다. 블로킹
        의미론은 계약의 일부다(contract.py 의 독스트링 참조).
        """
        # 물어보기 전에 이미 취소됐으면 묻지 않는다. 사용자가 취소를 눌렀는데
        # 새 질문이 뜨는 것만큼 헷갈리는 것이 없다.
        self.check_cancelled()

        correlation_id = uuid.uuid4().hex
        pending = _PendingAsk()
        with self._asks_lock:
            self._asks[correlation_id] = pending

        try:
            # 상태를 먼저 바꾸고 이벤트를 낸다. 반대로 하면 UI가 질문을 받고
            # 상태를 물었을 때 아직 RUNNING으로 보인다.
            if self._job.try_transition(JobState.WAITING_USER):
                self._events.put(StateChanged(self._job.id, self._job.state))
            self._events.put(UserActionRequested(self._job.id, correlation_id, request))
            while not pending.done.wait(timeout=0.1):
                if self._cancel.is_set():
                    raise RuneCancelled("사용자 응답을 기다리는 중 취소되었습니다")
            if pending.response is None:
                # 취소가 깨운 경우. wait 가 풀린 직후 취소 확인으로 다시 걸린다.
                raise RuneCancelled("사용자 응답 없이 해제되었습니다")
            return pending.response
        finally:
            with self._asks_lock:
                self._asks.pop(correlation_id, None)

    def check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise RuneCancelled("취소가 요청되었습니다")

    def load_checkpoint(self) -> dict | None:
        return self._checkpoints.load(self._key, rune_version=self._rune_version)

    def save_checkpoint(self, data: dict) -> None:
        # dry-run은 기록하지 않는다. 남기면 **다음 실제 실행이 하지도 않은
        # 작업을 건너뛴다** — EONM 이라면 입고가 통째로 빠진다. 룬의 메모리 상태는
        # 그대로 두므로 한 번의 dry-run 안에서 같은 버킷이 반복되지는 않는다.
        if self._dry_run:
            self.log("info", "DRY-RUN: 체크포인트를 저장하지 않습니다")
            return
        self._checkpoints.save(self._key, data, rune_version=self._rune_version)

    def clear_checkpoint(self) -> None:
        # 지우는 것도 막는다. 리허설이 진짜 재개 지점을 날리면 안 된다.
        if self._dry_run:
            self.log("info", "DRY-RUN: 체크포인트를 지우지 않습니다")
            return
        self._checkpoints.clear(self._key)

    def read_table(self, source: Input, sheet: str | None = None) -> list[list]:
        from vader.core.tables import TableError, read_table

        if source not in self._job.inputs:   # 이번 작업에 주어진 것만 — 경로를 지어내 읽지 못하게
            raise TableError("이번 작업의 입력만 읽을 수 있습니다")
        from vader.core.screen import own_table

        if not Path(source.path).exists():
            raise TableError("입력 파일이 없어요. 화면에서 읽은 표는 한 번 읽으면 지워지니 다시 복사한 뒤 실행해 주세요.")
        mine = own_table(source.path)
        rows = read_table(source.path, sheet)
        if mine:   # 화면에서 읽은 표는 룬이 읽으면 바로 지운다 (마스터 2026-11-18). 못 지우면 보관 시간 뒤 정리가 지운다
            try:
                Path(source.path).unlink()
            except OSError:
                pass
        return rows

    def write_table(self, name: str, rows: list[list]) -> str:
        from vader.core.tables import write_table

        return write_table(self._job.workdir, name, rows)

    def ocr(self, path: Path, prompt: str, schema: type) -> object:
        from vader.core.ocr import extract

        return extract(
            path, prompt, schema, assignment=self._assign("extract"), log=self.log
        )

    def infer(self, prompt: str, schema: type) -> object:
        from vader.core.ocr import infer

        return infer(prompt, schema, assignment=self._assign("infer"), log=self.log)

    def http_get(self, url: str, *, headers: dict | None = None):
        return self._http().get(url, headers=headers)

    def http_post(self, url: str, *, data=None, json=None, headers=None):
        return self._http().post(url, data=data, json=json, headers=headers)

    def replay(self, name: str, stop=None) -> bool:
        """녹화해 둔 시연을 다시 눌러 준다.

        **마스터 결정(2026-08-11) — 코어에 남긴다.** 룬이 녹화 파일을 직접
        읽으면 룬마다 저장 위치·형식·창 찾기를 다시 짜야 하고, 그 순간
        녹화는 룬 하나의 사정이 된다. 여기 두면 새 룬은 이름만 알면 된다.

        **어느 창인지는 녹화가 안다.** 녹화할 때 창 제목을 같이 적었다.

        연습 실행에서도 누른다. 녹화는 대개 **화면을 옮기는 일**(메뉴 열기)
        이라 업무 데이터를 안 쓴다. 쓰기가 있는 자리는 룬이 `dry_run` 을
        보고 스스로 건너뛴다 — `http_post` 와 달리 여기서는 무엇이 쓰기인지
        코어가 모른다.

        ## `stop` — 어디까지 재생하나

        **녹화는 부르는 쪽이 원하는 것보다 길다.** 마스터는 화면을 열고 나서도
        손을 멈추지 않는다. 2026-08-19 녹화가 그랬다 — 메뉴로 입고 화면을 연
        뒤 부서를 고르고, 품목창을 누르고, `{ENTER}` 와 `{TAB 6}` 까지 눌렀다.
        그 꼬리는 **룬이 자기 코드로 다시 하는 일**이라 통째로 재생하면 같은
        키가 두 번 들어간다. 두 번째 `{TAB 6}` 은 출발점을 옮기고, 그러면 날짜
        칸에 설 것이 단가 칸에 선다 — 마스터가 "로그인부터 시작하면 뒤로
        밀린다" 고 한 것이 이것이다.

        그래서 부르는 쪽이 **멈출 조건**을 준다. 누르기 **전에** 그 단계를
        보여 주고 물으며, 참이면 그 단계부터 안 누른다. 조건을 안 주면
        지금까지처럼 끝까지 간다.

        **자를 자리를 우리가 세지 않는다.** 몇 번째 단계인지는 다시 녹화하면
        바뀐다. 부르는 쪽은 자기가 아는 것 — 자기 셀렉터 — 으로 알아본다.
        그리고 **녹화 파일은 손대지 않는다.** 마스터가 누른 원본이다.

        ## 앞쪽은 반대로 관대하다

        **녹화는 앞으로도 길다.** 마스터는 로그인 창부터, 때로는 바탕화면부터
        녹화한다. 그런데 룬은 `login()` 으로 이미 로그인한 뒤에 이걸 부른다 —
        그 창은 사라진 뒤다.

        그래서 **아직 아무것도 안 눌렀고 그 창이 아예 안 떠 있으면** 지나간다.
        창이 없으면 키를 아무 데도 안 보냈다는 뜻이라 안전하다.

        **그것뿐이다.** 창을 찾았는데 못 누른 것은 시작 전이라도 실패다 —
        이미 눌러 본 것이고, 그걸 넘기면 메뉴가 반쯤 열린 채 다음 키가 나간다
        (codex 지적). 한 번 성공한 뒤로는 창을 못 찾는 것도 실패다.

        건너뛴 것은 **경고로 남긴다.** 조용히 지나가면 녹화가 통째로 헛돌아도
        아무도 모른다. 하나도 못 눌렀으면 실패다.

        그리고 `stop` 을 줬는데 **어느 단계도 해당하지 않으면 아예 안 누른다.**
        인계 지점이 없다는 것은 이 녹화가 어디서 끝나는지 모른다는 뜻이고,
        모르는 채로 끝까지 누르면 업무 키가 두 번 들어간다.
        """
        from vader.core import recorder

        # 룬 안에 녹화 폴더가 있으면 **거기서만** 읽는다 — 코드와 함께 설치·승인된 녹화다 (2026-10-15).
        # 없을 때만 옛 전역 녹화. 룬 안 파일이 깨졌을 때 옛 것으로 조용히 넘어가지 않는다 (codex1 지적).
        own = config.rune_dir() / self._job.rune_id
        steps = recorder.load(name, own) if (own / "recordings").is_dir() else recorder.load(name)
        if not steps:
            self.log("info", "녹화가 없습니다", name=name)
            return False

        if stop is not None and not any(stop(step) for step in steps):
            # **끝까지 재생하지 않는다.** 부르는 쪽이 "여기부터 내 일" 이라고
            # 표시한 자리를 못 찾았다는 것은, 이 녹화가 어디서 끝나는지 우리가
            # 모른다는 뜻이다. 모르는 채로 끝까지 누르면 업무 키가 두 번
            # 들어간다 — 그게 탭이 밀린 이유였다 (codex 지적 2026-08-19).
            self.log(
                "error",
                "녹화에서 인계 지점을 못 찾아 재생하지 않았습니다. "
                "그 화면까지 다시 녹화해 주세요",
                name=name,
                steps=len(steps),
            )
            return False

        self.log("info", "녹화를 재생합니다", name=name, steps=len(steps))
        # **창은 단계마다 다시 찾는다.** 메뉴를 누르면 새 창이 열리고, 그
        # 다음 단계는 거기 있다. 첫 창에 전부 넣으려 하면 뒤가 통째로 안
        # 눌린다 — 마스터 녹화가 정확히 그 모양이었다(본 창 → 드롭다운 →
        # 입고 화면 → 목록 창).
        window = None
        #: 실제로 눌린 단계 수. 0 이면 아직 시작도 안 한 것이다.
        played = 0
        skipped: list[str] = []
        for step in steps:
            # **누르기 전에 묻는다.** 부르는 쪽은 "이 단계는 내가 직접 한다"
            # 를 아는 유일한 쪽이다. 누른 뒤에 물으면 이미 늦다.
            if stop is not None and stop(step):
                self.log(
                    "info",
                    "여기서부터는 부르는 쪽이 직접 합니다. 재생을 멈춥니다",
                    name=name,
                    played=played,
                    of=len(steps),
                    next=step.label(),
                )
                # **누른 것이 하나도 없으면 성공이 아니다** — 아래로 내려가 그렇게 말한다.
                # 예전엔 여기서 바로 성공을 돌려줘, 룬이 열리지도 않은 화면을 기다렸다.
                break
            if step.window_title:
                try:
                    found = _window_titled(step.window_title)
                except Exception as exc:  # noqa: BLE001 — 창은 있는데 못 붙었다. '안 떠 있음' 과 다르다
                    self.log(
                        "error",
                        "녹화한 창에 붙지 못해 녹화를 재생하지 못했습니다",
                        name=name,
                        error=f"{type(exc).__name__}: {exc}",
                        played=played,
                    )
                    return False
                if found is AMBIGUOUS_WINDOW:
                    self.log(
                        "error",
                        "녹화한 창이 여럿 떠 있어 어느 창인지 고를 수 없습니다. "
                        "같은 프로그램 창을 하나만 남기고 다시 실행해 주세요",
                        name=name,
                        window=stable_part(step.window_title),
                        played=played,
                    )
                    return False
                if found is None:
                    if not played:
                        # **아직 아무것도 안 눌렀다.** 이건 우리가 붙기 전의
                        # 준비 동작이다 — 마스터는 로그인부터 녹화하는데 룬은
                        # `login()` 으로 이미 했고, 그래서 그 창이 없다.
                        # 창이 없으면 키를 **아무 데도 안 보낸다.** 엉뚱한
                        # 창에 넣을 위험 없이 지나갈 수 있다.
                        skipped.append(step.label())
                        continue
                    self.log(
                        "error",
                        "녹화한 창을 하나로 못 찾았습니다. 없거나 여럿입니다",
                        name=name,
                        window=stable_part(step.window_title),
                        played=played,
                    )
                    return False
                window = found
            if window is None:
                # 첫 단계부터 창을 모른다. 좌표를 어디에 대야 할지도 모른다.
                self.log("error", "어느 창인지 모르는 단계입니다", what=step.label())
                return False
            # **한 단계라도 못 누르면 실패다.** 절반만 눌린 메뉴는 아무것도
            # 안 누른 것보다 나쁘다 — 화면이 어디에 있는지 아무도 모른다.
            #
            # **여기는 시작 전이라고 봐주지 않는다** (codex 지적). 창을 찾았다는
            # 것은 이미 눌러 봤다는 뜻이다. 그 실패를 넘기면 메뉴가 반쯤 열린
            # 채로 다음 키가 나간다 — 위 원칙이 막으려던 바로 그 모양이다.
            if not recorder.replay(step, window, log=self.log):
                return False
            played += 1

        if skipped:
            # **크게 남긴다.** 조용히 건너뛰면 녹화가 통째로 헛돌아도 모른다.
            self.log(
                "warning",
                "녹화 앞부분을 건너뛰었습니다. 우리가 붙기 전의 동작으로 봅니다",
                name=name,
                skipped=skipped,
                played=played,
            )
        if not played:
            # **"녹화가 없다" 와 다른 말이어야 한다.** 사용자가 할 일이 다르다 —
            # 저쪽은 녹화를 뜨는 것이고 이쪽은 창을 띄우는 것이다. 그래서
            # 무엇을 못 찾았는지 같이 남긴다.
            self.log(
                "error",
                "녹화한 창이 하나도 안 떠 있어 한 단계도 재생하지 못했습니다",
                name=name,
                skipped=skipped,
            )
            return False
        return True

    def _http(self):
        from vader.core.http import HttpGateway

        return HttpGateway(dry_run=self._dry_run, log=self.log)

    def _assign(self, role: str):
        """이 Job 이 이 역할에 쓸 제공자. 처음 한 번만 고르고 붙잡는다."""
        from vader.core.ocr import resolve

        if role not in self._assignments:
            chosen = resolve(role)
            self._assignments[role] = chosen
            self.log(
                "info",
                "제공자 배정",
                role=role,
                provider=chosen.provider,
                model=chosen.model,
                mode="자동" if chosen.automatic else "수동",
            )
        return self._assignments[role]

    # ── 러너가 쓰는 것 ──────────────────────────────────────────

    def deliver(self, correlation_id: str, response: UserActionResponse) -> bool:
        """UI 스레드에서 답을 넣는다. 짝이 없으면 False.

        짝이 없는 경우는 정상적으로 일어난다 — 사용자가 답하는 사이 취소가
        먼저 워커를 깨웠으면 그 질문은 이미 사라졌다.
        """
        with self._asks_lock:
            pending = self._asks.get(correlation_id)
        if pending is None:
            return False
        pending.response = response
        pending.done.set()
        return True

    def has_pending_ask(self) -> bool:
        with self._asks_lock:
            return bool(self._asks)
