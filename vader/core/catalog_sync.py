"""광장 받기 — 우리 웹에서 목록 문서를 받아 신뢰 코어에 넘기고, 허용된 계정은 패키지를 받아 설치한다 (2026-10-30, 5단계).

판정은 전부 `catalog.py` 가 한다. 여기는 **나르기만** 한다:
- `refresh()`  위임 → snapshot 순서로 받아 `accept_*` 에 넘긴다. 로그인 없이 (회수는 받기 권한과 무관하게 받는다).
               설치본에 root 키가 없으면(목록 기능 꺼짐) 아무것도 하지 않는다.
- `listing()`  지금 쓸 수 있는 snapshot 의 버전과 이 PC 에 설치된 버전.
- `install()`  웹이 허용을 DB 에서 다시 확인한 뒤 준 발행 문서와 5분짜리 주소로 zip 을 받아 `install_catalog`.
               다운로드 주소는 다른 호스트(Blob)라 로그인 머리를 붙이지 않는다. 크기는 발행 문서의 size 로 자른다.
설치는 실전 승인이 아니다 — 그 PC 에서 연습한 뒤 사람이 봉인해야 실전이 된다.
"""
from __future__ import annotations

import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from vader.core import account, catalog, config, permissions

#: 하루 한 번 이상 (서명 PC 가 매일 새 snapshot 을 올린다 — 계약 3절). 앱이 켜져 있는 동안 이 간격으로 다시 받는다.
REFRESH_SECONDS = 6 * 3600
#: 실패하면 짧게 다시 — 시작할 때 잠깐 오프라인이었다고 6시간을 잃지 않게 (codex1 5단계 P2-6).
RETRY_SECONDS = 5 * 60
DOWNLOAD_TIMEOUT = 60
#: 받는 패키지의 상한 — 서명된 목록의 size 와 함께 본다 (서버 제출 상한과 같다).
PACKAGE_LIMIT = 2 * 1024 * 1024

_last = {"next": 0.0, "error": "", "access": None}
_running = threading.Lock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_download = urllib.request.build_opener(_NoRedirect)


class SyncError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(catalog.ROOT_KEYS)


def refresh() -> dict:
    """위임과 snapshot 을 받아 반영한다. 결과 {"ok", "error"}. 실패해도 예외를 밖으로 내지 않는다(화면 폴링에서 부른다)."""
    if not enabled():
        return {"ok": False, "error": ""}
    with _running:
        return _refresh_locked()


def _refresh_locked() -> dict:
    try:
        delegation = account.fetch_public("/api/catalog/delegation/", catalog.MAX_DOC_BYTES)
        if delegation is not None:
            catalog.accept_delegation(delegation)
        snapshot = account.fetch_public("/api/catalog/snapshot/", catalog.MAX_DOC_BYTES)
        if snapshot is not None:
            catalog.accept_snapshot(snapshot)
        error = "" if delegation is not None and snapshot is not None else "광장 목록이 아직 공개되지 않았습니다."
        _last["access"] = _access()
    except (account.AccountError, catalog.CatalogError) as exc:
        error = str(exc)
    except Exception:  # noqa: BLE001 — 화면 폴링에서 부른다. 예외를 밖으로 내지 않는다
        error = "목록 정보를 받는 중 문제가 생겼습니다. 잠시 뒤 다시 시도합니다."
    _last.update(error=error, next=time.time() + (RETRY_SECONDS if error else REFRESH_SECONDS))
    return {"ok": not error, "error": error}


def _access() -> set | None:
    """이 계정이 받을 수 있는 룬 (공유 범위, 2026-11-19). 모르면 None — 목록을 숨기지 않고 받기에서 서버가 거절한다."""
    current = catalog.current_snapshot() or {}
    ids = sorted({r["rune_id"] for r in current.get("releases", [])})
    if not ids:
        return None
    try:
        found = account.call("GET", "/api/catalog/access/?" + urllib.parse.urlencode({"runes": ",".join(ids)}))
        return set(found.get("runes") or [])
    except Exception:  # noqa: BLE001 — 로그인 전·오프라인. 숨길지 모를 때는 숨기지 않는다
        return None


def shares() -> dict:
    """발행자 화면: 룬마다 {restricted, people:[{account_id, handle}]}."""
    return account.call("GET", "/api/catalog/shares/").get("shares") or {}


def save_share(rune_id: str, restricted: bool, accounts: list[str]) -> dict:
    import json
    body = json.dumps({"restricted": bool(restricted), "accounts": [str(a) for a in accounts]}).encode("utf-8")
    found = account.call("PUT", f"/api/catalog/shares/{urllib.parse.quote(str(rune_id), safe='')}/", body)
    _last["access"] = None   # 다음 새로 고침 때 다시 받는다
    return found.get("shares") or {}


