"""OCR 게이트웨이를 네트워크 없이 검증한다."""
from __future__ import annotations

import dataclasses
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from vader.core.usage import Call, UsageLedger, now
from vader.core.ocr import (
    ClaudeProvider,
    FakeOcrProvider,
    GeminiProvider,
    OcrGateway,
    OcrProvider,
    OcrProviderError,
    OcrProviderUnavailable,
    OcrSchemaError,
)


def _fake_anthropic(monkeypatch, calls, tool_input):
    class Messages:
        def create(self, **kwargs):
            calls["create"] = kwargs
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use", name="submit_result", input=tool_input
                    )
                ]
            )

    client = SimpleNamespace(messages=Messages())
    anthropic = SimpleNamespace(Anthropic=lambda **kwargs: client)
    monkeypatch.setattr(
        "vader.core.ocr.claude._import_anthropic", lambda: anthropic
    )
    monkeypatch.setattr("vader.core.ocr.claude.config.api_key", lambda name: "key")
    return client


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network access is forbidden in OCR tests")

    monkeypatch.setattr(socket, "socket", forbidden)


def source(tmp_path: Path, content: bytes = b"document") -> Path:
    path = tmp_path / "input.pdf"
    path.write_bytes(content)
    return path


def gateway(
    tmp_path: Path,
    provider: FakeOcrProvider,
    *,
    log=None,
    sleep=lambda seconds: None,
    retry_max: int = 3,
    request_delay_seconds: float = 0,
    daily_limit: int = 1400,
    ledger: UsageLedger | None = None,
) -> OcrGateway:
    return OcrGateway(
        provider,
        ledger=ledger or UsageLedger(tmp_path / "usage"),
        cache_dir=tmp_path / "cache",
        cache_days=30,
        request_delay_seconds=request_delay_seconds,
        retry_max=retry_max,
        daily_limit=daily_limit,
        log=log,
        sleep=sleep,
    )


def ledger_of(tmp_path: Path) -> UsageLedger:
    return UsageLedger(tmp_path / "usage")


def test_schema_success_with_fake_provider(tmp_path):
    provider = FakeOcrProvider([{"rows": 2}])

    result = gateway(tmp_path, provider).extract(source(tmp_path), "prompt", dict)

    assert result == {"rows": 2}
    assert provider.calls[0][1:] == ("prompt", dict)


def test_schema_mismatch_is_clear(tmp_path):
    provider = FakeOcrProvider([["not", "a", "dict"]])

    with pytest.raises(OcrSchemaError, match="스키마"):
        gateway(tmp_path, provider).extract(source(tmp_path), "prompt", dict)


def test_provider_parsing_failure_stays_a_schema_error(tmp_path):
    provider = FakeOcrProvider([OcrSchemaError("JSON 파싱 실패")])

    with pytest.raises(OcrSchemaError, match="JSON 파싱 실패"):
        gateway(tmp_path, provider).extract(source(tmp_path), "prompt", dict)


def test_provider_failure_is_wrapped(tmp_path):
    provider = FakeOcrProvider([ValueError("provider exploded")])

    with pytest.raises(OcrProviderError, match="provider exploded"):
        gateway(tmp_path, provider).extract(source(tmp_path), "prompt", dict)


def test_content_cache_hit_skips_second_provider_call(tmp_path):
    provider = FakeOcrProvider([{"cached": True}])
    ocr = gateway(tmp_path, provider)
    first = source(tmp_path, b"same bytes")
    second = tmp_path / "renamed.pdf"
    second.write_bytes(b"same bytes")

    assert ocr.extract(first, "same prompt", dict) == {"cached": True}
    assert ocr.extract(second, "same prompt", dict) == {"cached": True}
    assert len(provider.calls) == 1


def test_retryable_failure_then_success(tmp_path):
    provider = FakeOcrProvider(
        [RuntimeError("503 overloaded"), {"recovered": True}]
    )
    sleeps: list[float] = []

    result = gateway(
        tmp_path,
        provider,
        sleep=sleeps.append,
    ).extract(source(tmp_path), "prompt", dict)

    assert result == {"recovered": True}
    assert len(provider.calls) == 2
    assert sleeps == [10]


def test_retry_exhaustion_raises_provider_error(tmp_path):
    provider = FakeOcrProvider(
        [
            RuntimeError("429 quota"),
            RuntimeError("429 quota"),
            RuntimeError("429 quota"),
        ]
    )

    with pytest.raises(OcrProviderError, match="429 quota"):
        gateway(tmp_path, provider).extract(source(tmp_path), "prompt", dict)

    assert len(provider.calls) == 3


