"""운영자 서명 — 동료에게 줄 룬 패키지의 출처 표시 (2026-10-14 codex1 합의 3번).

서명은 **"이 운영자가 검토하고 내보낸 바로 그 패키지다"** 라는 증거다. 안전 보증이 아니다.

- 서명하는 것: 패키지의 `manifest.json` 바이트. 그 안에 파일별 sha256·id·버전·프로필·시험 기록이 있고,
  설치기가 파일을 그 해시와 대조하므로 manifest 하나에 서명하면 내용 전체가 묶인다.
- 키: Ed25519. 개인키는 `~/.vaderai-operator/` 에 **운영자만 아는 암호로 잠가** 둔다. 같은 Windows 계정의
  프로그램(룬을 만드는 LLM 포함)은 파일을 읽을 수 있어도 암호 없이는 서명하지 못한다 — 이것이 '제작·시험
  환경과의 분리' 다. 암호를 코드·설정·로그에 남기지 않는다.
- 받는 PC: 서명자를 **한 번 등록**한다. 등록 전에 확인 번호를 운영자에게 다른 길(전화·대면)로 확인한다.
  서명이 깨졌으면 설치하지 않는다. 등록되지 않은 서명자는 '확인 안 됨' 으로 보여 준다.

공개 카탈로그 단계의 서명은 운영 서비스가 따로 한다 — 이 개인 키를 그쪽으로 옮기지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from datetime import datetime
from pathlib import Path

from vader.core import config

SIGNATURE = "signature.json"
#: 운영자 키 자리. 제작 공간(`~/.vaderai/workshop`)과 다른 폴더다.
KEY_DIR_NAME = ".vaderai-operator"
MIN_PASSPHRASE = 8


class SigningError(ValueError):
    """사람에게 그대로 보여 줄 수 있는 거절."""


def key_dir() -> Path:
    return Path.home() / KEY_DIR_NAME


def operator() -> dict | None:
    """이 PC 의 운영자 키 정보(이름·확인 번호). 없으면 None. 개인키는 읽지 않는다."""
    try:
        return json.loads((key_dir() / "operator.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def create_key(name: str, passphrase: str) -> dict:
    """운영자 키를 만든다. 이미 있으면 거부한다 — 덮어쓰면 동료가 등록한 서명자가 끊긴다."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    name = str(name).strip()
    if not name:
        raise SigningError("서명자 이름을 적어 주세요 (동료에게 보이는 이름).")
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise SigningError(f"서명 암호는 {MIN_PASSPHRASE}자 이상이어야 합니다.")
    if operator() is not None:
        raise SigningError("이 PC 에는 이미 운영자 키가 있습니다.")
    private = Ed25519PrivateKey.generate()
    public = _raw(private.public_key())
    folder = key_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "operator.key").write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(passphrase.encode("utf-8"))))
    info = {"name": name, "public_key": base64.b64encode(public).decode(), "key_id": key_id(public),
            "created_at": _now()}
    (folder / "operator.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def sign(archive: Path, passphrase: str) -> Path:
    """검사를 통과한 표 룬 패키지에 서명해 `<이름>-signed.zip` 을 만든다. 원본 zip 은 그대로 둔다."""
    from cryptography.hazmat.primitives import serialization

    me = operator()
    if me is None:
        raise SigningError("운영자 키가 없습니다. 먼저 서명 키를 만드세요.")
    archive = Path(archive)
    manifest_bytes, members = _read(archive)
    manifest = json.loads(manifest_bytes)
    if manifest.get("profile") != "table":
        raise SigningError("표 룬만 공유용으로 서명합니다. 기존 방식 룬은 이 PC 에서만 씁니다.")
    if not manifest.get("tested"):
        raise SigningError("시험 기록이 없는 패키지입니다. 제작소에서 시험·포장한 zip 을 고르세요.")
    _check_contents(archive)
    try:
        private = serialization.load_pem_private_key((key_dir() / "operator.key").read_bytes(),
                                                     password=passphrase.encode("utf-8"))
    except (TypeError, ValueError):
        raise SigningError("서명 암호가 맞지 않습니다.") from None
    signature = {"signer": me["name"], "key_id": me["key_id"], "public_key": me["public_key"],
                 "signature": base64.b64encode(private.sign(manifest_bytes)).decode(), "signed_at": _now()}
    out = archive.with_name(archive.stem.removesuffix("-signed") + "-signed.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", manifest_bytes)
        for name, data in members.items():
            z.writestr(name, data)
        z.writestr(SIGNATURE, json.dumps(signature, ensure_ascii=False, indent=2))
    return out


def verify(archive: Path) -> dict:
    """서명 상태. `state`: none(서명 없음) · trusted(등록된 서명자) · unknown(올바르지만 등록 안 됨) · invalid(깨짐)."""
    from vader.core.installer import InstallError, check_archive

    try:
        check_archive(archive)        # 읽기 전에 목차부터 (codex1 2026-10-29)
    except InstallError:
        return {"state": "invalid"}
    with zipfile.ZipFile(archive) as book:
        if SIGNATURE not in book.namelist():
            return {"state": "none"}
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        try:
            signed = json.loads(book.read(SIGNATURE))
            public = base64.b64decode(signed["public_key"])
            Ed25519PublicKey.from_public_bytes(public).verify(base64.b64decode(signed["signature"]),
                                                             book.read("manifest.json"))
        except (KeyError, ValueError, TypeError, InvalidSignature):
            return {"state": "invalid"}
    found = {"signer": str(signed.get("signer", "")), "key_id": key_id(public), "public_key": signed["public_key"]}
    known = _signers().get(found["key_id"])
    return {**found, "state": "trusted" if known and known.get("public_key") == found["public_key"] else "unknown"}


def trust(status: dict) -> None:
    """받는 PC 에 서명자를 등록한다. 사람이 확인 번호를 다른 길로 확인한 뒤에만 부른다."""
    if status.get("state") not in ("unknown", "trusted"):
        raise SigningError("올바른 서명이 아니라 등록할 수 없습니다.")
    signers = _signers()
    signers[status["key_id"]] = {"name": status["signer"], "public_key": status["public_key"], "added_at": _now()}
    path = _signers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(signers, ensure_ascii=False, indent=2), encoding="utf-8")


