"""룬 zip 의 경로가 Windows 에서 설치 폴더 밖으로 나가지 못한다 (codex1 2026-10-29 P1-3)."""
from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from vader.core.installer import InstallError, install_package


def _zip(tmp_path, names: list[str]):
    archive = tmp_path / "pkg.zip"
    files = {name.split("/", 1)[1]: hashlib.sha256(b"x = 1\n").hexdigest() for name in names}
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", json.dumps({"id": "demo", "files": files}))
        for name in names:
            z.writestr(name, "x = 1\n")
    return archive


@pytest.mark.parametrize("bad", [
    # 역슬래시: Windows 의 zipfile 은 읽을 때 `/` 로 바꾼다 — `..` 로 걸린다 (2026-10-29 확인).
    # 다른 OS·서버에서 풀 때를 위해 `_safe_part` 도 역슬래시를 거부한다.
    r"demo/..\..\escaped.py",
    "demo/c:x.py",                  # 드라이브·ADS
    "demo/con.py",                  # 예약 장치 이름
    "demo/x.py.",                   # 끝 점·공백은 Windows 가 지운다
    "demo/x.py ",
    "demo/../escaped.py",
])
def test_a_path_that_windows_reads_outside_the_folder_is_refused(tmp_path, bad):
    with pytest.raises(InstallError, match="허용되지 않는 경로"):
        install_package(_zip(tmp_path, [bad, "demo/__init__.py"]), tmp_path / "runes")
    assert not (tmp_path / "escaped.py").exists() and not (tmp_path.parent / "escaped.py").exists()


def test_two_names_that_differ_only_by_case_are_refused(tmp_path):
    with pytest.raises(InstallError, match="같은 이름"):
        install_package(_zip(tmp_path, ["demo/A.py", "demo/a.py"]), tmp_path / "runes")


def test_oversized_metadata_and_bombs_are_refused_before_anything_reads_them(tmp_path, monkeypatch):
    """서명 확인도 zip 을 읽는다 — 목차 검사가 그보다 먼저다 (codex1 2026-10-29)."""
    from vader.core import signing

    monkeypatch.setattr(signing, "verify", lambda archive: pytest.fail("목차 검사 전에 읽었다"))
    cases = {
        "big_manifest": {"manifest.json": "x" * (2 * 1024 * 1024)},
        "big_signature": {"manifest.json": "{}", "signature.json": "x" * (128 * 1024)},
        "bomb": {"manifest.json": "{}", "demo/a.py": "0" * (8 * 1024 * 1024)},
        "dup_manifest": {"manifest.json": "{}", "MANIFEST.json": "{}"},
        "no_manifest": {"demo/a.py": "x"},
    }
    for name, members in cases.items():
        archive = tmp_path / f"{name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
            for member, text in members.items():
                z.writestr(member, text)
        with pytest.raises(InstallError):
            install_package(archive, tmp_path / "runes")


def test_signature_paths_check_the_table_of_contents_first(tmp_path):
    """서명 확인·서명하기도 내용을 읽기 전에 목차 검사를 한다 (codex1 2026-10-29 계약 검토 P2-1)."""
    pytest.importorskip("cryptography")
    from vader.core import signing

    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", "{}")
        z.writestr("signature.json", "x" * (128 * 1024))
    assert signing.verify(archive) == {"state": "invalid"}
    with pytest.raises(signing.SigningError):
        signing._read(archive)