def test_a_spent_daily_quota_is_not_retried(tmp_path):
    """하루 한도는 20초 뒤에도 그대로다. 다시 부르면 실패만 쌓인다."""
    provider = FakeOcrProvider(
        [RuntimeError("429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier")]
    )
    sleeps: list[float] = []

    with pytest.raises(OcrProviderError, match="하루 한도"):
        gateway(tmp_path, provider, sleep=sleeps.append).extract(
            source(tmp_path), "prompt", dict
        )

    assert len(provider.calls) == 1
    assert sleeps == []


def test_the_server_retry_delay_is_respected(tmp_path):
    provider = FakeOcrProvider(
        [RuntimeError("429 RESOURCE_EXHAUSTED {'retryDelay': '37s'}"), {"ok": True}]
    )
    sleeps: list[float] = []

    gateway(tmp_path, provider, sleep=sleeps.append).extract(
        source(tmp_path), "prompt", dict
    )

    assert sleeps == [37.0]


def test_request_delay_is_applied_before_provider_call(tmp_path):
    provider = FakeOcrProvider([{"ok": True}])
    sleeps: list[float] = []

    gateway(
        tmp_path,
        provider,
        request_delay_seconds=2.5,
        sleep=sleeps.append,
    ).extract(source(tmp_path), "prompt", dict)

    assert sleeps == [2.5]


def test_daily_limit_warning_uses_log_not_stdout(tmp_path, capsys):
    ledger = ledger_of(tmp_path)
    for _ in range(4):
        ledger.record(
            Call(at=now(), provider="fake", model=None, role="extract", ok=True)
        )
    provider = FakeOcrProvider([{"ok": True}])
    logs = []

    def log(level, message, **fields):
        logs.append((level, message, fields))

    gateway(
        tmp_path,
        provider,
        daily_limit=5,
        ledger=ledger,
        log=log,
    ).extract(source(tmp_path), "prompt", dict)

    assert any(
        level == "warning" and "80%" in message
        for level, message, fields in logs
    )
    assert capsys.readouterr().out == ""


def test_fake_provider_path_never_opens_a_socket(tmp_path):
    provider = FakeOcrProvider([{"offline": True}])

    assert gateway(tmp_path, provider).extract(
        source(tmp_path), "prompt", dict
    ) == {"offline": True}


def test_unavailable_provider_has_a_clear_error(tmp_path):
    provider = FakeOcrProvider([], available=False, provider_name="missing")

    with pytest.raises(OcrProviderUnavailable, match="missing"):
        gateway(tmp_path, provider).extract(source(tmp_path), "prompt", dict)


def test_gemini_is_unavailable_without_optional_package(monkeypatch):
    monkeypatch.setattr("vader.core.ocr.gemini._import_genai", lambda: None)

    assert GeminiProvider().is_available() is False


def test_gateway_explains_missing_gemini_package(tmp_path, monkeypatch):
    monkeypatch.setattr("vader.core.ocr.gemini._import_genai", lambda: None)
    ocr = OcrGateway(
        GeminiProvider(),
        cache_dir=tmp_path / "cache",
        cache_days=30,
        request_delay_seconds=0,
        retry_max=3,
        daily_limit=1400,
    )

    with pytest.raises(OcrProviderUnavailable, match="gemini"):
        ocr.extract(source(tmp_path), "prompt", dict)


def test_gemini_uploads_ascii_temp_and_passes_prompt_and_schema(
    tmp_path, monkeypatch
):
    calls = {}

    class Files:
        def upload(self, *, file):
            upload_path = Path(file)
            calls["upload_name"] = upload_path.name
            calls["upload_exists"] = upload_path.exists()
            return SimpleNamespace(name="uploaded-file")

        def delete(self, *, name):
            calls["deleted"] = name

    class Models:
        def generate_content(self, **kwargs):
            calls["generate"] = kwargs
            return SimpleNamespace(parsed={"ok": True}, text="")

    client = SimpleNamespace(files=Files(), models=Models())
    genai = SimpleNamespace(Client=lambda **kwargs: client)
    monkeypatch.setattr(
        "vader.core.ocr.gemini._import_genai",
        lambda: genai,
    )
    monkeypatch.setattr("vader.core.ocr.gemini.config.api_key", lambda name: "key")
    monkeypatch.setattr(
        "vader.core.ocr.gemini.config.get",
        lambda key, default=None: "test-model",
    )
    path = tmp_path / "한글 명세서.pdf"
    path.write_bytes(b"pdf")

    provider = GeminiProvider()
    monkeypatch.setattr(provider, "model", lambda: "contract-model")
    result = provider.extract(path, "룬 프롬프트", dict)

    assert result == {"ok": True}
    assert calls["upload_exists"] is True
    assert calls["upload_name"].isascii()
    assert calls["generate"]["contents"][0] == "룬 프롬프트"
    assert calls["generate"]["model"] == "contract-model"
    assert calls["generate"]["config"]["response_schema"] is dict
    assert calls["deleted"] == "uploaded-file"


