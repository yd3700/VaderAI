"""OCR 제공자 공통 운영 정책: 캐시, 지연, 재시도, 일일 한도."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, get_args, get_origin

from vader.core import config
from vader.core.ocr.base import (
    OcrProvider,
    OcrProviderError,
    OcrProviderUnavailable,
    OcrSchemaError,
)
from vader.core.usage import Call, UsageLedger, now

Log = Callable[..., None]

# 키 구성이나 캐시 파일 표현이 바뀌면 올린다. 기존 캐시와의 호환은 의도적으로
# 끊는다. 제공자별 요청/스키마 전략 버전은 OcrProvider.adapter_version()이 맡는다.
CACHE_FORMAT_VERSION = "2"


class OcrGateway:
    def __init__(
        self,
        provider: OcrProvider,
        *,
        cache_dir: Path | None = None,
        cache_days: int | None = None,
        request_delay_seconds: float | None = None,
        retry_max: int | None = None,
        daily_limit: int | None = None,
        ledger: UsageLedger | None = None,
        log: Log | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.cache_dir = (
            Path(cache_dir)
            if cache_dir is not None
            else config.data_dir() / "ocr_cache"
        )
        self.cache_days = config.cache_days() if cache_days is None else cache_days
        self.request_delay_seconds = (
            config.api_request_delay_seconds()
            if request_delay_seconds is None
            else request_delay_seconds
        )
        self.retry_max = config.api_retry_max() if retry_max is None else retry_max
        self.daily_limit = (
            int(config.get(f"api.providers.{provider.name()}.daily_limit", 1400))
            if daily_limit is None
            else daily_limit
        )
        self.ledger = (
            UsageLedger(config.data_dir() / "usage") if ledger is None else ledger
        )
        self._log = log or (lambda level, message, **fields: None)
        self._sleep = sleep

    def extract(self, path: Path, prompt: str, schema: type) -> object:
        path = Path(path)
        cache_path = self._cache_path(path, prompt, schema)
        cached = self._load_cache(cache_path, schema)
        if cached is not None:
            self._log(
                "info",
                "OCR 캐시 적중",
                provider=self.provider.name(),
                path=str(path),
            )
            self._record("extract", ok=True, cached=True)
            return cached

        if not self.provider.is_available():
            raise OcrProviderUnavailable(
                f"OCR 제공자 '{self.provider.name()}'를 사용할 수 없습니다. "
                "선택적 패키지와 API 키 설정을 확인하세요."
            )

        self._warn_daily_limit()
        if self.request_delay_seconds > 0:
            self._log(
                "info",
                "OCR API 호출 전 대기",
                provider=self.provider.name(),
                seconds=self.request_delay_seconds,
            )
            self._sleep(self.request_delay_seconds)

        # 스키마 검증을 기록되는 구간 **안**에 둔다. 밖에 두면 제공자가 던진
        # OcrSchemaError 는 ok=False 인데 게이트웨이가 걸러낸 같은 실패는
        # ok=True 로 남아, 같은 사건이 원장에서 두 가지로 세어졌다.
        result = self._call_with_retry(
            lambda: _coerce_schema(self.provider.extract(path, prompt, schema), schema),
            "extract",
        )
        self._save_cache(cache_path, result)
        return result

    def complete(self, prompt: str, schema: type) -> object:
        """파일 없이 텍스트로 묻는다. `extract` 와 같은 파이프라인을 탄다.

        캐시 키만 다르다 — 파일 내용 대신 프롬프트로 만든다.
        """
        cache_path = self._prompt_cache_path(prompt, schema)
        cached = self._load_cache(cache_path, schema)
        if cached is not None:
            self._log("info", "추론 캐시 적중", provider=self.provider.name())
            self._record("infer", ok=True, cached=True)
            return cached

        if not self.provider.is_available():
            raise OcrProviderUnavailable(
                f"제공자 '{self.provider.name()}'를 사용할 수 없습니다. "
                "선택적 패키지와 API 키 설정을 확인하세요."
            )

        self._warn_daily_limit()
        if self.request_delay_seconds > 0:
            self._sleep(self.request_delay_seconds)

        result = self._call_with_retry(
            lambda: _coerce_schema(self.provider.complete(prompt, schema), schema),
            "infer",
        )
        self._save_cache(cache_path, result)
        return result

    def _call_with_retry(self, call, role: str) -> object:
        attempts = max(1, self.retry_max)
        for attempt in range(attempts):
            try:
                result = call()
            except OcrSchemaError as exc:
                # 응답은 받았다. 할당량은 이미 썼다.
                self._record(role, ok=False, error=type(exc).__name__)
                raise
            except OcrProviderUnavailable as exc:
                # 로그인·구독 한도 — 다시 불러도 같다. 앞서 읽은 쪽까지 다시 보내지 않는다 (codex1 지적).
                self._record(role, ok=False, error=type(exc).__name__)
                raise
            except Exception as exc:
                self._record(role, ok=False, error=type(exc).__name__)
                if _is_daily_quota(exc):
                    # 하루 한도는 10초·20초 뒤에도 그대로다. 다시 부르면 실패만
                    # 쌓인다(2026-10-20, Gemini 무료 하루 20회를 23회 씀).
                    raise OcrProviderError(
                        f"OCR 제공자 '{self.provider.name()}' 의 하루 한도를 다 "
                        "썼습니다. 재시도하지 않습니다. Gemini 무료 등급은 미국 "
                        f"태평양 자정(한국 16~17시)에 다시 채워집니다: {exc}"
                    ) from exc
                retryable = _is_retryable(exc)
                if retryable and attempt < attempts - 1:
                    # 서버가 기다릴 시간을 알려주면(분당 한도) 그만큼은 기다린다.
                    wait = max(5 * (2 ** (attempt + 1)), _server_retry_delay(exc))
                    self._log(
                        "warning",
                        "OCR 제공자 과부하로 재시도합니다",
                        provider=self.provider.name(),
                        attempt=attempt + 1,
                        max_attempts=attempts,
                        wait_seconds=wait,
                    )
                    self._sleep(wait)
                    continue
                raise OcrProviderError(
                    f"OCR 제공자 '{self.provider.name()}' 호출 실패: {exc}"
                ) from exc
            self._record(role, ok=True)
            return result
        raise AssertionError("unreachable")

    def _prompt_cache_path(self, prompt: str, schema: type) -> Path:
        digest = hashlib.sha256()
        self._update_cache_identity(digest)
        # 파일 기반 캐시 키와 섞이지 않게 접두사를 둔다.
        digest.update(b"infer\0")
        digest.update(prompt.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_schema_identity(schema).encode("utf-8"))
        return self.cache_dir / f"{digest.hexdigest()}.json"

    def _cache_path(self, path: Path, prompt: str, schema: type) -> Path:
        digest = hashlib.sha256()
        self._update_cache_identity(digest)
        digest.update(path.read_bytes())
        digest.update(b"\0")
        digest.update(prompt.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_schema_identity(schema).encode("utf-8"))
        return self.cache_dir / f"{digest.hexdigest()}.json"

    def _update_cache_identity(self, digest: Any) -> None:
        """모든 캐시 키가 공유하는 명시적 신원 계약을 해시에 넣는다."""
        for value in (
            CACHE_FORMAT_VERSION,
            self.provider.name(),
            self.provider.model(),
            self.provider.adapter_version(),
        ):
            digest.update(str(value).encode("utf-8"))
            digest.update(b"\0")

    def _load_cache(self, path: Path, schema: type) -> object | None:
        if not path.exists():
            return None
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days > self.cache_days:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return _coerce_schema(data, schema)
        except (OSError, UnicodeError, json.JSONDecodeError, OcrSchemaError):
            return None

    def _save_cache(self, path: Path, result: object) -> None:
        try:
            serializable = _to_jsonable(result)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    json.dump(serializable, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            except BaseException:
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                raise
        except Exception as exc:
            # 캐시 실패는 OCR 성공을 실패로 바꾸지 않는다.
            self._log("warning", "OCR 캐시 저장 실패", error=str(exc))

    def _record(
        self, role: str, *, ok: bool, cached: bool = False, error: str | None = None
    ) -> None:
        recorded = self.ledger.record(
            Call(
                at=now(),
                provider=self.provider.name(),
                model=self.provider.model(),
                role=role,
                ok=ok,
                cached=cached,
                error=error,
            )
        )
        if not recorded:
            # 못 센 호출은 한도 경고를 늦게 울린다. 조용히 넘기지 않는다.
            self._log("warning", "사용량 원장 기록 실패", provider=self.provider.name())

    def _warn_daily_limit(self) -> None:
        if self.daily_limit <= 0:
            return
        # 한도는 제공자마다 따로다. 전에는 다른 제공자 호출까지 합산했다.
        calls_today = self.ledger.calls_on(provider=self.provider.name())
        if calls_today / self.daily_limit >= 0.8:
            self._log(
                "warning",
                "OCR API 일일 한도 80%에 도달했습니다",
                provider=self.provider.name(),
                calls_today=calls_today,
                daily_limit=self.daily_limit,
            )


def _is_retryable(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(token in message for token in ("503", "429", "demand", "quota"))


def _is_daily_quota(exc: Exception) -> bool:
    """429 중 **하루** 한도다. Gemini 는 quota id 에 `PerDay` 를 넣는다."""
    return "perday" in str(exc).casefold()


def _server_retry_delay(exc: Exception) -> float:
    """429 본문의 `retryDelay: '37s'`. 없으면 0, 길어도 2분까지만."""
    found = re.search(r"retrydelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", str(exc).casefold())
    return min(float(found.group(1)), 120.0) if found else 0.0


def _schema_identity(schema: type) -> str:
    return f"{getattr(schema, '__module__', '')}.{getattr(schema, '__qualname__', repr(schema))}"


def _coerce_schema(value: object, schema: type) -> object:
    rows = _row_model(schema)
    if rows is not None:
        return _coerce_rows(value, rows)

    if isinstance(value, schema):
        return value

    validator = getattr(schema, "model_validate", None)
    if callable(validator):
        try:
            return validator(value)
        except Exception as exc:
            raise OcrSchemaError(f"OCR 응답 스키마 검증 실패: {exc}") from exc

    if dataclasses.is_dataclass(schema) and isinstance(value, dict):
        try:
            return schema(**value)
        except (TypeError, ValueError) as exc:
            raise OcrSchemaError(f"OCR 응답 스키마 검증 실패: {exc}") from exc

    raise OcrSchemaError(
        f"OCR 응답이 요청한 스키마 {getattr(schema, '__name__', schema)!s}와 다릅니다"
    )


def _row_model(schema: object) -> type | None:
    """`list[Row]` 의 `Row`. 그냥 `list` 면 None."""
    if get_origin(schema) is list:
        arguments = get_args(schema)
        return arguments[0] if arguments else None
    return None


def _coerce_rows(value: object, model: type) -> object:
    """행마다 모델의 필드를 갖는지 보고 **dict 그대로 돌려준다.**

    행을 모델 객체로 바꾸지 않는 것은 의도다. 룬이 행을 제자리에서 고치고
    (`data["mapped_code"] = ...`) 뒤의 버킷팅·합산이 그 dict 를 그대로 따라
    간다. 모델은 **제공자에게 줄 스키마**와 **여기서 할 검사**에 쓴다.

    전에는 `schema=list` 라 "리스트인가" 만 봤다. 행 안에 무엇이 있든
    통과했다 — 제공자가 필드 이름을 바꿔 보내도, 통째로 빠뜨려도.
    """
    if not isinstance(value, list):
        raise OcrSchemaError(
            f"OCR 응답이 배열이 아닙니다: {type(value).__name__}"
        )

    required = _field_names(model)
    rows = []
    for index, row in enumerate(value):
        # **제공자가 모델 객체로 파싱해 주기도 한다.** google-genai 는
        # `response_schema` 로 dataclass 를 주면 응답을 그 타입으로 만들어
        # 돌려준다. 룬은 dict 를 기대하므로(제자리 수정) 여기서 되돌린다.
        #
        # 이걸 놓쳐서 실제 Gemini 경로가 깨졌다. 가짜 응답과 JSON 을 직접
        # 파싱하는 제공자로만 시험했더니 안 보였다 — **스키마를 바꾸면
        # 제공자마다 응답 모양이 달라진다.**
        if dataclasses.is_dataclass(row) and not isinstance(row, type):
            row = dataclasses.asdict(row)
        if not isinstance(row, dict):
            raise OcrSchemaError(
                f"{index + 1}번째 행이 객체가 아닙니다: {type(row).__name__}"
            )
        missing = [name for name in required if name not in row]
        if missing:
            raise OcrSchemaError(
                f"{index + 1}번째 행에 없는 항목이 있습니다: {', '.join(missing)}"
            )
        rows.append(row)
    return rows


def _field_names(model: type) -> list[str]:
    """**꼭 있어야 하는** 필드만. 기본값이 있으면 없어도 된다.

    전에는 전부 요구해서 선택 필드를 만들 수 없었다. 룬이 "있으면 좋고
    없어도 되는" 값(명세서의 합계금액 같은)을 받으려면 그 자리가 필요하다 —
    없다고 파일 전체를 실패시키면 안 되는 값이 있다.
    """
    if dataclasses.is_dataclass(model):
        return [
            field.name
            for field in dataclasses.fields(model)
            if field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING
        ]
    fields = getattr(model, "model_fields", None)
    if isinstance(fields, dict):
        return list(fields)
    return []


def _to_jsonable(value: object) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    json.dumps(value)
    return value
