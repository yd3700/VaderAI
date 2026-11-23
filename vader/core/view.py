"""
이벤트를 화면 상태로 접는다.

UI 를 테스트하려고 창을 띄우지 않기 위해 이 부분만 떼어 뒀다. tkinter 쪽은
`ui.py` 에 있고 여기서 만든 상태를 위젯에 옮기기만 한다 — 판단이 없으니
눈으로 확인할 수 있다.

MVVM 같은 것을 만든 게 아니다. 진행률 계산과 버튼 활성 조건이 유일하게
틀릴 수 있는 로직이라 그것만 꺼냈다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from vader.core.events import Finished, JobEvent, LogLine, Progress, StateChanged
from vader.core.job import TERMINAL, JobState
from vader.sdk import CheckpointKey, Input, RuneField

#: 로그를 무한히 쌓지 않는다. 긴 배치에서 메모리가 늘고, 사람은 최근 것만 본다.
# ponytail: 단순 상한. 잘린 앞부분이 필요해지면 파일 로그를 따로 둔다.
MAX_LOGS = 500


@dataclass
class JobView:
    """한 Job 의 화면 상태."""

    state: JobState | None = None
    current: int = 0
    #: None 이면 전체 개수를 모른다 — 불확정 진행률로 그린다.
    total: int | None = None
    message: str = ""
    logs: list[str] = field(default_factory=list)
    artifacts: list[Path] = field(default_factory=list)
    failure: str | None = None

    @property
    def is_done(self) -> bool:
        return self.state in TERMINAL

    @property
    def can_cancel(self) -> bool:
        """돌고 있을 때만 취소를 누를 수 있다."""
        return self.state is not None and not self.is_done

    @property
    def percent(self) -> int | None:
        """0~100. 전체 개수를 모르면 None."""
        if self.total is None or self.total <= 0:
            return None
        return min(100, round(self.current / self.total * 100))

    @property
    def status_text(self) -> str:
        if self.state is None:
            return "대기"
        if self.state is JobState.WAITING_USER:
            return "사용자 입력 대기"
        if self.state is JobState.CANCEL_REQUESTED:
            # 취소를 눌러도 즉시 멈추지 않는다. 왜 아직 도는지 사용자에게 말해준다.
            return "취소 요청됨 — 현재 항목 완료 후 중단합니다"
        if self.state is JobState.CANCELLED:
            return "취소됨"
        if self.state is JobState.SUCCEEDED:
            return "완료"
        if self.state is JobState.FAILED:
            return f"실패: {self.failure}" if self.failure else "실패"
        return self.message or "실행 중"


def apply(view: JobView, event: JobEvent) -> None:
    """이벤트 하나를 반영한다. 모르는 종류는 무시한다."""
    if isinstance(event, StateChanged):
        view.state = event.state
    elif isinstance(event, Progress):
        view.current = event.current
        view.total = event.total
        view.message = event.message
    elif isinstance(event, LogLine):
        view.logs.append(f"[{event.level}] {event.message}{_detail(event.fields)}")
        if len(view.logs) > MAX_LOGS:
            del view.logs[: len(view.logs) - MAX_LOGS]
    elif isinstance(event, Finished):
        view.state = event.state


#: 로그 한 줄에 붙일 **항목 하나**의 길이 상한.
#:
#: 전에는 값 전체를 이 길이로 잘랐다. 그래서 창 목록이 다섯 개쯤에서 끊겼고
#: **답이 그 뒤에 있었다** — 마스터 로그에서 이온엠 본 창이 잘려 나갔다.
#: 목록은 항목마다 자르고 전부 보여준다.
MAX_FIELD = 60


def _detail(fields: dict | None) -> str:
    """`ctx.log(..., titles=[...])` 의 필드를 화면에 보이게 만든다.

    **여기가 비어 있어서 진단이 통째로 안 보였다.** 룬이 무엇을 봤는지
    정성껏 남겼는데 화면은 메시지만 그렸고, 실측에서 마스터가 줄 수 있는
    것은 "하나로 좁히지 못했습니다" 한 줄뿐이었다 — 무엇을 봤는지가 빠진
    그 줄로는 아무도 고칠 수 없다.
    """
    if not fields:
        return ""
    parts = []
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            # **목록은 다 보여준다.** 잘라 낸 뒤쪽에 답이 있을 수 있다.
            text = "[" + ", ".join(_short(item) for item in value) + "]"
        else:
            text = _short(value)
        parts.append(f"{key}={text}")
    return "  ·  " + " · ".join(parts)


def _short(value) -> str:
    text = str(value)
    return text if len(text) <= MAX_FIELD else text[:MAX_FIELD] + "…"


def apply_all(view: JobView, events: list[JobEvent]) -> None:
    for event in events:
        apply(view, event)


def checkpoint_key_for(rune_id: str, paths: list[Path]) -> CheckpointKey | None:
    """지금 고른 입력의 체크포인트 키. **입력이 하나라도 사라졌으면 None.**

    키는 입력 **내용**으로 만든다 — `CheckpointKey.from_inputs` 가 파일을 읽는다.
    그런데 성공한 실행은 입력을 `done_dir` 로 옮긴다. 완료 직후 같은 경로로 키를
    다시 만들면 `FileNotFoundError` 다.

    그 예외가 UI 폴링을 통째로 죽였다. 화면은 멀쩡해 보이는데 다음 실행의 로그가
    안 나오고 버튼이 안 살아난다 — **작업은 되는데 화면만 죽은** 상태다.

    없는 입력에 이어하기가 없는 것은 맞는 답이므로, 여기서 None 을 돌려준다.
    `contract.py` 의 "내용으로 키를 만든다" 규칙은 건드리지 않는다. 그걸 바꾸면
    기존 체크포인트가 전부 무효가 되고 이어하기의 의미도 달라진다.
    """
    if not paths:
        return None
    if not all(Path(path).is_file() for path in paths):
        return None
    return CheckpointKey.from_inputs(
        rune_id, [Input(path=Path(path)) for path in paths]
    )


def is_resumable(checkpoint: dict | None) -> bool:
    """이어받을 진행이 남아 있는가.

    **빈 dict 는 "진행 없음" 이다.** 룬은 파일을 끝내면 진행 목록에서 그 파일을
    빼고 남은 것을 저장하는데, 마지막 파일이 끝나면 `{}` 가 저장된다. 이걸
    `is not None` 으로 보면 **성공한 실행 뒤에도 "이어서 실행합니다" 가 뜬다** —
    실제로는 전부 다시 도는데 이어받는다고 안내한다.
    """
    return bool(checkpoint)


class FieldError(ValueError):
    """입력값을 설정에 넣을 수 없다. 메시지를 사용자에게 그대로 보여준다."""


#: 창 제목 안의 "변하는 것". 시:분 과 YYYY-MM-DD 만 본다. 기관코드 `(10000001)`
#: 같은 순수 8자리는 안 잡는다 — 그건 안정적인 식별자라 막으면 오탐이다.
_VOLATILE_TITLE = re.compile(r"\d{1,2}:\d{2}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")


def coerce_field(field: RuneField, raw: str) -> object:
    """화면에서 받은 문자열을 설정에 넣을 값으로 바꾼다.

    마법사는 어떤 위젯이든 문자열을 준다. `kind`가 그걸 무엇으로 읽을지 정한다.
    """
    text = raw.strip()
    label = field.label or field.name

    if text == "":
        if getattr(field, "optional", False):
            # **빈 값이 곧 "안 쓴다" 는 뜻이다.** 룬이 그렇게 읽기로 선언했다.
            return ""
        raise FieldError(f"{label}: 값이 필요합니다")

    if field.kind == "number":
        try:
            return int(text)
        except ValueError:
            raise FieldError(f"{label}: 숫자를 넣으세요 (받은 값: {text!r})") from None

    if field.kind == "choice" and field.options and text not in field.options:
        raise FieldError(f"{label}: {', '.join(field.options)} 중에서 고르세요")

    if field.kind == "window" and _VOLATILE_TITLE.search(text):
        # 창 목록에서 고른 제목을 그대로 저장하면 기동 시각이 같이 박힌다.
        # 드라이버가 제목 전체를 `title_re=".*{...}.*"` 에 넣으므로 대상 앱을
        # 다시 띄우는 순간 못 찾는다. 오늘은 되고 내일은 안 되는 설정이다.
        raise FieldError(
            f"{label}: 창 제목에 날짜나 시각이 들어 있습니다. "
            "프로그램을 다시 띄우면 이 값이 바뀌어 창을 못 찾습니다. "
            "변하지 않는 부분만 남기세요 (예: 화면 이름)"
        )

    if field.kind == "path" and not Path(text).is_file():
        # 지금 없는 파일을 저장해두면 실행 중에 알 수 없는 이유로 실패한다.
        raise FieldError(f"{label}: 파일을 찾을 수 없습니다 ({text})")

    if field.kind == "dir" and not Path(text).is_dir():
        # 룬이 사용자 파일을 여기로 옮긴다. 오타를 저장하면 파일이 엉뚱한 곳에 간다.
        raise FieldError(f"{label}: 폴더를 찾을 수 없습니다 ({text})")

    return text


@dataclass(frozen=True)
class SealState:
    """운영 실행 버튼을 열어도 되나. **판단은 여기, 위젯은 ui.py.**"""

    #: 참이면 운영 실행이 잠긴다. 연습 실행만 된다.
    locked: bool
    #: 사용자에게 보일 한 줄. 잠기지 않았으면 빈 문자열.
    reason: str = ""


def seal_state(rune_id: str) -> SealState:
    """봉인 상태를 화면 상태로 바꾼다.

    **잠그는 것만으로는 부족하다.** 그냥 안 눌리면 사용자는 고장으로 안다.
    왜 잠겼는지, 지금 무엇을 할 수 있는지를 같이 말해야 한다.

    설치 폴더가 없으면 잠그지 않는다 — 러너와 같은 규칙이다. 설치를 안 거친
    룬은 목록에도 안 뜨므로 실제로는 닿지 않는 가지다.
    """
    from vader.core import config, seal

    source = config.rune_dir() / rune_id
    if not source.is_dir() or seal.sealed(rune_id, source):
        return SealState(locked=False)
    return SealState(locked=True, reason=seal.why(rune_id, source))


#: 사지선답에 줄 지우기 버튼을 몇 개까지 그릴까. 다 그리면 버튼 줄이 넘친다.
MAX_DROP_BUTTONS = 4


def recording_lines(steps: list) -> str:
    """잡은 단계를 사람이 읽을 글로. **화면이 그린다.**

    모델에게 맡겼더니 "이대로 저장하시겠어요?" 한 줄로 요약했다 (마스터
    실측 2026-08-11). 사람이 확인해야 할 자리에서 확인할 것이 사라졌다.
    """
    if not steps:
        return "아무것도 안 잡혔습니다."
    lines = [f"{len(steps)}단계를 잡았습니다."]
    for index, step in enumerate(steps, start=1):
        mark = "" if step.findable() else "   ⚠ 좌표로만 찾음"
        lines.append(f"{index}. {step.label()}{mark}")
    return "\n".join(lines)


def recording_choices(steps: list) -> list[str]:
    """녹화를 마친 뒤 고를 것들. **글자만 정한다** — 누르면 무엇을 할지는
    화면이 안다.

    마스터 요청(2026-08-12): 팝업 대신 대화창 안에서 사지선답으로.
    """
    if not steps:
        return ["다시 녹화", "그만"]
    return (
        ["이대로 저장"]
        + [f"{i + 1}번 빼기" for i in range(min(MAX_DROP_BUTTONS, len(steps)))]
        + ["버리기"]
    )
