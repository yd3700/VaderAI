"""운영자 서명과 '검사한 것만 포장' (2026-10-14 codex1 합의 3번).

서명은 출처 표시다: 운영자 암호 없이는 못 만들고, 서명 뒤 manifest 를 바꾸면 깨지고, 받는 PC 는
확인 번호를 확인한 서명자만 '등록됨' 으로 본다.
"""
import json
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from vader.core import config, seal, signing, workshop  # noqa: E402
from vader.core.installer import InstallError, install_package, preview  # noqa: E402

from tests.test_table_profile import make_project  # noqa: E402

PASS = "암호는여덟자이상"


@pytest.fixture
def packed(tmp_path, monkeypatch):
    """운영자 홈을 임시로 두고, 시험·포장까지 마친 표 룬 zip."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    config.set_dev_mode(True)
    home = make_project()
    workshop.allow_test("dept_totals", seal.fingerprint(home / "dept_totals"))
    assert workshop.run_test("dept_totals")["ok"]
    return Path(workshop.pack("dept_totals")["archive"])


def test_a_signed_package_is_trusted_once_the_receiver_registers_the_signer(packed, tmp_path):
    me = signing.create_key("마스터", PASS)
    assert b"ENCRYPTED" in (signing.key_dir() / "operator.key").read_bytes()   # 암호 없이는 못 쓴다
    signed = signing.sign(packed, PASS)

    first = preview(signed)["signature"]
    assert first["state"] == "unknown" and first["key_id"] == me["key_id"] and first["signer"] == "마스터"
    install_package(signed, tmp_path / "runes", trust_signer=True)
    assert preview(signed)["signature"]["state"] == "trusted"
    assert preview(packed)["signature"] == {"state": "none"}


def test_signing_needs_the_operators_passphrase(packed):
    signing.create_key("마스터", PASS)
    with pytest.raises(signing.SigningError, match="암호"):
        signing.sign(packed, "틀린암호입니다아")
    with pytest.raises(signing.SigningError, match="이미"):
        signing.create_key("다른 사람", PASS)   # 덮어쓰면 동료가 등록한 서명자가 끊긴다


def test_a_manifest_changed_after_signing_breaks_the_signature(packed, tmp_path):
    signing.create_key("마스터", PASS)
    signed = signing.sign(packed, PASS)
    forged = tmp_path / "forged.zip"
    with zipfile.ZipFile(signed) as src, zipfile.ZipFile(forged, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            if item.filename == "manifest.json":
                data = data.replace(b'"version": "1.0.0"', b'"version": "9.9.9"')
            dst.writestr(item, data)
    assert signing.verify(forged)["state"] == "invalid"
    with pytest.raises(InstallError, match="서명이 깨진"):
        install_package(forged, tmp_path / "runes")


def test_only_tested_table_runes_are_signed_for_sharing(packed, tmp_path):
    signing.create_key("마스터", PASS)
    legacy = tmp_path / "legacy.zip"
    with zipfile.ZipFile(packed) as src, zipfile.ZipFile(legacy, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            if item.filename == "manifest.json":
                data = json.dumps({**json.loads(data), "profile": "legacy"}).encode()
            dst.writestr(item, data)
    with pytest.raises(signing.SigningError, match="표 룬만"):
        signing.sign(legacy, PASS)


def test_pack_refuses_files_it_would_leave_out():
    """빠지는 파일이 있으면 시험한 지문과 포장 내용이 달라진다 — 검사한 그대로만 포장한다."""
    config.set_dev_mode(True)
    home = make_project()
    (home / "dept_totals" / "메모.xlsx").write_bytes(b"x")
    workshop.allow_test("dept_totals", seal.fingerprint(home / "dept_totals"))
    assert workshop.run_test("dept_totals")["ok"]
    with pytest.raises(workshop.WorkshopError, match="메모.xlsx"):
        workshop.pack("dept_totals")


def test_a_package_whose_contents_differ_from_the_tested_fingerprint_is_refused(packed, tmp_path):
    other = tmp_path / "other.zip"
    with zipfile.ZipFile(packed) as src, zipfile.ZipFile(other, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            if item.filename == "manifest.json":
                data = json.dumps({**json.loads(data), "fingerprint": "0" * 64}).encode()
            dst.writestr(item, data)
    with pytest.raises(InstallError, match="지문"):
        install_package(other, tmp_path / "runes")


def test_the_market_lists_my_signed_copies_and_registered_signers(packed, tmp_path):
    signing.create_key("마스터", PASS)
    signed = signing.sign(packed, PASS)
    mine, = signing.shared(workshop.root())
    assert mine["file"] == signed.name and mine["signer"] == "마스터" and mine["version"] == "1.0.0"

    install_package(signed, tmp_path / "runes", trust_signer=True)
    assert [s["name"] for s in signing.signers()] == ["마스터"]
