"""선택적 anthropic 패키지를 사용하는 Claude OCR 어댑터."""
from __future__ import annotations

import base64
import dataclasses
from pathlib import Path
from types import ModuleType
from typing import Any, get_args, get_origin, get_type_hints

from vader.core import config
from vader.core.ocr.base import OcrProviderUnavailable, OcrSchemaError

_RESULT_TOOL = "submit_result"
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class ClaudeProvider:
    def __init__(self, model: str | None = None) -> None:
        #: 역할이 덮어쓴 모델. 없으면 제공자 기본값을 쓴다.
        self._model = model

    def name(self) -> str:
        return "claude"

    def model(self) -> str:
        return str(
            self._model
            or config.get("api.providers.claude.model", "claude-haiku-4-5")
        )

    def adapter_version(self) -> str:
        """요청 구성·응답 스키마 전략이 바뀌면 올리는 버전."""
        return "1"

    def supports(self, role: str) -> bool:
        """문서도 텍스트도 읽는다."""
        return True

    def is_available(self) -> bool:
        try:
            return _import_anthropic() is not None and bool(config.api_key("claude"))
        except (ImportError, ModuleNotFoundError, ValueError):
            return False

    def extract(self, path: Path, prompt: str, schema: type) -> object:
        path = Path(path)
        media_type = _media_type(path)
        file_block = {
            "type": "document" if media_type == "application/pdf" else "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            },
        }
        return self._request(
            [file_block, {"type": "text", "text": prompt}], schema
        )

    def chat(self, messages: list[dict]) -> str:
        """대화. system 은 별도 인자로 빠진다 — Claude 의 모양이다."""
        anthropic = _import_anthropic()
        if anthropic is None:
            raise OcrProviderUnavailable(
                "Claude 대화에는 선택적 패키지 'anthropic'이 필요합니다"
            )
        system = " ".join(
            m["content"] for m in messages if m.get("role") == "system"
        )
        turns = [m for m in messages if m.get("role") != "system"]

        client = anthropic.Anthropic(api_key=config.api_key("claude"))
        response = client.messages.create(
            model=self.model(),
            max_tokens=int(config.get("api.providers.claude.max_tokens", 8192)),
            **({"system": system} if system else {}),
            messages=turns,
        )
        _reject_truncated(response)
        return "".join(
            getattr(block, "text", "") or ""
            for block in getattr(response, "content", [])
        ).strip()

    def complete(self, prompt: str, schema: type) -> object:
        """파일 없이 텍스트만 보내고 tool 입력으로 결과 스키마를 강제한다."""
        return self._request(prompt, schema)

    def _request(self, content: object, schema: type) -> object:
        anthropic = _import_anthropic()
        if anthropic is None:
            raise OcrProviderUnavailable(
                "Claude OCR에는 선택적 패키지 'anthropic'이 필요합니다"
            )

        tool, unwrap = _result_tool(schema)
        client = anthropic.Anthropic(api_key=config.api_key("claude"))
        response = client.messages.create(
            model=self.model(),
            max_tokens=int(config.get("api.providers.claude.max_tokens", 8192)),
            messages=[{"role": "user", "content": content}],
            tools=[tool],
            tool_choice={"type": "tool", "name": _RESULT_TOOL},
        )
        _reject_truncated(response)
        return unwrap(_tool_input(response))


def _reject_truncated(response: object) -> None:
    """길이 제한에 걸린 응답은 **버린다.**

    표가 긴 문서에서 이게 조용하면 가장 나쁜 형태로 틀린다 — 행이 잘린 채
    문법에 맞는 리스트가 돌아오면 게이트웨이도 스키마 검증도 통과한다.
    **덜 들어간 입고**는 안 들어간 입고보다 찾기 어렵다.

    `max_tokens` 는 설정으로 올릴 수 있다(`api.providers.claude.max_tokens`).
    """
    if getattr(response, "stop_reason", None) == "max_tokens":
        raise OcrSchemaError(
            "Claude 응답이 길이 제한에서 잘렸습니다. 결과가 불완전할 수 있어 "
            "버립니다. api.providers.claude.max_tokens 를 올리거나 문서를 "
            "나눠 주세요."
        )


