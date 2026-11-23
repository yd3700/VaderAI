"""룬 소스 봉인 원장.

봉인은 룬의 선언이 아니라 코어가 남긴 기록이다. 기록한 뒤 소스 디렉터리의
파일 이름이나 바이트가 하나라도 달라지면 운영 실행에 쓸 수 없다.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from vader.core import config

__all__ = ["sealed", "seal", "why", "fingerprint", "trust", "trusted", "loadable"]

_LOCK = threading.RLock()
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def sealed(rune_id: str, source: Path | str) -> bool:
    """현재 소스가 원장에 기록된 것과 같으면 참이다.

    원장이나 소스를 읽지 못하는 모든 경우는 봉인되지 않은 것으로 친다.
    원장을 지우거나 깨뜨리는 일이 운영 잠금 우회가 되어서는 안 된다.
    """
    try:
        with _LOCK:
            record = _records().get(rune_id)
            return (
                isinstance(record, dict)
                and isinstance(record.get("digest"), str)
                and record["digest"] == _digest(Path(source))
            )
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return False


def seal(rune_id: str, source: Path | str, by: str, note: str) -> None:
    """현재 소스를 봉인한다. 저장하지 못하면 예외를 던진다.

    기존 원장을 읽지 못할 때 새 원장으로 덮지 않는다. 손상된 원장 안의 다른
    룬 기록을 조용히 잃는 것보다 봉인 작업을 멈추는 편이 안전하다.
    """
    if not isinstance(rune_id, str) or not rune_id.strip():
        raise ValueError("rune_id must not be empty")
    if not isinstance(by, str) or not by.strip():
        raise ValueError("by must not be empty")
    if not isinstance(note, str):
        raise TypeError("note must be a string")

    source_digest = _digest(Path(source))
    with _LOCK:
        records = _records()
        records[rune_id] = {
            "digest": source_digest,
            "by": by,
            "note": note,
            "sealed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        _write_records(records)


def why(rune_id: str, source: Path | str) -> str:
    """운영 실행이 잠긴 이유를 사람이 읽을 한 줄로 돌려준다."""
    try:
        with _LOCK:
            record = _records().get(rune_id)
            if not isinstance(record, dict) or not isinstance(record.get("digest"), str):
                return "승인 전입니다. 연습 실행만 할 수 있습니다."
            try:
                current = _digest(Path(source))
            except (OSError, ValueError, TypeError):
                return "룬 파일을 읽을 수 없어 승인 여부를 확인할 수 없습니다."
            if record["digest"] != current:
                return "승인한 뒤 룬이 바뀌었습니다. 다시 승인해야 합니다."
            return "승인된 룬입니다."
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return "승인 기록을 읽을 수 없어 실사용 실행이 잠겼습니다."


def trust(key: str, source: Path | str) -> None:
    """사람이 이 소스를 확인했다 — 설치를 누르거나 시험을 허용했다. 이 지문의 코드만 불러온다 (2026-10-14 합의).

    승인(봉인)보다 한 단계 아래다. 승인은 실전, 이것은 '이 PC 에서 돌려도 된다' 다.
    """
    digest = _digest(Path(source))
    with _LOCK:
        records = _records(_trust_path())
        records[key] = digest
        _write_records(records, _trust_path())


def trusted(key: str, source: Path | str) -> bool:
    """원장이 없거나 깨졌으면 거짓이다 — 원장을 지우는 일이 확인을 건너뛰는 길이 되면 안 된다."""
    try:
        with _LOCK:
            return _records(_trust_path()).get(key) == _digest(Path(source))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return False


def loadable(rune_id: str, source: Path | str) -> bool:
    """이 코드를 불러와도 되나. 사람이 확인했거나 승인한 바로 그 지문이어야 한다."""
    return trusted(rune_id, source) or sealed(rune_id, source)


def fingerprint(source: Path | str) -> str | None:
    """룬 소스의 지문. 설치 폴더가 없으면 None. 승인과 같은 계산이다 — '룬이 바뀌었다' 의 뜻이 하나다."""
    source = Path(source)
    return _digest(source) if source.is_dir() else None


def _digest(source: Path) -> str:
    """디렉터리의 상대 파일 이름과 원본 바이트로 SHA-256을 만든다."""
    if not source.is_dir():
        raise ValueError(f"rune source is not a directory: {source}")

    files: list[tuple[str, Path]] = []
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            raise ValueError(f"symbolic links are not supported in rune source: {path}")
        if path.is_file() and path.suffix.lower() not in _IGNORED_SUFFIXES:
            files.append((relative.as_posix(), path))

    digest = hashlib.sha256()
    for relative, path in sorted(files, key=lambda item: item[0]):
        name = relative.encode("utf-8")
        content = path.read_bytes()
        # 길이 경계가 없으면 ("a", "bc")와 ("ab", "c") 같은 결합이
        # 모호해진다. 이름과 내용을 각각 길이로 감싼다.
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _path() -> Path:
    return config.data_dir() / "seals.json"


def _trust_path() -> Path:
    return config.data_dir() / "trusted.json"


def _records(path: Path | None = None) -> dict:
    path = path or _path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain an object")
    return data


def _write_records(records: dict, path: Path | None = None) -> None:
    path = path or _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(records, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