def test_claude_is_unavailable_without_optional_package(monkeypatch):
    monkeypatch.setattr("vader.core.ocr.claude._import_anthropic", lambda: None)

    assert ClaudeProvider().is_available() is False


def test_claude_is_available_with_package_and_key(monkeypatch):
    monkeypatch.setattr(
        "vader.core.ocr.claude._import_anthropic", lambda: SimpleNamespace()
    )
    monkeypatch.setattr("vader.core.ocr.claude.config.api_key", lambda name: "key")

    assert ClaudeProvider().is_available() is True


@pytest.mark.parametrize(
    ("suffix", "media_type", "block_type"),
    [
        (".pdf", "application/pdf", "document"),
        (".png", "image/png", "image"),
    ],
)
def test_claude_sends_pdf_and_image_as_base64_blocks(
    tmp_path, monkeypatch, suffix, media_type, block_type
):
    calls = {}
    _fake_anthropic(monkeypatch, calls, {"result": [{"ok": True}]})
    path = tmp_path / f"한글 명세서{suffix}"
    path.write_bytes(b"file bytes")

    provider = ClaudeProvider(model="contract-model")
    result = provider.extract(path, "룬 프롬프트", list)

    assert result == [{"ok": True}]
    request = calls["create"]
    assert request["model"] == "contract-model"
    content = request["messages"][0]["content"]
    assert content[0] == {
        "type": block_type,
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": "ZmlsZSBieXRlcw==",
        },
    }
    assert content[1] == {"type": "text", "text": "룬 프롬프트"}
    tool = request["tools"][0]
    assert tool["input_schema"]["properties"]["result"]["type"] == "array"
    assert request["tool_choice"] == {"type": "tool", "name": "submit_result"}


def test_claude_complete_uses_a_tool_and_unwraps_plain_text(monkeypatch):
    calls = {}
    _fake_anthropic(monkeypatch, calls, {"result": "BB000021"})

    result = ClaudeProvider().complete("어느 코드입니까", str)

    assert result == "BB000021"
    request = calls["create"]
    assert request["messages"] == [
        {"role": "user", "content": "어느 코드입니까"}
    ]
    assert request["tools"][0]["input_schema"]["properties"]["result"] == {
        "type": "string"
    }


def test_claude_dict_schema_uses_the_tool_input_directly(monkeypatch):
    calls = {}
    _fake_anthropic(monkeypatch, calls, {"ok": True})

    assert ClaudeProvider().complete("판단", dict) == {"ok": True}
    assert calls["create"]["tools"][0]["input_schema"] == {"type": "object"}


def test_claude_missing_tool_result_is_a_schema_error(monkeypatch):
    class Messages:
        def create(self, **kwargs):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="no tool")])

    anthropic = SimpleNamespace(
        Anthropic=lambda **kwargs: SimpleNamespace(messages=Messages())
    )
    monkeypatch.setattr(
        "vader.core.ocr.claude._import_anthropic", lambda: anthropic
    )
    monkeypatch.setattr("vader.core.ocr.claude.config.api_key", lambda name: "key")

    with pytest.raises(OcrSchemaError, match="submit_result"):
        ClaudeProvider().complete("판단", dict)


def test_claude_rejects_files_outside_the_document_and_image_contract(tmp_path):
    path = tmp_path / "input.txt"
    path.write_text("not a supported document", encoding="utf-8")

    with pytest.raises(ValueError, match="지원하지 않는 파일 형식"):
        ClaudeProvider().extract(path, "프롬프트", dict)


def test_claude_is_registered():
    from vader.core import ocr

    assert ocr.PROVIDERS["claude"] is ClaudeProvider


def test_claude_optional_dependency_is_declared():
    import tomllib

    project = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]

    assert project["optional-dependencies"]["claude"] == ["anthropic>=0.49.0"]


def test_the_template_ships_no_ai_connection():
    """새 PC 는 아무 AI 에도 연결되지 않은 채 시작한다 — 은퇴한 모델·자리표시 키가 기본 AI 로 뜨던 문제 (2026-10-21)."""
    from vader.core import config

    template = json.loads(config.TEMPLATE_PATH.read_text(encoding="utf-8"))

    assert template["api"]["providers"] == {} and "roles" not in template["api"]


def test_claude_model_override_reaches_the_request(monkeypatch):
    calls = {}
    _fake_anthropic(monkeypatch, calls, {"ok": True})
    monkeypatch.setattr(
        "vader.core.ocr.claude.config.get",
        lambda key, default=None: "configured-model" if "model" in key else default,
    )

    assert ClaudeProvider().model() == "configured-model"
    ClaudeProvider(model="role-model").complete("판단", dict)
    assert calls["create"]["model"] == "role-model"