def _media_type(path: Path) -> str:
    try:
        return _MEDIA_TYPES[path.suffix.casefold()]
    except KeyError as exc:
        supported = ", ".join(sorted(_MEDIA_TYPES))
        raise ValueError(
            f"Claude OCR이 지원하지 않는 파일 형식입니다: {path.suffix!r} "
            f"(지원: {supported})"
        ) from exc


def _result_tool(schema: type):
    result_schema = _json_schema(schema)
    if result_schema.get("type") == "object":
        input_schema = result_schema

        def unwrap(value: object) -> object:
            return value

    else:
        input_schema = {
            "type": "object",
            "properties": {"result": result_schema},
            "required": ["result"],
            "additionalProperties": False,
        }

        def unwrap(value: object) -> object:
            if not isinstance(value, dict) or "result" not in value:
                raise OcrSchemaError("Claude tool 응답에 result 필드가 없습니다")
            return value["result"]

    return (
        {
            "name": _RESULT_TOOL,
            "description": "요청한 분석 결과를 지정된 JSON 스키마로 제출합니다.",
            "input_schema": input_schema,
        },
        unwrap,
    )


def _json_schema(schema: type) -> dict[str, Any]:
    model_json_schema = getattr(schema, "model_json_schema", None)
    if callable(model_json_schema):
        generated = model_json_schema()
        if isinstance(generated, dict):
            return generated

    if dataclasses.is_dataclass(schema):
        annotations = get_type_hints(schema)
        properties = {
            field.name: _annotation_schema(annotations.get(field.name, field.type))
            for field in dataclasses.fields(schema)
        }
        required = [
            field.name
            for field in dataclasses.fields(schema)
            if field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING
        ]
        result: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            result["required"] = required
        return result

    return _annotation_schema(schema)


def _annotation_schema(annotation: object) -> dict[str, Any]:
    primitive = {str: "string", int: "integer", float: "number", bool: "boolean"}
    if annotation in primitive:
        return {"type": primitive[annotation]}
    if annotation is dict:
        return {"type": "object"}
    if annotation is list:
        return {"type": "array"}

    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is list:
        # `list[Row]` 의 Row 를 그대로 흘리면 items 가 {} 가 돼서 **행 스키마가
        # 사라진다.** Gemini 는 같은 타입에서 필드까지 다 만들어 내므로, 여기서
        # 놓치면 같은 룬이 제공자에 따라 다른 것을 받는다 — 이 단계가 없애려던
        # 바로 그 차이다.
        item = _json_schema(arguments[0]) if arguments else {}
        return {"type": "array", "items": item}
    if origin is dict:
        value = _annotation_schema(arguments[1]) if len(arguments) == 2 else {}
        return {"type": "object", "additionalProperties": value}

    # 게이트웨이가 최종 타입 검증을 하므로 알 수 없는 사용자 타입을 이
    # 어댑터가 임의의 구조로 해석하지 않는다.
    return {}


def _tool_input(response: object) -> object:
    for block in getattr(response, "content", []):
        block_type = (
            block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
        )
        block_name = (
            block.get("name") if isinstance(block, dict) else getattr(block, "name", None)
        )
        if block_type == "tool_use" and block_name == _RESULT_TOOL:
            value = (
                block.get("input")
                if isinstance(block, dict)
                else getattr(block, "input", None)
            )
            if not isinstance(value, dict):
                raise OcrSchemaError("Claude tool 입력이 JSON 객체가 아닙니다")
            return value
    raise OcrSchemaError("Claude 응답에 submit_result tool 호출이 없습니다")


def _import_anthropic() -> ModuleType | None:
    try:
        import anthropic
    except (ImportError, ModuleNotFoundError):
        return None
    return anthropic
