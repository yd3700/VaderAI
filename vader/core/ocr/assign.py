"""역할에 제공자를 고른다. 수동과 자동 둘이다.

```json
"roles": {
  "extract": { "provider": "gemini" },                  수동 — 못 박는다
  "infer":   { "providers": ["claude", "gemini"] }      자동 — 앞에서부터
}
```

**수동은 안 바뀐다.** 한도를 넘겨도 그 제공자로 간다 — 못 박은 것을 코어가
말없이 옮기면 그게 조용한 데이터 변경이다.

**자동은 오늘 사용량으로 고른다.** 목록 앞에서부터, 한도에 안 닿은 첫
제공자. 전부 닿으면 **실패한다.** 조용히 계속하지 않는 이유는 하나다 — 그
지점에서 사람이 뭔가 해야 하고, 성공한 척하면 아무도 안 한다.

고른 결과는 **Job 하나 동안 안 바뀐다**(`JobContext` 가 붙잡는다). 한 작업의
앞 절반이 Gemini, 뒤 절반이 Claude 에서 나오면 결과가 섞인다. 제공자마다
OCR 이 다르게 나오므로 이건 가용성이 아니라 조용한 데이터 변경이다.
"""
from __future__ import annotations

from dataclasses import dataclass

from vader.core import config
from vader.core.ocr.base import OcrProviderUnavailable
from vader.core.usage import UsageLedger

__all__ = ["Assignment", "Candidate", "candidates", "choose", "resolve"]


@dataclass(frozen=True)
class Assignment:
    provider: str
    #: 역할이 덮어쓴 모델. 없으면 제공자 기본값.
    model: str | None
    #: 자동으로 고른 것인가. 로그와 배정 화면이 쓴다.
    automatic: bool


@dataclass(frozen=True)
class Candidate:
    """자동 배정이 본 후보 하나. 배정 화면이 그대로 그린다."""

    provider: str
    calls_today: int
    daily_limit: int
    #: 못 쓰면 그 이유. 쓸 수 있으면 None.
    blocked: str | None

    @property
    def usable(self) -> bool:
        return self.blocked is None


def resolve(role: str, *, ledger: UsageLedger | None = None) -> Assignment:
    if "default" in (config.get("api", {}) or {}):
        return Assignment(choose(role, ledger=ledger), None, True)
    manual = config.get(f"api.roles.{role}.provider")
    automatic = config.get(f"api.roles.{role}.providers")
    model = config.model_for(role)

    if manual and automatic:
        raise ValueError(
            f"역할 {role!r} 에 provider 와 providers 가 같이 있습니다. "
            "하나만 두세요 — provider 는 수동, providers 는 자동입니다."
        )

    if automatic:
        return Assignment(_first_usable(role, automatic, ledger), model, True)
    return Assignment(manual or config.ocr_provider(), model, False)


def choose(role: str, *, ledger: UsageLedger | None = None) -> str:
    """기본 AI 하나 정책 (2026-10-14 마스터 결정 — 역할 배정 화면 없음, codex1 합의 `ai-simple-codex.md`).

    - 그 역할을 기본 AI 가 할 수 있으면 기본 AI.
    - 문서 읽기만 예외: 기본 AI 가 문서를 못 읽으면 **문서를 읽는 연결된 AI** 하나 (설정 순서). 화면에 '문서 처리: …' 로 보인다.
    - 그 밖에는 다른 연결된 AI 로 넘기지 않는다 — 자료가 어디로 가는지 사람이 알게. 실패하면 이유를 말한다.
      기본 AI 가 한도·확인 실패로 막혔을 때도 넘기지 않는다 — '못 읽는다' 만 넘길 이유다 (codex1 지적).
    작업을 시작할 때 한 번 고르고, 작업 중에는 `JobContext` 가 붙잡는다.
    """
    from vader.core.ocr import provider_factory

    ledger = ledger or UsageLedger(config.data_dir() / "usage")
    documents = config.get("api.documents")
    if role == "extract" and documents:
        # 사람이 문서 읽기를 따로 맡긴 AI (로그인된 Claude Code·Codex). 막혀도 다른 AI 로 몰래 넘기지 않는다.
        picked = _look(documents, ledger, role)
        if picked.usable:
            return documents
        raise OcrProviderUnavailable(f"문서 읽기를 맡은 AI 를 쓸 수 없습니다 ({documents}: {picked.blocked}). "
                                     "'AI 연결' 에서 확인하세요.")
    default = config.get("api.default")
    first = _look(default, ledger, role) if default else None
    if first is not None and first.usable:
        return default
    factory = provider_factory(default) if default else None
    if role == "extract" and factory is not None and not factory().supports(role):
        for name in (config.get("api.providers", {}) or {}):
            if name != default and _look(name, ledger, role).usable:
                return name
    if first is None:
        raise OcrProviderUnavailable("기본 AI 가 없습니다. 'AI 연결' 에서 AI 를 연결하세요.")
    raise OcrProviderUnavailable(f"기본 AI 를 쓸 수 없습니다 ({default}: {first.blocked}). 'AI 연결' 에서 확인하세요.")