def _fake_genai(monkeypatch, calls):
    class Models:
        def generate_content(self, **kwargs):
            calls["generate"] = kwargs
            return SimpleNamespace(parsed=None, text=" BB000021 ")

    client = SimpleNamespace(models=Models())
    monkeypatch.setattr(
        "vader.core.ocr.gemini._import_genai",
        lambda: SimpleNamespace(Client=lambda **kwargs: client),
    )
    monkeypatch.setattr("vader.core.ocr.gemini.config.api_key", lambda name: "key")
    monkeypatch.setattr(
        "vader.core.ocr.gemini.config.get",
        lambda key, default=None: "test-model",
    )


def test_every_provider_satisfies_the_protocol():
    """`complete()` 를 프로토콜에 넣고 실제 제공자에 안 넣은 일이 있었다.

    가짜 제공자만 쓰는 테스트는 그것을 잡지 못한다. 실행 시점에 AttributeError 가
    되고 게이트웨이가 그것을 OcrProviderError 로 감싸 이유가 흐려진다.
    """
    assert isinstance(GeminiProvider(), OcrProvider)
    assert isinstance(FakeOcrProvider([{}]), OcrProvider)


def test_gemini_complete_with_str_schema_does_not_force_json(monkeypatch):
    """값 하나만 받는 프롬프트에 JSON 스키마를 씌우면 답이 달라진다.

    EONM 의 `ai_match` 가 "순수 텍스트만 출력하세요" 로 묻는다. 이식 중 동작
    변경은 원칙 위반이다.
    """
    calls = {}
    _fake_genai(monkeypatch, calls)

    provider = GeminiProvider()
    monkeypatch.setattr(provider, "model", lambda: "contract-model")
    result = provider.complete("어느 코드입니까", str)

    assert result == "BB000021"
    assert calls["generate"]["model"] == "contract-model"
    assert calls["generate"]["contents"] == "어느 코드입니까"
    assert "config" not in calls["generate"]


def test_gemini_complete_with_a_real_schema_asks_for_json(monkeypatch):
    calls = {}
    _fake_genai(monkeypatch, calls)
    monkeypatch.setattr(
        "vader.core.ocr.gemini._parse_json_response", lambda response: {"ok": True}
    )

    assert GeminiProvider().complete("물어본다", dict) == {"ok": True}
    assert calls["generate"]["config"]["response_schema"] is dict


def test_infer_caches_a_plain_string_result(tmp_path):
    """문자열도 캐시를 왕복해야 한다 — json.dumps/loads 를 지난다."""
    provider = FakeOcrProvider(["BA000819"])
    ocr = OcrGateway(
        provider,
        cache_dir=tmp_path / "cache",
        cache_days=30,
        request_delay_seconds=0,
        retry_max=3,
        daily_limit=1400,
    )

    assert ocr.complete("후보 20개 중 어느 것", str) == "BA000819"
    # 두 번째는 제공자를 부르지 않는다. 응답이 하나뿐이라 불렀으면 터진다.
    assert ocr.complete("후보 20개 중 어느 것", str) == "BA000819"


# --- 사용량 원장 -------------------------------------------------------------
#
# 전에는 "오늘 만들어진 캐시 파일 수" 로 호출을 셌다. 아래 넷이 그 방식이
# 놓치던 것이고, 그래서 일일 한도 경고가 늦게 울렸다.


def test_a_retry_counts_as_two_calls_not_one(tmp_path):
    """재시도는 캐시 파일을 하나만 남기지만 할당량은 두 번 쓴다."""
    provider = FakeOcrProvider([RuntimeError("503 overloaded"), {"ok": True}])
    ledger = ledger_of(tmp_path)

    gateway(tmp_path, provider, ledger=ledger).extract(
        source(tmp_path), "prompt", dict
    )

    assert [(call.ok, call.error) for call in ledger.read()] == [
        (False, "RuntimeError"),
        (True, None),
    ]
    assert ledger.calls_on() == 2


def test_a_failed_call_is_still_a_call(tmp_path):
    """끝내 실패하면 캐시 파일이 안 생긴다. 할당량은 그래도 썼다."""
    provider = FakeOcrProvider([RuntimeError("boom")])
    ledger = ledger_of(tmp_path)

    with pytest.raises(OcrProviderError):
        gateway(tmp_path, provider, ledger=ledger).extract(
            source(tmp_path), "prompt", dict
        )

    assert ledger.calls_on() == 1


