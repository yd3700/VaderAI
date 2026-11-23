"""표 룬 실행 프로필 — 허용 목록으로 쓰는 룬 문법 (2026-10-14 codex1 합의 2번).

`RuneManifest(profile="table")` 인 룬은 여기 적힌 것만 쓴다. 제작소의 정적 검사와 **설치기가 둘 다**
이 검사를 부른다 — 제작소만 보면 손으로 만든 zip 이 '표 룬' 이라고 우길 수 있다.

**보안 증명이 아니다.** 파이썬을 안전한 언어로 바꾸지 못한다. 하는 일은 셋:

- 쓸 수 있는 import 를 모듈이 아니라 **이름 단위로** 허용한다
- 파일·프로세스·네트워크로 가는 이름과, 객체 내부로 파고드는 접근(`_` 로 시작하는 속성, `format`)을 막는다
- 파일은 `ctx.read_table` / `ctx.write_table` 로만 — 경로(`.path`·`workdir`)를 만지지 못하게 한다

이 프로필이 아닌 룬은 '기존 방식' 이다. 막지는 않지만 동료 공유·공개 대상이 아니다.
"""
from __future__ import annotations

import ast
from pathlib import Path

PROFILE = "table"

#: `from 모듈 import 이름` 으로만, 여기 있는 이름만. `import 모듈` 은 받지 않는다 — 모듈을 통째로
#: 쥐면 그 안의 무엇이든 부를 수 있다.
ALLOWED_IMPORTS: dict[str, frozenset[str]] = {
    "__future__": frozenset({"annotations"}),
    "vader.sdk": frozenset({"Input", "RuneContext", "RuneField", "RuneManifest", "RuneResult", "Failure",
                            "UserActionRequest", "UserActionResponse", "RuneCancelled"}),
    "math": frozenset({"ceil", "floor", "fsum", "isclose", "isnan", "inf", "sqrt", "trunc"}),
    "datetime": frozenset({"date", "datetime", "time", "timedelta"}),
    "decimal": frozenset({"Decimal", "ROUND_HALF_UP", "ROUND_DOWN", "ROUND_UP"}),
    "json": frozenset({"dumps", "loads"}),
    "re": frozenset({"fullmatch", "match", "search", "sub", "split", "findall", "IGNORECASE"}),
    "collections": frozenset({"Counter", "defaultdict", "OrderedDict", "namedtuple"}),
    "statistics": frozenset({"mean", "median"}),
    "dataclasses": frozenset({"dataclass", "field"}),
    "typing": frozenset({"Any", "Optional"}),
}

#: 이름만으로 바깥에 닿거나 검사를 우회하는 내장 함수.
FORBIDDEN_NAMES = frozenset({
    "open", "eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr", "globals",
    "locals", "vars", "breakpoint", "input", "help", "exit", "quit", "memoryview", "__builtins__",
})

#: 속성 이름. 경로를 쥐거나(path·workdir·datadir), 경로로 파일을 만지거나, 문자열 서식으로
#: 객체 내부를 읽거나(format), 표 중개를 건너뛰는 ctx 통로(ocr·http·replay)다.
FORBIDDEN_ATTRIBUTES = frozenset({
    "path", "workdir", "datadir", "parent", "parents",
    "write_text", "write_bytes", "read_text", "read_bytes", "open", "unlink", "rmdir", "mkdir",
    "rename", "touch", "iterdir", "glob", "rglob", "chmod", "symlink_to", "hardlink_to",
    "expanduser", "home", "cwd", "format", "format_map",
    "ocr", "infer", "http_get", "http_post", "replay",
})

#: `_` 로 시작해도 되는 속성 — 클래스 계약의 메서드.
ALLOWED_DUNDER_ATTRIBUTES = frozenset({"__init__"})
ALLOWED_DECORATORS = frozenset({"staticmethod", "classmethod", "property", "dataclass"})


def declared(source: Path) -> str:
    """매니페스트의 profile 값. 없으면 빈 문자열(기존 방식)."""
    from vader.core.installer import _manifest_string

    return _manifest_string(source, "profile")


def check(source: Path) -> list[str]:
    """표 룬 문법을 어긴 곳. 비어 있으면 통과. **코드를 실행하지 않는다.**"""
    problems: list[str] = []
    for path in sorted(Path(source).rglob("*")):
        if "__pycache__" in path.parts or not path.is_file():
            continue
        where = path.relative_to(Path(source)).as_posix()
        if path.suffix.lower() != ".py":
            if path.suffix.lower() in {".pyd", ".dll", ".so", ".pyc", ".exe"}:
                problems.append(f"{where}: 파이썬 소스가 아닌 실행 파일은 넣을 수 없습니다")
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeError) as exc:
            problems.append(f"{where}: 읽을 수 없는 코드 ({exc})")
            continue
        problems += [f"{where}:{line} {message}" for line, message in _walk(tree)]
    return problems


def _walk(tree: ast.Module):
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            yield line, f"`import {node.names[0].name}` 대신 `from 모듈 import 이름` 으로 허용된 이름만 씁니다"
        elif isinstance(node, ast.ImportFrom):
            if node.level:   # 패키지 안의 상대 import — 그 파일도 같은 검사를 받는다
                continue
            allowed = ALLOWED_IMPORTS.get(node.module or "")
            for alias in node.names:
                if allowed is None or alias.name not in allowed:
                    yield line, f"표 룬에서 쓸 수 없는 import: {node.module}.{alias.name}"
        elif isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id.startswith("__")):
            yield line, f"표 룬에서 쓸 수 없는 이름: {node.id}"
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") and node.attr not in ALLOWED_DUNDER_ATTRIBUTES:
                yield line, f"객체 내부 접근은 쓸 수 없습니다: .{node.attr}"
            elif node.attr in FORBIDDEN_ATTRIBUTES:
                yield line, f"표 룬에서 쓸 수 없는 기능: .{node.attr} (파일은 ctx.read_table·ctx.write_table 로)"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if not (isinstance(target, ast.Name) and target.id in ALLOWED_DECORATORS):
                    yield line, "허용하지 않은 데코레이터입니다 (staticmethod·classmethod·property·dataclass 만)"
            if isinstance(node, ast.ClassDef) and node.keywords:
                yield line, "메타클래스 등 class 키워드 인자는 쓸 수 없습니다"
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            yield line, "global·nonlocal 은 쓸 수 없습니다"
