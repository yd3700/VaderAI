"""앱 로그인 — 가짜 웹(로컬 HTTP)과 메모리 보관소로 한 바퀴 (2026-10-30, codex1 검토 반영)."""
import base64
import hashlib
import json
import socket
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vader.core import account, config, secrets


class Web:
    """웹의 /api/device/* 대역. 코드 1회·verifier·redirect, 기기 단위 회전·재사용 탐지·끊기까지."""

    def __init__(self):
        self.codes, self.tokens, self.revoked, self.calls, self.headers = {}, {}, set(), [], []
        self.redirect_me = None

    def handle(self, method, path, body, headers):
        self.calls.append(path)
        self.headers.append(headers)
        if path == "/api/device/me/":
            if self.redirect_me:
                return 302, {}, {"Location": self.redirect_me}
            return 200, {"account": {"id": "a-1", "display_name": "가나메디칼 담당", "catalog_access": True}}, {}
        if path == "/api/device/token/":
            code = self.codes.pop(body["code"], None)
            digest = base64.urlsafe_b64encode(hashlib.sha256(body["verifier"].encode()).digest()).rstrip(b"=").decode()
            if not code or code["challenge"] != digest or code["redirect"] != body["redirect"]:
                return 401, {}, {}
            return 200, self.grant(f"dev-{len(self.calls)}"), {}
        if path == "/api/device/refresh/":
            device, used = self.tokens.get(body["device_token"], (None, True))
            if not device or device in self.revoked:
                return 401, {}, {}
            if used:                                    # 옛 토큰 재사용 — 기기 전체 끊기
                self.revoked.add(device)
                return 401, {}, {}
            self.tokens[body["device_token"]] = (device, True)
            return 200, self.grant(device), {}
        if path == "/api/device/logout/":
            self.revoked.add(self.tokens.get(body["device_token"], (None,))[0])
            return 200, {}, {}
        return 404, {}, {}

    def grant(self, device):
        token = f"tok-{len(self.calls)}"
        self.tokens[token] = (device, False)
        return {"device_token": token, "access_token": f"acc-{len(self.calls)}", "expires_in": 600,
                "account": {"id": "a-1", "display_name": "가나메디칼 담당"}}