def candidates(role: str, *, ledger: UsageLedger | None = None) -> list[Candidate]:
    """자동 목록과 각 후보의 오늘 사용량. 자동이 아니면 빈 목록."""
    names = config.get(f"api.roles.{role}.providers") or []
    ledger = ledger or UsageLedger(config.data_dir() / "usage")
    return [_look(name, ledger, role) for name in names]


def _first_usable(role: str, names: list[str], ledger: UsageLedger | None) -> str:
    ledger = ledger or UsageLedger(config.data_dir() / "usage")
    looked = [_look(name, ledger, role) for name in names]
    for candidate in looked:
        if candidate.usable:
            return candidate.provider
    raise OcrProviderUnavailable(
        f"역할 {role!r} 의 제공자를 전부 쓸 수 없습니다. "
        + "; ".join(f"{c.provider}: {c.blocked}" for c in looked)
    )


def _failed_check(name: str) -> bool:
    """지금 모델로 한 마지막 연결 확인이 실패했나. 확인한 적 없으면 아니다(옛 설정을 끊지 않는다)."""
    checked = config.get(f"api.providers.{name}.checked") or {}
    return checked.get("ok") is False and checked.get("model") == config.get(f"api.providers.{name}.model")


def _failed_chat(name: str) -> bool:
    """지금 모델로 한 대화 확인이 실패했나 (로그인 AI 만 'chat' 을 따로 남긴다 — codex1 next3 회신 2 #3)."""
    checked = config.get(f"api.providers.{name}.checked") or {}
    return checked.get("chat") is False and checked.get("model") == config.get(f"api.providers.{name}.model")


def _look(name: str, ledger: UsageLedger, role: str = "") -> Candidate:
    # 순환 import 를 피한다 — 등록표는 게이트웨이 쪽에 있다.
    from vader.core.ocr import PROVIDERS, provider_factory

    limit = int(config.get(f"api.providers.{name}.daily_limit", 1400))
    # 한도는 키(연결)의 것이다 — 같은 키에 모델을 더해 한도를 비켜 가지 않게 함께 센다.
    conn = config.connection_of(name)
    used = sum(ledger.calls_on(provider=n) for n in (config.get("api.providers", {}) or {})
               if config.connection_of(n) == conn)
    factory = provider_factory(name)

    if factory is None:
        blocked = f"모르는 제공자입니다 (쓸 수 있는 것: {', '.join(sorted(PROVIDERS))})"
    elif role and not factory().supports(role):
        blocked = f"'{role}' 역할을 못 합니다 (문서를 못 읽습니다)"
    elif not factory().is_available():
        blocked = "패키지나 API 키가 없습니다"
    elif _failed_check(name):
        blocked = "연결 확인에 실패했습니다"
    elif role == "chat" and _failed_chat(name):
        blocked = "대화 확인에 실패했습니다"   # 로그인 AI — 문서 확인만 통과했다. 문서 읽기는 그대로 맡는다
    elif limit > 0 and used >= limit:
        blocked = f"오늘 한도를 다 썼습니다 ({used}/{limit})"
    else:
        blocked = None

    return Candidate(provider=name, calls_today=used, daily_limit=limit, blocked=blocked)
