"""Windows 클립보드 읽기 — 콘솔의 우클릭 붙여넣기와 탐색기에서 복사한 파일용 (2026-10-27).

사람이 붙여넣기를 눌렀을 때만 읽는다. 상시 읽지 않고, 읽은 내용은 로그에 남기지 않는다.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

CF_UNICODETEXT = 13
CF_HDROP = 15


def read() -> dict:
    """{'text': 글자, 'files': [절대 경로, ...]}. 못 열면(다른 프로그램이 쥐고 있으면) 빈 값."""
    user32, shell32, kernel32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    out = {"text": "", "files": []}
    if not user32.OpenClipboard(None):
        return out
    try:
        drop = user32.GetClipboardData(CF_HDROP)
        if drop:
            count = shell32.DragQueryFileW(drop, 0xFFFFFFFF, None, 0)
            for i in range(count):
                buffer = ctypes.create_unicode_buffer(shell32.DragQueryFileW(drop, i, None, 0) + 1)
                shell32.DragQueryFileW(drop, i, buffer, len(buffer))
                out["files"].append(buffer.value)
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if handle:
            pointer = kernel32.GlobalLock(handle)
            if pointer:
                try:
                    out["text"] = ctypes.wstring_at(pointer)
                finally:
                    kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    return out
