"""서명 PC 도구 → 받는 PC 코어 한 바퀴 (신뢰 계약 v2, codex1 2026-10-29 서명 PC 보장 7가지 + 재검토)."""
import hashlib
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from vader.core import catalog, publisher  # noqa: E402

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
PASS = "충분히 긴 시험 암호입니다"


@pytest.fixture
def keys(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "home", lambda: tmp_path / "publisher")
    publisher.init_ledger()
    root_pub = publisher.create_key("root", tmp_path / "usb" / "root.key", PASS)
    cat_pub = publisher.create_key("catalog", tmp_path / "publisher" / "catalog.key", PASS)
    monkeypatch.setattr(catalog, "ROOT_KEYS", (root_pub,))
    monkeypatch.setattr(catalog, "ALLOWED_IDS", ("reader",))
    monkeypatch.setattr(catalog, "PUBLISHERS", ("master",))
    root = publisher.load_key("root", tmp_path / "usb" / "root.key", PASS)
    cat = publisher.load_key("catalog", tmp_path / "publisher" / "catalog.key", PASS)
    window = {"public_key": cat_pub, "not_before": (NOW - timedelta(days=1)).isoformat(),
              "not_after": (NOW + timedelta(days=300)).isoformat()}
    return root, cat, window, tmp_path


def packed():
    from test_catalog import packed_reader
    return packed_reader()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def release(cat, archive, **more):
    args = {"reviewed_sha256": sha(archive), "publisher_account_id": "master", "submission_id": "s1",
            "revision": 1, "now": NOW, **more}
    return publisher.issue_release(cat, archive, **args)


def test_a_published_rune_goes_all_the_way_to_another_pc(keys):
    root, cat, window, _ = keys
    archive = packed()
    catalog.accept_delegation(publisher.issue_delegation(root, [window], now=NOW), NOW)
    doc = release(cat, archive)
    catalog.accept_snapshot(publisher.issue_snapshot(cat, now=NOW), NOW)
    catalog.install_catalog(archive, doc, NOW)
    assert catalog.verdict("reader", NOW)["level"] == "ok"
    assert len(publisher.ledger()["docs"]) == 3, "서명한 문서는 원장에 먼저 남는다"


def test_keys_are_role_bound_and_never_overwritten(keys):
    root, cat, window, tmp = keys
    with pytest.raises(publisher.PublishError, match="root 키가 아닙니다"):
        publisher.load_key("root", tmp / "publisher" / "catalog.key", PASS)
    with pytest.raises(publisher.PublishError, match="덮어쓰지"):
        publisher.create_key("root", tmp / "usb" / "root.key", PASS)
    with pytest.raises(publisher.PublishError, match="암호"):
        publisher.load_key("root", tmp / "usb" / "root.key", "틀린 암호는 이것입니다")
    with pytest.raises(publisher.PublishError, match="root 키로만"):
        publisher.issue_delegation(cat, [window], now=NOW)              # 목록 키로 위임 발행
    publisher.issue_delegation(root, [window], now=NOW)
    before = publisher.ledger()
    with pytest.raises(publisher.PublishError, match="catalog 키로만"):
        publisher.issue_snapshot(root, now=NOW)                          # root 로 목록 정보 발행
    assert publisher.ledger() == before, "거절되면 원장이 바뀌지 않는다"


def test_a_key_outside_the_current_delegation_signs_nothing(keys, tmp_path):
    root, cat, window, _ = keys
    publisher.issue_delegation(root, [window], now=NOW)
    publisher.create_key("catalog", tmp_path / "publisher" / "other.key", PASS)
    other = publisher.load_key("catalog", tmp_path / "publisher" / "other.key", PASS)
    with pytest.raises(publisher.PublishError, match="위임에 들어 있지 않"):
        publisher.issue_snapshot(other, now=NOW)
    with pytest.raises(publisher.PublishError, match="기간 밖"):
        publisher.issue_snapshot(cat, now=NOW + timedelta(days=301))


def test_a_retired_key_stays_retired_even_if_the_next_delegation_forgets(keys):
    root, cat, window, _ = keys
    publisher.issue_delegation(root, [window], retired=[window["public_key"]], now=NOW)
    raw = publisher.issue_delegation(root, [window], now=NOW + timedelta(days=1))     # 은퇴 인자를 빠뜨렸다
    payload = catalog.parse(raw)["payload"]
    assert window["public_key"] in payload["retired_keys"]
    assert catalog._time(payload["catalog_keys"][0]["not_after"]) <= NOW + timedelta(seconds=1)
    with pytest.raises(publisher.PublishError, match="은퇴"):
        publisher.issue_snapshot(cat, now=NOW + timedelta(days=1))


def test_the_same_version_is_never_signed_with_different_bytes(keys, tmp_path):
    root, cat, window, _ = keys
    archive = packed()
    publisher.issue_delegation(root, [window], now=NOW)
    first = release(cat, archive)
    assert release(cat, archive, now=NOW + timedelta(hours=1)) == first, "같은 제출의 재전송"
    with pytest.raises(publisher.PublishError, match="다른 제출"):
        release(cat, archive, submission_id="s2")
    other = tmp_path / "other.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(other, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item))
        dst.comment = b"different bytes"
    with pytest.raises(publisher.PublishError, match="다른 내용"):
        release(cat, other, submission_id="s2")


