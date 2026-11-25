"""내 룬 [업데이트] 목록 — 광장에 더 새 판이 있는 설치된 룬만, 늘어나는 권한과 함께 (2026-11-25)."""
from types import SimpleNamespace

from vader.core import catalog_sync, permissions
from vader.desktop.controller import DesktopController


def test_only_installed_runes_with_a_newer_plaza_version_get_an_update(monkeypatch):
    monkeypatch.setattr(catalog_sync, "confirmed_access", lambda: {"reader", "same", "ghost", "older"})
    def release(rune_id, state, perms, version="1.1.0"):
        return {"rune_id": rune_id, "version": version, "name": rune_id, "installed": "1.0.0", "state": state,
                "archive_sha256": "a" * 64, "permissions": perms}

    monkeypatch.setattr(catalog_sync, "listing", lambda: {"enabled": True, "releases": [
        release("reader", "update", ["file_read", "network"]),
        release("same", "installed", []),
        release("ghost", "update", []),          # 설치되지 않은 룬 — 내 룬에 없다
        release("older", "newer", []),
    ]})
    rune = lambda perms: SimpleNamespace(manifest=SimpleNamespace(permissions=perms))   # noqa: E731
    me = SimpleNamespace(runes={"reader": rune(["file_read"]), "same": rune([]), "older": rune([])})
    assert DesktopController._market_updates(me) == {"reader": {
        "version": "1.1.0", "installed": "1.0.0", "archive_sha256": "a" * 64, "name": "reader",
        "added": permissions.describe(["network"])}}


def test_updates_need_access_confirmed_for_the_signed_in_account(monkeypatch):
    """접근 목록을 모르거나(오프라인·로그인 전) 다른 계정으로 받은 것이면 내 룬에 [업데이트] 를 띄우지 않는다 (codex1 P2-3)."""
    from vader.core import account
    monkeypatch.setattr(catalog_sync, "listing", lambda: {"enabled": True, "releases": [
        {"rune_id": "reader", "version": "1.1.0", "name": "reader", "installed": "1.0.0", "state": "update",
         "archive_sha256": "a" * 64, "permissions": []}]})
    me = SimpleNamespace(runes={"reader": SimpleNamespace(manifest=SimpleNamespace(permissions=[]))})
    monkeypatch.setitem(catalog_sync._last, "access", {"reader"})
    monkeypatch.setitem(catalog_sync._last, "access_account", "A")
    monkeypatch.setattr(account, "current_id", lambda: "A")
    assert list(DesktopController._market_updates(me)) == ["reader"]
    monkeypatch.setattr(account, "current_id", lambda: "B")          # 계정을 바꿨다 — 옛 계정의 접근 목록
    assert DesktopController._market_updates(me) == {}
    monkeypatch.setattr(account, "current_id", lambda: None)         # 로그아웃
    assert DesktopController._market_updates(me) == {}
    monkeypatch.setattr(account, "current_id", lambda: "A")
    monkeypatch.setitem(catalog_sync._last, "access", None)          # 접근 조회 실패
    assert DesktopController._market_updates(me) == {}
    monkeypatch.setitem(catalog_sync._last, "access", set())         # 이 계정은 받을 수 없다
    assert DesktopController._market_updates(me) == {}


def test_an_access_answer_that_arrives_after_an_account_switch_is_dropped(monkeypatch):
    """A 로 보낸 접근 조회가 B 로 바뀐 뒤 돌아오면 B 의 목록으로 저장하지 않는다 (codex1 회신 3)."""
    from vader.core import account, catalog
    monkeypatch.setattr(account, "fetch_public", lambda path, limit: b"{}")   # 공개 목록은 받아졌다
    monkeypatch.setattr(catalog, "accept_delegation", lambda doc: None)
    monkeypatch.setattr(catalog, "accept_snapshot", lambda doc: None)
    monkeypatch.setattr(account, "_access", {"account": {"id": "A"}})
    monkeypatch.setattr(account, "_generation", [0])
    monkeypatch.setattr(catalog_sync, "_last", {"next": 0.0, "error": "", "access": None, "access_account": None})
    monkeypatch.setattr(catalog_sync, "enabled", lambda: True)

    def slow_access():
        account._access["account"] = {"id": "B"}     # 응답을 기다리는 사이 다른 계정으로
        account._generation[0] += 1
        return {"only_for_A"}
    monkeypatch.setattr(catalog_sync, "_access", slow_access)
    import time
    catalog_sync.refresh()
    assert catalog_sync._last["access"] is None and catalog_sync.confirmed_access() is None
    assert catalog_sync._last["next"] <= time.time() + catalog_sync.RETRY_SECONDS   # 6시간이 아니라 곧 다시

    monkeypatch.setattr(catalog_sync, "_access", lambda: {"reader"})     # 안정된 새 세션에서 다시 — 그 계정 목록으로 복구
    catalog_sync.refresh()
    assert catalog_sync.confirmed_access() == {"reader"}
    assert catalog_sync._last["next"] > time.time() + catalog_sync.RETRY_SECONDS
