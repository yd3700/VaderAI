"""
룬 설치 — 소스 폴더를 `config.rune_dir()` 아래로 복사한다.

IchorAI의 `installer.py`는 소스 트리에 설치하고 빈 `NotImplementedError` 스텁을
만들었다(알려진 결함 #6). 그래서 패키징이 불가능했고 "룬 0개" 상태가 도달할 수
없었다. 여기서는 소스 트리 밖으로만 나간다(D8).

**검증은 로더가 한다.** 복사한 뒤 그 자리에서 로더의 규칙으로 읽어보고, 로드에
실패하면 되돌린다. 설치는 사람이 고르고 누른 것이라 그 지문을 '확인함' 으로 남긴다
(`seal.trust`) — 그 뒤에 바뀐 폴더는 로더가 불러오지 않는다. 설치기가 매니페스트를 따로 검사하면 로더와 규칙이 갈라지고,
갈라지는 순간 "설치는 됐는데 안 보이는 룬"이 생긴다.

설정은 건드리지 않는다. 무엇을 채울지는 사람이 정하고, 설치기는 **무엇이 비었는지
알려주기만** 한다. 빈 값을 미리 써 넣으면 룬의 필수값 검사가 통과해 버린다.
"""
from __future__ import annotations

import ast
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from vader.core import config, permissions, profile, seal
from vader.core.loader import LoadFailure, _load_package, _validate_module, purge_modules
from vader.sdk import RuneManifest


class InstallError(Exception):
    """설치가 끝나지 못했다. 대상 폴더는 원래 상태로 되돌려져 있다."""


@dataclass
class Installed:
    manifest: RuneManifest
    path: Path
    #: 아직 `settings.json`의 `runes.{id}`에 없는 필수 설정. 비어 있으면 바로 쓸 수 있다.
    missing_settings: list[str] = field(default_factory=list)
    #: 이 룬이 요구하는 권한을 사람이 읽는 문장으로. 설치기가 보여줄 것.
    granted: list[str] = field(default_factory=list)
    #: "new" | "upgrade" | "downgrade" | "reinstall" | "same_version"(코드는 바뀌었는데
    #: 버전이 그대로 — 화면 버전이 안 바뀌는 이유). 사용자에게 무엇이 일어났는지
    #: 말해주려고 남긴다. 어느 쪽도 막지 않는다 — 다운그레이드도 사용자의 선택이다.
    change: str = "new"
    #: 덮어쓴 이전 버전. 새 설치면 빈 문자열.
    previous_version: str = ""


