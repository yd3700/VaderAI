"""받는 PC 신뢰 코어 — 위임·snapshot·회수·판정 (신뢰 계약 v2 2~5절, 2026-10-29)."""
import base64
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from vader.core import catalog, config  # noqa: E402
from vader.core.signing import _raw  # noqa: E402

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
ROOT = Ed25519PrivateKey.generate()
KEY = Ed25519PrivateKey.generate()


def pub(private):
    return base64.b64encode(_raw(private.public_key())).decode()


def iso(moment):
    return moment.isoformat()


@pytest.fixture(autouse=True)
def root(monkeypatch):
    monkeypatch.setattr(catalog, "ROOT_KEYS", (pub(ROOT),))
    # 시험용 정책 — 실제 설치본은 세 룬 id 와 마스터 계정만 (계약 10절)
    monkeypatch.setattr(catalog, "ALLOWED_IDS", ("reader", "supply_weekly"))
    monkeypatch.setattr(catalog, "PUBLISHERS", ("p1",))


def raw(doc):
    return catalog.canonical(doc)


def delegation(epoch=1, sequence=1, keys=(KEY,), compromised=()):
    return raw(catalog.sign("delegation", {
        "schema": 1, "purpose": "delegation", "epoch": epoch, "sequence": sequence,
        "issued_at": iso(NOW), "expires_at": iso(NOW + timedelta(days=365)),
        "catalog_keys": [{"public_key": pub(k), "not_before": iso(NOW - timedelta(days=1)),
                          "not_after": iso(NOW + timedelta(days=365))} for k in keys],
        "compromised_keys": [pub(k) for k in compromised], "retired_keys": []}, ROOT))


def snapshot(sequence=1, epoch=1, key=KEY, issued=NOW, days=7, revocations=()):
    return raw(catalog.sign("snapshot", {
        "schema": 1, "purpose": "snapshot", "epoch": epoch, "sequence": sequence,
        "issued_at": iso(issued), "expires_at": iso(issued + timedelta(days=days)),
        "releases": [], "revocations": list(revocations)}, key))


def installed(digest="a" * 64, key=KEY):
    catalog.record_origin("supply_weekly", {"state": "installed", "archive_sha256": digest, "public_key": pub(key)})


def test_canonical_json_refuses_what_python_and_node_read_differently():
    for bad in ({"x": 1.5}, {"x": None}, {"x": 2**53}, {"x": -1}):
        with pytest.raises(catalog.CatalogError):
            catalog.canonical(bad)
    with pytest.raises(catalog.CatalogError, match="같은 키"):
        catalog.parse(b'{"a":1,"a":2}')
    assert catalog.canonical({"b": 1, "a": "가"}) == '{"a":"가","b":1}'.encode()


def test_a_snapshot_is_accepted_only_from_a_delegated_key_in_time_and_in_order():
    catalog.accept_delegation(delegation(), NOW)
    catalog.accept_snapshot(snapshot(sequence=5), NOW)
    stranger = Ed25519PrivateKey.generate()
    cases = {
        "모르는 키": snapshot(sequence=6, key=stranger),
        "오래된 순번": snapshot(sequence=4),
        "7일 넘는 유효기간": snapshot(sequence=6, days=8),
        "만료": snapshot(sequence=6, issued=NOW - timedelta(days=8)),
        "미래 발행": snapshot(sequence=6, issued=NOW + timedelta(hours=1)),
        "다른 세대": snapshot(sequence=6, epoch=2),
    }
    for why, doc in cases.items():
        with pytest.raises(catalog.CatalogError):
            catalog.accept_snapshot(doc, NOW)
    assert catalog.current_snapshot(NOW)["sequence"] == 5, "거절된 문서가 저장된 것을 바꾸지 않는다"


