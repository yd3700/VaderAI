"""룬이 선언한 권한과 실제 코드를 대조한다.

**이것은 샌드박스가 아니다.** 룬은 인프로세스에서 도는 임의 파이썬이라 코어가
런타임에 막을 수 있는 것이 없다. `safety.py` 가 dry-run 에 대해 같은 말을 한다 —
외부 시스템을 만지는 코드가 룬 안에 있어 가로챌 자리가 없다.

그래서 이 모듈이 하는 일은 하나다: **설치 시점에 룬이 코드로 하는 일과 매니페스트에
적은 것이 다르면 설치를 거부한다.** 패키지 관리자가 하는 일과 같다.

막는 것    선언하지 않은 능력을 조용히 쓰는 것 (실수, 게으른 악의)
못 막는 것 `__import__("pywin" + "auto")` 같은 은폐, `eval`, 컴파일된 확장

은폐 비용을 올리는 것이고 은폐를 불가능하게 하는 것이 아니다. 신뢰 판단은 설치하는
사람에게 남는다 — 그래서 설치기가 무엇을 요구하는지 보여줘야 한다.

동적 import 자체를 능력으로 본다. 그것이 가장 싼 우회로이므로, 쓰려면 선언하게
만든다.
"""
from __future__ import annotations

import ast
from pathlib import Path

#: 최상위 모듈 이름 → 필요한 권한.
#: 룬이 이 모듈을 import 하면 매니페스트에 해당 권한이 있어야 한다.
CAPABILITY_BY_MODULE: dict[str, str] = {
    # 화면·키보드를 통해 다른 앱을 조작한다
    "pywinauto": "ui.automation",
    "pyautogui": "ui.automation",
    "win32gui": "ui.automation",
    "win32com": "ui.automation",
    # 임의 네이티브 코드를 부른다. ctypes 는 무엇이든 할 수 있어 따로 둔다.
    "ctypes": "native.code",
    "cffi": "native.code",
    # 네트워크. 룬은 `ctx.ocr`·`ctx.infer` 로 게이트웨이를 타야 한다.
    "socket": "network",
    "urllib": "network",
    "http": "network",
    "requests": "network",
    "httpx": "network",
    "ftplib": "network",
    "smtplib": "network",
    # 다른 프로그램을 띄운다
    "subprocess": "process.spawn",
    "multiprocessing": "process.spawn",
    # 실행 시점에 코드를 만들거나 불러온다 — 가장 싼 우회로다
    "importlib": "dynamic.code",
}

#: 이름만으로 능력을 뜻하는 호출. `eval("...")` 은 import 가 없어도 잡아야 한다.
CAPABILITY_BY_CALL: dict[str, str] = {
    "eval": "dynamic.code",
    "exec": "dynamic.code",
    "compile": "dynamic.code",
    "__import__": "dynamic.code",
}


def required_capabilities(source: Path) -> dict[str, list[str]]:
    """룬 소스를 훑어 필요한 권한과 그 근거를 돌려준다.

    `{"ui.automation": ["driver.py: pywinauto"], ...}` 형태다. 근거를 함께 주는
    이유는 거부 메시지가 "무엇을 고쳐야 하는지" 를 말해야 하기 때문이다.
    """
    found: dict[str, list[str]] = {}

    for path in sorted(Path(source).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeError):
            # 파싱이 안 되면 설치 자체가 뒤에서 실패한다. 여기서 판단하지 않는다.
            continue

        where = path.relative_to(Path(source)).as_posix()
        for node in ast.walk(tree):
            for module, capability in _modules_in(node):
                found.setdefault(capability, []).append(f"{where}: {module}")
            name = _dangerous_call(node)
            if name:
                found.setdefault(CAPABILITY_BY_CALL[name], []).append(f"{where}: {name}()")

    return {capability: sorted(set(reasons)) for capability, reasons in found.items()}


