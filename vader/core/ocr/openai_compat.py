"""OpenAI 호환 엔드포인트를 쓰는 어댑터.

NVIDIA NIM, Together, Groq, 로컬 vLLM 처럼 `/chat/completions` 를 그대로
흉내내는 곳이 많다. **하나를 붙이면 전부 붙는다** — 설정에 `base_url` 이
있는 제공자는 이 어댑터가 맡는다. 제공자를 더할 때 코드를 안 고쳐도 된다.

`openai` 패키지를 안 쓴다. 요청 하나가 JSON POST 한 번이라 stdlib 로
충분하고, 그러면 SDK 판올림에 끌려다니지 않는다. 코어의 `HttpGateway` 를
그대로 타므로 시간 제한·재시도·4xx 판정이 공짜로 붙는다.

**문서를 못 읽는다.** gpt-oss-120b 나 deepseek-v4-pro 는 텍스트 전용이라
`extract`(문서→값) 역할을 못 한다. 그래서 `supports()` 가 생겼다 — 배정
화면에서 미리 막지 않으면 새벽에 Job 이 죽어서야 알게 된다.
"""
from __future__ import annotations

import json
from pathlib import Path

from vader.core import config
from vader.core.http import HttpGateway
from vader.core.ocr.base import (
    ChatAnswer,
    OcrProviderUnavailable,
    OcrSchemaError,
    ToolCall,
)

#: 이 어댑터가 할 수 있는 역할. `extract` 는 문서를 읽어야 해서 빠진다.
TEXT_ROLES = {"infer", "chat", "command"}


class OpenAICompatProvider:
    def __init__(self, name: str, model: str | None = None) -> None:
        self._name = name
        self._model = model

    def name(self) -> str:
        return self._name

    def model(self) -> str:
        return str(self._model or config.get(f"api.providers.{self._name}.model", ""))

    def adapter_version(self) -> str:
        return "1"

    def supports(self, role: str) -> bool:
        return role in TEXT_ROLES

    def is_available(self) -> bool:
        try:
            return bool(self._base_url()) and bool(config.api_key(self._name))
        except ValueError:
            return False

    def extract(self, path: Path, prompt: str, schema: type) -> object:
        raise OcrProviderUnavailable(
            f"제공자 '{self._name}' 는 문서를 읽지 못합니다. "
            "extract 역할에는 문서를 읽는 제공자를 배정하세요."
        )

    def chat(self, messages: list[dict], tools: list[dict] | None = None):
        """대화. **캐시를 안 탄다** — 게이트웨이가 아니라 여기로 바로 온다.

        `tools` 를 주면 `ChatAnswer` 를 돌려준다. 모델이 도구를 부르겠다고
        하면 `tool_calls` 가 차고, **실행은 코어가 한다.**
        """
        if tools is None:
            return self._say(messages)
        choice = self._call(messages, tools=tools, temperature=0.7)
        message = choice.get("message") or {}
        return ChatAnswer(
            content=(message.get("content") or "").strip(),
            tool_calls=tuple(_tool_calls(message)),
            finish_reason=str(choice.get("finish_reason") or ""),
        )

    def complete(self, prompt: str, schema: type) -> object:
        if schema is not str:
            # 스키마를 강제하는 방식이 엔드포인트마다 다르다. 말로 시키고
            # 게이트웨이가 다시 검증한다 — 어차피 마지막 경계는 거기다.
            prompt = f"{prompt}\n\nJSON 만 출력하세요. 설명이나 코드펜스를 붙이지 마세요."

        text = self._say([{"role": "user", "content": prompt}], temperature=0)
        return text if schema is str else _json_body(text)

    def _call(self, messages, *, tools=None, temperature: float = 0.7) -> dict:
        body = {
            "model": self.model(),
            "messages": messages,
            "max_tokens": int(
                config.get(f"api.providers.{self._name}.max_tokens", 4096)
            ),
            "temperature": temperature,
        }
        if tools:
            body["tools"] = tools
        answer = self._gateway().post(
            f"{self._base_url().rstrip('/')}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {config.api_key(self._name)}"},
        )
        return _first_choice(answer.json())

    def _say(self, messages: list[dict], *, temperature: float = 0.7) -> str:
        choice = self._call(messages, temperature=temperature)
        text = (choice.get("message", {}).get("content") or "").strip()

        if choice.get("finish_reason") == "length":
            # 잘린 답을 그대로 쓰면 가장 나쁜 형태로 틀린다. Claude 에서
            # 같은 것을 막았고 이유도 같다.
            raise OcrSchemaError(
                f"'{self._name}' 응답이 길이 제한에서 잘렸습니다. "
                f"api.providers.{self._name}.max_tokens 를 올리세요."
            )
        if not text:
            raise OcrSchemaError(f"'{self._name}' 가 빈 답을 줬습니다")
        return text

    def _base_url(self) -> str:
        return str(config.get(f"api.providers.{self._name}.base_url", ""))

    def _gateway(self) -> HttpGateway:
        # dry_run 은 여기서 거짓이다. 연습 실행에서도 읽기는 한다 — 무엇을
        # 읽었는지 봐야 리허설이고, LLM 호출은 바깥을 안 바꾼다.
        return HttpGateway(dry_run=False)


def _first_choice(body: object) -> dict:
    choices = body.get("choices") if isinstance(body, dict) else None
    if not choices:
        raise OcrSchemaError(f"응답에 choices 가 없습니다: {str(body)[:200]}")
    return choices[0]


def _json_body(text: str) -> object:
    """텍스트에서 JSON 을 꺼낸다. 못 꺼내면 스키마 오류다.

    말로 시킨 스키마라 모델이 코드펜스를 붙이거나 앞뒤에 설명을 단다.
    실측이다 — deepseek 은 ```json 펜스를 붙였고, 다른 프롬프트에서는
    JSON 뒤에 한 줄이 더 붙어 통째로 파싱하다 깨졌다.

    **첫 여는 괄호부터 한 값만 읽는다.** 뒤에 뭐가 붙어 있든 앞의 값이 답이다.
    """
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    if start < 0:
        raise OcrSchemaError(f"응답에 JSON 이 없습니다: {text[:200]}")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:])
        return value
    except json.JSONDecodeError as exc:
        raise OcrSchemaError(f"응답이 JSON 이 아닙니다: {exc}") from exc


def _tool_calls(message: dict):
    """OpenAI 모양의 `tool_calls` 를 계약 타입으로 옮긴다.

    인자는 문자열 JSON 으로 온다. 못 읽으면 그 호출만 버린다 — 하나가
    깨졌다고 나머지를 못 쓰면 안 된다.
    """
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(arguments, dict):
            yield ToolCall(
                id=str(call.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=arguments,
            )
