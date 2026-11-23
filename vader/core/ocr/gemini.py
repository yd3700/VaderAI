"""선택적 google-genai 패키지를 사용하는 Gemini OCR 어댑터."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from types import ModuleType

from vader.core import config
from vader.core.ocr.base import OcrProviderUnavailable, OcrSchemaError

#: 요청 하나의 상한(밀리초). **없으면 서버가 끊을 때까지 기다린다** — SDK 기본이
#: None 이라 2026-10-20 에 한 시도가 2분씩 걸리다 실패했다. 24쪽 통째 OCR 이
#: 성공에 253초 걸린 적이 있어 그보다 넉넉히 둔다.
# ponytail: 고정값. 쪽 분할을 하면 묶음 크기에 맞춰 줄인다.
REQUEST_TIMEOUT_MS = 300_000


class GeminiProvider:
    def __init__(self, model: str | None = None) -> None:
        #: 역할이 덮어쓴 모델. 없으면 제공자 기본값을 쓴다.
        self._model = model

    def name(self) -> str:
        return "gemini"

    def model(self) -> str:
        """이 어댑터가 실제 요청에 고정해 사용하는 모델."""
        return str(
            self._model or config.get("api.providers.gemini.model", "gemini-2.5-flash")
        )

    def adapter_version(self) -> str:
        """요청 구성·응답 스키마 전략이 바뀌면 올리는 버전."""
        return "1"

    def supports(self, role: str) -> bool:
        """문서도 텍스트도 읽는다."""
        return True

    def chat(self, messages: list[dict]) -> str:
        """대화. Gemini 는 assistant 를 `model` 이라 부르고 system 이 따로다."""
        genai = _import_genai()
        if genai is None:
            raise OcrProviderUnavailable(
                "Gemini 대화에는 선택적 패키지 'google-genai'가 필요합니다"
            )
        system = " ".join(
            m["content"] for m in messages if m.get("role") == "system"
        )
        contents = [
            {
                "role": "model" if m.get("role") == "assistant" else "user",
                "parts": [{"text": m.get("content", "")}],
            }
            for m in messages
            if m.get("role") != "system"
        ]

        client = genai.Client(api_key=config.api_key("gemini"), http_options={"timeout": REQUEST_TIMEOUT_MS})
        response = client.models.generate_content(
            model=self.model(),
            contents=contents,
            config={"system_instruction": system} if system else {},
        )
        return (getattr(response, "text", "") or "").strip()

    def is_available(self) -> bool:
        try:
            return _import_genai() is not None and bool(config.api_key("gemini"))
        except (ImportError, ModuleNotFoundError, ValueError):
            return False

    def extract(self, path: Path, prompt: str, schema: type) -> object:
        genai = _import_genai()
        if genai is None:
            raise OcrProviderUnavailable(
                "Gemini OCR에는 선택적 패키지 'google-genai'가 필요합니다"
            )

        client = genai.Client(api_key=config.api_key("gemini"), http_options={"timeout": REQUEST_TIMEOUT_MS})
        uploaded = self._upload_file(client, Path(path))
        try:
            response = client.models.generate_content(
                model=self.model(),
                contents=[prompt, uploaded],
                config={
                    "response_mime_type": "application/json",
                    "response_schema": schema,
                },
            )
            return _parse_json_response(response)
        finally:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass

    def complete(self, prompt: str, schema: type) -> object:
        """파일 없이 텍스트로 묻는다. `ctx.infer()` 가 쓴다.

        `schema` 가 `str` 이면 JSON 을 강제하지 않는다. 값 하나만 돌려달라는
        프롬프트에 JSON 스키마를 씌우면 답이 달라지고, 이식 중 동작 변경은
        원칙 위반이다. EONM 의 `ai_match` 가 정확히 그런 프롬프트다.
        """
        genai = _import_genai()
        if genai is None:
            raise OcrProviderUnavailable(
                "Gemini 추론에는 선택적 패키지 'google-genai'가 필요합니다"
            )

        client = genai.Client(api_key=config.api_key("gemini"), http_options={"timeout": REQUEST_TIMEOUT_MS})
        model = self.model()

        if schema is str:
            response = client.models.generate_content(model=model, contents=prompt)
            return getattr(response, "text", "").replace("```", "").strip()

        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "response_schema": schema,
            },
        )
        return _parse_json_response(response)

    def _upload_file(self, client, path: Path):
        """한글 파일명을 피하기 위해 ASCII 임시 경로로 복사해 업로드한다."""
        with tempfile.NamedTemporaryFile(
            prefix="vader_ocr_",
            suffix=path.suffix,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
        try:
            shutil.copyfile(path, temporary)
            return client.files.upload(file=str(temporary))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _parse_json_response(response) -> object:
    parsed = getattr(response, "parsed", None)
    if parsed is not None:
        return parsed
    text = getattr(response, "text", "")
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise OcrSchemaError(f"Gemini JSON 파싱 실패: {exc}") from exc


def _import_genai() -> ModuleType | None:
    try:
        from google import genai
    except (ImportError, ModuleNotFoundError):
        return None
    return genai