def test_a_cache_hit_is_recorded_but_does_not_spend_quota(tmp_path):
    provider = FakeOcrProvider([{"ok": True}])
    ledger = ledger_of(tmp_path)
    document = source(tmp_path)

    for _ in range(2):
        gateway(tmp_path, provider, ledger=ledger).extract(document, "prompt", dict)

    assert [call.cached for call in ledger.read()] == [False, True]
    assert ledger.calls_on() == 1


def test_the_role_separates_extract_from_infer(tmp_path):
    """Phase 8-A 가 역할별로 제공자를 고르려면 역할이 원장에 남아야 한다."""
    ledger = ledger_of(tmp_path)
    engine = gateway(tmp_path, FakeOcrProvider([{"a": 1}, {"b": 2}]), ledger=ledger)

    engine.extract(source(tmp_path), "prompt", dict)
    engine.complete("prompt", dict)

    assert [call.role for call in ledger.read()] == ["extract", "infer"]


def test_the_model_is_recorded_so_cost_can_be_attributed(tmp_path):
    ledger = ledger_of(tmp_path)

    provider = FakeOcrProvider([{"ok": True}], model_name="flash-9")
    gateway(tmp_path, provider, ledger=ledger).extract(source(tmp_path), "prompt", dict)

    assert ledger.read()[0].model == "flash-9"


def test_a_broken_line_does_not_hide_the_rest(tmp_path):
    """죽는 순간 반쯤 쓰인 줄 하나가 원장 전체를 못 읽게 만들면 안 된다."""
    ledger = ledger_of(tmp_path)
    ledger.record(Call(at=now(), provider="fake", model=None, role="infer", ok=True))
    path = next((tmp_path / "usage").glob("*.jsonl"))
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"at": "2026-08-05T00:00:0')

    assert ledger.calls_on() == 1


def test_the_ledger_never_breaks_the_call_it_records(tmp_path):
    """원장을 못 쓰는 것이 OCR 성공을 실패로 바꾸면 안 된다."""
    ledger = ledger_of(tmp_path)
    (tmp_path / "usage").write_text("이 자리에 폴더를 못 만든다", encoding="utf-8")

    assert gateway(tmp_path, FakeOcrProvider([{"ok": True}]), ledger=ledger).extract(
        source(tmp_path), "prompt", dict
    ) == {"ok": True}


# --- 역할 → 제공자 배정 -------------------------------------------------------
#
# 전에는 `create_gateway` 안의 `if provider_name == "gemini"` 하나였다.
# 이제 역할(extract·infer)마다 다른 제공자를 태울 수 있다. 배정을 안 하면
# 전과 똑같이 돈다 — 그것이 아래 첫 테스트다.


def settings(path, monkeypatch, data: dict) -> None:
    from vader.core import config

    path.write_text(json.dumps(data), encoding="utf-8")
    config.invalidate()
    monkeypatch.setattr(config, "_cache", None)


@pytest.fixture
def two_providers(monkeypatch):
    from vader.core import ocr

    made = []

    def factory(name):
        def make(model=None):
            made.append(name if model is None else f"{name}/{model}")
            return FakeOcrProvider([{"ok": True}], provider_name=name)

        return make

    monkeypatch.setattr(
        ocr, "PROVIDERS", {"gemini": factory("gemini"), "claude": factory("claude")}
    )
    return made


def test_without_an_assignment_every_role_rides_the_global_provider(
    isolated_settings, monkeypatch, two_providers
):
    from vader.core import ocr

    settings(isolated_settings, monkeypatch, {"api": {"ocr_provider": "claude"}})

    ocr.create_gateway("extract")
    ocr.create_gateway("infer")

    assert two_providers == ["claude", "claude"]


def test_assigning_one_role_leaves_the_other_alone(
    isolated_settings, monkeypatch, two_providers
):
    """절약의 형태가 이것이다 — 추출은 그대로 두고 추론만 싼 쪽으로 옮긴다."""
    from vader.core import ocr

    settings(
        isolated_settings,
        monkeypatch,
        {"api": {"ocr_provider": "gemini", "roles": {"infer": {"provider": "claude"}}}},
    )

    ocr.create_gateway("extract")
    ocr.create_gateway("infer")

    assert two_providers == ["gemini", "claude"]


def test_an_unknown_provider_names_what_is_available(
    isolated_settings, monkeypatch, two_providers
):
    from vader.core import ocr

    settings(
        isolated_settings,
        monkeypatch,
        {"api": {"roles": {"infer": {"provider": "오타"}}}},
    )

    with pytest.raises(OcrProviderUnavailable, match="claude, gemini"):
        ocr.create_gateway("infer")