@pytest.fixture
def web(isolated_settings, monkeypatch):
    fake, vault = Web(), {}
    monkeypatch.setattr(secrets, "available", lambda: True)
    monkeypatch.setattr(secrets, "get", lambda name, device=False: vault.get(name) if device else None)
    monkeypatch.setattr(secrets, "put", lambda name, value, device=False: vault.__setitem__(name, value) if device else None)
    monkeypatch.setattr(secrets, "delete", lambda name, device=False: vault.pop(name, None) if device else None)

    class Api(BaseHTTPRequestHandler):
        def _answer(self, body):
            status, payload, extra = fake.handle(self.command, self.path, body, dict(self.headers))
            data = json.dumps(payload).encode()
            self.send_response(status)
            for k, v in extra.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802
            self._answer(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

        def do_GET(self):  # noqa: N802
            self._answer({})

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    site = {"value": origin}
    monkeypatch.setattr(config, "device_get", lambda key, default=None: site["value"] if key == "account.site" else default)
    account._access.clear()
    fake.origin, fake.site = origin, site
    yield fake, vault
    server.shutdown()


def browser(fake, state_override=None, before=None):
    """사람이 웹에서 '연결' 을 누른 것처럼 — 코드를 만들고 loopback 주소로 돌려준다."""
    def open_url(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        fake.codes["code-1"] = {"challenge": q["challenge"], "redirect": q["redirect"]}
        assert q["redirect"].startswith("http://127.0.0.1:") and q["redirect"].endswith("/callback")

        def visit():
            if before:
                before(q["redirect"])
            back = f"{q['redirect']}?" + urllib.parse.urlencode({"code": "code-1", "state": state_override or q["state"]})
            try:
                urllib.request.urlopen(back, timeout=10).read()
            except Exception:  # noqa: BLE001 — 틀린 state 는 400
                pass
        threading.Thread(target=visit, daemon=True).start()
    return open_url


def test_login_keeps_the_device_token_per_site_and_rotates_it(web):
    fake, vault = web
    assert account.login(open_browser=browser(fake), timeout=10)["id"] == "a-1"
    name = account.vault_name(fake.origin)
    first = vault[name]
    assert account.access_token().startswith("acc-")          # 메모리 토큰을 그대로
    account._access.clear()                                   # 10분이 지났다
    assert account.access_token() and vault[name] != first, "새 기기 토큰을 바로 저장"
    fake.site["value"] = "http://127.0.0.1:1"                 # 주소를 바꿨다 — 옛 서버 토큰을 보내지 않는다
    assert not account.signed_in() and account.access_token() is None
    fake.site["value"] = fake.origin
    account.logout()
    assert name not in vault and not account.signed_in()


def test_a_wrong_state_and_a_stalled_connection_do_not_win(web):
    fake, vault = web

    def stall(redirect):                                      # 헤더를 끝내지 않는 로컬 연결
        port = int(redirect.rsplit(":", 1)[1].split("/")[0])
        sock = socket.create_connection(("127.0.0.1", port))
        sock.sendall(b"GET /callback?code=x")
        fake.stalled = sock
    started = time.monotonic()
    assert account.login(open_browser=browser(fake, before=stall), timeout=20)["id"] == "a-1"
    assert time.monotonic() - started < 15, "멈춘 연결은 몇 초 안에 끊긴다"
    account.logout()
    with pytest.raises(account.AccountError, match="시간 안에"):
        account.login(open_browser=browser(fake, state_override="x" * 32), timeout=2)


def test_reusing_an_old_token_or_a_web_disconnect_signs_out(web):
    fake, vault = web
    account.login(open_browser=browser(fake), timeout=10)
    old = vault[account.vault_name(fake.origin)]
    account._access.clear()
    account.access_token()                                    # 회전 — 이제 old 는 옛 토큰
    vault[account.vault_name(fake.origin)] = old              # 응답을 못 받아 옛 토큰만 남은 경우
    account._access.clear()
    assert account.access_token() is None and not account.signed_in(), "보안 우선 — 다시 로그인"


def test_credentials_never_follow_a_redirect_and_bypass_is_bound_to_its_site(web):
    fake, vault = web
    vault[account.BYPASS_NAME] = "https://elsewhere.example preview-value"
    account.login(open_browser=browser(fake), timeout=10)
    assert all("x-vercel-protection-bypass" not in {k.lower() for k in h} for h in fake.headers)
    vault[account.BYPASS_NAME] = f"{fake.origin} preview-value"
    fake.redirect_me = "http://127.0.0.1:9/steal"
    with pytest.raises(account.AccountError, match="HTTP 302"):
        account.account()
    assert {k.lower(): v for k, v in fake.headers[-1].items()}.get("x-vercel-protection-bypass") == "preview-value"
    assert fake.calls[-1] == "/api/device/me/", "리다이렉트를 따라가지 않았다"


def test_logout_while_waiting_cancels_the_login(web):
    fake, vault = web

    def logout_first(redirect):
        account.logout()
    with pytest.raises(account.AccountError, match="로그아웃"):
        account.login(open_browser=browser(fake, before=logout_first), timeout=10)
    assert not vault and fake.calls[-1] == "/api/device/logout/"


def test_an_http_site_other_than_localhost_is_refused(web, monkeypatch):
    fake, _ = web
    fake.site["value"] = "http://example.com"
    with pytest.raises(account.AccountError, match="https"):
        account.site()


def test_a_slow_drip_connection_is_cut_by_the_absolute_deadline(web):
    fake, vault = web

    def drip(redirect):                                       # 5초보다 짧게 한 글자씩 — 읽기 시한에는 안 걸린다
        port = int(redirect.rsplit(":", 1)[1].split("/")[0])
        sock = socket.create_connection(("127.0.0.1", port))

        def feed():
            try:
                for ch in b"GET /callback?code=" + b"x" * 40:
                    sock.sendall(bytes([ch]))
                    time.sleep(1)
            except OSError:
                pass
        threading.Thread(target=feed, daemon=True).start()
        time.sleep(0.5)
    started = time.monotonic()
    assert account.login(open_browser=browser(fake, before=drip), timeout=30)["id"] == "a-1"
    assert time.monotonic() - started < 20, "느린 연결도 전체 시한에 끊긴다"


def test_a_late_401_does_not_delete_a_newer_login(web):
    fake, vault = web
    account.login(open_browser=browser(fake), timeout=10)
    name = account.vault_name(fake.origin)
    original = fake.handle

    first = [True]

    def me_then_relogin(method, path, body, headers):
        if path == "/api/device/me/" and first[0]:
            first[0] = False
            vault[name] = newer                               # 요청 중에 다른 로그인이 끝났다
            return 401, {}, {}
        return original(method, path, body, headers)
    fake.calls.append("(another login)")                      # 가짜 서버 토큰 이름이 겹치지 않게
    newer = fake.grant("dev-newer")["device_token"]
    fake.handle = me_then_relogin
    assert account.account() is not None, "옛 로그인의 거절 — 새 로그인으로 다시 물어 성공한다 (codex1 회신 8)"
    assert vault.get(name) not in (None,) and fake.tokens[vault[name]][0] == "dev-newer", "요청을 시작할 때의 토큰만 지운다"


def test_a_login_on_another_pc_is_told_apart_from_a_plain_401(web):
    """한 계정 한 PC (2026-11-25): 서버가 '다른 PC 로그인' 이라고 끊으면 문이 그렇게 알린다. 다시 로그인하면 지운다."""
    from vader.core import gate
    fake, vault = web
    account.login(open_browser=browser(fake), timeout=10)
    original = fake.handle

    def replaced(method, path, body, headers):
        if path == "/api/device/me/":
            return 401, {"message": "다른 PC 에서 이 계정으로 로그인해 이 PC 는 로그아웃됐어요.", "reason": "replaced"}, {}
        return original(method, path, body, headers)
    fake.handle = replaced
    assert account.account() is None and account.ended_reason() == "replaced"
    assert gate.who() == {"state": "replaced"}
    # 끊기기 전에 보낸 옛 요청의 늦은 일반 401 은 이유를 지우지 못한다 (codex1 회신 6 P2-4)
    account._forget(fake.origin, "tok-of-the-cut-login", None)
    assert account.ended_reason() == "replaced"
    fake.handle = original
    account.login(open_browser=browser(fake), timeout=10)
    assert account.ended_reason() is None
    # 일반 401(끊김·만료)은 그냥 로그인 화면
    fake.handle = lambda m, p, b, h: (401, {}, {}) if p == "/api/device/me/" else original(m, p, b, h)
    assert account.account() is None and account.ended_reason() is None
    assert gate.who() == {"state": "login"}


def test_a_late_401_for_an_old_login_keeps_the_new_login_in_memory(web):
    """새로 로그인한 뒤 옛 요청의 401 이 와도 새 로그인의 계정·세대·종료 이유를 그대로 둔다 (codex1 회신 7 P2-3)."""
    fake, vault = web
    account.login(open_browser=browser(fake), timeout=10)
    assert account.account() is not None
    before = account.session()
    account._forget(fake.origin, "tok-of-an-older-login", "replaced")
    assert account.session() == before and account.ended_reason() is None
    assert vault.get(account.vault_name(fake.origin))


def test_an_old_logins_401_answered_after_a_relogin_asks_again_with_the_new_one(web):
    """/me 를 묻는 사이 새로 로그인했으면 옛 거절로 문을 닫지 않는다 — 새 로그인으로 다시 물어 ok, 확인 기록도 그대로 (codex1 회신 8)."""
    from vader.core import gate
    fake, vault = web
    account.login(open_browser=browser(fake), timeout=10)
    assert gate.who()["state"] == "ok"
    verified = [k for k in vault if k.startswith("account.verified.")]
    name = account.vault_name(fake.origin)
    original, first = fake.handle, [True]

    def relogin_mid_request(method, path, body, headers):
        if path == "/api/device/me/" and first[0]:
            first[0] = False
            fake.calls.append("(another login)")
            vault[name] = fake.grant("dev-new")["device_token"]      # 요청 중에 새 로그인이 끝났다
            account._access.clear()
            return 401, {}, {}
        return original(method, path, body, headers)
    fake.handle = relogin_mid_request
    assert gate.who()["state"] == "ok"
    assert all(k in vault for k in verified), "오프라인 확인 기록을 지우지 않는다"


def test_an_old_logins_write_is_not_resent_as_the_new_login(web):
    """A 의 쓰기 요청 도중 B 로 다시 로그인했으면, 그 요청은 B 로 다시 보내지 않는다 — 한 번만, B 의 토큰·세대 그대로 (codex1 회신 9)."""
    fake, vault = web
    account.login(open_browser=browser(fake), timeout=10)
    name = account.vault_name(fake.origin)
    original, writes = fake.handle, []

    def write_then_relogin(method, path, body, headers):
        if path == "/api/messenger/friend/":
            writes.append(headers.get("Authorization"))
            fake.calls.append("(another login)")
            vault[name] = fake.grant("dev-b")["device_token"]
            return 401, {}, {}
        return original(method, path, body, headers)
    fake.handle = write_then_relogin
    with pytest.raises(account.AccountError):
        account.call("POST", "/api/messenger/friend/", b"{}")
    assert len(writes) == 1
    assert fake.tokens[vault[name]][0] == "dev-b"


def test_a_token_saved_without_its_owner_never_picks_the_old_account_offline(web):
    """새 토큰만 저장되고 소유자 기록은 옛 계정 것으로 남으면(두 저장 사이 중단) 소유자를 모른다 — 오프라인 입장 없이 서버 확인 (codex1 회신 10)."""
    fake, vault = web
    account.login(open_browser=browser(fake), timeout=10)
    assert account.stored_account(fake.origin) == "a-1"
    fake.calls.append("(another login)")
    vault[account.vault_name(fake.origin)] = fake.grant("dev-b")["device_token"]   # 토큰만 바뀌고 끊겼다
    assert account.stored_account(fake.origin) is None
