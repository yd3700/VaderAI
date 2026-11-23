"""로컬 관찰 — 세션, 활성 앱, Job 완료.

경계는 `wiki/architecture/observation-boundary.md` 가 정한다. 요약:

  관찰한다     세션 시작·종료 시각, 최상단 창의 **프로세스 실행파일 이름**,
               Job 완료 횟수
  관찰 안 한다 키 입력, 마우스, 클립보드, 화면 내용, 파일 내용, **창 제목**

창 제목을 빼는 것이 이 모듈의 핵심 결정이다. 실제 제목에는 임상 메모와 거래처
문서명이 들어 있고(`observation-boundary.md` 에 실물 예시), 이 프로젝트는 픽스처에
환자명 0건을 테스트로 강제한다. 게다가 제목은 필요한 값도 아니다 — 앱 다양성은
앱 신원을 세는 것이고 제목은 문서마다 바뀐다.

어디로도 보내지 않는다. 이 파일에 네트워크 코드가 없고 테스트가 `socket` 을 막는다.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from vader.core import config

#: 관찰 종류. 늘리기 전에 경계 문서를 고친다.
SESSION_START = "session_start"
SESSION_END = "session_end"
FOREGROUND = "foreground"
JOB_FINISHED = "job_finished"

KINDS = (SESSION_START, SESSION_END, FOREGROUND, JOB_FINISHED)


@dataclass(frozen=True)
class Observation:
    kind: str
    at: datetime
    #: 앱 이름 같은 짧은 문자열. 개인 내용을 담지 않는다.
    value: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {"kind": self.kind, "at": self.at.isoformat(timespec="seconds"), "value": self.value},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, line: str) -> "Observation | None":
        """못 읽는 줄은 None. 관찰 로그가 앱을 못 띄우게 하지 않는다."""
        try:
            data = json.loads(line)
            return cls(
                kind=str(data["kind"]),
                at=datetime.fromisoformat(data["at"]),
                value=str(data.get("value", "")),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None


def enabled() -> bool:
    return bool(config.get("character.enabled", True))


def retention_days() -> int:
    return int(config.get("character.retention_days", 90))


def sample_seconds() -> int:
    """활성 앱을 얼마나 자주 볼지. 초당 볼 이유가 없다."""
    return int(config.get("character.sample_seconds", 60))


class ObservationStore:
    """JSONL 추가 전용 저장소.

    한 줄 한 관찰. 추가만 하고, 보존 기간을 넘긴 줄은 쓰기 때 정리한다.
    """

    def __init__(self, path: Path | None = None, *, days: int | None = None) -> None:
        self.path = path if path is not None else config.data_dir() / "observations.jsonl"
        self._days = days

    @property
    def days(self) -> int:
        return retention_days() if self._days is None else self._days

    def record(self, observation: Observation) -> bool:
        """관찰 하나를 남긴다. 꺼져 있으면 아무것도 하지 않고 False."""
        if not enabled():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(observation.to_json() + "\n")
        return True

    def load(self) -> list[Observation]:
        """읽을 수 있는 줄만. 시간순은 파일 순서를 믿는다 (추가 전용)."""
        if not self.path.exists():
            return []
        out: list[Observation] = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            item = Observation.from_json(line)
            if item is not None:
                out.append(item)
        return out

    def prune(self, *, now: datetime | None = None) -> int:
        """보존 기간을 넘긴 줄을 지운다. 지운 개수를 돌려준다."""
        if not self.path.exists():
            return 0
        cutoff = (now or datetime.now()) - timedelta(days=self.days)
        kept = [item for item in self.load() if item.at >= cutoff]
        removed = len(self.load()) - len(kept)
        if removed:
            self.path.write_text(
                "".join(item.to_json() + "\n" for item in kept), encoding="utf-8"
            )
        return removed

    def reset(self) -> None:
        """관찰을 전부 지운다. 사용자가 요청할 때만 부른다."""
        self.path.unlink(missing_ok=True)


class SessionObserver:
    """세션과 활성 앱을 관찰한다.

    자기 스레드를 만들지 않는다. UI 가 이미 `after()` 로 도는 루프를 갖고 있어
    거기서 `sample()` 을 불러 주면 된다 — 스레드가 하나 줄면 종료가 단순해진다.
    """

    def __init__(
        self,
        store: ObservationStore | None = None,
        *,
        now=datetime.now,
        app_name=None,
    ) -> None:
        self.store = store or ObservationStore()
        self._now = now
        self._app_name = app_name or foreground_app_name
        self._last_sample: datetime | None = None

    def start(self) -> None:
        self.store.record(Observation(SESSION_START, self._now()))
        self.store.prune(now=self._now())

    def stop(self) -> None:
        self.store.record(Observation(SESSION_END, self._now()))

    def sample(self) -> bool:
        """`sample_seconds` 가 지났으면 활성 앱을 한 번 남긴다."""
        now = self._now()
        if self._last_sample is not None:
            if (now - self._last_sample).total_seconds() < sample_seconds():
                return False
        name = self._app_name()
        if not name:
            return False
        self._last_sample = now
        return self.store.record(Observation(FOREGROUND, now, name))

    def job_finished(self) -> bool:
        """룬 실행이 끝났다는 사실만. 성장에는 쓰지 않는다."""
        return self.store.record(Observation(JOB_FINISHED, self._now()))


def foreground_app_name() -> str:
    """최상단 창을 소유한 프로세스의 실행파일 이름. 창 제목은 읽지 않는다."""
    if sys.platform != "win32":
        return ""

    import ctypes
    import ctypes.wintypes as wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.basename(buffer.value).lower()
    finally:
        kernel32.CloseHandle(handle)