def test_the_verdict_table():
    rid = "supply_weekly"
    assert catalog.verdict(rid, NOW) == catalog.ALLOW_ALL                          # 목록 룬이 아님
    catalog.record_origin(rid, {"state": "pending", "archive_sha256": "a" * 64})
    assert not catalog.verdict(rid, NOW)["import"]                                 # 설치가 끝나지 않음
    installed()
    assert not catalog.verdict(rid, NOW)["import"]                                 # 목록 정보 없음
    catalog.accept_delegation(delegation(), NOW)
    catalog.accept_snapshot(snapshot(), NOW)
    assert catalog.verdict(rid, NOW) == catalog.ALLOW_ALL
    assert not catalog.verdict(rid, NOW + timedelta(days=8))["import"]             # 만료되면 불러오기도 안 됨

    catalog.accept_snapshot(snapshot(sequence=2, revocations=[
        {"rune_id": rid, "version": "1.1.0", "archive_sha256": "a" * 64, "level": "operational", "reason": "계산 오류"}]),
        NOW)
    v = catalog.verdict(rid, NOW)
    assert v["import"] and v["practice"] and not v["live"]                         # 업무 오류: 실전만 막음


def test_a_malicious_recall_stays_after_the_snapshot_cache_is_deleted():
    installed()
    catalog.accept_delegation(delegation(), NOW)
    catalog.accept_snapshot(snapshot(revocations=[
        {"rune_id": "supply_weekly", "version": "1.1.0", "archive_sha256": "a" * 64, "level": "malicious",
         "reason": "악성"}]), NOW)
    (catalog._file("snapshot.json")).unlink()
    catalog.accept_snapshot(snapshot(sequence=2), NOW)                             # 회수가 빠진 새 목록
    assert not any(catalog.verdict("supply_weekly", NOW)[k] for k in ("import", "practice", "live"))


def test_a_leaked_key_blocks_what_it_already_installed_and_root_recovers_with_a_new_epoch():
    installed()
    catalog.accept_delegation(delegation(), NOW)
    catalog.accept_snapshot(snapshot(sequence=999_999), NOW)                       # 탈취 키가 큰 순번을 미리 썼다
    new_key = Ed25519PrivateKey.generate()
    catalog.accept_delegation(delegation(epoch=2, sequence=1, keys=(new_key,), compromised=(KEY,)), NOW)
    assert not catalog.verdict("supply_weekly", NOW)["import"]                     # 이미 설치된 것도 막힘
    catalog.accept_snapshot(snapshot(sequence=1, epoch=2, key=new_key), NOW)       # 새 세대는 1 부터 받는다
    assert catalog.current_snapshot(NOW)["epoch"] == 2
    with pytest.raises(catalog.CatalogError):
        catalog.accept_delegation(delegation(epoch=1, sequence=5), NOW)            # 낮은 세대로 되돌리기 거절


def test_a_hand_edited_cache_is_not_trusted():
    catalog.accept_delegation(delegation(), NOW)
    catalog.accept_snapshot(snapshot(), NOW)
    path = catalog._file("snapshot.json")
    path.write_text(path.read_text(encoding="utf-8").replace('"sequence": 1', '"sequence": 7'), encoding="utf-8")
    assert catalog.current_snapshot(NOW) is None


def test_without_a_root_key_the_catalog_is_off(monkeypatch):
    monkeypatch.setattr(catalog, "ROOT_KEYS", ())
    with pytest.raises(catalog.CatalogError, match="root"):
        catalog.accept_delegation(delegation(), NOW)


# ── 받아 설치 (계약 6절) ─────────────────────────────────

def packed_reader():
    """제작소에서 배포 사본 → 시험 → 포장한 진짜 zip (가짜 룬 'reader')."""
    import shutil
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_release import FIXTURES, case_with_fixtures, installed_rune
    from vader.core import config, seal, workshop

    config.set_dev_mode(True)
    installed_rune()
    workshop.release_copy("reader")
    home = workshop.project("reader")
    case_with_fixtures(home, FIXTURES)
    workshop.allow_test("reader", seal.fingerprint(home / "reader"))
    assert workshop.run_test("reader")["ok"]
    archive = Path(workshop.pack("reader")["archive"])
    shutil.rmtree(config.rune_dir() / "reader")          # 받는 PC 에는 아직 없다
    return archive