def undeclared(declared: list[str], source: Path) -> dict[str, list[str]]:
    """선언되지 않은 권한만. 비어 있으면 설치해도 된다."""
    granted = set(declared)
    return {
        capability: reasons
        for capability, reasons in required_capabilities(source).items()
        if capability not in granted
    }


def describe(declared: list[str]) -> list[str]:
    """설치기가 사용자에게 보여줄 문장. 선언 순서를 지킨다."""
    wording = {
        "ui.automation": "다른 프로그램의 창을 조작합니다 (키 입력·클릭)",
        "native.code": "네이티브 코드를 직접 호출합니다",
        "network": "네트워크에 접속합니다",
        "process.spawn": "다른 프로그램을 실행합니다",
        "dynamic.code": "실행 중에 코드를 만들거나 불러옵니다",
    }
    return [wording.get(item, f"알 수 없는 권한: {item}") for item in declared]


#: 이름은 네트워크 모듈 아래 있지만 **입출력을 안 하는** 것들.
#:
#: `urllib.parse` 는 URL 문자열을 만들고 쪼갠다. 소켓을 안 연다. 이걸
#: `network` 로 세면 주소를 조립하기만 하는 룬이 네트워크 권한을 선언하게
#: 되고, **선언이 실제 능력을 안 나타내면 선언 자체가 값을 잃는다.**
#: 실제로 걸렸다 — 날씨 룬은 `ctx.http_get` 으로만 나가는데 `quote()` 하나
#: 때문에 네트워크 권한을 요구받았다.
NOT_ACTUALLY_IO = frozenset({"urllib.parse", "urllib.error"})


def _modules_in(node: ast.AST):
    """이 노드가 import 하는 (모듈, 권한) 쌍."""
    names: list[str] = []
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        names = [node.module]

    for name in names:
        if name in NOT_ACTUALLY_IO:
            continue
        top = name.split(".")[0]
        capability = CAPABILITY_BY_MODULE.get(top)
        if capability:
            yield name, capability


def _dangerous_call(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in CAPABILITY_BY_CALL:
            return node.func.id
    return None


#: 권한 선언으로는 안 잡히지만 사람이 알아야 할 동작 (2026-10-14 합의: 시험·설치 전 요약).
#: 호출 이름만 본다 — 은폐는 못 잡는다. 판단은 확인하는 사람에게 남는다.
WATCH_CALLS: dict[str, str] = {
    "open": "파일 열기", "write_text": "파일 쓰기", "write_bytes": "파일 쓰기",
    "rmtree": "폴더 삭제", "remove": "파일 삭제", "unlink": "파일 삭제", "rmdir": "폴더 삭제",
    "rename": "파일 이동", "move": "파일 이동",   # replace 는 뺀다 — str.replace 와 구분이 안 된다
    "system": "프로그램 실행", "startfile": "파일·프로그램 열기", "popen": "프로그램 실행",
    "getattr": "이름으로 기능 꺼내기", "setattr": "기능 바꿔치기",
}


def summary(source: Path) -> dict:
    """이 코드가 하려는 일을 사람이 훑어볼 만큼. 실행하지 않고 AST 만 읽는다.

    `{"files": [...], "modules": [...], "needs": [권한 문장], "watch": ["파일 쓰기 · a.py 12·40줄", ...]}`
    """
    files, modules, watch, lines = [], set(), [], {}
    for path in sorted(Path(source).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        where = path.relative_to(Path(source)).as_posix()
        files.append(where)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeError):
            watch.append(f"읽을 수 없는 코드 · {where}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in WATCH_CALLS:
                    lines.setdefault((WATCH_CALLS[name], where), set()).add(node.lineno)
    watch += [f"{what} · {where} {'·'.join(map(str, sorted(at)))}줄" for (what, where), at in sorted(lines.items())]
    return {"files": files, "modules": sorted(modules - {"vader", "__future__"}),
            "needs": describe(sorted(required_capabilities(source))), "watch": watch}