def test_only_the_reviewed_tested_bytes_are_signed(keys, tmp_path):
    root, cat, window, _ = keys
    archive = packed()
    publisher.issue_delegation(root, [window], now=NOW)
    with pytest.raises(publisher.PublishError, match="심사한 파일과 다릅니다"):
        release(cat, archive, reviewed_sha256="0" * 64)
    untested = tmp_path / "untested.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(untested, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            if item.filename == "manifest.json":
                manifest = json.loads(data)
                manifest.pop("tested")
                data = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
            dst.writestr(item, data)
    with pytest.raises(publisher.PublishError, match="시험 기록"):
        release(cat, untested, reviewed_sha256=sha(untested))


def test_recalls_only_accumulate_and_reach_the_receiving_pc(keys):
    root, cat, window, _ = keys
    archive = packed()
    catalog.accept_delegation(publisher.issue_delegation(root, [window], now=NOW), NOW)
    doc = release(cat, archive)
    catalog.accept_snapshot(publisher.issue_snapshot(cat, now=NOW), NOW)
    catalog.install_catalog(archive, doc, NOW)
    publisher.revoke("reader", "0.1.0", "malicious", "악성 코드 발견", now=NOW)
    with pytest.raises(publisher.PublishError, match="낮출 수 없습니다"):
        publisher.revoke("reader", "0.1.0", "operational", "실수", now=NOW)
    later = NOW + timedelta(minutes=1)
    catalog.accept_snapshot(publisher.issue_snapshot(cat, now=later), later)
    assert catalog.verdict("reader", later)["level"] == "malicious"


def test_an_unknown_rune_or_publisher_is_not_signed(keys):
    root, cat, window, _ = keys
    archive = packed()
    publisher.issue_delegation(root, [window], now=NOW)
    with pytest.raises(publisher.PublishError, match="발행자"):
        release(cat, archive, publisher_account_id="someone")


def test_a_held_lock_or_a_lost_ledger_stops_signing(keys, monkeypatch):
    root, cat, window, tmp = keys
    publisher.issue_delegation(root, [window], now=NOW)
    monkeypatch.setattr(publisher, "LOCK_WAIT_SECONDS", 0.1)
    (tmp / "publisher" / "ledger.lock").write_text("123", encoding="utf-8")       # 다른 프로세스가 발행 중
    with pytest.raises(publisher.PublishError, match="다른 발행"):
        publisher.issue_snapshot(cat, now=NOW)
    (tmp / "publisher" / "ledger.lock").unlink()
    (tmp / "publisher" / "ledger.json").unlink()
    with pytest.raises(publisher.PublishError, match="원장이 없습니다"):
        publisher.issue_snapshot(cat, now=NOW)
    (tmp / "publisher" / "catalog.key").unlink()        # 키는 USB 에 — 원장 폴더에 키가 없어도 새로 시작하지 않는다
    with pytest.raises(publisher.PublishError, match="원장이 없습니다"):
        publisher.issue_snapshot(cat, now=NOW)
    publisher.create_key("catalog", tmp / "usb" / "new-catalog.key", PASS)   # 원장 유실 뒤 키 교체도
    publisher.create_key("update", tmp / "usb" / "update.key", PASS)
    assert not (tmp / "publisher" / "ledger.json").exists(), "키 만들기가 빈 원장을 되살리지 않는다"
    with pytest.raises(publisher.PublishError, match="원장이 없습니다"):
        publisher.issue_delegation(root, [window], now=NOW)
    publisher.init_ledger()                              # 처음 설정만 원장을 만들고, 두 번은 안 된다
    with pytest.raises(publisher.PublishError, match="이미 있습니다"):
        publisher.init_ledger()


def test_a_pulled_usb_stops_signing_and_says_so(keys, monkeypatch):
    root, cat, window, tmp = keys
    gone = tmp / "pulled-usb" / "vaderai-catalog"
    monkeypatch.setattr(publisher, "home", lambda: gone)
    with pytest.raises(publisher.PublishError, match="원장 폴더가 없습니다"):
        publisher.issue_snapshot(cat, now=NOW)
    assert not gone.exists(), "빠진 USB 자리를 만들어 내지 않는다"
    monkeypatch.delenv("VADERAI_CATALOG_HOME", raising=False)   # 서명 PC 에서 돌려도 받는 PC 경우를 따로 본다
    assert publisher.renewal_notice(NOW) is None
    monkeypatch.setenv("VADERAI_CATALOG_HOME", str(gone))
    assert "접근할 수 없어" in publisher.renewal_notice(NOW)["message"]


def test_catalog_home_follows_the_usb_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("VADERAI_CATALOG_HOME", str(tmp_path / "usb"))
    assert publisher.home() == tmp_path / "usb"
    monkeypatch.delenv("VADERAI_CATALOG_HOME")
    assert publisher.home() == Path.home() / ".vaderai-catalog"


