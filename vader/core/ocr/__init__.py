"""룬에서 사용하는 OCR 게이트웨이."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from vader.core import config
from vader.core.ocr.base import (
    FakeOcrProvider,
    OcrError,
    OcrProvider,
    OcrProviderError,
    OcrProviderUnavailable,
    OcrSchemaError,
)
from vader.core.ocr.gateway import Log, OcrGateway
from vader.core.ocr.claude import ClaudeProvider
from vader.core.ocr.console import ConsoleProvider
from vader.core.ocr.gemini import GeminiProvider
from vader.core.ocr.openai_compat import OpenAICompatProvider
from vader.core.ocr.assign import Assignment, Candidate, candidates, resolve


#: 이름 → 만드는 법. **제공자를 더하는 곳은 여기 한 줄이다.**
#: 전에는 `create_gateway` 안의 `if` 였고, 제공자마다 분기가 하나씩
#: 늘어나는 자리였다.
PROVIDERS: dict[str, Callable[..., OcrProvider]] = {
    "claude": ClaudeProvider,
    "gemini": GeminiProvider,
    # 로그인된 Claude Code·Codex 로 읽는다 — API 키 없음 (vader/core/ocr/console.py)
    "claude_console": lambda model=None: ConsoleProvider("claude", model),
    "codex_console": lambda model=None: ConsoleProvider("codex", model),
    "agy_console": lambda model=None: ConsoleProvider("agy", model),
}


def provider_factory(name: str):
    """이름 → 만드는 법. 없으면 None.

    등록표에 없어도 설정에 `base_url` 이 있으면 **OpenAI 호환**으로 본다.
    NVIDIA NIM·Together·Groq·로컬 vLLM 이 전부 같은 모양이라, 제공자 하나
    더 쓰려고 코드를 고치게 하면 그게 병목이 된다.
    """
    if name in PROVIDERS:
        return PROVIDERS[name]
    if config.get(f"api.providers.{name}.base_url"):
        return lambda model=None: OpenAICompatProvider(name, model=model)
    return None


def create_gateway(
    role: str = "extract",
    *,
    assignment: Assignment | None = None,
    log: Log | None = None,
) -> OcrGateway:
    """역할이 탈 게이트웨이.

    `assignment` 를 주면 그것을 쓴다. Job 이 한 번 고른 제공자를 그 Job 이
    끝날 때까지 붙잡기 위한 것이다 — 자동 배정이 작업 도중에 흔들리면 한
    작업의 결과가 두 모델에서 나온다.
    """
    chosen = assignment or resolve(role)
    factory = provider_factory(chosen.provider)
    if factory is None:
        raise OcrProviderUnavailable(
            f"지원하지 않는 제공자입니다: {chosen.provider!r} (역할 {role!r}). "
            f"쓸 수 있는 것: {', '.join(sorted(PROVIDERS))} "
            "(또는 api.providers.{이름}.base_url 을 둔 OpenAI 호환 엔드포인트)"
        )
    provider = factory(model=chosen.model)
    if not provider.supports(role):
        raise OcrProviderUnavailable(
            f"제공자 '{chosen.provider}' 는 '{role}' 역할을 못 합니다."
        )
    return OcrGateway(provider, log=log)


def extract(
    path: Path,
    prompt: str,
    schema: type,
    *,
    assignment: Assignment | None = None,
    log: Log | None = None,
) -> object:
    return create_gateway("extract", assignment=assignment, log=log).extract(
        path, prompt, schema
    )


def infer(
    prompt: str,
    schema: type,
    *,
    assignment: Assignment | None = None,
    log: Log | None = None,
) -> object:
    """파일 없이 텍스트로 묻는다. `extract` 와 같은 게이트웨이를 탄다."""
    return create_gateway("infer", assignment=assignment, log=log).complete(
        prompt, schema
    )


__all__ = [
    "FakeOcrProvider",
    "ClaudeProvider",
    "GeminiProvider",
    "OcrError",
    "OcrGateway",
    "OcrProvider",
    "OcrProviderError",
    "OcrProviderUnavailable",
    "OcrSchemaError",
    "PROVIDERS",
    "Assignment",
    "Candidate",
    "candidates",
    "resolve",
    "create_gateway",
    "extract",
    "infer",
]

