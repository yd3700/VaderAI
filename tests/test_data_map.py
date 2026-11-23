"""데이터 지도가 코드와 일치하는지 본다.

`observation-boundary.md` 는 관찰 데이터를 어디로도 안 보낸다고 약속했고,
그 근거로 **"코어에 네트워크 코드가 없다"** 를 들었다. 2026-08-05 에 그게
거짓이 됐다 — 세 번째 룬이 외부 API 를 불러야 해서 `vader/core/http.py` 가
생겼다.

관찰을 보내는 코드는 여전히 없다. 하지만 **약속을 떠받치던 구조적 보장이
"안 쓴다는 규율" 로 내려앉았다.** 규율은 잊힌다. 그래서 검사로 바꾼다.

설계는 `wiki/architecture/data-map.md`.
"""
from __future__ import annotations
from tests.private import needs

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: 네트워크가 실제로 지나는 파일. **여기 줄을 더하려면 데이터 지도를 같이
#: 고쳐야 한다** — 문이 하나 늘어난다는 뜻이다.
#:
#: 2026-08-08 부터 **양방향**이다. Phase 15 의 `server.py` 는 들어오는 문이라
#: 지키는 것이 반대다 — 나가는 문은 "무엇을 보내나", 들어오는 문은 "누구를
#: 들이나". 규칙 2(관찰을 못 본다)는 방향과 무관하게 그대로 적용된다.
NETWORK_DOORS = {
    "vader/core/http.py",
    "vader/core/ocr/gemini.py",
    "vader/core/ocr/claude.py",
    "vader/core/server.py",
    "vader/core/account.py",
    "vader/core/catalog_sync.py",
}

#: 관찰에서 나온 것을 다루는 파일.
OBSERVATION_MODULES = {
    "vader/core/observe.py",
    "vader/core/character.py",
}

#: 실제로 바깥과 이야기하는 것들. `urlencode` 같은 문자열 조립은 뺀다.
TRANSPORTS = {
    "socket",
    "urllib.request",
    "http.client",
    "requests",
    "httpx",
    "anthropic",
    "google.genai",
    "google",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _python_files() -> list[Path]:
    return sorted(
        p
        for p in (ROOT / "vader").rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_the_network_doors_are_the_ones_we_listed():
    """문이 조용히 하나 더 생기면 지도가 거짓이 된다."""
    doors = {
        _rel(path)
        for path in _python_files()
        if _imports(path) & TRANSPORTS
    }

    assert doors == NETWORK_DOORS, (
        "네트워크를 여는 파일이 데이터 지도와 다릅니다. "
        "wiki/architecture/data-map.md 를 같이 고치세요.\n"
        f"  더 생김: {sorted(doors - NETWORK_DOORS)}\n"
        f"  없어짐: {sorted(NETWORK_DOORS - doors)}"
    )


@pytest.mark.parametrize("module", sorted(OBSERVATION_MODULES))
def test_an_observation_module_cannot_see_a_network_door(module):
    """관찰 모듈이 문을 못 본다.

    보내려면 누군가 그 사이를 잇는 코드를 **새로 써야 한다.** 실수로는 안 된다.
    """
    imported = _imports(ROOT / module)
    forbidden = sorted(
        name
        for name in imported
        if name.startswith(("vader.core.http", "vader.core.ocr"))
        or name in TRANSPORTS
    )

    assert forbidden == [], f"{module} 이 네트워크에 닿습니다: {forbidden}"


@pytest.mark.parametrize("door", sorted(NETWORK_DOORS))
def test_a_network_door_cannot_see_the_observations(door):
    """문이 관찰 모듈을 못 본다. 반대 방향도 막아야 한 방향이 의미가 있다."""
    imported = _imports(ROOT / door)
    forbidden = sorted(
        name
        for name in imported
        if name.startswith(("vader.core.observe", "vader.core.character"))
    )

    assert forbidden == [], f"{door} 이 관찰 데이터에 닿습니다: {forbidden}"


@needs("wiki")
def test_the_boundary_document_no_longer_claims_the_core_has_no_network():
    """그 문장은 2026-08-05 에 거짓이 됐다. 거짓인 근거를 남겨 두면 안 된다."""
    text = (ROOT / "wiki/architecture/observation-boundary.md").read_text(
        encoding="utf-8"
    )

    assert "코어에 네트워크 코드가 없고" not in text
    assert "data-map.md" in text, "무엇이 그 보장을 대신하는지 가리켜야 합니다"