def test_key_recovery_reapproves_the_same_package_in_the_new_epoch(keys, tmp_path):
    root, cat, window, _ = keys
    archive = packed()
    catalog.accept_delegation(publisher.issue_delegation(root, [window], now=NOW), NOW)
    release(cat, archive)
    catalog.accept_snapshot(publisher.issue_snapshot(cat, now=NOW), NOW)
    new_pub = publisher.create_key("catalog", tmp_path / "publisher" / "new.key", PASS)
    new = publisher.load_key("catalog", tmp_path / "publisher" / "new.key", PASS)
    catalog.accept_delegation(publisher.issue_delegation(root, [{**window, "public_key": new_pub}], new_epoch=True,
                                                         compromised=[window["public_key"]], now=NOW), NOW)
    doc = release(new, archive, submission_id="s1-recovery")                # 같은 바이트를 새 세대로 다시 승인
    payload = catalog.accept_snapshot(publisher.issue_snapshot(new, now=NOW), NOW)
    assert (payload["epoch"], payload["sequence"], len(payload["releases"])) == (2, 1, 1)
    catalog.install_catalog(archive, doc, NOW)
    assert catalog.verdict("reader", NOW)["level"] == "ok"


def test_the_signing_pc_is_reminded_from_what_the_server_accepted(keys):
    root, cat, window, tmp = keys
    publisher.issue_delegation(root, [window], now=NOW)
    assert publisher.renewal_notice(NOW) is None                       # 아직 발행한 것이 없다
    doc = publisher.issue_snapshot(cat, now=NOW, days=7)
    assert publisher.renewal_notice(NOW)["action"] == "push"           # 서명만 했다 — 같은 문서를 다시 올린다
    publisher.mark_published(doc, NOW)
    assert publisher.renewal_notice(NOW + timedelta(days=3)) is None
    assert "3일 뒤 만료" in publisher.renewal_notice(NOW + timedelta(days=4))["message"]   # 서명 3일 넘음
    assert "이미 만료" in publisher.renewal_notice(NOW + timedelta(days=8))["message"]
    newer = publisher.issue_snapshot(cat, now=NOW + timedelta(days=1), days=1)
    assert publisher.renewal_notice(NOW + timedelta(days=1))["action"] == "push"
    assert publisher.renewal_notice(NOW + timedelta(days=3))["action"] == "resign"     # 못 올린 문서가 만료됐다
    publisher.mark_published(newer, NOW + timedelta(days=1))
    publisher.mark_published(doc, NOW + timedelta(days=1))           # 늦게 온 예전 성공은 덮지 않는다
    assert "1일 뒤 만료" in publisher.renewal_notice(NOW + timedelta(days=1, hours=1))["message"]  # 짧은 수명도 안내
    (tmp / "publisher" / "ledger.json").write_text("[]", encoding="utf-8")
    assert "읽지 못했어요" in publisher.renewal_notice(NOW)["message"]


def test_a_new_epoch_without_a_list_is_flagged(keys):
    root, cat, window, _ = keys
    publisher.issue_delegation(root, [window], now=NOW)
    publisher.mark_published(publisher.issue_snapshot(cat, now=NOW), NOW)
    publisher.issue_delegation(root, [window], now=NOW, new_epoch=True)
    assert "위임이 바뀌어" in publisher.renewal_notice(NOW)["message"]


def test_one_button_resign_signs_then_pushes_and_refuses_the_preview_server(keys, monkeypatch):
    """앱의 [다시 서명] — 운영 서버·발행자 계정일 때만 키를 열고, 원장에 먼저 서명한 뒤 위임·snapshot 을 올린다."""
    from vader.core import account
    root, cat, window, tmp = keys
    publisher.issue_delegation(root, [window], now=NOW)
    sent = []
    monkeypatch.setattr(account, "account", lambda: {"id": "master"})
    monkeypatch.setattr(account, "call", lambda m, path, body=None: sent.append(json.loads(body)["kind"]) or {})
    monkeypatch.setattr(account, "site", lambda: "https://preview.example")
    with pytest.raises(publisher.PublishError, match="운영 서버"):
        publisher.resign(tmp / "publisher" / "catalog.key", PASS)
    assert sent == [] and publisher.ledger()["snapshot"] is None
    monkeypatch.setattr(account, "site", lambda: account.DEFAULT_SITE)
    monkeypatch.setattr(account, "account", lambda: {"id": "other", "display_name": "다라상사"})
    with pytest.raises(publisher.PublishError, match=r"계정\(다라상사\)"):    # 어느 계정으로 들어왔는지 보인다
        publisher.resign(tmp / "publisher" / "catalog.key", PASS)
    monkeypatch.setattr(account, "account", lambda: {"id": "master"})
    with pytest.raises(publisher.PublishError, match="암호"):
        publisher.resign(tmp / "publisher" / "catalog.key", "틀린 암호")
    lines = publisher.resign(tmp / "publisher" / "catalog.key", PASS)
    assert sent == ["delegation", "snapshot"] and lines[-1] == "snapshot: 올렸습니다."
    assert publisher.renewal_notice() is None                           # 방금 올린 것 — 안내가 사라진다
