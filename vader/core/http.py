"""룬이 외부 시스템을 읽고 쓰는 유일한 통로.

세 번째 룬(웹에서 찾아 요약해 보내기)이 계약을 압박한 자리다. 지금까지 룬이
바깥과 이야기하는 방법은 `ctx.ocr`·`ctx.infer` 둘뿐이었고, 둘 다 **LLM
제공자**로만 나간다. 임의의 API 를 부르려면 룬이 `network` 권한을 선언하고
직접 `urllib` 을 쓰는 수밖에 없었다.

그러면 셋을 잃는다.

- **dry-run 이 다시 권고가 된다.** `ctx.dry_run` 은 코어가 강제하지 못한다고
  계약에 적혀 있다 — 외부 시스템을 만지는 코드가 룬 안에 있어 가로챌 자리가
  없기 때문이다. 통로가 코어에 있으면 **가로챌 자리가 생긴다.** 연습 실행이
  진짜로 아무것도 안 보낸다
- **재시도·간격·로그가 룬마다 흩어진다.** IchorAI 결함 #4 가 그거였다
- **무엇을 어디로 보냈는지 아무도 모른다.** 카톡으로 보내는 룬이라면 그게
  곧 개인정보 경계다

그래서 코어가 통로를 준다. 룬은 여전히 `network` 를 선언하지 않는다 —
`urllib` 을 import 하는 것은 이 파일이지 룬이 아니다.
"""
from __future__ import annotations

import json as jsonlib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

# 룬이 잡아야 하는 오류라 계약에 산다. 룬은 vader.core 를 import 할 수 없다.
from vader.sdk import HttpError

__all__ = ["HttpError", "HttpGateway", "Response"]

Log = Callable[..., None]

#: 한 요청이 기다리는 최대 시간. 룬이 늘리지 못하게 코어가 잡는다 — 무한정
#: 매달린 요청 하나가 Job 취소를 못 듣는 상태로 만든다.
TIMEOUT_SECONDS = 20

#: 본문 상한. 웹 페이지 하나를 읽어 요약하는 용도라 이걸로 충분하고, 넘으면
#: 자른다. 룬이 200MB 짜리 파일을 메모리에 올리는 것을 막는다.
MAX_BYTES = 2_000_000


@dataclass(frozen=True)
class Response:
    status: int
    text: str
    #: 참이면 **연습 실행이라 보내지 않았다.** 룬은 성공으로 보고 계속하면
    #: 되지만, 이 값을 읽어 "보낸 척" 을 로그에 남길 수 있다.
    skipped: bool = False
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        try:
            return jsonlib.loads(self.text)
        except jsonlib.JSONDecodeError as exc:
            raise HttpError(f"응답이 JSON 이 아닙니다: {exc}") from exc


class HttpGateway:
    def __init__(
        self,
        *,
        dry_run: bool = False,
        log: Log | None = None,
        retry_max: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] = urllib.request.urlopen,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._dry_run = dry_run
        self._log = log or (lambda level, message, **fields: None)
        self._retry_max = max(1, retry_max)
        self._sleep = sleep
        self._opener = opener
        self._timeout = timeout

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> Response:
        """읽기. 연습 실행에서도 **한다** — 읽는 것은 아무것도 안 바꾼다."""
        return self._send("GET", url, None, headers)

    def post(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        """쓰기. **연습 실행에서는 안 나간다.**

        이것이 `ctx.dry_run` 이 처음으로 권고가 아니게 되는 자리다. EONM 은
        룬이 지켜야 했고, 그래서 지키는지 테스트로만 확인할 수 있었다.
        """
        if self._dry_run:
            self._log("warning", "DRY-RUN: 보내지 않습니다", url=_safe(url), method="POST")
            return Response(status=200, text="", skipped=True)

        body, sent_headers = _body(data, json, headers)
        return self._send("POST", url, body, sent_headers)

    def _send(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: dict[str, str] | None,
    ) -> Response:
        if not url.lower().startswith(("http://", "https://")):
            raise HttpError(f"http/https 주소가 아닙니다: {url!r}")

        request = urllib.request.Request(
            url, data=body, headers=headers or {}, method=method
        )
        for attempt in range(self._retry_max):
            try:
                with self._opener(request, timeout=self._timeout) as answer:
                    raw = answer.read(MAX_BYTES)
                    return Response(
                        status=getattr(answer, "status", 200),
                        text=raw.decode("utf-8", errors="replace"),
                        headers=dict(getattr(answer, "headers", {}) or {}),
                    )
            except urllib.error.HTTPError as exc:
                # 4xx 는 다시 보내도 같다. 5xx·429 만 재시도한다.
                if exc.code in (429, 500, 502, 503, 504) and attempt < self._retry_max - 1:
                    wait = 2 ** (attempt + 1)
                    self._log(
                        "warning",
                        "요청 실패로 재시도합니다",
                        url=_safe(url),
                        status=exc.code,
                        wait_seconds=wait,
                    )
                    self._sleep(wait)
                    continue
                raise HttpError(f"{method} {_safe(url)} 실패 ({exc.code})") from exc
            except Exception as exc:
                if attempt < self._retry_max - 1:
                    self._sleep(2 ** (attempt + 1))
                    continue
                raise HttpError(f"{method} {_safe(url)} 실패: {exc}") from exc
        raise AssertionError("unreachable")


def _body(
    data: dict[str, str] | None, json: Any, headers: dict[str, str] | None
) -> tuple[bytes | None, dict[str, str]]:
    sent = dict(headers or {})
    if json is not None:
        sent.setdefault("Content-Type", "application/json")
        return jsonlib.dumps(json, ensure_ascii=False).encode("utf-8"), sent
    if data is not None:
        sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
        return urllib.parse.urlencode(data).encode("utf-8"), sent
    return None, sent


def _safe(url: str) -> str:
    """로그에 남길 주소. **쿼리를 자른다** — 검색어와 토큰이 거기 있다."""
    return url.split("?", 1)[0]
