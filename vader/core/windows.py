"""Windows 최상위 창 열거."""
from __future__ import annotations

import ctypes
import sys


def list_window_titles() -> list[str]:
    """현재 보이는 최상위 창의 고유한 제목을 정렬해 반환한다."""

    if sys.platform != "win32":
        return []

    titles: list[str] = []

    def callback(hwnd, _lparam):
        if ctypes.windll.user32.IsWindowVisible(hwnd):
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buffer = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value.strip()
                if title and title not in ("Default IME", "MSCTFIME UI"):
                    titles.append(title)
        return True

    enum_windows_proc = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
    )
    ctypes.windll.user32.EnumWindows(enum_windows_proc(callback), 0)
    return sorted(set(titles))
