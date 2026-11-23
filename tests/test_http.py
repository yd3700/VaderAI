"""룬이 외부 시스템을 읽고 쓰는 통로.

가장 중요한 것은 첫 테스트다. **연습 실행이 처음으로 진짜가 된다** —
지금까지 `ctx.dry_run` 은 룬이 지키는 권고였고, 코어가 강제할 자리가 없었다.
"""
from __future__ import annotations

import socket
import urllib.error
from types import SimpleNamespace

import pytest

from vader.core.http import HttpGateway, Response
from vader.sdk import HttpError


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("실제 소켓을 열면 안 된다")

    monkeypatch.setattr(socket, "socket", forbidden)


class Answer:
    """`with urlopen(...) as answer` 를 흉내낸다.

    던더는 타입에서 찾으므로 인스턴스에 붙이면 `with` 가 못 본다.
    """

    def __init__(self, status=200, body=b"{}", reads=None):
        self.status = status
        self.headers = {}
        self._body = body
        self._reads = reads

    def read(self, size):
        if self._reads is not None:
            self._reads.append(size)
            return b"x" * size
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def answering(status=200, body=b"{}", record=None):
    def opener(request, timeout=None):
        if record is not None:
            record.append(
                SimpleNamespace(
                    url=request.full_url,
                    method=request.get_method(),
                    body=request.data,
                    headers=dict(request.header_items()),
                    timeout=timeout,
                )
            )
        return Answer(status, body)

    return opener


def test_a_dry_run_never_sends_the_write(monkeypatch):
    """이것이 dry-run 이 권고가 아니게 되는 자리다.

    EONM 은 쓰기가 룬 안에 있어 코어가 가로챌 수 없었고, 지키는지 테스트로만
    확인할 수 있었다. 통로가 코어에 있으면 코어가 막는다.
    """
    sent = []

    answer = HttpGateway(dry_run=True, opener=answering(record=sent)).post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        json={"요약": "오늘의 소식"},
    )

    assert sent == []
    assert answer.skipped is True
    assert answer.status == 200


def test_a_dry_run_still_reads():
    """읽는 것은 아무것도 안 바꾼다. 연습 실행이 진짜 내용을 봐야 리허설이다."""
    sent = []

    answer = HttpGateway(dry_run=True, opener=answering(body="내용".encode("utf-8"), record=sent)).get(
        "https://example.com/article"
    )

    assert answer.text == "내용"
    assert answer.skipped is False
    assert len(sent) == 1


def test_the_log_does_not_carry_the_query_string():
    """검색어와 토큰이 쿼리에 산다. 로그로 새면 개인정보 경계가 무너진다."""
    logs = []

    HttpGateway(
        dry_run=True, log=lambda level, message, **fields: logs.append(fields)
    ).post("https://kapi.kakao.com/send?access_token=비밀&q=병원+이름")

    assert logs[0]["url"] == "https://kapi.kakao.com/send"
    assert "비밀" not in str(logs)


def test_a_slow_host_cannot_hang_the_job():
    """무한정 매달린 요청 하나가 Job 을 취소도 못 듣는 상태로 만든다."""
    sent = []

    HttpGateway(opener=answering(record=sent)).get("https://example.com")

    assert sent[0].timeout == 20


def test_a_client_error_is_not_retried():
    """4xx 는 다시 보내도 같다. 재시도는 상대 서버만 두드린다."""
    attempts = []

    def opener(request, timeout=None):
        attempts.append(1)
        raise urllib.error.HTTPError(request.full_url, 401, "인증 실패", {}, None)

    with pytest.raises(HttpError, match="401"):
        HttpGateway(opener=opener, sleep=lambda s: None).get("https://example.com")

    assert len(attempts) == 1


def test_an_overloaded_host_is_retried():
    attempts = []

    def opener(request, timeout=None):
        attempts.append(1)
        if len(attempts) < 3:
            raise urllib.error.HTTPError(request.full_url, 503, "과부하", {}, None)
        return Answer(body=b"ok")

    result = HttpGateway(opener=opener, sleep=lambda s: None).get("https://example.com")

    assert result.text == "ok"
    assert len(attempts) == 3


def test_a_file_url_is_refused():
    """룬이 이 통로로 로컬 파일을 읽게 두면 통로가 아니라 구멍이다."""
    with pytest.raises(HttpError, match="http/https"):
        HttpGateway().get("file:///C:/Windows/win.ini")


def test_a_huge_body_is_cut():
    """룬이 200MB 를 메모리에 올리는 것을 코어가 막는다."""
    reads = []

    def opener(request, timeout=None):
        return Answer(reads=reads)

    HttpGateway(opener=opener).get("https://example.com")

    assert reads == [2_000_000]


def test_json_and_form_bodies_carry_their_content_type():
    sent = []
    gateway = HttpGateway(opener=answering(record=sent))

    gateway.post("https://example.com", json={"a": "한글"})
    gateway.post("https://example.com", data={"b": "값"})

    assert sent[0].headers["Content-type"] == "application/json"
    assert "한글".encode("utf-8") in sent[0].body
    assert sent[1].headers["Content-type"] == "application/x-www-form-urlencoded"


def test_a_non_json_answer_says_so():
    answer = Response(status=200, text="<html>오류 페이지</html>")

    with pytest.raises(HttpError, match="JSON"):
        answer.json()