def install(source: Path, rune_dir: Path | None = None, expect: str | None = None,
            merge_recordings: bool = True, catalog_install: bool = False) -> Installed:
    """`source` 폴더를 룬 디렉터리로 복사하고 로드되는지 확인한다.

    같은 id가 이미 있으면 덮어쓴다 — 재설치와 업그레이드가 같은 동작이다.
    실패하면 이전 설치본을 되살린다. `expect` 는 사람이 요약을 보고 확인한 지문이다
    (`preview`) — 그 사이에 바뀐 소스는 설치하지 않는다.
    """
    source = Path(source)
    if not (source / "__init__.py").is_file():
        raise InstallError(f"룬 패키지가 아닙니다(‘__init__.py’ 없음): {source}")
    if expect is not None and seal.fingerprint(source) != expect:
        raise InstallError("확인한 뒤에 룬 파일이 바뀌었습니다. 다시 골라 주세요.")
    # 목록 관리 룬·차단된 코드는 일반 설치로 들어오지 않는다 — import 하기 **전에** (codex1 2026-10-29 P1-5)
    from vader.core import catalog

    try:
        if not catalog_install and source.name in catalog.managed():
            raise InstallError("목록에서 받은 룬입니다. 일반 설치로 덮어쓰지 않습니다 — 목록에서 다시 받으세요.")
        reason = catalog.blocked_install(fingerprint=seal.fingerprint(source))
    except catalog.CatalogError as exc:
        raise InstallError(f"목록 상태를 확인하지 못해 설치하지 않습니다: {exc}") from None
    if reason:
        raise InstallError(f"사용할 수 없는 코드입니다 ({reason}).")

    root = Path(rune_dir) if rune_dir is not None else config.rune_dir()
    target = root / source.name
    root.mkdir(parents=True, exist_ok=True)

    # 복사 **전에** 권한을 본다. 선언하지 않은 능력을 쓰는 룬은 디스크에 닿지
    # 않는다. 샌드박스가 아니다 — permissions.py 독스트링이 무엇을 못 막는지
    # 적어 뒀다. 여기서 막는 것은 조용히 쓰는 것이다.
    missing = permissions.undeclared(_declared(source), source)
    if missing:
        detail = "; ".join(
            f"{capability} ({', '.join(reasons)})"
            for capability, reasons in sorted(missing.items())
        )
        raise InstallError(
            f"룬이 선언하지 않은 권한을 사용합니다: {detail}. "
            "매니페스트의 permissions에 추가하거나 해당 코드를 제거하세요."
        )

    # 표 룬이라고 적었으면 정말 표 룬 문법인지 본다 — 제작소를 거치지 않은 zip 도 같은 검사를 받는다.
    if profile.declared(source) == profile.PROFILE:
        broken = profile.check(source)
        if broken:
            raise InstallError("표 룬 규칙을 어긴 곳이 있습니다: " + "; ".join(broken[:5])
                               + (f" 외 {len(broken) - 5}곳" if len(broken) > 5 else ""))

    # 무엇이 일어나는지 먼저 안다. 체크포인트는 룬 버전이 다르면 무시되므로
    # (`checkpoint.py` 참조) 업그레이드가 옛 payload 로 깨지지 않는다.
    before = _installed_version(target)
    code_changed = target.exists() and _code(source) != _code(target)

    # 덮어쓰기 전에 옆으로 치워 둔다. 새 것이 로드에 실패하면 되돌린다.
    backup = target.with_name(f".{source.name}.backup")
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        target.rename(backup)

    try:
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
        # 새 소스에 녹화가 없으면 깔려 있던 녹화를 옮겨 둔다 — 업데이트 한 번에 사람이 한 녹화가 지워지지 않게 (2026-10-22).
        # 목록 설치는 합치지 않는다 — 설치 폴더가 서명된 파일 그대로여야 한다 (신뢰 계약 v2 6·7절).
        if merge_recordings and (backup / "recordings").is_dir() and not (target / "recordings").exists():
            shutil.copytree(backup / "recordings", target / "recordings")
        manifest = _verify(target)
        seal.trust(manifest.id, target)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        if backup.exists():
            backup.rename(target)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)

    installed = config.rune_config(manifest.id)
    return Installed(
        manifest=manifest,
        path=target,
        granted=permissions.describe(manifest.permissions),
        change=("same_version" if before == manifest.version and code_changed
                else _classify(before, manifest.version)),
        previous_version=before,
        missing_settings=[f.name for f in manifest.required_fields if f.name not in installed],
    )


#: 마지막 `install_bundled` 에서 실패한 룬 — 앱을 켤 때 한 번 채워지고 화면이 읽는다.
bundle_failures: list[LoadFailure] = []


def install_bundled(bundle: Path) -> list[str]:
    """설치본에 실린 룬을 깐다 — 아직 없거나 실린 쪽 버전이 더 높을 때만. 깐 id 들 (2026-10-21).

    새 PC 에서 zip 을 따로 받지 않아도 되고, 앱 업데이트가 룬 고침도 함께 나른다.
    같은 버전이면 안 건드린다 — 고침을 나르려면 룬 버전을 올린다.
    """
    done = []
    bundle_failures.clear()
    for source in sorted(bundle.iterdir()) if bundle.is_dir() else []:
        if not (source / "__init__.py").is_file():
            continue
        # 한 룬이 실패해도 나머지는 깐다. 실패는 숨기지 않고 화면의 '문제가 있는 룬' 에 남긴다 (2026-10-27).
        try:
            before = _installed_version(config.rune_dir() / source.name)
            if not before or _as_tuple(_manifest_string(source, "version")) > _as_tuple(before):
                install(source)
                done.append(source.name)
        except Exception as exc:
            bundle_failures.append(LoadFailure(source.name, f"기본 룬을 설치하지 못했습니다: {exc}"))
    return done


def uninstall(rune_id: str, rune_dir: Path | None = None) -> bool:
    """설치된 룬을 지운다. 없었으면 False.

    **설정과 체크포인트는 남긴다.** 지우면 다시 설치할 때 사용자가 손으로 채운
    값을 또 채워야 하고, 이어하던 작업도 잃는다. 제거는 룬을 떼는 것이지
    사용자 데이터를 버리는 것이 아니다. 정말 지우려면 별도 동작이어야 한다.

    `sys.modules` 를 함께 비운다. 안 그러면 폴더는 없는데 아직 import 되는 룬이
    남는다 — 같은 프로세스에서 제거 후 재설치하면 옛 코드가 돈다.
    """
    root = Path(rune_dir) if rune_dir is not None else config.rune_dir()
    target = root / rune_id
    if not target.is_dir():
        return False

    purge_modules(target)
    shutil.rmtree(target)
    return True


