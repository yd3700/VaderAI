"""
Job 모델과 상태 전이.

Job은 "이 룬을 이 입력으로 한 번 실행한다"의 단위다. 재시도는 새 Job이다 —
그래서 체크포인트를 job_id로 키잉하면 안 된다(contract.py의 CheckpointKey 참조).

상태는 일곱 개뿐이고 전이는 아래 표가 전부다. 표에 없는 전이는 예외다.
상태를 문자열로 아무 데서나 바꾸면 "취소했는데 성공으로 끝난" 같은 것이
조용히 생긴다.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from vader.sdk import Failure, Input, RuneResult


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    #: 룬이 request_user_action()에서 사람을 기다리는 중. 워커 스레드는 블록돼 있다.
    WAITING_USER = "waiting_user"
    #: 취소를 요청받았지만 아직 안전 지점에 닿지 않았다. 룬은 계속 돌고 있다.
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: 각 상태에서 갈 수 있는 곳. 여기 없는 전이는 버그다.
_ALLOWED: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset(
        {
            JobState.WAITING_USER,
            JobState.CANCEL_REQUESTED,
            JobState.SUCCEEDED,
            JobState.FAILED,
        }
    ),
    # 사람을 기다리는 중에도 취소는 받는다. 응답이 오면 다시 RUNNING.
    JobState.WAITING_USER: frozenset(
        {
            JobState.RUNNING,
            JobState.CANCEL_REQUESTED,
            JobState.SUCCEEDED,
            JobState.FAILED,
        }
    ),
    # 취소를 요청해도 룬이 실패로 끝날 수 있다. 그건 실패다 — 요청했다고
    # 결과를 취소로 덮으면 무엇이 일어났는지 잃는다.
    JobState.CANCEL_REQUESTED: frozenset(
        {JobState.CANCELLED, JobState.FAILED, JobState.SUCCEEDED}
    ),
    JobState.CANCELLED: frozenset(),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
}

#: 더 이상 변하지 않는 상태.
TERMINAL = frozenset({JobState.CANCELLED, JobState.SUCCEEDED, JobState.FAILED})


class InvalidTransition(Exception):
    def __init__(self, current: JobState, target: JobState) -> None:
        super().__init__(f"{current.value} → {target.value} 는 허용되지 않는 전이입니다")
        self.current = current
        self.target = target


@dataclass
class Job:
    """실행 한 번. 상태 변경은 transition()으로만 한다."""

    rune_id: str
    inputs: list[Input]
    workdir: Path
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    result: RuneResult | None = None
    failure: Failure | None = None

    _state: JobState = field(default=JobState.QUEUED, init=False)
    #: 상태는 워커와 UI 두 스레드가 함께 읽고 쓴다.
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def state(self) -> JobState:
        with self._lock:
            return self._state

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    def transition(self, target: JobState) -> None:
        with self._lock:
            if target not in _ALLOWED[self._state]:
                raise InvalidTransition(self._state, target)
            self._state = target

    def try_transition(self, target: JobState) -> bool:
        """경합에서 진 쪽이 조용히 물러나야 할 때 쓴다.

        취소 요청과 룬의 정상 종료가 거의 동시에 일어날 수 있다. 그때 둘 중
        하나는 실패하는 것이 정상이므로 예외로 만들지 않는다.
        """
        try:
            self.transition(target)
            return True
        except InvalidTransition:
            return False
