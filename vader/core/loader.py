"""소스 트리 밖에 설치된 룬을 발견하고 검증한다."""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import cast

from vader.core import catalog, config, seal
from vader.sdk import Rune, RuneManifest


UNCONFIRMED = "설치한 뒤 룬 파일이 바뀌어 불러오지 않았습니다. 룬 설치로 다시 설치하세요."


@dataclass(frozen=True)
class LoadedRune:
    manifest: RuneManifest
    rune: Rune


@dataclass(frozen=True)
class LoadFailure:
    rune_id: str
    reason: str


def discover(
    rune_dir: Path | None = None,
) -> tuple[list[LoadedRune], list[LoadFailure]]:
    """설치 디렉터리의 룬을 각각 import하고 계약을 검증한다.

    디스크가 설치 여부의 원본이다. ``config.installed_rune_ids()``는 설정
    블록의 목록일 뿐이며, 파일이 없거나 설정이 없는 상태도 가능하므로
    룬 발견에 사용하지 않는다.

    **사람이 확인한 지문만 import 한다** (2026-10-14 합의). 설치 뒤 폴더가 바뀌었으면
    목록에 띄우는 것만으로 그 코드가 돌지 않게, 실패로만 남긴다.
    """

    root = Path(rune_dir) if rune_dir is not None else config.rune_dir()
    if not root.is_dir():
        return [], []

    loaded: list[LoadedRune] = []
    failures: list[LoadFailure] = []

    for directory in sorted(
        (path for path in root.iterdir() if path.is_dir()),
        key=lambda path: path.name,
    ):
        if directory.name == "__pycache__":
            continue

        if not seal.loadable(directory.name, directory):
            failures.append(LoadFailure(rune_id=directory.name, reason=UNCONFIRMED))
            continue
        # 목록에서 받은 룬은 판정표가 먼저다 — legacy 는 불러오는 것 자체가 실행이다 (신뢰 계약 v2 5절)
        allowed = catalog.verdict(directory.name)
        if not allowed["import"]:
            failures.append(LoadFailure(rune_id=directory.name, reason=allowed["reason"]))
            continue
        try:
            module = _load_package(directory)
            loaded.append(_validate_module(directory.name, module))
        except Exception as exc:
            failures.append(
                LoadFailure(
                    rune_id=directory.name,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )

    return loaded, failures


def module_name_for(directory: Path) -> str:
    """설치 룬이 `sys.modules` 에서 갖는 이름.

    경로 해시를 쓴다. 제거할 때 같은 이름을 지워야 하므로 규칙을 여기 한 곳에
    둔다 — 설치기가 자기 나름대로 계산하면 규칙이 갈라지고, 갈라지는 순간
    "제거했는데 아직 import 되는 룬" 이 생긴다.
    """
    resolved = str(Path(directory).resolve())
    suffix = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return f"_vader_installed_rune_{suffix}"


def purge_modules(directory: Path) -> int:
    """이 경로에서 로드된 모듈을 `sys.modules` 에서 지운다. 지운 개수를 돌려준다."""
    prefix = module_name_for(directory)
    stale = [n for n in sys.modules if n == prefix or n.startswith(prefix + ".")]
    for name in stale:
        del sys.modules[name]
    return len(stale)


def _load_package(directory: Path) -> ModuleType:
    entrypoint = directory / "__init__.py"
    if not entrypoint.is_file():
        raise ImportError("rune package has no __init__.py")

    module_name = module_name_for(directory)
    spec = importlib.util.spec_from_file_location(
        module_name,
        entrypoint,
        submodule_search_locations=[str(directory)],
    )
    if spec is None or spec.loader is None:
        raise ImportError("could not create an import spec")

    # 상대 import와 실행 중의 지연 import가 동작하도록 설치 패키지를
    # sys.modules에 둔다. 같은 경로를 다시 발견할 때는 새로 실행한다.
    purge_modules(directory)

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        purge_modules(directory)
        raise
    return module


def _validate_module(directory_name: str, module: ModuleType) -> LoadedRune:
    # 룬은 두 가지 모양으로 쓸 수 있다. `rune = MyRune()` 을 노출하면 그것이
    # 룬이고, 없으면 모듈 자체를 룬으로 본다(모듈 수준 manifest + run).
    #
    # 계약(contract.py)의 Rune Protocol 은 `def run(self, ...)` 라 인스턴스를
    # 전제하고 README 예제도 클래스다. 그런데 모듈 수준 함수도 구조적으로는
    # 같은 모양이라 작은 룬에는 그쪽이 간결하다. 둘 다 받는 것이 한 줄이므로
    # 하나를 고르는 대신 규약만 명시한다 — `rune` 이 있으면 그것이 이긴다.
    candidate = getattr(module, "rune", module)

    if not hasattr(candidate, "manifest"):
        raise ValueError("missing manifest")

    manifest = candidate.manifest
    if not isinstance(manifest, RuneManifest):
        raise TypeError("manifest is not a RuneManifest")
    if not manifest.id or not manifest.id.strip():
        raise ValueError("manifest.id is empty")
    if manifest.id != directory_name:
        raise ValueError(
            f"manifest.id {manifest.id!r} does not match directory {directory_name!r}"
        )
    if not callable(getattr(candidate, "run", None)):
        raise TypeError("run is missing or not callable")
    if not isinstance(candidate, Rune):
        raise TypeError("rune does not satisfy the Rune protocol")

    return LoadedRune(manifest=manifest, rune=cast(Rune, candidate))

