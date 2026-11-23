"""
코어가 UI에 보내는 이벤트.

룬은 print()를 쓰지 않고 코어는 UI를 import하지 않는다. 그 사이를 잇는 것이
이 파일의 타입들과 큐 하나뿐이다.

**큐로 주고 콜백으로 부르지 않는 이유.** 룬은 워커 스레드에서 돈다. 거기서
콜백을 부르면 UI 코드가 워커 스레드에서 실행되는데, tkinter는 위젯을 만든
스레드 밖에서 만지면 조용히 깨지거나 죽는다. 큐에 넣어두면 UI가 자기 스레드에서
`after()`로 꺼내 간다. 코어는 어떤 UI인지 몰라도 된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vader.core.job import JobState
from vader.sdk import UserActionRequest


@dataclass(frozen=True)
class JobEvent:
    """모든 이벤트의 공통 부분."""

    job_id: str


@dataclass(frozen=True)
class StateChanged(JobEvent):
    state: JobState


@dataclass(frozen=True)
class LogLine(JobEvent):
    level: str
    message: str
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Progress(JobEvent):
    current: int
    #: None이면 전체 개수를 모르는 작업이다. UI는 불확정 표시를 쓴다.
    total: int | None
    message: str = ""


@dataclass(frozen=True)
class UserActionRequested(JobEvent):
    """룬이 사람을 기다리며 블록됐다.

    UI는 이걸 받아 화면을 띄우고, 답이 나오면 `runner.respond(correlation_id, ...)`를
    부른다. correlation_id 없이 job_id만으로 짝을 맞추면, 한 Job이 연달아 두 번
    물었을 때 늦게 온 답이 엉뚱한 질문에 붙는다.
    """

    correlation_id: str
    request: UserActionRequest


@dataclass(frozen=True)
class Finished(JobEvent):
    """Job이 끝났다. state는 succeeded / failed / cancelled 중 하나."""

    state: JobState