def signers() -> list[dict]:
    """이 PC 에 등록한 서명자 (공개키는 빼고)."""
    return [{"key_id": k, "name": v.get("name", ""), "added_at": v.get("added_at", "")}
            for k, v in sorted(_signers().items())]


def shared(base: Path) -> list[dict]:
    """`base/<id>/dist/` 의 서명한 공유본. 서명 상태는 다시 확인한다 — 파일 이름만 믿지 않는다."""
    out = []
    for archive in sorted(Path(base).glob("*/dist/*-signed.zip")):
        try:
            status = verify(archive)
            if status["state"] == "invalid":   # 목차 검사에 걸린 zip 은 열지 않는다 (codex1 2026-10-29)
                continue
            with zipfile.ZipFile(archive) as book:
                manifest = json.loads(book.read("manifest.json"))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            continue
        if status["state"] == "invalid":
            continue
        out.append({"file": archive.name, "path": str(archive), "id": manifest.get("id", ""),
                    "name": manifest.get("name", ""), "version": manifest.get("version", ""),
                    "signer": status.get("signer", ""), "size": archive.stat().st_size,
                    "at": datetime.fromtimestamp(archive.stat().st_mtime).isoformat(timespec="minutes")})
    return out


def key_id(public: bytes) -> str:
    """사람이 전화로 불러 줄 수 있는 확인 번호. 공개키 sha256 앞 16자리를 네 묶음으로."""
    digest = hashlib.sha256(public).hexdigest()[:16].upper()
    return "-".join(digest[i:i + 4] for i in range(0, 16, 4))


def _check_contents(archive: Path) -> None:
    """설치기와 같은 검사로 풀어 본다 — 파일 해시와 시험한 지문이 manifest 와 맞아야 서명한다."""
    import tempfile

    from vader.core.installer import InstallError, _unpack

    with tempfile.TemporaryDirectory(prefix="vader-sign-") as tmp:
        try:
            _unpack(archive, Path(tmp))
        except InstallError as exc:
            raise SigningError(str(exc)) from exc


def _read(archive: Path) -> tuple[bytes, dict[str, bytes]]:
    from vader.core.installer import InstallError, check_archive

    try:
        check_archive(archive)
    except InstallError as exc:
        raise SigningError(str(exc)) from None
    try:
        with zipfile.ZipFile(archive) as book:
            members = {i.filename: book.read(i) for i in book.infolist()
                       if not i.is_dir() and i.filename not in ("manifest.json", SIGNATURE)}
            return book.read("manifest.json"), members
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise SigningError(f"룬 패키지(zip)를 읽을 수 없습니다: {exc}") from exc


def _raw(public) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _signers_path() -> Path:
    return config.data_dir() / "signers.json"


def _signers() -> dict:
    try:
        data = json.loads(_signers_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
