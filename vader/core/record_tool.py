"""대화로 녹화를 시작하고 멈추고 저장한다.

마스터 요청 (2026-08-11):

> 완성된 룬을 녹화로 수정할 때, 지금처럼 llm에서 이야기하는게 아닌 **실시간
> 대화를 통해서** 이루어지도록 하면 좋겠어.

지금까지의 왕복은 이랬다.

    마스터가 실행 → 실패 → 나(클로드)에게 로그를 붙여넣음 → 내가 고침 →
    재설치 → 다시 실행

**그 가운데 내가 없어야 한다.** 마스터가 펫에게 "메뉴 다시 알려줄게" 라고
말하고, 녹화하고, 펫이 무엇을 잡았는지 읽어 주고, 마스터가 "그걸로 해" 라고
하면 끝나야 한다.

## 승인은 그대로다

`settings_tool` 과 같은 규칙이다 — **사람이 예라고 해야 저장된다.** 녹화는
룬이 다음에 무엇을 누를지 정하는 것이라 설정보다 무겁다.

## 여기서 안 하는 것

**키 입력은 여전히 안 담는다.** 대화로 시작한다고 달라지지 않는다 —
`recorder.py` 가 애초에 그 길을 안 만들었고, 이 파일은 그것을 부를 뿐이다.
"""
from __future__ import annotations

from vader.core import recorder

__all__ = ["TOOL_SCHEMA", "RecordTool"]

TOOL_SCHEMA = {
    "name": "record_demo",
    "description": (
        "화면 조작을 시연으로 배운다. 사용자가 '녹화 시작', '이거 다시 알려줄게', "
        "'메뉴 순서 바꿀게' 처럼 말하면 부른다. "
        "action='start' 로 시작하고, 사용자가 시연을 마쳤다고 하면 "
        "action='stop' 으로 멈춘다. 사용자가 확인한 뒤 action='save' 를 부르면 "
        "저장과 동시에 다음 실행부터 적용된다. 저장 뒤 별도의 '적용' 단계는 없다."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "stop", "save", "discard"],
                "description": "start 시작 · stop 멈추고 보기 · save 저장 · discard 버리기",
            },
            "name": {
                "type": "string",
                "enum": [name for name, _purpose in recorder.PURPOSES],
                "description": "녹화 용도. 현재 화면 이동에는 navigation만 쓴다",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


class RecordTool:
    """대화가 부르는 녹화 도구. **세션 하나에 녹화 하나.**"""

    def __init__(self, *, ask=None, log=None) -> None:
        #: 저장해도 되는지 사람에게 묻는 함수. **없으면 저장 안 한다.**
        self._ask = ask
        self._log = log or (lambda level, message, **fields: None)
        self._recorder: recorder.Recorder | None = None
        self._last_saved: tuple[str, int] | None = None

    def current_recorder(self) -> recorder.Recorder | None:
        """대화창의 녹화 패널이 이어받을 현재 녹화기를 돌려준다."""
        return self._recorder

    def finish_recorder(
        self,
        made: recorder.Recorder,
        *,
        saved_as: str | None = None,
    ) -> bool:
        """화면이 빌린 녹화기를 끝내고 저장 여부를 도구와 맞춘다.

        화면은 녹화기를 빌릴 뿐이고 회차 상태는 이 도구가 소유한다. 녹화기
        객체까지 확인해서 이전 회차의 늦은 콜백이 새 회차를 비우지 못하게 한다.
        """
        if made is not self._recorder:
            return False
        if saved_as is not None:
            allowed = {key for key, _purpose in recorder.PURPOSES}
            if saved_as not in allowed:
                return False
        if made.running:
            made.stop()
        self._last_saved = (
            (saved_as, len(made.steps)) if saved_as is not None else None
        )
        self._recorder = None
        return True

    def invoke(self, action: str, name: str = "") -> str:
        if action == "start":
            return self._start()
        if action == "stop":
            return self._stop()
        if action == "save":
            return self._save(name or "navigation")
        if action == "discard":
            if self._recorder is not None:
                self.finish_recorder(self._recorder)
            else:
                self._last_saved = None
            return "녹화한 것을 버렸습니다."
        return f"'{action}' 은 모르는 동작입니다. start · stop · save · discard 중 하나입니다."

    def _start(self) -> str:
        if self._recorder is not None and self._recorder.running:
            return "이미 녹화 중입니다. 시연을 마치면 말씀해 주세요."
        self._recorder = recorder.Recorder()
        self._last_saved = None
        self._recorder.start()
        self._log("info", "대화로 녹화를 시작했습니다")
        return (
            "녹화를 시작했습니다. 평소대로 눌러 보여 주세요. "
            "**키 입력은 기록하지 않습니다** — 비밀번호가 파일로 남지 않습니다. "
            "다 하시면 말씀해 주세요."
        )

    def _stop(self) -> str:
        if self._recorder is None:
            return "녹화 중인 것이 없습니다."
        self._recorder.stop()
        steps = self._recorder.steps
        if not steps:
            return "아무것도 안 잡혔습니다. 다시 해 보시겠어요?"
        # **잡은 것을 그대로 읽어 준다.** 사람이 여기서 거른다.
        lines = [f"{len(steps)}단계를 잡았습니다."]
        for index, step in enumerate(steps, start=1):
            mark = "" if step.findable() else "  (좌표로만 찾음 — 창 크기가 바뀌면 깨집니다)"
            lines.append(f"{index}. {step.label()}{mark}")
        lines.append("목록을 확인한 뒤 저장하면 다음 실행부터 바로 적용됩니다.")
        return "\n".join(lines)

    def _save(self, name: str) -> str:
        allowed = {key for key, _purpose in recorder.PURPOSES}
        if name not in allowed:
            choices = ", ".join(sorted(allowed))
            return f"'{name}' 은 쓸 수 없는 녹화 이름입니다. 사용 가능한 이름: {choices}"
        if self._recorder is None:
            if self._last_saved is not None:
                saved_name, count = self._last_saved
                return (
                    f"이미 '{saved_name}' 으로 {count}단계를 저장해 적용했습니다. "
                    "다시 적용할 단계는 없습니다."
                )
            return "저장할 녹화가 없습니다."
        if not self._recorder.steps:
            return "저장할 녹화가 없습니다."
        if self._recorder.running:
            self._recorder.stop()
        if self._ask is None:
            return "승인 통로가 없어 저장하지 않았습니다."
        steps = self._recorder.steps
        if not self._ask(
            "녹화를 저장할까요?",
            f"'{name}' 으로 {len(steps)}단계를 저장합니다.\n\n"
            + "\n".join(f"{i}. {s.label()}" for i, s in enumerate(steps, start=1))
            + "\n\n다음 실행부터 이 순서로 누릅니다.",
        ):
            return "사용자가 승인하지 않아 저장하지 않았습니다."
        recorder.save(name, steps)
        self._last_saved = (name, len(steps))
        self._recorder = None
        self._log("info", "대화로 녹화를 저장했습니다", name=name, steps=len(steps))
        return f"'{name}' 으로 저장해 적용했습니다. **다음 실행부터 이 순서로 누릅니다.**"
