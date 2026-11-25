"""광장 받기 — 목록 문서 나르기와 패키지 받아 설치 (2026-10-30, 5단계). 웹은 가짜, 서명·판정은 진짜."""
import io
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_catalog as tc  # noqa: E402
from test_catalog import NOW, delegation, packed_reader, publish  # noqa: E402

from vader.core import account, catalog, catalog_sync, config  # noqa: E402

root = tc.root          # 같은 시험용 root·발행자 정책


@pytest.fixture(autouse=True)
def at_now(monkeypatch):
    monkeypatch.setattr(catalog, "_now", lambda: NOW)
    catalog_sync._last.update(next=0.0, error="", access=None)


class Web:
    def __init__(self, documents, release=None, archive=b""):
        self.documents, self.release, self.archive, self.calls = documents, release, archive, []

    def fetch(self, path, limit):
        self.calls.append(path)
        return self.documents.get(path)

    def call(self, method, path, body=None, content_type="application/json"):
        self.calls.append(path)
        if self.release is None:
            raise account.AccountError("자동화 패키지 받기가 허용되지 않은 계정입니다.")
        return {"document": self.release.decode(), "url": "https://blob.example/package.zip?signed"}

    def open(self, request, timeout):
        self.calls.append(request.full_url)
        assert "Authorization" not in request.headers, "다른 호스트로 로그인 머리를 보내지 않는다"
        return io.BytesIO(self.archive)


def wire(monkeypatch, web):
    monkeypatch.setattr(account, "fetch_public", web.fetch)
    monkeypatch.setattr(account, "call", web.call)
    monkeypatch.setattr(catalog_sync, "_download", web)


def test_refresh_hands_documents_to_the_trust_core_and_lists_them(monkeypatch):
    archive = packed_reader()
    release, entry = publish(archive)                        # 시험 도우미가 이미 받아 둔 상태를 지운다
    snapshot = catalog.canonical(catalog._read("snapshot.json", {}))
    for name in ("delegation.json", "snapshot.json", "state.json"):
        catalog._file(name).unlink()
    web = Web({"/api/catalog/delegation/": delegation(), "/api/catalog/snapshot/": snapshot})
    wire(monkeypatch, web)
    assert catalog_sync.refresh() == {"ok": True, "error": ""}
    listed = catalog_sync.listing()
    assert listed["fresh"] and [(r["rune_id"], r["state"]) for r in listed["releases"]] == [("reader", "new")]


def test_install_downloads_the_signed_bytes_and_refuses_anything_else(monkeypatch):
    archive = packed_reader()
    release, entry = publish(archive)
    data = archive.read_bytes()
    wire(monkeypatch, Web({}, release, data[:-1] + b"x"))    # 한 바이트 바뀐 zip
    with pytest.raises(catalog_sync.SyncError, match="발행 문서와 다릅니다"):
        catalog_sync.install("reader", "0.1.0")
    wire(monkeypatch, Web({}, release, data + b"more"))      # 크기가 다르다 — 끝까지 읽지 않는다
    with pytest.raises(catalog_sync.SyncError, match="크기"):
        catalog_sync.install("reader", "0.1.0")
    web = Web({}, release, data)
    wire(monkeypatch, web)
    done = catalog_sync.install("reader", "0.1.0")
    assert web.calls[0] == "/api/catalog/releases/reader/0.1.0"     # 끝 슬래시 없이 — 있으면 서버가 308 로 돌려보낸다
    assert done.manifest.id == "reader" and catalog.origins()["reader"]["state"] == "installed"
    assert catalog_sync.listing()["releases"][0]["state"] == "installed"


def test_an_account_without_access_gets_the_server_reason(monkeypatch):
    archive = packed_reader()
    publish(archive)
    wire(monkeypatch, Web({}, None))
    with pytest.raises(catalog_sync.SyncError, match="허용되지 않은"):
        catalog_sync.install("reader", "0.1.0")


def test_without_a_root_key_nothing_is_fetched(monkeypatch):
    monkeypatch.setattr(catalog, "ROOT_KEYS", ())
    web = Web({})
    wire(monkeypatch, web)
    assert catalog_sync.refresh()["ok"] is False and web.calls == []
    assert catalog_sync.listing() == {"enabled": False, "releases": []}


