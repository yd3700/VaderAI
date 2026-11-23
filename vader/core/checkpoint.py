"""룬 실행 체크포인트의 파일 저장소."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from pathlib import Path

from vader.core import config
from vader.sdk import CHECKPOINT_SCHEMA_VERSION, CheckpointKey


class CheckpointStore:
    """체크포인트를 입력 내용 기준으로 저장한다.

    Job은 재시도할 때마다 새로 만들어지므로 job_id를 저장 키로 사용하지 않는다.
    저장 파일에는 코어가 관리하는 스키마 버전과 룬이 넘긴 payload를 분리해 기록한다.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else config.data_dir() / "checkpoints"
        # 같은 키에 두 스레드가 동시에 저장하면 Windows 의 os.replace 가
        # PermissionError(WinError 5) 로 실패한다. 40회 반복에서 13회 재현했다.
        # 파일이 깨지지는 않지만 한 Job 의 save_checkpoint 가 예외로 죽는다.
        #
        # 같은 키가 나오는 경우: 같은 룬 + 같은 입력으로 Job 을 둘 띄우는 것.
        # 실수로 같은 파일을 두 번 넣거나, 첫 Job 이 도는 중에 재시도하면 그렇게 된다.
        #
        # 한 프로세스 안의 스레드들만 다룬다. JobRunner 하나가 저장소 하나를 모든
        # JobContext 에 나눠 주므로 인스턴스 락으로 충분하다.
        # ponytail: 프로세스가 여럿이 되면 파일 락이 필요하다. 지금은 앱이 하나다.
        self._lock = threading.Lock()

    def load(self, key: CheckpointKey, *, rune_version: str = "") -> dict | None:
        """현재 스키마와 **같은 룬 버전**의 payload를 읽는다.

        파일이 없거나, 읽을 수 없거나, JSON이 깨졌거나, 스키마 버전이 다르거나,
        룬 버전이 다르면 재개하지 않고 처음부터 실행할 수 있도록 ``None``을 반환한다.

        룬 버전을 보는 이유: payload 의 모양은 **룬이** 정한다. 룬을 업그레이드해
        모양이 바뀌면 옛 payload 가 새 코드에 그대로 먹혀 조용히 깨진다.
        `CHECKPOINT_SCHEMA_VERSION` 은 코어의 스키마라서 올리면 모든 룬의 체크포인트가
        같이 무효가 된다 — 잘못된 단위다. 룬 단위로 본다.

        `rune_version` 을 안 넘기면 버전을 보지 않는다. 옛 파일에는 이 값이 없고,
        그 경우도 통과시킨다 — 없던 필드가 생긴 것으로 재개를 깨뜨리지 않는다.
        """

        try:
            document = json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

        if not isinstance(document, dict):
            return None
        if document.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            return None
        stored = document.get("rune_version")
        if rune_version and stored is not None and stored != rune_version:
            return None

        data = document.get("data")
        return data if isinstance(data, dict) else None

    def save(self, key: CheckpointKey, data: dict, *, rune_version: str = "") -> None:
        """payload를 같은 디렉터리의 임시 파일에 쓴 뒤 원자적으로 교체한다."""

        self.root.mkdir(parents=True, exist_ok=True)
        target = self._path(key)
        document = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "rune_version": rune_version,
            "data": data,
        }

        with self._lock:
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.root,
                    prefix=f".{target.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    json.dump(document, stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())

                os.replace(temporary, target)
            except BaseException:
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass
                raise

    def clear(self, key: CheckpointKey) -> None:
        """체크포인트가 있으면 삭제한다. 이미 없어도 성공이다."""

        with self._lock:
            try:
                self._path(key).unlink()
            except FileNotFoundError:
                pass

    def _path(self, key: CheckpointKey) -> Path:
        rune_id = _safe_component(key.rune_id)
        identity = _safe_component(key.input_identity)
        return self.root / f"{rune_id}__{identity}.json"


def _safe_component(value: str) -> str:
    """사람이 알아볼 수 있으면서 충돌과 경로 탈출이 없는 파일명 조각."""

    text = str(value)
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip(" ._")
    readable = readable[:64] or "empty"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{digest}"

