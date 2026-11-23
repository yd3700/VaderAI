"""OpenAI 호환 엔드포인트 어댑터.

NVIDIA NIM 으로 실측했다(2026-08-06). 여기 있는 응답 모양은 지어낸 것이
아니라 그때 실제로 돌아온 것이다.
"""
from __future__ import annotations

import dataclasses
import json
import socket
from types import SimpleNamespace

import pytest

from vader.core import config
from vader.core.http import Response
from vader.core.ocr import OcrProviderUnavailable, OcrSchemaError, provider_factory
from vader.core.ocr.openai_compat import OpenAICompatProvider


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("실제 소켓을 열면 안 된다")

    monkeypatch.setattr(socket, "socket", forbidden)


def settings(path, providers: dict, roles: dict | None = None) -> None:
    path.write_text(
        json.dumps({"api": {"providers": providers, "roles": roles or {}}}),
        encoding="utf-8",
    )
    config.invalidate()


def answering(monkeypatch, content="", finish_reason="stop", sent=None):
    class Stub:
        def __init__(self, **kwargs):
            pass

        def post(self, url, *, json=None, headers=None):
            if sent is not None:
                sent.append(SimpleNamespace(url=url, json=json, headers=headers))
            return Response(
                status=200,
                text=jsonlib.dumps(
                    {
                        "choices": [
                            {
                                "message": {"content": content},
                                "finish_reason": finish_reason,
                            }
                        ]
                    }
                ),
            )

    import json as jsonlib

    monkeypatch.setattr("vader.core.ocr.openai_compat.HttpGateway", Stub)


def test_a_text_only_provider_cannot_take_the_document_role():
    """gpt-oss·deepseek 은 문서를 못 읽는다.

    배정 화면에서 미리 막지 않으면 **새벽에 Job 이 죽어서야** 알게 된다.
    """
    provider = OpenAICompatProvider("gptoss")

    assert provider.supports("infer") is True
    assert provider.supports("extract") is False
    with pytest.raises(OcrProviderUnavailable, match="문서를 읽지 못합니다"):
        provider.extract(__file__, "prompt", str)


def test_base_url_alone_registers_a_provider(isolated_settings):
    """제공자 하나 더 쓰려고 코드를 고치게 하면 그게 병목이다."""
    settings(isolated_settings, {"내회사": {"base_url": "https://llm.내회사.kr/v1"}})

    factory = provider_factory("내회사")

    assert factory is not None
    assert factory().name() == "내회사"
    assert provider_factory("들어본적없음") is None


def test_a_truncated_answer_is_refused(isolated_settings, monkeypatch):
    """잘린 답을 그대로 쓰면 가장 나쁜 형태로 틀린다. Claude 와 같은 이유다."""
    settings(isolated_settings, {"gptoss": {"base_url": "https://x/v1", "api_key": "k"}})
    answering(monkeypatch, content="앞부분만", finish_reason="length")

    with pytest.raises(OcrSchemaError, match="잘렸습니다"):
        OpenAICompatProvider("gptoss").complete("긴 답을 주세요", str)


def test_an_empty_answer_is_refused(isolated_settings, monkeypatch):
    settings(isolated_settings, {"gptoss": {"base_url": "https://x/v1", "api_key": "k"}})
    answering(monkeypatch, content="   ")

    with pytest.raises(OcrSchemaError, match="빈 답"):
        OpenAICompatProvider("gptoss").complete("무엇이든", str)


@pytest.mark.parametrize(
    "content",
    [
        '```json\n[{"품목명": "사과", "수량": 3}]\n```',
        '[{"품목명": "사과", "수량": 3}]\n\n위와 같이 정리했습니다.',
        '다음과 같습니다:\n[{"품목명": "사과", "수량": 3}]',
    ],
)
def test_json_survives_fences_and_chatter(isolated_settings, monkeypatch, content):
    """말로 시킨 스키마라 모델이 펜스를 붙이거나 설명을 단다. 셋 다 실측이다."""
    settings(isolated_settings, {"d": {"base_url": "https://x/v1", "api_key": "k"}})
    answering(monkeypatch, content=content)

    @dataclasses.dataclass
    class Row:
        품목명: str
        수량: int

    assert OpenAICompatProvider("d").complete("행으로", list[Row]) == [
        {"품목명": "사과", "수량": 3}
    ]


def test_a_plain_text_answer_is_not_parsed(isolated_settings, monkeypatch):
    """`schema=str` 은 EONM 3차 코드 추론이 쓰는 모양이다. 그대로 돌려준다."""
    settings(isolated_settings, {"d": {"base_url": "https://x/v1", "api_key": "k"}})
    answering(monkeypatch, content="  BA000869  ")

    assert OpenAICompatProvider("d").complete("어느 코드입니까", str) == "BA000869"


def test_the_key_rides_the_header_and_the_model_rides_the_body(
    isolated_settings, monkeypatch
):
    sent = []
    settings(
        isolated_settings,
        {"d": {"base_url": "https://x/v1/", "api_key": "비밀키", "model": "큰모델"}},
    )
    answering(monkeypatch, content="답", sent=sent)

    OpenAICompatProvider("d").complete("질문", str)

    assert sent[0].url == "https://x/v1/chat/completions"
    assert sent[0].headers["Authorization"] == "Bearer 비밀키"
    assert sent[0].json["model"] == "큰모델"
    # 프롬프트에 키가 섞이면 캐시 파일과 원장 어디로든 샌다.
    assert "비밀키" not in json.dumps(sent[0].json, ensure_ascii=False)


def test_a_role_override_beats_the_configured_model(isolated_settings, monkeypatch):
    sent = []
    settings(isolated_settings, {"d": {"base_url": "https://x/v1", "api_key": "k", "model": "큰모델"}})
    answering(monkeypatch, content="답", sent=sent)

    OpenAICompatProvider("d", model="싼모델").complete("질문", str)

    assert sent[0].json["model"] == "싼모델"


def test_the_assignment_screen_says_why_a_role_is_impossible(isolated_settings):
    """실패를 실행 시점이 아니라 배정 시점으로 당긴다."""
    from vader.core.ocr import candidates
    from vader.core.usage import UsageLedger

    settings(
        isolated_settings,
        {"gptoss": {"base_url": "https://x/v1", "api_key": "k"}},
        roles={"extract": {"providers": ["gptoss"]}},
    )

    looked = candidates("extract", ledger=UsageLedger(isolated_settings.parent))

    assert looked[0].usable is False
    assert "문서를 못 읽습니다" in looked[0].blocked