def refresh_if_due() -> None:
    """화면이 자주 묻는 자리에서 부른다 — 때가 됐고 아무도 받고 있지 않을 때만 뒤에서 한 번."""
    if not enabled() or time.time() < _last["next"] or not _running.acquire(blocking=False):
        return

    def run():
        try:
            _refresh_locked()
        finally:
            _running.release()
    threading.Thread(target=run, daemon=True).start()


def listing() -> dict:
    """{"enabled", "fresh", "releases": [{rune_id, version, name, summary, installed, state}], "error"}."""
    if not enabled():
        return {"enabled": False, "releases": []}
    snapshot = catalog.current_snapshot()
    origins = catalog.origins()
    managed = set(catalog.managed())
    revoked = {v["archive_sha256"] for v in (snapshot or {}).get("revocations", [])}
    newest: dict = {}                       # 룬마다 회수 안 된 가장 높은 버전만 (codex1 P2-5)
    for r in (snapshot or {}).get("releases", []):
        if r["archive_sha256"] in revoked:
            continue
        best = newest.get(r["rune_id"])
        if best is None or catalog._version(r["version"]) > catalog._version(best["version"]):
            newest[r["rune_id"]] = r
    releases = []
    access = _last["access"]
    for r in newest.values():
        if access is not None and r["rune_id"] not in access:
            continue                        # 공유 범위 밖 — 받을 수 없는 룬은 보이지 않는다
        mine = origins.get(r["rune_id"]) or {}
        present = (config.rune_dir() / r["rune_id"] / "__init__.py").is_file()
        installed = mine.get("version") if mine.get("state") == "installed" and present else None
        if catalog.local_rune(r["rune_id"]):
            state = "local"                 # 직접 설치한 같은 룬 — 지운 뒤에 받는다 (install_catalog 도 거절)
        elif present and (r["rune_id"] in managed) != bool(mine) or mine and mine.get("state") != "installed":
            state = "check"                 # 기록이 반쯤 남았거나 설치가 멈췄다 — 다시 받기로 고친다
        elif installed is None:
            state = "new"
        elif installed == r["version"]:
            state = "installed"
        else:
            state = "update" if catalog._version(r["version"]) > catalog._version(installed) else "newer"
        releases.append({"rune_id": r["rune_id"], "version": r["version"], "name": str(r.get("name") or r["rune_id"]),
                         "summary": str(r.get("summary") or ""), "installed": installed, "state": state,
                         "permissions": r["permissions"], "permission_text": permissions.describe(r["permissions"])})
    return {"enabled": True, "fresh": snapshot is not None, "releases": releases, "error": _last["error"]}


def install(rune_id: str, version: str, rune_dir: Path | None = None):
    """허용된 계정으로 패키지를 받아 서명·목록·파일을 모두 확인한 뒤 설치한다."""
    if not enabled():
        raise SyncError("이 설치본에는 광장 받기가 켜져 있지 않습니다.")
    quoted = "/".join(urllib.parse.quote(part, safe="") for part in (rune_id, version))
    try:
        found = account.call("GET", f"/api/catalog/releases/{quoted}/")
    except account.AccountError as exc:
        raise SyncError(str(exc)) from None
    document = str(found.get("document", "")).encode("utf-8")
    try:
        # 받기 **전에** 서명·위임·목록 등재·회수·고른 것과 같은지 (codex1 5단계 P1-2·P1-3)
        _doc, _release, listed = catalog.check_release(document, expect=(rune_id, version))
    except catalog.CatalogError as exc:
        raise SyncError(str(exc)) from None
    size = int(listed["size"])                     # 서명된 목록의 크기 — 서버 응답을 믿지 않는다
    if not 0 < size <= PACKAGE_LIMIT:
        raise SyncError("패키지 크기가 받을 수 있는 범위를 넘습니다.")
    url = str(found.get("url", ""))
    if urllib.parse.urlsplit(url).scheme != "https":
        raise SyncError("다운로드 주소가 안전하지 않습니다.")
    try:
        with _download.open(urllib.request.Request(url), timeout=DOWNLOAD_TIMEOUT) as response:
            data = response.read(size + 1)
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SyncError("패키지를 받지 못했습니다. 잠시 뒤 다시 시도해 주세요.") from None
    if len(data) != size:
        raise SyncError("받은 파일 크기가 발행 문서와 다릅니다.")
    with tempfile.TemporaryDirectory(prefix="vader-market-") as tmp:
        archive = Path(tmp) / "package.zip"
        archive.write_bytes(data)
        try:
            done = catalog.install_catalog(archive, document, rune_dir=rune_dir)
        except catalog.CatalogError as exc:
            raise SyncError(str(exc)) from None
    if (done.manifest.id, done.manifest.version) != (rune_id, version):
        raise SyncError("설치된 것이 고른 패키지와 다릅니다.")
    return done
