"""메신저가 웹을 부르는 경로 — 웹은 trailingSlash 라 끝 / 가 없으면 308 이고, 앱은 리디렉션을 따라가지 않는다.
0.1.25 는 이 / 하나가 빠져 모든 메신저 요청이 실패했다 (vodozemac 없이도 도는 시험)."""
import json

import pytest

from vader.core import account, messenger


@pytest.mark.parametrize("method, action, body", [("GET", "me", None), ("POST", "register", {"device_id": "x"})])
def test_messenger_calls_the_web_with_a_trailing_slash(monkeypatch, method, action, body):
    seen = []
    monkeypatch.setattr(messenger, "_live", lambda: None)
    monkeypatch.setattr(account, "call", lambda m, path, data=None: seen.append((m, path, data)) or {})
    messenger._api(method, action, body)
    assert seen == [(method, f"/api/messenger/{action}/", json.dumps(body).encode() if body is not None else None)]
