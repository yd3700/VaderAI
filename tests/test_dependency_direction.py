"""
의존 방향 강제.

    vader.core  ──►  vader.sdk  ◄──  runes.*

IchorAI에서는 이 방향이 지켜지지 않았다. OCR 프로바이더(코어)가 RPA 설정
(룬)을 읽었고, `_pipeline_mixin`(코어 UI)이 `main`(EONM 러너)을 직접 import
했다. 그래서 룬 하나를 떼어낼 수 없었다.

외부 도구(import-linter) 없이 ast로 검사한다. pytest만 있으면 돈다.
"""
import ast
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _imported_modules(path: Path) -> set[str]:
    """파일이 import하는 최상위 모듈 경로 집합."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module)
    return found


def _python_files(package: str) -> list[Path]:
    base = ROOT / package.replace(".", os.sep)
    if not base.exists():
        return []
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _violations(source_package: str, forbidden_prefix: str) -> list[str]:
    out = []
    for path in _python_files(source_package):
        for module in _imported_modules(path):
            if module == forbidden_prefix or module.startswith(forbidden_prefix + "."):
                rel = path.relative_to(ROOT).as_posix()
                out.append(f"{rel} imports {module}")
    return out


def test_core_does_not_import_runes():
    """코어는 어떤 룬도 알면 안 된다. 룬은 런타임에 로드된다."""
    bad = _violations("vader.core", "runes")
    assert bad == [], "코어가 룬을 import합니다:\n  " + "\n  ".join(bad)


def test_sdk_does_not_import_core():
    """SDK는 계약만 담는다. 코어를 끌어오면 룬이 코어에 묶인다."""
    bad = _violations("vader.sdk", "vader.core")
    assert bad == [], "SDK가 코어를 import합니다:\n  " + "\n  ".join(bad)


def test_sdk_does_not_import_runes():
    bad = _violations("vader.sdk", "runes")
    assert bad == [], "SDK가 룬을 import합니다:\n  " + "\n  ".join(bad)


def test_runes_do_not_import_core():
    """룬은 vader.sdk만 쓴다. 코어 내부를 만지면 계약이 무의미해진다."""
    bad = _violations("runes", "vader.core")
    assert bad == [], "룬이 코어를 import합니다:\n  " + "\n  ".join(bad)


def test_rune_package_sources_do_not_import_core():
    """배포 전 룬 소스도 설치된 룬과 같은 의존 규칙을 지킨다."""
    bad = _violations("rune-packages", "vader.core")
    assert bad == [], "룬 패키지 소스가 코어를 import합니다:\n  " + "\n  ".join(bad)


#: 룬이 import 해도 되는 것. `vader.sdk` 는 계약이고, `vader.blocks` 는
#: 선언을 도는 실행기다 — 둘 다 코어를 모르고 외부 패키지도 안 쓴다.
RUNE_MAY_IMPORT = {"vader.sdk"}


def _foreign_vader_imports(package: str) -> list[str]:
    out = []
    for path in _python_files(package):
        for module in _imported_modules(path):
            if not module.startswith("vader"):
                continue
            top = ".".join(module.split(".")[:2])
            if top not in RUNE_MAY_IMPORT:
                out.append(f"{path.relative_to(ROOT).as_posix()} imports {module}")
    return out


@pytest.mark.parametrize("package", ["runes", "rune-packages"])
def test_runes_import_only_the_two_things_they_may(package):
    """`vader.core` 만 막으면 **새 최상위 모듈이 조용히 통과한다.**

    `vader/blocks/` 가 생겼을 때 실제로 그랬다 — 룬이 그것을 import 했는데
    검사가 `vader.core` 만 보고 있어 아무 말도 안 했다. 그 층은 2026-08-11 에
    지웠지만 검사는 남긴다. 목록을 뒤집어
    **허용된 것만** 통과시킨다. 새 층을 만들면 여기 한 줄을 더해야 하고,
    그 한 줄이 "룬이 이것에 묶여도 되는가" 를 묻는 자리가 된다.
    """
    bad = _foreign_vader_imports(package)
    assert bad == [], (
        "룬이 허용되지 않은 코어 모듈을 import합니다 "
        f"(허용: {', '.join(sorted(RUNE_MAY_IMPORT))}):\n  " + "\n  ".join(bad)
    )


def test_the_block_engine_stays_on_the_rune_side_of_the_line():
    """룬이 import 해도 되는 것이라면 그것도 코어를 몰라야 한다."""
    bad = _violations("vader.blocks", "vader.core")
    assert bad == [], "블록 실행기가 코어를 import합니다:\n  " + "\n  ".join(bad)


def test_sdk_has_no_third_party_dependencies():
    """룬이 SDK를 쓰려고 무거운 패키지를 깔게 만들지 않는다."""
    stdlib_ok = {
        "__future__", "hashlib", "dataclasses", "pathlib", "typing",
        "abc", "enum", "json", "datetime",
    }
    offenders = []
    for path in _python_files("vader.sdk"):
        for module in _imported_modules(path):
            top = module.split(".")[0]
            if top in stdlib_ok or module.startswith("vader.sdk"):
                continue
            offenders.append(f"{path.relative_to(ROOT).as_posix()} imports {module}")
    assert offenders == [], "SDK에 외부 의존이 있습니다:\n  " + "\n  ".join(offenders)


def test_core_declares_no_required_dependencies():
    """`pip install vaderai` 는 아무것도 끌어오지 않는다.

    룬의 라이브러리를 필수로 올리면 룬을 떼어내도 그 의존은 남는다. 그러면
    탈부착이 이름만 남는다. 룬 묶음은 optional-dependencies 에 둔다.

    코어가 서드파티를 진짜로 필요로 하게 되는 날 이 테스트를 지운다. 지금은
    tkinter 와 stdlib 뿐이다.
    """
    import tomllib

    project = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]

    assert project["dependencies"] == [], (
        "코어 필수 의존이 생겼습니다. 룬 것이면 optional-dependencies 로 옮기세요:\n  "
        + "\n  ".join(project["dependencies"])
    )


def test_runes_package_is_empty_by_default():
    """룬 0개가 정상 상태다. 내장 룬을 소스 트리에 심지 않는다.

    IchorAI는 eonm_ipgo와 ins_singo가 src/domains/ 안에 있어서 '룬 없는 상태'가
    구조적으로 도달 불가능했고, 그래서 아무도 검증한 적이 없었다.
    """
    modules = [
        p for p in _python_files("runes")
        if p.name != "__init__.py"
    ]
    assert modules == [], (
        "runes/ 에 소스가 있습니다. 설치 룬은 config.rune_dir() 밑에 둡니다:\n  "
        + "\n  ".join(p.relative_to(ROOT).as_posix() for p in modules)
    )


def test_every_native_dialog_names_its_owner_window():
    """`filedialog`·`messagebox` 는 `parent=` 없이 부르면 안 된다.

    소유자 없는 네이티브 대화상자는 Windows 에서 **앱 창 뒤로 숨는다.** 그런데
    모달이라 앱은 입력을 안 받는다 — 화면에는 아무것도 안 뜨고 앱만 굳는 것처럼
    보인다. "룬 설치를 눌렀더니 프로그램이 행이 걸렸다" 가 이것이었다.

    자체 `Toplevel`(`SetupDialog`·`AskDialog`)은 `transient` + `grab_set` 으로
    이미 지키고 있었고 네이티브 대화상자만 빠져 있었다. tkinter 는 눈으로만
    확인할 수 있어 놓치기 쉬우니 ast 로 고정한다.
    """
    source = (ROOT / "vader" / "core" / "ui.py").read_text(encoding="utf-8")
    missing = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
            continue
        if func.value.id not in ("filedialog", "messagebox"):
            continue
        if "parent" not in {keyword.arg for keyword in node.keywords}:
            missing.append(f"ui.py:{node.lineno} {func.value.id}.{func.attr}")

    assert missing == [], "parent= 를 넘기지 않는 대화상자: " + ", ".join(missing)


def test_the_pump_reschedules_itself_from_a_finally():
    """폴링 재예약은 **예외 경로에서도** 일어나야 한다.

    `root.after` 가 `_pump` 본문 맨 끝에 있던 동안, 예외 한 번에 폴링이 영구히
    멎었다. ERP 입력은 정상인데 화면만 죽어서, 사용자가 다시 실행하면 중복
    입고가 된다. 구조로 고정한다 — 동작 검증은
    `test_view.py::test_the_pump_reschedules_even_when_the_tick_raises` 다.
    """
    source = (ROOT / "vader" / "core" / "ui.py").read_text(encoding="utf-8")
    pump = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_pump"
    )

    finallys = [
        node for node in ast.walk(pump) if isinstance(node, ast.Try) and node.finalbody
    ]
    assert finallys, "_pump 에 finally 가 없다"

    rescheduled = [
        node
        for handler in finallys
        for body in [handler.finalbody]
        for node in ast.walk(ast.Module(body=body, type_ignores=[]))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "after"
    ]
    assert rescheduled, "재예약(root.after)이 finally 안에 없다"
