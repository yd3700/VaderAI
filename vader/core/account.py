"""앱 로그인 — 브라우저로 웹에 로그인하고 기기 토큰을 받는다 (2026-10-30, 계획 v2 P1-5·v3 P1-6).

흐름 (RFC 8252 loopback + PKCE):
1. 앱이 `127.0.0.1:<임의 포트>/callback` 에서 기다리고, S256 challenge·state 를 붙여 웹 `/ko/device/` 를 브라우저로 연다.
2. 사람이 웹에서 로그인하고 "연결" 을 누르면 웹이 일회용 코드를 그 loopback 주소로 돌려준다.
3. 앱이 코드 + verifier 로 기기 토큰을 받는다. 기기 토큰은 Windows 보호 저장소(DPAPI)에만 둔다.

기기 토큰은 쓸 때마다 새 것으로 바뀐다(서버가 옛 토큰 재사용을 도난으로 본다) — 받은 새 토큰을 바로 저장한다.
접근 토큰(10분)은 메모리에만. 토큰·코드는 로그·화면에 내지 않는다.

codex1 3단계 검토 반영:
- 토큰은 **발급한 웹 주소별로** 따로 둔다. 주소를 바꾸면 다른 서버에 옛 토큰을 보내지 않고 다시 로그인한다 (P1-3).
- 인증 요청은 리다이렉트를 따라가지 않는다. 운영 주소는 https 만 (P1-3).
- 우회 값은 그 값을 넣을 때 정한 미리보기 주소에만 붙인다 (P1-3).
- loopback 연결 하나가 읽기를 붙잡아도 5초면 끊는다 (P2-1).
- 서버가 401 이면(끊김·비활성) 로그아웃 상태로 (P2-2). 로그인은 한 번에 하나, 로그아웃이 기다리던 로그인을 무효로 한다 (P2-4).
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets as pysecrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from vader.core import config, secrets

#: 웹 주소. 설정 `account.site` 로 바꾼다 (시험: dev 미리보기 주소).
DEFAULT_SITE = "https://vaderai-web.vercel.app"
#: 시험용 미리보기는 Vercel 배포 보호 뒤에 있다 — 'Protection Bypass for Automation' 값과 그 값을 쓸 주소를 함께 둔다.
BYPASS_NAME = "account.vercel_bypass"
LOGIN_SECONDS = 300
TIMEOUT_SECONDS = 20
CALLBACK_READ_SECONDS = 5

_access: dict = {}          # {"site", "token", "expires", "account"} — 메모리에만
_lock = threading.Lock()
_login = {"generation": 0, "busy": False}


class AccountError(RuntimeError):
    status: int | None = None


def site() -> str:
    """설정의 웹 주소. https 만 — 127.0.0.1 은 시험용 예외."""
    value = str(config.device_get("account.site", DEFAULT_SITE) or DEFAULT_SITE).rstrip("/")
    parts = urllib.parse.urlsplit(value)
    local = parts.scheme == "http" and parts.hostname == "127.0.0.1"
    if not (parts.scheme == "https" or local) or parts.path or parts.query or parts.username:
        raise AccountError("웹 주소 설정이 올바르지 않습니다 (https://… 만).")
    return f"{parts.scheme}://{parts.netloc}"


def vault_name(origin: str) -> str:
    return "account.device_token." + hashlib.sha256(origin.encode()).hexdigest()[:16]


def signed_in() -> bool:
    try:
        return bool(secrets.available() and secrets.get(vault_name(site()), device=True))
    except AccountError:
        return False


def login(open_browser=webbrowser.open, timeout: float = LOGIN_SECONDS) -> dict:
    """브라우저를 열고, 사람이 웹에서 '연결' 을 누를 때까지 기다린다. 성공하면 계정 {id, display_name}."""
    if not secrets.available():
        raise AccountError("이 PC 에서는 로그인 정보를 보호 저장할 수 없습니다.")
    origin = site()
    with _lock:
        if _login["busy"]:
            raise AccountError("이미 브라우저에서 연결을 기다리고 있습니다.")
        _login["busy"] = True
        generation = _login["generation"]
    try:
        code, verifier, redirect = _wait_for_code(origin, open_browser, timeout)
        grant = _post(origin, "/api/device/token/", {"code": code, "verifier": verifier, "redirect": redirect})
        with _lock:
            if _login["generation"] != generation:      # 기다리는 사이에 로그아웃했다
                stale = True
            else:
                stale = False
                _keep(origin, grant)
        if stale:
            try:
                _post(origin, "/api/device/logout/", {"device_token": grant["device_token"]})
            except AccountError:
                pass
            raise AccountError("로그인을 기다리는 사이 로그아웃해서 연결하지 않았습니다.")
        return grant["account"]
    finally:
        with _lock:
            _login["busy"] = False


def _wait_for_code(origin: str, open_browser, timeout: float) -> tuple[str, str, str]:
    verifier = pysecrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = pysecrets.token_urlsafe(24)
    got: dict = {}

    class Callback(BaseHTTPRequestHandler):
        timeout = CALLBACK_READ_SECONDS            # 연결을 열고 헤더를 안 보내는 프로세스가 로그인을 붙잡지 못하게

        def setup(self):
            super().setup()
            # 조금씩 계속 보내는 연결도 전체 시한에는 끊는다 (codex1 4단계 P2-6)
            left = max(deadline[0] - time.monotonic(), 0.1)
            self._cutoff = threading.Timer(min(left, CALLBACK_READ_SECONDS * 2), self._close)
            self._cutoff.daemon = True
            self._cutoff.start()

        def _close(self):
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def finish(self):
            self._cutoff.cancel()
            try:
                super().finish()
            except OSError:
                pass

        def do_GET(self):  # noqa: N802
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            ok = url.path == "/callback" and query.get("state", [""])[0] == state and "code" in query
            if ok:
                got["code"] = query["code"][0]
            body = ("VaderAI 로 돌아가세요. 이 창은 닫아도 됩니다." if ok else "잘못된 요청입니다.").encode()
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 주소에 코드가 있다 — 남기지 않는다
            pass

    deadline = [time.monotonic() + timeout]
    server = HTTPServer(("127.0.0.1", 0), Callback)
    try:
        redirect = f"http://127.0.0.1:{server.server_address[1]}/callback"
        query = urllib.parse.urlencode({"challenge": challenge, "redirect": redirect,
                                        "name": (socket.gethostname() or "PC")[:60], "state": state})
        open_browser(f"{origin}/{config.ui_lang()}/device/?{query}")
        deadline[0] = time.monotonic() + timeout
        while "code" not in got:
            left = deadline[0] - time.monotonic()
            if left <= 0:
                raise AccountError("시간 안에 로그인하지 않았습니다. 다시 시도해 주세요.")
            server.timeout = min(left, 1.0)
            server.handle_request()
    finally:
        server.server_close()
    return got["code"], verifier, redirect


def account() -> dict | None:
    """로그인한 계정 {id, display_name, catalog_access}. 로그인 안 했거나 끊겼으면 None."""
    try:
        return call("GET", "/api/device/me/")["account"]
    except AccountError as exc:
        if exc.status == 401:
            return None
        if str(exc) == NOT_SIGNED_IN:
            return None
        raise


def call(method: str, path: str, body: bytes | None = None, content_type: str = "application/json") -> dict:
    """로그인한 계정으로 우리 웹 API 를 부른다 (목록 제출·승인 등). 로그인 안 했으면 AccountError."""
    origin = site()                                 # 한 번 읽은 주소로 끝까지 (codex1 P2-5)
    token, device = _access_for(origin)
    if not token:
        raise AccountError(NOT_SIGNED_IN)
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = content_type
    try:
        return _request(origin, method, path, body=body, headers=headers)
    except AccountError as exc:
        if exc.status == 401:                       # 계정 비활성·삭제 — 요청을 시작할 때의 토큰일 때만 버린다
            _forget(origin, device)
        raise


NOT_SIGNED_IN = "먼저 앱에서 로그인해 주세요."


def fetch_public(path: str, limit: int) -> bytes | None:
    """로그인 없이 받는 우리 웹 문서(목록 위임·snapshot). 없으면 None. `limit` 바이트를 넘으면 거절."""
    origin = site()
    headers = {}
    bypass = secrets.get(BYPASS_NAME, device=True) if secrets.available() else None
    if bypass and bypass.partition(" ")[0] == origin:
        headers["x-vercel-protection-bypass"] = bypass.partition(" ")[2]
    request = urllib.request.Request(origin + path, headers=headers)
    try:
        with _opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            data = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise AccountError(f"목록 정보를 받지 못했습니다 (HTTP {exc.code}).") from None
    except (urllib.error.URLError, TimeoutError):
        raise AccountError("웹에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.") from None
    if len(data) > limit:
        raise AccountError("목록 정보가 너무 큽니다.")
    return data


def access_token() -> str | None:
    """10분짜리 접근 토큰. 만료가 가까우면 기기 토큰으로 새로 받는다(기기 토큰도 바뀐다)."""
    return _access_for(site())[0]


def _access_for(origin: str) -> tuple[str | None, str | None]:
    """(접근 토큰, 그 토큰을 받은 기기 토큰) — 나중에 401 이 와도 그때의 기기 토큰만 지우게."""
    with _lock:
        device = _stored(origin)
        if not device:
            return None, None
        if _access.get("site") == origin and _access.get("expires", 0) - 60 > time.time():
            return _access["token"], device
        try:
            grant = _post(origin, "/api/device/refresh/", {"device_token": device})
        except AccountError as exc:
            if exc.status == 401:                   # 끊겼거나 만료 — 로그아웃 상태로
                _forget_locked(origin, device)
                return None, None
            raise
        _keep(origin, grant)
        return _access["token"], grant["device_token"]


def logout() -> None:
    """이 PC 의 연결을 끊는다. 서버에 못 알려도 이 PC 의 토큰은 지운다. 기다리던 로그인도 무효가 된다."""
    origin = site()
    with _lock:
        _login["generation"] += 1
        device = _stored(origin)
        _access.clear()
        if device:
            secrets.delete(vault_name(origin), device=True)
    if device:
        try:
            _post(origin, "/api/device/logout/", {"device_token": device})
        except AccountError:
            pass


def _stored(origin: str) -> str | None:
    return secrets.get(vault_name(origin), device=True) if secrets.available() else None


def _forget(origin: str, device: str | None) -> None:
    with _lock:
        _forget_locked(origin, device)


def _forget_locked(origin: str, device: str | None) -> None:
    if device and _stored(origin) == device:        # 그사이 새로 로그인했으면 새 토큰은 지우지 않는다
        secrets.delete(vault_name(origin), device=True)
    _access.clear()


def _keep(origin: str, grant: dict) -> None:
    secrets.put(vault_name(origin), grant["device_token"], device=True)   # 새 토큰부터 저장 — 옛 토큰은 이제 쓰면 도난 판정
    _access.update(site=origin, token=grant["access_token"], expires=time.time() + int(grant["expires_in"]),
                   account=grant["account"])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None                                  # 토큰·우회 값이 다른 곳으로 따라가지 않게


_opener = urllib.request.build_opener(_NoRedirect)


def _post(origin: str, path: str, body: dict) -> dict:
    return _request(origin, "POST", path, body=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"})


def _request(origin: str, method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> dict:
    headers = dict(headers or {})
    bypass = secrets.get(BYPASS_NAME, device=True) if secrets.available() else None
    if bypass:
        value_origin, _, value = bypass.partition(" ")
        if value_origin == origin and value:       # 넣을 때 정한 미리보기 주소에만
            headers["x-vercel-protection-bypass"] = value
    request = urllib.request.Request(origin + path, data=body, method=method, headers=headers)
    try:
        with _opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:   # 서버가 보낸 한국어 이유 (토큰은 담기지 않는다)
            reason = str(json.loads(exc.read()).get("message") or "")[:300]
        except (ValueError, OSError, AttributeError):
            reason = ""
        error = AccountError("다시 로그인해 주세요." if exc.code == 401
                             else f"서버가 거절했습니다 (HTTP {exc.code}){': ' + reason if reason else ''}.")
        error.status = exc.code
        raise error from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise AccountError("웹에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.") from None