def publish(archive, revocations=(), tamper_snapshot=False, sequence=1):
    import hashlib
    import zipfile

    data = archive.read_bytes()
    with zipfile.ZipFile(archive) as book:
        manifest = book.read("manifest.json")
    entry = {"rune_id": "reader", "version": "0.1.0", "archive_sha256": hashlib.sha256(data).hexdigest(),
             "manifest_sha256": hashlib.sha256(manifest).hexdigest(), "size": len(data),
             "profile": "legacy", "permissions": []}
    release = raw(catalog.sign("release", {"schema": 1, "purpose": "release", **entry, "submission_id": "s1",
                                           "revision": 1, "publisher_account_id": "p1", "approved_at": iso(NOW),
                                           "epoch": 1}, KEY))
    listed = {**entry, "name": "읽기", "summary": "", "archive_sha256": "0" * 64} if tamper_snapshot else \
        {**entry, "name": "읽기", "summary": ""}
    catalog.accept_delegation(delegation(), NOW)
    doc = catalog.sign("snapshot", {"schema": 1, "purpose": "snapshot", "epoch": 1, "sequence": sequence,
                                    "issued_at": iso(NOW), "expires_at": iso(NOW + timedelta(days=7)),
                                    "releases": [listed], "revocations": list(revocations)}, KEY)
    catalog.accept_snapshot(raw(doc), NOW)
    return release, entry


def test_a_catalog_package_installs_only_when_every_digest_agrees():
    from vader.core import config

    archive = packed_reader()
    release, entry = publish(archive)
    done = catalog.install_catalog(archive, release, NOW)
    origin = catalog.origins()["reader"]
    assert origin["state"] == "installed" and origin["archive_sha256"] == entry["archive_sha256"]
    assert catalog.verdict("reader", NOW) == catalog.ALLOW_ALL
    assert (config.rune_dir() / "reader") == done.path


def test_a_changed_zip_or_a_snapshot_that_disagrees_is_refused(tmp_path):
    archive = packed_reader()
    release, _ = publish(archive, tamper_snapshot=True)
    with pytest.raises(catalog.CatalogError, match="다릅니다"):
        catalog.install_catalog(archive, release, NOW)

    other = tmp_path / "other.zip"
    other.write_bytes(archive.read_bytes() + b"\0")
    (catalog._file("snapshot.json")).unlink()
    release, _ = publish(archive, sequence=2)                     # 캐시를 지워도 순번은 이어서 센다
    with pytest.raises(catalog.CatalogError):
        catalog.install_catalog(other, release, NOW)


def test_a_recalled_version_is_not_installed():
    import hashlib

    archive = packed_reader()
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    release, _ = publish(archive, revocations=[{"rune_id": "reader", "version": "0.1.0", "archive_sha256": digest,
                                                "level": "operational", "reason": "오류"}])
    with pytest.raises(catalog.CatalogError, match="회수"):
        catalog.install_catalog(archive, release, NOW)


def test_an_install_cut_off_halfway_stays_blocked(monkeypatch):
    from vader.core import installer

    archive = packed_reader()
    release, _ = publish(archive)

    def killed(*a, **k):
        raise SystemExit("프로세스가 끝났다")              # 처리할 틈 없이 끝남 — 원장은 '설치 중' 으로 남는다
    monkeypatch.setattr(installer, "install", killed)
    with pytest.raises(SystemExit):
        catalog.install_catalog(archive, release, NOW)
    assert catalog.origins()["reader"]["state"] == "pending"
    assert not catalog.verdict("reader", NOW)["import"]


def test_a_handled_install_failure_restores_the_ledger(monkeypatch):
    from vader.core import installer

    archive = packed_reader()
    release, _ = publish(archive)

    def fails(*a, **k):
        raise OSError("디스크 오류")                        # 설치기가 파일을 되돌린 경우 — 원장도 되돌린다
    monkeypatch.setattr(installer, "install", fails)
    with pytest.raises(OSError):
        catalog.install_catalog(archive, release, NOW)
    assert "reader" not in catalog.origins() and "reader" not in catalog.managed()


def test_loader_and_runner_follow_the_verdict(monkeypatch):
    """불러오기는 discover 에서, 연습·실전은 runner.submit 에서 같은 판정을 쓴다."""
    from vader.core import config, runner
    from vader.core.loader import discover

    archive = packed_reader()
    release, _ = publish(archive)
    catalog.install_catalog(archive, release, NOW)
    # 시계를 고정한다 — 실제 시계에 기대면 NOW+7일(snapshot 만료) 전에는 실패했다 (2026-11-07)
    monkeypatch.setattr(catalog, "_now", lambda: NOW + timedelta(days=8))
    loaded, failures = discover(config.rune_dir())
    assert "reader" not in [r.manifest.id for r in loaded]           # 8일 뒤라 snapshot 이 만료
    assert any(f.rune_id == "reader" and "만료" in f.reason for f in failures)
    with pytest.raises(RuntimeError, match="만료"):
        runner._reject_unsealed("reader", dry_run=True)