def test_ctx_ocr_and_ctx_infer_ride_their_own_roles(
    isolated_settings, monkeypatch, two_providers, tmp_path
):
    """룬은 역할을 모른다. `ctx.ocr` 이 extract, `ctx.infer` 가 infer 다."""
    from vader.core import config, ocr

    settings(
        isolated_settings,
        monkeypatch,
        {
            "api": {
                "ocr_provider": "gemini",
                "roles": {"infer": {"provider": "claude"}},
                "request_delay_seconds": 0,
            },
            "processing": {"data_dir": str(tmp_path)},
        },
    )

    ocr.extract(source(tmp_path), "prompt", dict)
    ocr.infer("prompt", dict)

    assert two_providers == ["gemini", "claude"]


def test_the_dead_fallback_setting_is_gone_from_the_template():
    """읽는 코드가 없는 설정은 지운다.

    제공자마다 OCR 결과가 달라 자동 fallback 은 "가용성 향상" 이 아니라
    조용한 데이터 변경이다. 한도에 닿으면 사람이 배정을 바꾼다.
    """
    from vader.core import config

    template = json.loads(config.TEMPLATE_PATH.read_text(encoding="utf-8"))

    assert "fallback_provider" not in template["api"]


# --- codex 리뷰가 찾은 것 (2026-08-05) ---------------------------------------


def test_an_unreadable_ledger_does_not_stop_the_work(tmp_path, monkeypatch):
    """원장을 못 읽는 것이 OCR 자체를 멈추면 안 된다.

    `calls_on` 은 실제 호출 직전의 한도 경고에서 불린다. 여기서 예외가 나면
    잠긴 파일이나 권한 하나가 업무를 세운다.
    """
    ledger = ledger_of(tmp_path)
    ledger.record(Call(at=now(), provider="fake", model=None, role="extract", ok=True))
    path = next((tmp_path / "usage").glob("*.jsonl"))

    def locked(*args, **kwargs):
        raise PermissionError("다른 프로세스가 잡고 있다")

    monkeypatch.setattr(Path, "read_text", locked)

    assert gateway(tmp_path, FakeOcrProvider([{"ok": True}]), ledger=ledger).extract(
        source(tmp_path), "prompt", dict
    ) == {"ok": True}
    assert path.exists()


def test_two_gateways_share_one_write_lock(tmp_path):
    """게이트웨이는 호출마다 새로 만들어진다 — 원장 객체도 새로 생긴다.

    락이 인스턴스에 있으면 서로 다른 락을 잡고 같은 파일에 쓴다. 잠근 것처럼
    보이지만 아무것도 안 잠근다.
    """
    from vader.core import usage

    first = ledger_of(tmp_path)
    second = ledger_of(tmp_path)

    assert usage._WRITE_LOCK is not None
    assert not hasattr(first, "_lock"), "락이 인스턴스에 있으면 공유되지 않는다"

    for owner in (first, second):
        owner.record(
            Call(at=now(), provider="fake", model=None, role="infer", ok=True)
        )

    assert first.calls_on() == 2


def test_a_torn_last_line_does_not_swallow_the_next_record(tmp_path):
    """줄바꿈 없이 끊긴 줄 뒤에 그냥 붙이면 새 줄까지 같이 깨진다."""
    ledger = ledger_of(tmp_path)
    ledger.record(Call(at=now(), provider="fake", model=None, role="infer", ok=True))
    path = next((tmp_path / "usage").glob("*.jsonl"))
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"at": "2026-08-05T00:00:0')

    ledger.record(Call(at=now(), provider="fake", model=None, role="infer", ok=True))

    assert ledger.calls_on() == 2


def test_a_schema_failure_is_a_failed_call_wherever_it_is_caught(tmp_path):
    """제공자가 던지든 게이트웨이가 걸러내든 같은 사건이다.

    통신은 성공했지만 할당량은 썼고 얻은 것은 없다. 원장에서 두 가지로
    세어지면 어느 제공자가 쓸모없는 응답을 주는지 알 수 없다.
    """
    from_provider = ledger_of(tmp_path / "a")
    from_gateway = ledger_of(tmp_path / "b")

    with pytest.raises(OcrSchemaError):
        gateway(
            tmp_path,
            FakeOcrProvider([OcrSchemaError("제공자가 던졌다")]),
            ledger=from_provider,
        ).extract(source(tmp_path), "prompt", dict)

    with pytest.raises(OcrSchemaError):
        gateway(
            tmp_path,
            FakeOcrProvider([["게이트웨이가 걸러낸다"]]),
            ledger=from_gateway,
        ).extract(source(tmp_path), "prompt", dict)

    def shape(ledger):
        # `at` 은 뺀다. 두 호출이 다른 초에 걸리면 값이 갈리는데, 이 테스트가
        # 보는 것은 시각이 아니라 **같은 사건인가** 다.
        return [(c.role, c.ok, c.error, c.cached) for c in ledger.read()]

    assert shape(from_provider) == [("extract", False, "OcrSchemaError", False)]
    assert shape(from_gateway) == shape(from_provider)


