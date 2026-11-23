"""기기 페어링. Phase 15.

목표는 한 줄이다 — **다른 화면에서 같은 펫을 다시 만난다.**

계정 서버는 안 만든다(`PLAN.md`). 같은 네트워크에서 **사람이 코드를 옮겨
적는 것**이 인증이다. 그 대신 코드는 짧고, 그래서 규칙이 필요하다.

    여섯 자리 · 5분 · 한 번만 · 다섯 번 틀리면 죽는다

여섯 자리는 100만 가지다. 무제한으로 찔러 보면 몇 분이면 뚫린다. 유효
시간과 시도 횟수 중 **하나만 있으면 부족하다** — 5분 안에 백만 번을 찌를
수 있고, 시도 제한만 있으면 코드가 영원히 산다.

기기 하나가 받는 것은 **토큰 하나**다. 토큰은 사람이 옮겨 적지 않으므로
길게 만든다(`secrets.token_urlsafe`). 끊고 싶으면 지운다 — 만료는 없다.
전화기를 잃어버렸을 때 사용자가 할 일이 "기다리기" 여서는 안 된다.

## 이 파일이 모르는 것

관찰도 룬도 모른다. **페어링은 정체성의 문이지 데이터의 문이 아니다.**
무엇이 그 문으로 나갈 수 있는지는 `server.py` 와 `data-map.md` 가 정한다.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vader.core import config

__all__ = ["Device", "Pending", "new_code", "claim", "device_for", "devices", "revoke"]

#: 코드가 사는 시간(초). 사람이 옆 방에서 폰을 가져올 만큼은 되고, 자리를
#: 비운 사이 남아 있을 만큼은 아니다.
CODE_TTL = 300

#: 이 횟수만큼 틀리면 코드가 죽는다. **유효 시간만으로는 부족하다** —
#: 5분이면 여섯 자리를 다 찔러 볼 수 있다.
MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class Device:
    """페어링을 마친 기기 하나."""

    token: str
    name: str
    paired_at: str


@dataclass
class Pending:
    """아직 아무도 안 쓴 코드. **메모리에만 있다.**

    디스크에 두면 앱이 죽어도 코드가 살아남는다. 화면에 떠 있는 동안만
    유효한 것이 맞다 — 창을 닫았는데 코드가 도는 것이 더 이상하다.
    """

    code: str
    born: float = field(default_factory=time.monotonic)
    attempts: int = 0

    @property
    def alive(self) -> bool:
        return (
            self.attempts < MAX_ATTEMPTS
            and time.monotonic() - self.born < CODE_TTL
        )

    @property
    def seconds_left(self) -> int:
        return max(0, int(CODE_TTL - (time.monotonic() - self.born)))


def _same(left: str, right: str) -> bool:
    """상수 시간 비교. **바이트로 바꿔서 한다.**

    `compare_digest` 는 비 ASCII 문자열을 받으면 TypeError 를 던진다. 비교
    대상은 폰이 보낸 값이라 무엇이든 올 수 있고, 여기서 예외가 나면 "틀린
    토큰" 이 **500** 이 된다 — 없는 것처럼 보여야 할 자리에서 서버가
    비명을 지르는 셈이다. 실제로 테스트가 잡았다.
    """
    return secrets.compare_digest(str(left).encode("utf-8"), str(right).encode("utf-8"))


def new_code() -> Pending:
    """여섯 자리. **`secrets` 로 뽑는다** — `random` 은 예측 가능하다."""
    return Pending(code=f"{secrets.randbelow(1_000_000):06d}")


def claim(pending: Pending | None, code: str, name: str) -> str:
    """코드를 토큰으로 바꾼다. **실패는 빈 문자열이다.**

    맞았는지 틀렸는지 이유를 나눠 주지 않는다. "만료됐습니다" 와 "틀렸습니다"
    를 가르면 그것만으로 어떤 코드가 살아 있었는지 알려 주는 셈이다.
    """
    if pending is None or not pending.alive:
        return ""
    # 틀린 시도를 **먼저** 센다. 비교하다 예외가 나도 횟수는 올라간다.
    pending.attempts += 1
    if not _same(pending.code, code):
        return ""
    # 맞았으면 코드는 그 자리에서 죽는다. 한 코드로 두 기기가 붙으면
    # 사용자는 자기가 붙인 기기가 몇 대인지 셀 수 없다.
    pending.attempts = MAX_ATTEMPTS

    device = Device(
        token=secrets.token_urlsafe(32),
        name=(str(name).strip() or "이름 없는 기기")[:40],
        paired_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    _write(devices() + [device])
    return device.token


def device_for(token: str) -> Device | None:
    """토큰의 주인. **상수 시간으로 비교한다.**

    `==` 로 비교하면 맞는 글자 수만큼 오래 걸린다. 로컬 네트워크에서 그
    차이를 재는 것은 어렵지만, 어려운 것과 못 하는 것은 다르다.
    """
    if not token:
        return None
    for device in devices():
        if _same(device.token, token):
            return device
    return None


def devices() -> list[Device]:
    """붙어 있는 기기들. **읽기 실패는 빈 목록이다.**"""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    out = []
    for row in data if isinstance(data, list) else []:
        try:
            out.append(Device(**row))
        except TypeError:
            continue
    return out


def revoke(token: str) -> bool:
    """기기 하나를 끊는다. 전화기를 잃어버렸을 때 할 일이 있어야 한다."""
    kept = [d for d in devices() if not _same(d.token, token)]
    if len(kept) == len(devices()):
        return False
    _write(kept)
    return True


def _path() -> Path:
    return config.data_dir() / "devices.json"


def _write(rows: list[Device]) -> None:
    target = _path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # 펫·체크포인트와 같은 방식. 쓰다 죽어도 반쯤 쓰인 파일이 안 남는다.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent,
        prefix=".devices.", suffix=".tmp", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        json.dump([asdict(row) for row in rows], stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