# ── codex1 신뢰 코어 검토 반영 (2026-10-29) ──

def test_an_expired_delegation_or_a_too_old_epoch_is_not_used(monkeypatch):
    catalog.accept_delegation(delegation(), NOW)
    assert catalog.current_delegation(NOW + timedelta(days=400)) is None          # 위임 만료
    monkeypatch.setattr(catalog, "MIN_EPOCH", 2)
    assert catalog.current_delegation(NOW) is None                               # 앱이 최소 세대를 올렸다


def test_deleting_the_cache_does_not_reopen_an_older_snapshot():
    catalog.accept_delegation(delegation(), NOW)
    catalog.accept_snapshot(snapshot(sequence=5), NOW)
    (catalog._file("snapshot.json")).unlink()
    with pytest.raises(catalog.CatalogError, match="오래된"):
        catalog.accept_snapshot(snapshot(sequence=3), NOW)


def test_a_leaked_key_is_checked_directly_even_if_the_block_list_was_lost():
    installed()
    catalog.record_origin("supply_weekly", {**catalog.origins()["supply_weekly"], "fingerprint": None})
    catalog.accept_delegation(delegation(), NOW)
    catalog.accept_delegation(delegation(sequence=2, keys=(KEY,), compromised=(KEY,)), NOW)
    (catalog._file("blocked.json")).unlink()
    assert "유출" in catalog.verdict("supply_weekly", NOW)["reason"] or not catalog.verdict("supply_weekly", NOW)["import"]


def test_a_leaked_key_stays_blocked_for_an_account_that_was_not_open():
    """유출 위임은 A 계정이 받았고 설치본은 B 에 있다. 뒤 위임이 키를 목록에서 빼도 B 에서 막힌다 (2단계 codex1 P1-6)."""
    installed()
    catalog.accept_delegation(delegation(), NOW)
    catalog.accept_snapshot(snapshot(), NOW)
    assert catalog.verdict("supply_weekly", NOW)["live"]                         # 처음엔 쓸 수 있다
    ledger = catalog._file("origins.json")
    aside = ledger.with_name("b-account.json")
    ledger.rename(aside)                                                          # 지금은 다른 계정이 열려 있다
    catalog.accept_delegation(delegation(sequence=2, compromised=(KEY,)), NOW)    # 이 계정엔 설치본이 없다
    catalog.accept_delegation(delegation(sequence=3), NOW)                        # 뒤 위임이 유출 목록에서 뺐다
    aside.rename(ledger)                                                          # B 계정을 다시 연다
    assert not catalog.blocked_install("a" * 64)                                  # 차단 목록에는 안 들어갔다
    verdict = catalog.verdict("supply_weekly", NOW)
    assert verdict["level"] == "malicious" and "유출" in verdict["reason"] and not verdict["import"]
    assert catalog._file("compromised.json").parent == config.HOME / "catalog"


def test_a_lost_or_broken_ledger_does_not_turn_a_catalog_rune_into_a_local_one():
    installed()
    (catalog._file("origins.json")).unlink()
    assert not catalog.verdict("supply_weekly", NOW)["import"]                   # managed 표시가 남아 있다
    (catalog._file("origins.json")).write_text("[1, 2", encoding="utf-8")
    assert "손상" in catalog.verdict("supply_weekly", NOW)["reason"]


def test_a_catalog_rune_cannot_be_overwritten_or_a_blocked_one_reinstalled_by_a_normal_install(tmp_path):
    from vader.core import installer, seal

    source = tmp_path / "supply_weekly"
    source.mkdir()
    (source / "__init__.py").write_text("rune = None\n", encoding="utf-8")
    installed()
    with pytest.raises(installer.InstallError, match="목록에서 받은 룬"):
        installer.install(source)
    catalog._write("managed.json", [])
    catalog._block({f"fp:{seal.fingerprint(source)}": "악성 회수"})
    with pytest.raises(installer.InstallError, match="사용할 수 없는"):
        installer.install(source)


