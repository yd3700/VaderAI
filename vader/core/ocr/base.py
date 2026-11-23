"""OCR 제공자 계약과 게이트웨이 오류."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

# 룬이 잡아야 하는 오류라 계약에 산다. 룬은 vader.core를 import할 수 없다.
from vader.sdk import OcrError, OcrProviderError, OcrProviderUnavailable, OcrSchemaError

__all__ = [
    "ChatAnswer",
    "ToolCall",
    "OcrError",
    "OcrProvider",
    "OcrProviderError",
    "OcrProviderUnavailable",
    "OcrSchemaError",
]


@dataclass(frozen=True)
class ToolCall:
    """모델이 "이 도구를 이 인자로 불러 달라" 고 한 것."""

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ChatAnswer:
    """도구를 준 대화의 답.

    `tool_calls` 가 비어 있지 않으면 **아직 답이 아니다** — 코어가 도구를
    실행하고 결과를 넣어 다시 물어야 한다. 실행 루프는 코어가 맡는다
    (codex): 룬도 제공자도 남의 도구를 돌리면 안 된다.
    """

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = ""


@runtime_checkable
class OcrProvider(Protocol):
    def extract(self, path: Path, prompt: str, schema: type) -> object: ...

    #: 파일 없이 텍스트로 묻는다. `ctx.infer()` 가 쓴다.
    def complete(self, prompt: str, schema: type) -> object: ...

    def name(self) -> str: ...

    def model(self) -> str: ...

    def adapter_version(self) -> str: ...

    def is_available(self) -> bool: ...

    #: `tools` 를 주면 `ChatAnswer`, 안 주면 문자열이다.
    #
    # ponytail: 반환 형이 갈리는 것은 도구 없는 호출(대부분)을 그대로 두려는
    # 것이다. 도구가 기본이 되면 그때 `ChatAnswer` 하나로 좁힌다.
    def chat(self, messages: list[dict], tools: list[dict] | None = None): ...

    #: 이 역할을 할 수 있나. 텍스트 전용 모델은 `extract` 를 못 한다.
    #: 배정 화면에서 미리 막지 않으면 새벽에 Job 이 죽어서야 알게 된다.
    def supports(self, role: str) -> bool: ...


class FakeOcrProvider:
    """네트워크 없이 게이트웨이 전체를 검증하는 제공자.

    ``outcomes``의 항목이 예외면 던지고, 그 외에는 응답으로 반환한다.
    """

    def __init__(
        self,
        outcomes: list[object],
        *,
        available: bool = True,
        provider_name: str = "fake",
        model_name: str = "fake-model",
        adapter_version: str = "1",
    ) -> None:
        self._outcomes = list(outcomes)
        self._available = available
        self._name = provider_name
        self._model = model_name
        self._adapter_version = adapter_version
        self.calls: list[tuple[Path | None, str, type]] = []

    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._available

    def supports(self, role: str) -> bool:
        return True

    def chat(self, messages: list[dict], tools: list[dict] | None = None):
        self.calls.append((None, messages, tools))
        answer = self._next()
        if isinstance(answer, ChatAnswer):
            return answer
        return answer if isinstance(answer, str) else str(answer)

    def model(self) -> str:
        return self._model

    def adapter_version(self) -> str:
        return self._adapter_version

    def extract(self, path: Path, prompt: str, schema: type) -> object:
        self.calls.append((path, prompt, schema))
        return self._next()

    def complete(self, prompt: str, schema: type) -> object:
        self.calls.append((None, prompt, schema))
        return self._next()

    def _next(self) -> object:
        if not self._outcomes:
            raise AssertionError("FakeOcrProvider 응답이 부족합니다")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