def test_a_server_that_answers_with_another_package_is_refused_before_download(monkeypatch):
    archive = packed_reader()
    release, entry = publish(archive)
    web = Web({}, release, archive.read_bytes())
    wire(monkeypatch, web)
    with pytest.raises(catalog_sync.SyncError, match="고른 것과 다른"):
        catalog_sync.install("reader", "9.9.9")
    assert not any(c.startswith("https://blob") for c in web.calls), "받기 전에 거절"


def test_install_takes_only_the_package_the_person_confirmed(monkeypatch):
    """확인 창에서 본 패키지(sha256)와 목록이 다르면(그사이 재발행·회수) 받지 않는다 — 내 룬 [업데이트]·광장 공통 (2026-11-25)."""
    archive = packed_reader()
    release, entry = publish(archive)
    web = Web({}, release, archive.read_bytes())
    wire(monkeypatch, web)
    listed = catalog_sync.listing()["releases"][0]
    assert listed["archive_sha256"] == entry["archive_sha256"]
    with pytest.raises(catalog_sync.SyncError, match="그사이 바뀌었어요"):
        catalog_sync.install("reader", "0.1.0", archive_sha256="0" * 64)
    assert not any(c.startswith("https://blob") for c in web.calls), "받기 전에 거절"
    done = catalog_sync.install("reader", "0.1.0", archive_sha256=listed["archive_sha256"])
    assert done.manifest.id == "reader"


def test_a_rune_installed_by_hand_is_not_silently_replaced_from_the_market(monkeypatch):
    archive = packed_reader()
    release, entry = publish(archive)
    catalog._write("managed.json", [])                       # 목록에서 받은 적 없는 룬으로
    catalog._write("origins.json", {})
    (config.rune_dir() / "reader").mkdir(parents=True, exist_ok=True)
    (config.rune_dir() / "reader" / "__init__.py").write_text("", encoding="utf-8")
    assert catalog_sync.listing()["releases"][0]["state"] == "local"
    wire(monkeypatch, Web({}, release, archive.read_bytes()))
    with pytest.raises(catalog_sync.SyncError, match="직접 설치한 같은 룬"):
        catalog_sync.install("reader", "0.1.0")


def test_listing_state_follows_the_records_and_the_real_folder():
    archive = packed_reader()
    publish(archive)
    folder = config.rune_dir() / "reader"
    state = lambda: catalog_sync.listing()["releases"][0]["state"]  # noqa: E731
    catalog._write("managed.json", [])
    catalog._write("origins.json", {})
    folder.mkdir(parents=True, exist_ok=True)
    assert state() == "new"                                  # 빈 잔여 폴더는 직접 설치한 룬이 아니다
    (folder / "__init__.py").write_text("", encoding="utf-8")
    assert state() == "local"
    catalog._write("managed.json", ["reader"])               # 관리 표시만 남고 출처가 없다 — 다시 받기
    assert state() == "check"
    catalog._write("origins.json", {"reader": {"state": "installed", "version": "0.1.0"}})
    (folder / "__init__.py").unlink()                        # 제거해서 폴더가 없다 — 다시 설치할 수 있다
    assert state() == "new"


def test_a_rune_shared_with_others_is_hidden_and_unknown_access_hides_nothing(monkeypatch):
    """광장 공유 범위 (2026-11-19) — 서버가 받을 수 있다고 한 룬만 보인다. 물어보지 못하면 숨기지 않는다(받기는 서버가 막는다)."""
    archive = packed_reader()
    publish(archive)
    asked = []

    def call(method, path, body=None, content_type="application/json"):
        asked.append(path)
        return {"runes": []}
    monkeypatch.setattr(account, "call", call)
    catalog_sync._last["access"] = catalog_sync._access()
    assert asked == ["/api/catalog/access/?runes=reader"] and catalog_sync.listing()["releases"] == []

    def offline(*a, **k):
        raise account.AccountError("먼저 앱에서 로그인해 주세요.")
    monkeypatch.setattr(account, "call", offline)
    catalog_sync._last["access"] = catalog_sync._access()
    assert [r["rune_id"] for r in catalog_sync.listing()["releases"]] == ["reader"]