def test_a_malformed_payload_is_refused_not_crashed():
    bad = catalog.sign("snapshot", {"schema": 1, "purpose": "snapshot", "epoch": 1, "sequence": 1,
                                    "issued_at": iso(NOW), "expires_at": iso(NOW + timedelta(days=1)),
                                    "releases": [], "revocations": []}, KEY)
    bad["payload"]["schema"] = True
    catalog.accept_delegation(delegation(), NOW)
    with pytest.raises(catalog.CatalogError):
        catalog.accept_snapshot(raw(bad), NOW)
    catalog._write("managed.json", ["supply_weekly"])
    catalog._write("origins.json", {"supply_weekly": ["엉뚱한", "모양"]})
    assert not catalog.verdict("supply_weekly", NOW)["import"]


# ── 안내와 실행 중 회수 (계약 3·8절) ──

def test_notices_show_a_new_version_a_live_recall_and_a_late_refresh():
    import hashlib

    archive = packed_reader()
    release, entry = publish(archive)
    catalog.install_catalog(archive, release, NOW)
    newer = {**entry, "version": "0.2.0", "archive_sha256": "b" * 64, "name": "읽기", "summary": ""}
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    doc = catalog.sign("snapshot", {"schema": 1, "purpose": "snapshot", "epoch": 1, "sequence": 2,
                                    "issued_at": iso(NOW), "expires_at": iso(NOW + timedelta(days=1)),
                                    "releases": [{**entry, "name": "읽기", "summary": ""}, newer],
                                    "revocations": [{"archive_sha256": digest, "level": "operational", "reason": "계산 오류"}]}, KEY)
    catalog.accept_snapshot(raw(doc), NOW)
    kinds = {n["kind"]: n for n in catalog.notices(NOW)}
    assert kinds["update"]["version"] == "0.2.0"
    assert "계산 오류" in kinds["recalled"]["message"]
    assert "1일" in kinds["stale"]["message"] or "0일" in kinds["stale"]["message"]


def test_a_recall_that_arrives_mid_run_cancels_the_job_and_asks_for_a_restart(tmp_path):
    import hashlib
    from types import SimpleNamespace

    from vader.core.checkpoint import CheckpointStore
    from vader.core.runner import JobRunner

    archive = packed_reader()
    release, _ = publish(archive)
    catalog.install_catalog(archive, release, NOW)
    runner = JobRunner(checkpoints=CheckpointStore(tmp_path / "cp"), data_dir=tmp_path / "data")
    runner._jobs["j1"] = SimpleNamespace(job=SimpleNamespace(rune_id="reader", is_terminal=False),
                                         ctx=SimpleNamespace(dry_run=False))
    cancelled = []
    runner.cancel = cancelled.append
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    import vader.core.catalog as module
    real = module.verdict
    module.verdict = lambda rid, now=None: real(rid, NOW)          # 시험 시계로 판정한다
    try:
        doc = catalog.sign("snapshot", {"schema": 1, "purpose": "snapshot", "epoch": 1, "sequence": 2,
                                        "issued_at": iso(NOW), "expires_at": iso(NOW + timedelta(days=7)),
                                        "releases": [], "revocations": [{"archive_sha256": digest, "level": "malicious",
                                                                         "reason": "악성"}]}, KEY)
        catalog.accept_snapshot(raw(doc), NOW)
    finally:
        module.verdict = real
    assert cancelled == ["j1"] and "다시 여세요" in runner.exit_required["message"]


def test_a_lost_managed_mark_with_a_surviving_origin_is_blocked_not_local():
    installed()
    (catalog._file("managed.json")).unlink()                              # 관리 표시만 사라졌다
    v = catalog.verdict("supply_weekly", NOW)
    assert not v["import"] and v["level"] == "corrupt"


