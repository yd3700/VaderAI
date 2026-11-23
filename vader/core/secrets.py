"""API 키 보관 — Windows 보호 저장(DPAPI, 현재 사용자 범위) (2026-10-14 마스터 결정, codex1 권고).

settings.json 에 평문으로 두던 키를 여기로 옮긴다. 파일(`<data_dir>/secrets.json`)에는 이 Windows 사용자만
풀 수 있는 암호문만 남는다.

**막는 것:** settings.json 을 복사·공유·백업하다 키가 새는 것, 다른 Windows 사용자가 읽는 것.
**못 막는 것:** 같은 사용자로 도는 프로그램 — DPAPI 는 같은 사용자의 요청이면 풀어 준다. 룬 실행 격리와는 별개다.
다른 PC 로 옮기면 풀리지 않는다 — 키를 다시 넣는다.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
from pathlib import Path
from uuid import uuid4

from vader.core import config

_LOCK = threading.Lock()
#: 이 앱의 암호문임을 표시하는 추가 엔트로피. 비밀은 아니다 — 다른 앱이 실수로 풀지 않게.
_ENTROPY = b"VaderAI api keys v1"


class SecretError(RuntimeError):
    """보호 저장을 쓸 수 없다."""


def available() -> bool:
    return sys.platform == "win32"


def get(name: str, *, device: bool = False) -> str | None:
    """`device=True` 는 기기 금고(로그인 토큰) — 로그인 전에도 연다. 기본은 계정 작업공간의 금고(AI 키)."""
    blob = _records(device).get(name)
    return _unprotect(base64.b64decode(blob)).decode("utf-8") if blob else None


def put(name: str, value: str, *, device: bool = False) -> None:
    """암호화해 저장하고, 다시 풀어 같은지 확인한다 — 확인 전에는 원래 자리의 키를 지우지 않게."""
    if not value:
        raise ValueError("빈 키는 저장하지 않습니다.")
    blob = base64.b64encode(_protect(value.encode("utf-8"))).decode()
    with _LOCK:
        records = _records(device)
        records[name] = blob
        _write(records, device)
    if get(name, device=device) != value:
        raise SecretError("키를 보호 저장소에 저장했지만 다시 읽지 못했습니다.")


def names() -> list[str]:
    return list(_records())


def delete(name: str, *, device: bool = False) -> None:
    with _LOCK:
        records = _records(device)
        if records.pop(name, None) is not None:
            _write(records, device)


def _path(device: bool = False) -> Path:
    return config.HOME / "device-secrets.json" if device else config.data_dir() / "secrets.json"


def _records(device: bool = False) -> dict:
    try:
        data = json.loads(_path(device).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise SecretError("secrets.json 이 올바르지 않습니다.")
    return data


def _write(records: dict, device: bool = False) -> None:
    path = _path(device)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(json.dumps(records, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


# ── DPAPI (ctypes) ──

def _blob_type():
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    return Blob


def _call(name: str, data: bytes) -> bytes:
    if not available():
        raise SecretError("Windows 보호 저장은 Windows 에서만 씁니다.")
    import ctypes

    Blob = _blob_type()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    source = Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    entropy = Blob(len(_ENTROPY), ctypes.cast(ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY)),
                                              ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    # 두 함수의 인자 모양이 같다: (입력, 설명, 엔트로피, 예약, 안내창, 플래그, 출력)
    if not getattr(crypt32, name)(ctypes.byref(source), None, ctypes.byref(entropy), None, None,
                                  0x1, ctypes.byref(out)):   # 0x1 = CRYPTPROTECT_UI_FORBIDDEN — 창을 띄우지 않는다
        raise SecretError(f"{name} 실패 (Windows 오류 {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _protect(data: bytes) -> bytes:
    return _call("CryptProtectData", data)


def _unprotect(data: bytes) -> bytes:
    return _call("CryptUnprotectData", data)