def test_a_failed_ledger_write_reaches_the_log(tmp_path):
    """못 센 호출은 한도 경고를 늦게 울린다. 조용히 넘기지 않는다."""
    ledger = ledger_of(tmp_path)
    (tmp_path / "usage").write_text("이 자리에 폴더를 못 만든다", encoding="utf-8")
    logs = []

    gateway(
        tmp_path,
        FakeOcrProvider([{"ok": True}]),
        ledger=ledger,
        log=lambda level, message, **fields: logs.append((level, message)),
    ).extract(source(tmp_path), "prompt", dict)

    assert ("warning", "사용량 원장 기록 실패") in logs


def test_yesterday_is_not_today(tmp_path):
    from datetime import date, timedelta

    ledger = ledger_of(tmp_path)
    ledger.record(Call(at=now(), provider="fake", model=None, role="infer", ok=True))

    assert ledger.calls_on() == 1
    assert ledger.calls_on(date.today() - timedelta(days=1)) == 0


def test_a_role_can_ride_a_cheaper_model_on_the_same_provider(
    isolated_settings, monkeypatch, two_providers
):
    """가장 흔한 절약 형태다 — 제공자는 그대로 두고 모델만 내린다."""
    from vader.core import ocr

    settings(
        isolated_settings,
        monkeypatch,
        {
            "api": {
                "ocr_provider": "gemini",
                "roles": {"infer": {"model": "gemini-2.5-flash-lite"}},
            }
        },
    )

    ocr.create_gateway("extract")
    ocr.create_gateway("infer")

    assert two_providers == ["gemini", "gemini/gemini-2.5-flash-lite"]


def test_the_override_reaches_the_request_not_just_the_ledger(isolated_settings, monkeypatch):
    """모델을 덮어썼는데 실제 요청이 옛 모델로 나가면 캐시 신원이 거짓말을 한다."""
    from vader.core import config
    from vader.core.ocr.gemini import GeminiProvider

    settings(isolated_settings, monkeypatch, {"api": {"providers": {"gemini": {"model": "big"}}}})

    assert GeminiProvider().model() == "big"
    assert GeminiProvider(model="small").model() == "small"


def test_claude_refuses_a_truncated_answer(monkeypatch):
    """행이 잘린 채 문법에 맞는 리스트가 오면 아무도 못 잡는다.

    **덜 들어간 입고**는 안 들어간 입고보다 찾기 어렵다. 길이 제한에 걸린
    응답은 버린다.
    """
    class Messages:
        def create(self, **kwargs):
            return SimpleNamespace(
                stop_reason="max_tokens",
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        name="submit_result",
                        input={"result": [{"품목": "앞의 세 줄만"}]},
                    )
                ],
            )

    client = SimpleNamespace(messages=Messages())
    monkeypatch.setattr(
        "vader.core.ocr.claude._import_anthropic",
        lambda: SimpleNamespace(Anthropic=lambda **kwargs: client),
    )
    monkeypatch.setattr("vader.core.ocr.claude.config.api_key", lambda name: "key")

    with pytest.raises(OcrSchemaError, match="잘렸습니다"):
        ClaudeProvider().complete("긴 표를 읽어라", list)


def test_claude_token_ceiling_is_configurable(monkeypatch):
    calls = {}
    _fake_anthropic(monkeypatch, calls, {"result": []})
    monkeypatch.setattr(
        "vader.core.ocr.claude.config.get",
        lambda key, default=None: 30000 if "max_tokens" in key else default,
    )

    ClaudeProvider(model="m").complete("prompt", list)

    assert calls["create"]["max_tokens"] == 30000


# --- 행 스키마 ---------------------------------------------------------------
#
# 전에는 두 룬이 `schema=list` 를 넘겼고 `_coerce_schema` 는 "리스트인가" 만
# 봤다. **행 안은 아무도 안 봤다** — 제공자가 필드 이름을 바꿔 보내도,
# 통째로 빠뜨려도 통과했다. 빠진 quantity 는 뒤에서 조용히 0 이 되고,
# 그건 덜 들어간 입고다.


@dataclasses.dataclass
class _Row:
    code: str
    quantity: int


def test_a_row_missing_a_field_is_rejected(tmp_path):
    provider = FakeOcrProvider([[{"code": "E2-011-331", "quantity": 3}, {"code": "C6"}]])

    with pytest.raises(OcrSchemaError, match="quantity"):
        gateway(tmp_path, provider).extract(source(tmp_path), "prompt", list[_Row])