def test_a_directly_installed_rune_is_switched_to_the_plaza_version_only_when_asked(monkeypatch):
    """직접 설치한 같은 룬 — 묻지 않으면, 고친 녹화가 있으면, 디스크 판이 같거나 새것이면 바꾸지 않는다.
    바꿀 때는 예전 폴더를 룬 경로 밖에 통째로 보관하고, 교체 뒤 실패는 pending 으로 잠근다 (2026-11-26, codex1 회신)."""
    import os
    import shutil
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_release import installed_rune
    from vader.core import config, installer, seal

    def local_copy():
        shutil.rmtree(config.rune_dir() / "reader", ignore_errors=True)
        folder = installed_rune()
        rune = folder / "rune.py"
        rune.write_text(rune.read_text(encoding="utf-8").replace('version="0.1.0"', 'version="0.0.9"'), encoding="utf-8")
        return folder

    archive = packed_reader()
    release, _entry = publish(archive)
    local = local_copy()
    with pytest.raises(catalog.CatalogError, match="직접 설치한"):
        catalog.install_catalog(archive, release, NOW)
    (local / "recordings" / "nav.json").write_text("{}", encoding="utf-8")
    with pytest.raises(catalog.CatalogError, match="녹화"):
        catalog.install_catalog(archive, release, NOW, replace_local=True)
    assert (local / "recordings" / "nav.json").read_text(encoding="utf-8") == "{}" and "reader" not in catalog.origins()

    shutil.rmtree(local)
    installed_rune()                                       # 디스크는 광장과 같은 0.1.0 — 덮지 않는다
    with pytest.raises(catalog.CatalogError, match="같거나 더 새것"):
        catalog.install_catalog(archive, release, NOW, replace_local=True)

    local = local_copy()                                   # 버전을 문자열로 읽을 수 없으면 바꾸지 않는다
    rune = local / "rune.py"
    rune.write_text(rune.read_text(encoding="utf-8").replace('version="0.0.9"', "version=VERSION"), encoding="utf-8")
    before = seal.fingerprint(local)
    with pytest.raises(catalog.CatalogError, match="버전을 읽지 못해"):
        catalog.install_catalog(archive, release, NOW, replace_local=True)
    assert seal.fingerprint(local) == before and "reader" not in catalog.origins()

    local = local_copy()                                   # 링크가 있으면 보관본도 만들지 않는다
    (local / "outside.md").symlink_to(archive)
    with pytest.raises(catalog.CatalogError, match="링크"):
        catalog.install_catalog(archive, release, NOW, replace_local=True)
    assert not (config.data_dir() / "replaced-runes").exists() and "reader" not in catalog.origins()
    (local / "outside.md").unlink()
    import subprocess                                      # 룬 폴더 자신이 정션 — 원장·보관본 전에 거절
    elsewhere = config.data_dir() / "elsewhere"
    local.rename(elsewhere)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(local), str(elsewhere)], check=True, capture_output=True)
    with pytest.raises(catalog.CatalogError, match="링크"):
        catalog.install_catalog(archive, release, NOW, replace_local=True)
    assert not (config.data_dir() / "replaced-runes").exists() and "reader" not in catalog.origins()
    os.rmdir(local)

    local_copy()
    real = config.rune_config
    monkeypatch.setattr(config, "rune_config", lambda *a: (_ for _ in ()).throw(OSError("설정 읽기 실패")))
    with pytest.raises(OSError):                           # 새 파일이 자리 잡은 뒤 실패 — 로컬로 돌리지 않고 잠근다
        catalog.install_catalog(archive, release, NOW, replace_local=True)
    assert catalog.origins()["reader"]["state"] == "pending" and not catalog.verdict("reader", NOW)["import"]
    monkeypatch.setattr(config, "rune_config", real)

    catalog._restore_origin("reader", None)
    local = local_copy()
    (local / "내 메모.md").write_text("옮기지 않은 파일", encoding="utf-8")
    catalog.install_catalog(archive, release, NOW, replace_local=True)
    origin = catalog.origins()["reader"]
    assert origin["state"] == "installed" and not catalog.local_rune("reader")
    kept = Path(origin["replaced_local"])
    assert (kept / "내 메모.md").is_file() and installer._installed_version(kept) == "0.0.9"
    assert config.rune_dir() not in kept.parents


def test_only_the_item_itself_counts_as_a_link_not_a_junctioned_parent(tmp_path):
    """작업공간(상위)만 정션이면 그 아래 일반 룬 폴더는 링크가 아니다. 그 폴더 자신이 정션이면 링크다 (codex1 회신 4)."""
    import subprocess
    (tmp_path / "real" / "reader").mkdir(parents=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "ws"), str(tmp_path / "real")], check=True, capture_output=True)
    assert not catalog._linked(tmp_path / "ws" / "reader") and not catalog._linked(tmp_path / "real" / "reader")
    assert catalog._linked(tmp_path / "ws")
