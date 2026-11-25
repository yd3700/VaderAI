"""로그인·잠금 — 계정 작업공간을 여는 문 (2단계 2a, 마스터 결정 2026-11-05, codex1 설계).

- 작업공간은 `HOME/accounts/<키>/`. 키 = sha256([웹 주소, 서버 계정 id]) — 이름·이메일은 안 쓴다.
  `profile.json` 이 그 쌍을 적어 두고, 열 때마다 맞춰 본다.
- 서버가 계정을 확인해 주면 기기 금고에 (계정, 시각)을 남긴다. 인터넷이 끊겼으면 그 기록이
  **7일 안**이고 시계가 뒤로 가지 않았을 때만 PIN 으로 들어간다. 서버가 거절(401)하면 바로 로그인부터.
- PIN 6자리, scrypt + 개별 salt. 5번 틀리면 30초부터 두 배씩(최대 1시간) 기다린다. 횟수는 파일에 남아
  앱을 다시 켜도 초기화되지 않는다. 파일이 깨졌으면 새 PIN 이 아니라 같은 계정 재로그인으로.

**이것은 화면 잠금이다.** 같은 Windows 사용자로 파일을 직접 여는 것은 막지 못한다 — 6자리는 파일을 가져간
공격자의 대입을 막을 만큼 크지 않다. DPAPI 도 같은 사용자를 가르지 않는다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path

from vader.core import account, config, secrets

OFFLINE_DAYS = 7
PIN_LENGTH = 6
_SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}
_pin_lock = threading.Lock()   # ponytail: 프로세스 안에서만 줄 세운다. 앱 두 개를 동시에 띄워 대입하는 것까지는 안 막는다


class GateError(ValueError):
    """화면에 그대로 보일 문장."""


def key(origin: str, account_id: str) -> str:
    return hashlib.sha256(json.dumps([origin, account_id]).encode()).hexdigest()


def root(origin: str, account_id: str) -> Path:
    return config.HOME / "accounts" / key(origin, account_id)


def who() -> dict:
    """지금 이 PC 에 로그인한 계정. {'state': 'ok', 'origin', 'account'} 또는 들어갈 수 없는 이유."""
    origin = account.site()
    if not account.signed_in():
        return {"state": "replaced" if account.ended_reason() == "replaced" else "login"}
    try:
        found = account.account()
    except account.AccountError:
        found = False                                   # 서버에 못 닿았다 — 끊긴 것과 다르다
    if found:
        record = {"id": found["id"], "name": found.get("display_name") or "", "at": time.time()}
        secrets.put(_verified_name(origin), json.dumps(record), device=True)
        return {"state": "ok", "origin": origin, "account": {"id": found["id"], "display_name": record["name"]}}
    if found is None:                                   # 서버가 거절 — 토큰은 account 가 이미 지웠다
        secrets.delete(_verified_name(origin), device=True)
        # 다른 PC 로그인으로 끊겼으면 그렇게 알린다 (한 계정 한 PC, 2026-11-25)
        return {"state": "replaced" if account.ended_reason() == "replaced" else "login"}
    try:
        record = json.loads(secrets.get(_verified_name(origin), device=True) or "null")
        age = time.time() - float(record["at"])
        account_id = str(record["id"])
    except (ValueError, TypeError, KeyError):
        return {"state": "offline"}
    if not -300 <= age <= OFFLINE_DAYS * 86400:          # 시계가 뒤로 갔거나 7일이 지났다 — 서버 확인부터
        return {"state": "offline"}
    if account.stored_account(origin) != account_id:     # 지금 로그인(저장된 토큰)의 계정 기록만 — 다른 계정으로 바뀌었으면 서버 확인부터 (codex1 회신 9)
        return {"state": "offline"}
    return {"state": "ok", "origin": origin, "account": {"id": account_id, "display_name": record.get("name", "")}}


def logout() -> None:
    """명시적 로그아웃은 오프라인 입장 기록도 지운다 (codex1)."""
    origin = account.site()
    account.logout()
    secrets.delete(_verified_name(origin), device=True)


def _verified_name(origin: str) -> str:
    return "account.verified." + hashlib.sha256(origin.encode()).hexdigest()[:16]


def open_profile(origin: str, account_id: str) -> Path:
    """작업공간 폴더를 만들거나 열고, 적힌 계정이 맞는지 본다."""
    folder = root(origin, account_id)
    profile = folder / "profile.json"
    want = {"origin": origin, "account_id": account_id}
    if profile.exists():
        try:
            have = json.loads(profile.read_text(encoding="utf-8"))
        except ValueError:
            have = None
        if not isinstance(have, dict) or {k: have.get(k) for k in want} != want:
            raise GateError("작업공간 정보가 이 계정과 맞지 않아요. 앱을 다시 켜 주세요.")
    else:
        folder.mkdir(parents=True, exist_ok=True)
        _save(profile, want)
    return folder


# ── PIN ──

def has_pin(folder: Path) -> bool:
    return (folder / "lock.json").exists()


def wait_seconds(folder: Path) -> int:
    try:
        return max(0, int(_load_pin(folder).get("until", 0) - time.time()))
    except GateError:
        return 0


def set_pin(folder: Path, pin: str) -> None:
    _check_shape(pin)
    salt = os.urandom(16)
    _save(folder / "lock.json", {"v": 1, **{k: _SCRYPT[k] for k in ("n", "r", "p")}, "salt": salt.hex(),
                                 "hash": _hash(pin, salt, _SCRYPT).hex(), "fails": 0, "until": 0})


def check_pin(folder: Path, pin: str) -> None:
    """맞으면 그냥 돌아온다. 틀리거나 기다려야 하면 GateError."""
    with _pin_lock:
        record = _load_pin(folder)
        left = int(record["until"] - time.time())
        if left > 0:
            raise GateError(f"{left}초 뒤에 다시 입력해 주세요.")
        params = {**_SCRYPT, **{k: record[k] for k in ("n", "r", "p")}}
        ok = isinstance(pin, str) and hmac.compare_digest(_hash(pin, bytes.fromhex(record["salt"]), params).hex(),
                                                          record["hash"])
        if ok:
            record.update(fails=0, until=0)
        else:
            record["fails"] += 1
            if record["fails"] >= 5:
                record["until"] = time.time() + min(3600, 30 * 2 ** (record["fails"] - 5))
        _save(folder / "lock.json", record)
    if not ok:
        raise GateError("PIN 이 맞지 않아요." + (" 잠시 뒤에 다시 입력해 주세요." if record["fails"] >= 5 else ""))


def clear_pin(folder: Path) -> None:
    """같은 계정으로 다시 로그인했을 때만 부른다 — 'PIN 을 잊었어요'."""
    (folder / "lock.json").unlink(missing_ok=True)


def _check_shape(pin) -> None:
    if not (isinstance(pin, str) and len(pin) == PIN_LENGTH and pin.isascii() and pin.isdigit()):
        raise GateError(f"PIN 은 숫자 {PIN_LENGTH}자리예요.")
    steps = {int(b) - int(a) for a, b in zip(pin, pin[1:])}
    if steps in ({0}, {1}, {-1}):
        raise GateError("같은 숫자나 연속된 숫자는 쓸 수 없어요.")


def _hash(pin: str, salt: bytes, params: dict) -> bytes:
    return hashlib.scrypt(pin.encode(), salt=salt, n=params["n"], r=params["r"], p=params["p"],
                          maxmem=params["maxmem"], dklen=params["dklen"])


def _load_pin(folder: Path) -> dict:
    try:
        record = json.loads((folder / "lock.json").read_text(encoding="utf-8"))
        if (record["v"] != 1 or not isinstance(record["fails"], int) or not isinstance(record["until"], (int, float))
                or len(bytes.fromhex(record["salt"])) != 16 or len(record["hash"]) != 64
                or not all(isinstance(record[k], int) for k in ("n", "r", "p"))):
            raise ValueError
        return record
    except (OSError, ValueError, TypeError, KeyError):
        raise GateError("잠금 정보가 손상됐어요. 'PIN 을 잊었어요' 로 다시 로그인해 주세요.") from None


def _save(path: Path, data: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temporary, path)


# ── 기기 잠금: 앱 하나 또는 옮기기(claim) 하나만 (2b codex1 P1-2) ──

_instance = None


def hold_instance() -> bool:
    """`HOME/app.lock` 을 잡는다. 이미 다른 프로세스가 잡았으면 False. 프로세스가 죽으면 OS 가 푼다."""
    global _instance
    if _instance is not None:
        return True
    config.HOME.mkdir(parents=True, exist_ok=True)
    handle = open(config.HOME / "app.lock", "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _instance = handle
    return True


def release_instance() -> None:
    global _instance
    if _instance is not None:
        _instance.close()
        _instance = None


def claim_pending() -> bool:
    """멈춘 옮기기의 원장이 남았나 — 그동안 앱은 계정을 열지 않는다 (2b codex1 P1-3)."""
    return (config.HOME / "claim.json").exists()


# ── 자식 프로세스(MCP·제작소 시험)와의 약속 ──

def mark(folder: Path | None, unlocked: bool, gen: str = "") -> None:
    """앱이 지금 어느 작업공간을 열고 있고 잠겼는지 — MCP 서버가 요청마다 본다.
    gen 은 앱이 잠금을 풀 때마다 새로 만든 값(맥박은 같은 값을 다시 씀). 파일의 옛 값에서 짐작하지 않는다 —
    잠금 표식 쓰기가 실패해도 다시 열면 새 값이라, 요청 앞뒤를 비교하면 그 사이 잠금을 안다 (codex1 화면 자동화 r2 P1-2)."""
    _save(config.HOME / "session.json", {"workspace": str(folder) if folder else "", "unlocked": unlocked,
                                         "pid": os.getpid(), "at": time.time(), "gen": gen if unlocked else ""})


def open_child() -> None:
    """자식 프로세스: 환경변수의 작업공간을 연다. 없거나 계정 폴더가 아니면 열지 않는다 — 공용 폴더로 내려가지 않는다."""
    config.REQUIRE_WORKSPACE = True
    value = os.environ.get(config.WORKSPACE_ENV, "")
    if not value:
        return
    folder = Path(value)
    if folder.parent != config.HOME / "accounts" or not (folder / "profile.json").is_file():
        return
    config.use_workspace(folder)


def _session() -> dict | None:
    """열려 있으면 session.json 레코드 한 번 읽은 것, 아니면 None."""
    try:
        session = json.loads((config.HOME / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = (isinstance(session, dict) and session.get("unlocked") is True
          and session.get("workspace") == str(config.workspace() or "-") and _alive(session.get("pid"))
          and isinstance(session.get("at"), (int, float)) and 0 <= time.time() - session["at"] < 15)
    return session if ok else None


def session_open() -> bool:
    """앱이 이 작업공간을 열어 둔 채 잠기지 않았나. 앱이 꺼졌거나 맥박이 15초 넘게 끊겼으면 닫힌 것으로 본다
    (잠글 때 표식을 못 써도 열린 채로 믿지 않게, codex1 P1-7)."""
    return _session() is not None


def session_gen() -> str | None:
    """열려 있으면 이번 열림의 세대 값, 아니면 None. 한 번 읽은 레코드로 열림과 세대를 함께 판단한다."""
    session = _session()
    gen = session.get("gen") if session else None
    return gen if isinstance(gen, str) and gen else None


def _alive(pid) -> bool:
    if not isinstance(pid, int):
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
