"""화면이 **실제로** 무엇으로 되어 있는지 찍어 본다.

    python tools/inspect_window.py 이온엠
    python tools/inspect_window.py 이온엠 8     # UIA 트리 깊이 (기본 3)

이온엠 메뉴를 세 번 고쳤고 세 번 다 빗나갔다. 원인은 하나다 — **나는 그
화면을 못 본다.** 셀렉터를 상상해서 쓰고, 마스터가 실행하고, 안 되고,
로그 한 줄로 다시 상상한다. 그 왕복을 끝내려고 만들었다.

찍는 것은 셋이다.

    1. 떠 있는 창 전부      제목 · PID · 메뉴 유무
    2. 고전 Win32 메뉴 트리  `GetMenu` 로 실제 항목 이름을 읽는다
    3. UIA 컨트롤 트리       pywinauto 가 무엇으로 보는지

**아무것도 안 누른다. 읽기만 한다.** 이온엠에 키 하나 안 들어간다.

이 파일은 도구지 제품이 아니다. 룬도 코어도 이걸 import 하지 않는다.
"""
from __future__ import annotations

import ctypes
import sys

_SW_SHOWMINIMIZED = 2
#: UIA 트리를 얼마나 깊이 볼까. 더 내려가면 화면 하나가 수백 줄이 된다.
MAX_DEPTH = 3


class _Placement(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint), ("flags", ctypes.c_uint), ("showCmd", ctypes.c_uint),
        ("ptMinX", ctypes.c_long), ("ptMinY", ctypes.c_long),
        ("ptMaxX", ctypes.c_long), ("ptMaxY", ctypes.c_long),
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


def windows(needle: str) -> list[dict]:
    found: list[dict] = []

    def visit(hwnd, _lparam):
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            if not needle or needle in buf.value:
                place = _Placement()
                place.length = ctypes.sizeof(_Placement)
                ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(place))
                pid = ctypes.c_ulong(0)
                ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append({
                    "hwnd": int(hwnd),
                    "title": buf.value,
                    "pid": int(pid.value),
                    "visible": bool(ctypes.windll.user32.IsWindowVisible(hwnd)),
                    "minimized": place.showCmd == _SW_SHOWMINIMIZED,
                    "menu": int(ctypes.windll.user32.GetMenu(hwnd)),
                })
        return True

    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(visit)
    ctypes.windll.user32.EnumWindows(proc, 0)
    return found


def menu_tree(handle: int, depth: int = 0) -> list[str]:
    """고전 Win32 메뉴의 항목 이름을 그대로 읽는다.

    **이름에 `&` 나 탭이 섞여 있을 수 있다.** `재고관리(&S)` 같은 것이면
    우리가 넣은 `재고관리` 로는 안 찾아진다 — 그게 이번 실패의 후보 하나다.
    """
    out: list[str] = []
    if not handle:
        return out
    count = ctypes.windll.user32.GetMenuItemCount(handle)
    for index in range(max(0, count)):
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetMenuStringW(handle, index, buf, 512, 0x400)
        label = buf.value
        child = ctypes.windll.user32.GetSubMenu(handle, index)
        out.append("  " * depth + (repr(label) if label else "(구분선)"))
        if child:
            out.extend(menu_tree(child, depth + 1))
    return out


def uia_tree(hwnd: int) -> list[str]:
    try:
        from pywinauto.application import Application
    except ImportError:
        return ["(pywinauto 가 없습니다)"]
    try:
        window = Application(backend="uia").connect(handle=hwnd, timeout=5).window(
            handle=hwnd
        )
    except Exception as exc:
        return [f"(붙지 못했습니다: {exc})"]

    lines: list[str] = []

    def walk(item, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        for child in item.children():
            info = child.element_info
            lines.append(
                "  " * depth
                + f"{info.control_type} title={info.name!r} auto_id={info.automation_id!r}"
            )
            walk(child, depth + 1)

    try:
        walk(window, 0)
    except Exception as exc:
        lines.append(f"(트리를 다 못 읽었습니다: {exc})")
    return lines


def main(needle: str) -> int:
    found = windows(needle)
    print(f"=== 제목에 {needle!r} 이 들어간 창 {len(found)}개 ===")
    for window in found:
        print(
            f"  hwnd={window['hwnd']} pid={window['pid']} "
            f"보임={window['visible']} 최소화={window['minimized']} "
            f"메뉴={'있음' if window['menu'] else '없음'}"
        )
        print(f"    제목: {window['title']!r}")

    usable = [w for w in found if w["visible"] and not w["minimized"]]
    if not usable:
        print("\n쓸 수 있는 창이 없습니다. 프로그램이 떠 있고 최소화가 아닌지 보세요.")
        return 1

    for window in usable:
        print(f"\n=== hwnd={window['hwnd']} 의 고전 메뉴 ===")
        tree = menu_tree(window["menu"])
        print("\n".join(tree) if tree else "  (고전 메뉴 없음)")

        print(f"\n=== hwnd={window['hwnd']} 의 UIA 트리 (깊이 {MAX_DEPTH}) ===")
        print("\n".join(uia_tree(window["hwnd"])) or "  (비어 있음)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2:
        MAX_DEPTH = int(sys.argv[2])
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
