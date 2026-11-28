"""진단 기록 (2026-11-28 마스터 결정 — 다른 PC 에서만 안 되는 문제를 가려내려고).

`%USERPROFILE%\\.vaderai\\crash` 에 **이 PC 에만** 남긴다. 어디로도 보내지 않는다. 업무 내용·파일 이름·경로·계정·PC 이름은 쓰지 않는다.
- env_*.txt        : 켤 때의 **기기** 환경 — 정해진 모양의 값만(정수·숫자 판). 업무 설정(settings.json)은 열지 않는다(codex1 diag 회신 2).
- native_*.txt     : 일꾼이 파이썬 밖에서 죽은 위치 — 다음에 켤 때 원본에서 **파일 이름·줄·함수만** 옮겨 적는다.
                     원본은 보여 줄 폴더 밖(`crash-raw`)에 두고, 옮긴 뒤 지운다.
- ui_events.txt    : 화면 알림 — 정해진 종류 코드, 앱 스크립트 이름·숫자 줄/칸, 오류 이름(TypeError 등)만. 셸은 shell_events.txt 에 따로.
(codex1 diag 회신 1)
"""
from __future__ import annotations

import faulthandler
import os
import platform
import re
import struct
import threading
from datetime import datetime
from pathlib import Path

from vader import __version__
from vader.core import config

KEEP = 20                      # 종류별로 최근 것만
EVENTS_MAX = 128 * 1024        # 넘으면 .old 로 한 번 돌린다(다시 읽어 쓰지 않는다)
UI_KINDS = {"error", "rejection", "render", "drag_enter", "drop"}
UI_ERRORS = {"Error", "TypeError", "ReferenceError", "RangeError", "SyntaxError", "URIError", "EvalError", "AggregateError"}
SCRIPTS = re.compile(r"(?:app|messenger|chat|i18n|i18n-en|visuals|icons|live-logs|gate|badge)\.js:\d{1,6}:\d{1,6}")
AREAS = {"core"}
_native = None                 # faulthandler 가 쥐고 있어야 하는 파일
_lock = threading.Lock()
_ui_count = 0


def root() -> Path:
    return config.HOME / "crash"