def test_the_rejection_says_which_row(tmp_path):
    """스무 행짜리 명세서에서 '어딘가 틀렸다' 는 쓸모가 없다."""
    provider = FakeOcrProvider(
        [[{"code": "A", "quantity": 1}, {"code": "B", "quantity": 2}, {"code": "C"}]]
    )

    with pytest.raises(OcrSchemaError, match="3번째 행"):
        gateway(tmp_path, provider).extract(source(tmp_path), "prompt", list[_Row])


def test_rows_come_back_as_dicts_not_models(tmp_path):
    """룬이 행을 제자리에서 고친다 — `data["mapped_code"] = ...`.

    모델 객체로 바꿔 주면 뒤의 버킷팅·합산이 전부 깨진다. 모델은 제공자에게
    줄 스키마와 여기서 할 검사에만 쓴다.
    """
    rows = [{"code": "A", "quantity": 1}]
    provider = FakeOcrProvider([rows])

    result = gateway(tmp_path, provider).extract(
        source(tmp_path), "prompt", list[_Row]
    )

    assert result == rows
    assert isinstance(result[0], dict)


def test_extra_fields_are_left_alone(tmp_path):
    """모르는 필드를 버리면 제공자가 더 준 것을 룬이 못 본다."""
    provider = FakeOcrProvider([[{"code": "A", "quantity": 1, "덤": "여분"}]])

    result = gateway(tmp_path, provider).extract(
        source(tmp_path), "prompt", list[_Row]
    )

    assert result[0]["덤"] == "여분"


def test_a_bare_list_still_means_no_row_check(tmp_path):
    """행 모델을 안 준 룬까지 막지는 않는다. 계약을 바꾸는 것은 룬의 선택이다."""
    provider = FakeOcrProvider([[{"뭐든": 1}]])

    assert gateway(tmp_path, provider).extract(
        source(tmp_path), "prompt", list
    ) == [{"뭐든": 1}]


def test_a_non_list_answer_to_a_row_schema_is_rejected(tmp_path):
    provider = FakeOcrProvider([{"code": "A", "quantity": 1}])

    with pytest.raises(OcrSchemaError, match="배열"):
        gateway(tmp_path, provider).extract(source(tmp_path), "prompt", list[_Row])


def test_both_providers_get_the_same_row_fields(tmp_path, monkeypatch):
    """같은 룬이 제공자에 따라 다른 것을 받으면 안 된다.

    Claude 는 `list[Row]` 에서 `items: {}` 를 만들어 **행 스키마를 잃었다.**
    Gemini 는 같은 타입에서 필드와 required 까지 다 만들어 낸다. 그 차이가
    바로 이 단계가 없애려던 것이다.
    """
    calls = {}
    _fake_anthropic(monkeypatch, calls, {"result": []})

    ClaudeProvider(model="m").complete("prompt", list[_Row])

    rows = calls["create"]["tools"][0]["input_schema"]["properties"]["result"]
    assert rows["type"] == "array"
    assert sorted(rows["items"]["properties"]) == ["code", "quantity"]
    assert sorted(rows["items"]["required"]) == ["code", "quantity"]

    genai = pytest.importorskip("google.genai")
    schema = genai._transformers.t_schema(None, list[_Row])
    assert sorted(schema.items.properties) == ["code", "quantity"]
    assert sorted(schema.items.required) == ["code", "quantity"]


def test_a_provider_that_parses_rows_into_models_still_yields_dicts(tmp_path):
    """**제공자가 모델 객체로 파싱해 주기도 한다.**

    google-genai 는 `response_schema` 로 dataclass 를 주면 응답을 그 타입으로
    만들어 돌려준다. 룬은 dict 를 기대한다(행을 제자리에서 고친다).

    이걸 놓쳐서 **실제 Gemini 경로가 깨졌다.** 가짜 응답과 JSON 을 직접
    파싱하는 제공자로만 시험했더니 안 보였고, 마스터 실행에서 `1번째 행이
    객체가 아닙니다: Row` 로 터졌다. 스키마를 바꾸면 제공자마다 응답 모양이
    달라진다.
    """
    provider = FakeOcrProvider([[_Row(code="A", quantity=1)]])

    result = gateway(tmp_path, provider).extract(
        source(tmp_path), "prompt", list[_Row]
    )

    assert result == [{"code": "A", "quantity": 1}]
    assert isinstance(result[0], dict)


def test_an_optional_field_may_be_missing(tmp_path):
    """기본값이 있으면 없어도 된다. 없다고 파일 전체를 실패시키면 안 된다."""

    @dataclasses.dataclass
    class _WithDefault:
        code: str
        total: int = 0

    provider = FakeOcrProvider([[{"code": "A"}]])

    assert gateway(tmp_path, provider).extract(
        source(tmp_path), "prompt", list[_WithDefault]
    ) == [{"code": "A"}]