def _installed_version(target: Path) -> str:
    """이미 설치된 룬의 버전. 없으면 빈 문자열.

    AST 로 읽는다 — 덮어쓰기 전이라 아직 검증되지 않은 코드를 실행하지 않는다.
    """
    if not target.exists():
        return ""
    return _manifest_string(target, "version")


def _code(folder: Path) -> dict:
    """룬의 파이썬 소스. 버전을 안 올린 수정을 알아채려고 본다 — 시험 산출물 같은 자료 파일은 빼고."""
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*.py")
            if "__pycache__" not in p.parts}


def _classify(before: str, after: str) -> str:
    """무엇이 일어났는지. 순서 비교는 숫자 부분만 본다."""
    if not before:
        return "new"
    if before == after:
        return "reinstall"
    return "upgrade" if _as_tuple(after) > _as_tuple(before) else "downgrade"


def _as_tuple(version: str) -> tuple:
    """"1.2.10" -> (1, 2, 10). 숫자가 아닌 조각은 0 으로 둔다.

    완전한 semver 비교가 아니다 — 프리릴리스 표기(`1.0.0-rc1`)를 다루지 않는다.
    ponytail: 필요해지면 packaging.version 을 쓴다. 지금은 선택적 의존성을
    코어에 들이는 비용이 얻는 것보다 크다.
    """
    parts = []
    for piece in str(version).split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _declared(source: Path) -> list[str]:
    """복사 전에 매니페스트의 permissions 만 읽는다.

    import 하지 않고 AST 로 읽는다 — 아직 검증하지 않은 코드를 실행하지 않기
    위해서다. 권한 검사가 import 를 요구하면 검사 전에 룬 코드가 도는 셈이 된다.

    못 읽으면 빈 목록이다. 그러면 능력을 쓰는 룬은 전부 거부되고, 매니페스트를
    읽을 수 없는 룬은 뒤의 `_verify` 가 어차피 막는다.
    """
    for keyword in _manifest_keywords(source):
        if keyword.arg == "permissions" and isinstance(keyword.value, ast.List):
            return [
                element.value
                for element in keyword.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
    return []


def _manifest_string(source: Path, field_name: str) -> str:
    for keyword in _manifest_keywords(source):
        if keyword.arg == field_name and isinstance(keyword.value, ast.Constant):
            if isinstance(keyword.value.value, str):
                return keyword.value.value
    return ""


def _manifest_keywords(source: Path):
    """소스에서 `RuneManifest(...)` 호출의 키워드 인자를 훑는다."""
    for path in sorted(Path(source).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name == "RuneManifest":
                yield from node.keywords


#: 패키지 하나가 풀려서 이보다 크면 거부한다 (압축 폭탄).
PACKAGE_LIMIT_BYTES = 50 * 1024 * 1024
PACKAGE_FILE_LIMIT = 2000
MANIFEST_LIMIT_BYTES = 1024 * 1024
#: Windows 가 장치로 읽는 이름 — 확장자가 붙어도 장치다
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


SIGNATURE_LIMIT_BYTES = 64 * 1024
#: 한 파일의 풀린 크기 / 압축 크기. 이보다 크면 압축 폭탄으로 본다 (1MB 넘는 파일만 본다).
RATIO_LIMIT = 200


def check_archive(archive: Path) -> None:
    """zip 을 **읽기 전에** 목차만 보고 거른다 — 항목 수·크기·압축비·같은 이름·manifest/signature 하나씩.

    내용을 읽는 모든 진입점(서명 확인·미리보기·풀기) 앞에 둔다. 경로 모양은 `_unpack` 이 본다.
    """
    import zipfile

    try:
        if Path(archive).stat().st_size > PACKAGE_LIMIT_BYTES:   # 목차를 읽기 전에 압축된 크기부터
            raise InstallError("룬 패키지가 너무 큽니다")
        with zipfile.ZipFile(archive) as book:
            entries = book.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise InstallError(f"룬 패키지(zip)를 열 수 없습니다: {exc}") from exc
    if len(entries) > PACKAGE_FILE_LIMIT * 2:          # 폴더 항목까지 센다
        raise InstallError("룬 패키지의 항목이 너무 많습니다")
    names = [e.filename for e in entries]
    if len({n.casefold() for n in names}) != len(names):
        raise InstallError("룬 패키지에 같은 이름의 항목이 둘 있습니다")
    sizes = {e.filename: e.file_size for e in entries}
    if "manifest.json" not in sizes:
        raise InstallError("룬 패키지에 manifest.json 이 없습니다")
    if sizes["manifest.json"] > MANIFEST_LIMIT_BYTES or sizes.get("signature.json", 0) > SIGNATURE_LIMIT_BYTES:
        raise InstallError("manifest.json 이나 signature.json 이 너무 큽니다")
    if sum(sizes.values()) > PACKAGE_LIMIT_BYTES:
        raise InstallError("룬 패키지가 너무 큽니다")
    if any(e.file_size > 1024 * 1024 and e.file_size > RATIO_LIMIT * max(e.compress_size, 1) for e in entries):
        raise InstallError("룬 패키지에 비정상적으로 압축된 파일이 있습니다")


def _safe_part(part: str) -> bool:
    r"""zip 경로의 한 마디가 Windows 에서도 그 폴더 안의 평범한 이름인가.

    Windows 의 zipfile 은 읽을 때 역슬래시를 `/` 로 바꿔 `..` 검사에 걸리게 한다 (2026-10-29 실측).
    그래도 역슬래시를 거부한다 — 서버·다른 OS 가 같은 zip 을 다르게 읽지 않게. Windows 가 특별히
    다루는 이름(드라이브·ADS `:`·예약 장치 이름·끝 점/공백)도 막는다 (codex1 검토).
    """
    return (part not in ("", ".", "..") and not any(c in part for c in r'\:*?"<>|')
            and not any(ord(c) < 32 for c in part) and part == part.rstrip(". ")
            and part.split(".")[0].casefold() not in _RESERVED)


def install_package(archive: Path, rune_dir: Path | None = None, expect: str | None = None,
                    trust_signer: bool = False) -> Installed:
    """제작소가 만든 zip(`manifest.json` + `<id>/...` [+ `signature.json`])을 검사해 풀고 `install` 한다.

    manifest 의 파일 목록·해시와 실제 내용이 하나라도 다르면 거부한다 — 포장 뒤에
    바뀐 패키지가 '시험한 그것' 행세를 하지 못하게. 안전 인증이 아니라 무변조 확인이다.
    서명이 있는데 깨졌으면 거부한다. `trust_signer` 는 사람이 확인 번호를 확인하고 서명자를 등록한다는 뜻이다.
    """
    import tempfile

    from vader.core import signing

    check_archive(archive)            # 서명 확인도 zip 을 읽는다 — 크기·개수 검사를 그보다 먼저 (codex1 2026-10-29)
    import hashlib

    from vader.core import catalog

    try:
        if catalog.blocked_install(hashlib.sha256(Path(archive).read_bytes()).hexdigest()):
            raise InstallError("회수된 패키지입니다. 설치하지 않습니다.")
    except catalog.CatalogError as exc:
        raise InstallError(f"목록 상태를 확인하지 못해 설치하지 않습니다: {exc}") from None
    status = signing.verify(archive)
    if status["state"] == "invalid":
        raise InstallError("서명이 깨진 패키지입니다. 서명한 뒤 바뀌었을 수 있습니다. 보낸 사람에게 다시 받으세요.")
    with tempfile.TemporaryDirectory(prefix="vader-pkg-") as tmp:
        done = install(_unpack(Path(archive), Path(tmp)), rune_dir, expect)
    if trust_signer and status["state"] == "unknown":
        signing.trust(status)
    return done


def preview(chosen: Path) -> dict:
    """설치 전에 사람에게 보여 줄 요약과 지문. **코드를 실행하지 않는다** (AST 만 읽는다).

    zip 이면 임시로 풀어 본다. 설치는 이 지문을 `expect` 로 넘겨 — 본 것과 같은 것만 설치한다.
    """
    import tempfile

    chosen = Path(chosen)
    with tempfile.TemporaryDirectory(prefix="vader-pkg-") as tmp:
        zipped = chosen.suffix.lower() == ".zip"
        if zipped:
            check_archive(chosen)
        source = _unpack(chosen, Path(tmp)) if zipped else chosen
        if not (source / "__init__.py").is_file():
            raise InstallError(f"룬 패키지가 아닙니다(‘__init__.py’ 없음): {source}")
        declared = _declared(source)
        return {"id": _manifest_string(source, "id") or source.name, "name": _manifest_string(source, "name"),
                "version": _manifest_string(source, "version"), "fingerprint": seal.fingerprint(source),
                "profile": "table" if profile.declared(source) == profile.PROFILE else "legacy",
                "signature": _signature(chosen) if zipped else {"state": "none"},
                "tested": _tested(chosen) if zipped else None,
                "granted": permissions.describe(declared), **permissions.summary(source)}


def _tested(archive: Path) -> dict | None:
    """포장 기록의 시험 정보 (언제·어느 사례). 제작자 PC 의 기록이라 독립 검증은 아니다."""
    import json
    import zipfile

    with zipfile.ZipFile(archive) as book:
        return json.loads(book.read("manifest.json")).get("tested")


def _signature(archive: Path) -> dict:
    from vader.core import signing

    status = signing.verify(archive)
    if status["state"] == "invalid":
        raise InstallError("서명이 깨진 패키지입니다. 서명한 뒤 바뀌었을 수 있습니다. 보낸 사람에게 다시 받으세요.")
    return status


def _unpack(archive: Path, into: Path) -> Path:
    """zip 을 검사하며 `into/<id>` 에 푼다."""
    import hashlib

    check_archive(archive)
    import json
    import zipfile

    try:
        book = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise InstallError(f"룬 패키지(zip)를 열 수 없습니다: {exc}") from exc
    with book:
        try:
            if book.getinfo("manifest.json").file_size > MANIFEST_LIMIT_BYTES:
                raise InstallError("manifest.json 이 너무 큽니다")
            manifest = json.loads(book.read("manifest.json"))
        except (KeyError, ValueError) as exc:
            raise InstallError("룬 패키지에 manifest.json 이 없습니다") from exc
        rune_id = str(manifest.get("id", ""))
        expected = manifest.get("files") or {}
        if not rune_id.isidentifier() or not expected:
            raise InstallError("manifest.json 의 id 또는 파일 목록이 올바르지 않습니다")
        members = [m for m in book.infolist() if not m.is_dir() and m.filename not in ("manifest.json", "signature.json")]
        if sum(m.file_size for m in members) > PACKAGE_LIMIT_BYTES or len(members) > PACKAGE_FILE_LIMIT:
            raise InstallError("룬 패키지가 너무 큽니다")
        names, folded = {}, set()
        for member in members:
            relative = PurePosixPath(member.filename)
            if (relative.is_absolute() or relative.parts[0] != rune_id or len(relative.parts) < 2
                    or not all(_safe_part(part) for part in relative.parts)):
                raise InstallError(f"룬 패키지에 허용되지 않는 경로가 있습니다: {member.filename}")
            name = PurePosixPath(*relative.parts[1:]).as_posix()
            if name.casefold() in folded:   # Windows 는 대소문자를 안 가린다 — 같은 파일을 두 번 쓰게 된다
                raise InstallError(f"룬 패키지에 같은 이름의 파일이 둘 있습니다: {member.filename}")
            folded.add(name.casefold())
            names[name] = member
        if set(names) != set(expected):
            raise InstallError("룬 패키지의 파일이 manifest.json 목록과 다릅니다")
        for name, member in names.items():
            data = book.read(member)
            if hashlib.sha256(data).hexdigest() != expected[name]:
                raise InstallError(f"포장 뒤에 바뀐 파일이 있습니다: {name}")
            target = Path(into, rune_id, *name.split("/"))
            if not target.resolve().is_relative_to(Path(into, rune_id).resolve()):
                raise InstallError(f"룬 패키지에 허용되지 않는 경로가 있습니다: {member.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    # 시험한 지문과 같아야 한다 — 시험한 것과 포장된 것이 같은 것이라는 연결 (2026-10-14 합의 3번).
    recorded = manifest.get("fingerprint")
    if recorded and recorded != seal.fingerprint(Path(into, rune_id)):
        raise InstallError("포장 기록의 지문과 패키지 내용이 다릅니다. 제작소에서 다시 시험·포장하세요.")
    return Path(into, rune_id)


def _verify(target: Path) -> RuneManifest:
    """설치된 자리에서 이 룬 **하나만** 로더의 규칙으로 읽어 본다 — 다른 룬의 코드는 돌리지 않는다."""
    try:
        return _validate_module(target.name, _load_package(target)).manifest
    except Exception as exc:
        purge_modules(target)
        raise InstallError(f"설치했으나 로드되지 않습니다: {type(exc).__name__}: {exc}") from exc