def raw_root() -> Path:
    """faulthandler 원본(절대 경로가 섞인다) — 보여 줄 폴더(crash) 밖."""
    return config.HOME / "crash-raw"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _prune(pattern: str) -> None:
    files = sorted(root().glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


_FRAME = re.compile(r'^\s*File "(?P<file>[^"]*)", line (?P<line>\d+) in (?P<func>[A-Za-z_][A-Za-z0-9_<>]*)\s*$')
_HEAD = re.compile(r"^(Fatal Python error: [A-Za-z ]+|Windows fatal exception: [a-z ]+(0x[0-9a-fA-F]+)?|"
                   r"(Current thread|Thread) 0x[0-9a-fA-F]+ \(most recent call first\):|Stack \(most recent call first\):)")


def clean_native(raw: str) -> str:
    """faulthandler 원본 → 파일 **이름**·줄·함수만. 정해진 모양이 아닌 줄은 버린다(경로·메시지가 섞일 수 있다)."""
    out = []
    for line in raw.splitlines():
        m = _FRAME.match(line)
        if m:
            name = re.sub(r"[^A-Za-z0-9_.\-]", "", m["file"].replace("\\", "/").rsplit("/", 1)[-1])[:60]
            out.append(f'  File "{name}", line {m["line"]} in {m["func"][:60]}')
        elif _HEAD.match(line.strip()):
            out.append(_HEAD.match(line.strip()).group(0))
    return "\n".join(out) + ("\n" if out else "")


def start() -> None:
    """일꾼이 켜질 때 한 번. 지난 원본을 정리본으로 옮기고 새 원본을 연다. 실패해도 앱을 막지 않는다."""
    global _native
    try:
        root().mkdir(parents=True, exist_ok=True)
        raw_root().mkdir(parents=True, exist_ok=True)
        for raw in [*raw_root().glob("native_raw_*.txt"), *root().glob("native_raw_*.txt")]:
            try:
                text = clean_native(raw.read_text(encoding="utf-8", errors="replace"))
                if text:
                    (root() / raw.name.replace("native_raw_", "native_")).write_text(text, encoding="utf-8")
                raw.unlink()
            except OSError:
                pass
        _prune("native_*.txt")
        _native = open(raw_root() / f"native_raw_{_stamp()}.txt", "w", encoding="utf-8")
        faulthandler.enable(file=_native, all_threads=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        (root() / f"env_{_stamp()}.txt").write_text(environment(), encoding="utf-8")
        _prune("env_*.txt")
    except Exception:  # noqa: BLE001
        pass


def environment() -> str:
    lines = ["VaderAI environment", f"recorded_at: {datetime.now().isoformat(timespec='seconds')}",
             f"vader_version: {_version(__version__)}", f"python: {_version(platform.python_version())} {struct.calcsize('P') * 8}bit",
             f"windows: {_version(platform.version())}"]
    for name, read in (("is_admin", _is_admin), ("uac", _uac), ("remote_session", _remote), ("client_animation", _animation),
                       ("system_dpi", _dpi), ("webview2", _webview2)):
        try:
            lines.append(f"{name}: {read()}")
        except Exception as exc:  # noqa: BLE001 — 읽지 못한 것도 그 사실만
            lines.append(f"{name}: unreadable_{type(exc).__name__[:30]}")
    return "\n".join(lines) + "\n"


def _version(text) -> str:
    """숫자 판(1.2.3)만 — 아니면 'other'."""
    text = str(text)
    return text if re.fullmatch(r"\d{1,6}(\.\d{1,6}){0,4}", text) else "other"


def _is_admin() -> bool:
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def _uac() -> str:
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System") as k:
        def get(n):
            try:
                value = winreg.QueryValueEx(k, n)[0]
            except OSError:
                return "none"
            return str(value) if isinstance(value, int) and 0 <= value <= 16 else "other"
        return " ".join(f"{n}={get(n)}" for n in ("EnableLUA", "ConsentPromptBehaviorAdmin", "PromptOnSecureDesktop",
                                                  "FilterAdministratorToken"))


def _remote() -> bool:
    import ctypes
    return bool(ctypes.windll.user32.GetSystemMetrics(0x1000))      # SM_REMOTESESSION


def _animation() -> bool:
    import ctypes
    on = ctypes.c_int(0)
    ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0)   # SPI_GETCLIENTAREAANIMATION
    return bool(on.value)


def _dpi() -> int:
    import ctypes
    return int(ctypes.windll.user32.GetDpiForSystem())


def _webview2() -> str:
    import winreg
    key = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for hive, path in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + key), (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + key),
                       (winreg.HKEY_CURRENT_USER, "SOFTWARE\\" + key)):
        try:
            with winreg.OpenKey(hive, path) as k:
                return _version(winreg.QueryValueEx(k, "pv")[0])
        except OSError:
            continue
    return "not_found"


def ui(kind: str, where: str = "", name: str = "") -> bool:
    """화면이 알린 일 — 정해진 종류 코드, 앱 스크립트 이름·숫자 줄/칸 또는 정해진 영역, 오류 이름만. 그 밖은 버린다. 세션 50건."""
    global _ui_count
    kind = str(kind)
    if kind not in UI_KINDS:
        return False
    where = str(where)
    m = SCRIPTS.search(where)
    where = m.group(0) if m else (where if where in AREAS else "")
    name = str(name) if str(name) in UI_ERRORS else ""
    with _lock:
        if _ui_count >= 50:
            return False
        _ui_count += 1
        _append("ui_events.txt", f"{datetime.now().isoformat(timespec='seconds')} {kind} {where} {name}".rstrip())
    return True


def _append(file: str, line: str) -> None:
    try:
        root().mkdir(parents=True, exist_ok=True)
        path = root() / file
        if path.exists() and path.stat().st_size > EVENTS_MAX:
            os.replace(path, path.with_suffix(".old"))     # 한 번 돌린다 — 다시 읽어 쓰지 않는다
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
