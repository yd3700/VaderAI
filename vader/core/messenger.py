"""1:1 메신저 — 앱 쪽 (3단계 3c, 마스터 결정·codex1 설계·검토).

본문은 이 PC 와 상대 PC 사이에서만 풀린다. 서버(/api/messenger/)는 암호문과 공개키만 맡는다.
암호는 vodozemac(Matrix, Olm 1:1 — Double Ratchet). 판은 0.10.0 으로 고정 (3a 시험).

지키는 약속 (codex1 P1-5, 3a 시험 6·7, 3c 검토):
- **보내기**: 암호화한 세션 상태·보낼 암호문·내 기록을 한 거래로 저장한 **뒤에** 올린다. 다시 보낼 때는
  저장한 바이트를 그대로, 그 암호문을 만든 **이 기기**로만. 29일이 지나면 다시 보내지 않는다(서버 기록 30일).
- **받기**: 풀고, 세션 상태·메시지·받은 표시를 한 거래로 저장한 **뒤에** 서버에 '받았다'(ack). 이미 받은 번호는 건너뛴다.
  풀 수 없는 메시지 하나가 뒤의 메시지를 막지 않는다. 저장이 실패하면 ack 하지 않는다.
- **믿기**: 상대 기기의 신원키(ed25519)는 처음 본 것을 붙잡아 둔다. 안전 번호를 확인하기 전, 또는 신원키가
  바뀐 뒤 다시 확인하기 전에는 **보내지 않는다** (codex1 P1-2). 서버가 준 암호키·일회용 키는 신원키 서명으로 확인한다.
  받은 메시지에는 받을 때 그 신원키를 확인했었는지 남긴다 — 나중에 확인해도 지난 메시지가 확인된 것처럼 보이지 않게.
- **일회용 키**: 만든 지 29일이 지난 묶음은 올리지 않는다 — 서버가 쓴 키 기록을 30일 뒤 지우므로 쓴 키가 되살아날 수 있다.
- **잠금**: 화면이 잠기면 하던 일을 다음 단계에서 멈춘다. 저장 중인 거래는 끝까지, ack 는 다음에 다시.

저장: 계정 작업공간 `messenger.sqlite3`. 본문과 세션 상태는 계정 금고(DPAPI)의 무작위 키로 암호화한다.
같은 Windows 사용자로 도는 프로그램까지 막지는 못한다 (2a 와 같은 고지).
본문을 로그·오류 문장·화면 도움말 대화·MCP 로 내보내지 않는다 (codex1 P1-7). 받은 본문은 명령·AI 입력으로 쓰지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from vader.core import account, config, secrets

API = "/api/messenger/"
STORE_KEY = "messenger.store_key"
MAX_TEXT = 2000
#: 서버에 올려 둘 일회용 키 (서버 상한 50 — 미사용 기준). vodozemac 내부 비밀 키 보관량은 이보다 훨씬 크다.
KEYS_TARGET = 40
KEYS_LOW = 15
#: 보내기·키 올리기 재시도 기한 — 서버의 보낸 기록·쓴 키 기록(30일)보다 짧아야 한다.
RETRY_SECONDS = 29 * 86400
_busy = threading.Lock()        # 한 번에 하나 — 세션 상태를 두 요청이 같이 고치지 않게
_guard = threading.local()      # 화면 쪽이 '아직 열려 있나' 를 넣는다 (host.py)
# ponytail: 예비 키는 n=1 하나로 계속 쓴다. 회전(n 올리기·옛 비밀 키 보존 기간)은 오래 쓴 예비 키를 갈 이유가 생기면.
# ponytail: 세션·기록 개수 상한·삭제·백업 복원 정책, 재시도 간격(Retry-After)은 쓰는 양이 보이면.

SCHEMA = """
CREATE TABLE IF NOT EXISTS me (id INTEGER PRIMARY KEY CHECK (id = 1), device TEXT NOT NULL,
  pickle TEXT NOT NULL, registered INTEGER NOT NULL DEFAULT 0, keys_since REAL NOT NULL);
CREATE TABLE IF NOT EXISTS peers (account TEXT PRIMARY KEY, handle TEXT, state TEXT NOT NULL,
  mine INTEGER NOT NULL, generation INTEGER NOT NULL, device TEXT, ed TEXT, curve TEXT, verified_ed TEXT);
CREATE TABLE IF NOT EXISTS peer_devices (device TEXT PRIMARY KEY, account TEXT NOT NULL, ed TEXT NOT NULL,
  curve TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions (sid TEXT PRIMARY KEY, device TEXT NOT NULL, pickle TEXT NOT NULL,
  used REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (client_id TEXT PRIMARY KEY, sender TEXT NOT NULL, peer TEXT NOT NULL,
  device TEXT NOT NULL, generation INTEGER NOT NULL, kind INTEGER NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, peer TEXT NOT NULL, outgoing INTEGER NOT NULL,
  text BLOB, at REAL NOT NULL, state TEXT NOT NULL, device TEXT, trusted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS messages_peer ON messages(peer, at);
CREATE TABLE IF NOT EXISTS seen (account TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS groups (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS share_pending (message TEXT PRIMARY KEY, account TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS group_members (account TEXT PRIMARY KEY, gid TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE);
"""


class MessengerError(RuntimeError):
    pass


class Stopped(MessengerError):
    def __init__(self):
        super().__init__("잠겨 있어요.")


def set_alive(check) -> None:
    """이 스레드의 다음 메신저 일이 '아직 열려 있나' 를 물을 함수. host 가 요청마다 넣는다."""
    _guard.check = check


def _live() -> None:
    check = getattr(_guard, "check", None)
    if check is not None and not check():
        raise Stopped()


def _vz():
    import vodozemac          # 화면 앱(desktop) 묶음에만 있다 — 코어를 가져올 때 끌어오지 않는다
    return vodozemac


# ── 서명 문장: 서버(web/server/messenger.ts)와 글자 하나까지 같아야 한다 ──

def bind_message(acct: str, session: str, device: str, curve: str) -> str:
    return f"vader-messenger-bind-v2|{acct}|{session}|{device}|{curve}"


def device_message(acct: str, device: str, curve: str) -> str:
    return f"vader-messenger-device-v1|{acct}|{device}|{curve}"


def key_message(device: str, key_id: str, key: str) -> str:
    return f"vader-messenger-otk-v1|{device}|{key_id}|{key}"


def fallback_message(device: str, n: int, key: str) -> str:
    return f"vader-messenger-fallback-v2|{device}|{n}|{key}"


def signed(ed: str, message: str, signature: str) -> bool:
    vz = _vz()
    try:
        vz.Ed25519PublicKey.from_base64(ed).verify_signature(message.encode(), vz.Ed25519Signature.from_base64(signature))
        return True
    except Exception:          # 모양이 틀린 키·서명도 '맞지 않음' 하나로
        return False


def safety_number(acct_a: str, ed_a: str, acct_b: str, ed_b: str) -> str:
    """두 사람이 각자 화면에서 같은 숫자를 봐야 한다. 60자리, 5자리씩 12묶음 — 모두 해시에서 나온다."""
    joined = "|".join(sorted([f"{acct_a}:{ed_a}", f"{acct_b}:{ed_b}"]))
    digest = hashlib.sha512(("vader-safety-v2|" + joined).encode()).digest()
    return " ".join(f"{int.from_bytes(digest[i:i + 4], 'big') % 100000:05d}" for i in range(0, 48, 4))


# ── 저장 ──

def _key() -> bytes:
    stored = secrets.get(STORE_KEY)
    if not stored:
        if (config.data_dir() / "messenger.sqlite3").exists():
            raise MessengerError("메신저 기록을 여는 열쇠가 없습니다. 이 PC 의 대화 기록은 열 수 없습니다.")
        stored = base64.b64encode(os.urandom(32)).decode()
        secrets.put(STORE_KEY, stored)
    return base64.b64decode(stored)


def _seal(text: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    nonce = os.urandom(12)
    return nonce + ChaCha20Poly1305(_key()).encrypt(nonce, text.encode(), b"vader-messenger-v1")


def _unseal(blob: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    return ChaCha20Poly1305(_key()).decrypt(blob[:12], blob[12:], b"vader-messenger-v1").decode()


def _db() -> sqlite3.Connection:
    _key()                     # 열쇠부터 — 열쇠 없이 새 기록 파일을 만들지 않는다
    con = sqlite3.connect(config.data_dir() / "messenger.sqlite3", isolation_level=None, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


@contextmanager
def _tx(con: sqlite3.Connection):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


@contextmanager
def _one(wait: bool = True):
    if not _busy.acquire(timeout=30 if wait else 0):
        if wait:
            raise MessengerError("메신저가 다른 일을 하고 있습니다. 잠시 후 다시 시도해 주세요.")
        yield None
        return
    try:
        _live()                # 기다리는 사이 잠겼으면 시작하지 않는다 (codex1 3c P2-3)
        con = _db()
        try:
            yield con
        finally:
            con.close()
    finally:
        _busy.release()


def _me(con):
    row = con.execute("SELECT * FROM me").fetchone()
    return row, (_vz().Account.from_pickle(row["pickle"], _key()) if row else None)


def _api(method: str, action: str, body: dict | None = None) -> dict:
    _live()                    # 네트워크로 나가기 전마다
    # 끝의 / 를 붙인다 — 웹은 trailingSlash 라 없으면 308 이고, 앱은 리디렉션을 따라가지 않는다 (0.1.25 실기 시험)
    return account.call(method, API + action + "/", json.dumps(body).encode() if body is not None else None)


def _who() -> tuple[str, str]:
    """접근 토큰에 적힌 (계정, 로그인 기기). 서명 문장에 넣는다 — 서버가 같은 토큰으로 다시 확인한다."""
    token = account.access_token()
    if not token:
        raise MessengerError(account.NOT_SIGNED_IN)
    body = token.split(".")[0]
    claim = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    return str(claim["a"]), str(claim["d"])


# ── 이 PC 를 메신저 기기로 ──

def connect() -> dict:
    """메신저를 열 때 부른다. 처음이면 키를 만들어 등록하고, 다시 로그인했으면 새 로그인에 다시 묶는다.

    state: not_joined(웹에서 시작 전) · other_pc(다른 PC 가 메신저 기기) · ready
    """
    acct, session = _who()
    with _one() as con:
        # 서버 상태는 잠금을 잡은 뒤에 본다 — 먼저 본 '기기 없음' 으로 방금 등록한 기기를 지우지 않게 (codex1 3c NEW-P1-1)
        status = _api("GET", "me")
        if not status["joined"]:
            return {"state": "not_joined"}
        row, me = _me(con)
        active = status.get("active_device")
        if row is not None and row["registered"] and active != row["device"]:
            if active:
                return {"state": "other_pc", "handle": status["handle"]}
            row = me = _forget_device(con)          # 웹에서 이 PC 를 해제했다 — 새 키로 새 기기가 된다
        if row is None:
            if active:
                return {"state": "other_pc", "handle": status["handle"]}
            row, me = _new_device(con)
        if status.get("device") != row["device"]:
            try:
                _register(con, row, me, acct, session)
            except account.AccountError as exc:
                if exc.status != 409 or row["registered"] or active:
                    raise
                # 등록은 됐는데 답을 못 받았고, 그 뒤 웹에서 해제됐다 — 해제된 기기로는 다시 못 묶는다
                _forget_device(con)
                row, me = _new_device(con)
                _register(con, row, me, acct, session)
            status = _api("GET", "me")              # 다시 묶은 뒤의 실제 키 수 (codex1 3c P2-1)
        with _tx(con):           # 등록 응답을 못 받았어도 서버가 이 기기를 알면 등록된 것이다
            con.execute("UPDATE me SET registered=1, pickle=? WHERE id=1", (me.pickle(_key()),))
        _top_up(con, me, row["device"], int(status["one_time_keys"]))
    return {"state": "ready", "handle": status["handle"], "device": row["device"]}


def move() -> dict:
    """이 PC 로 메신저 옮기기 (2026-11-25 마스터 결정: 안내하고 확인받은 뒤 정리). 사람이 화면에서 확인한 뒤에만 부른다.

    서버는 옛 메신저 기기가 살아 있는 로그인에 묶여 있지 않을 때만 해제한다(한 계정 한 PC 라 앞 PC 는 이미 끊겼다).
    옛 기기의 안 받은 메시지·키는 서버에서 지워지고, 이 PC 는 새 키로 새 기기가 된다 — 친구는 안전 번호를 다시 확인한다.
    """
    _api("POST", "move")
    return connect()


def _new_device(con):
    me = _vz().Account()
    me.generate_one_time_keys(KEYS_TARGET)
    me.generate_fallback_key()
    with _tx(con):       # 키를 먼저 저장한다 — 등록 응답을 못 받아도 같은 키로 다시
        con.execute("INSERT INTO me(id, device, pickle, keys_since) VALUES (1, ?, ?, ?)",
                    (str(uuid.uuid4()), me.pickle(_key()), time.time()))
    return con.execute("SELECT * FROM me").fetchone(), me


def _forget_device(con):
    """이 PC 의 메신저 기기를 버린다. 지난 대화 기록은 남기고, 옛 기기로 만든 보낼 암호문은 실패로 끝낸다 (codex1 3c P1-2)."""
    with _tx(con):
        con.execute("UPDATE messages SET state='failed' WHERE id IN (SELECT client_id FROM outbox)")
        con.execute("DELETE FROM outbox")
        # 내 신원키가 바뀌면 안전 번호도 바뀐다 — 지금까지의 확인은 다시 받는다 (지난 메시지의 trusted 는 그대로) (codex1 3c NEW-P2-1)
        con.execute("UPDATE peers SET verified_ed=NULL")
        con.execute("UPDATE peer_devices SET verified=0")
        con.execute("DELETE FROM me")
        con.execute("DELETE FROM sessions")
    return None


def _register(con, row, me, acct: str, session: str) -> None:
    device, curve = row["device"], me.curve25519_key.to_base64()
    body = {"device_id": device, "proof": me.sign(bind_message(acct, session, device, curve).encode()).to_base64()}
    if not row["registered"]:
        body.update(identity_ed=me.ed25519_key.to_base64(), identity_curve=curve,
                    identity_sig=me.sign(device_message(acct, device, curve).encode()).to_base64(),
                    one_time_keys=_unpublished(me, device) if _fresh(row) else [], fallback=_fallback(me, device))
    _api("POST", "register", body)
    me.mark_keys_as_published()     # 처음 등록이면 키가 함께 올라갔고, 다시 묶기면 이미 올라가 있다


def _fresh(row) -> bool:
    return time.time() - row["keys_since"] <= RETRY_SECONDS


def _unpublished(me, device: str) -> list[dict]:
    return [{"id": key_id, "key": key.to_base64(),
             "signature": me.sign(key_message(device, key_id, key.to_base64()).encode()).to_base64()}
            for key_id, key in me.one_time_keys.items()]


def _fallback(me, device: str) -> dict | None:
    keys = me.fallback_key
    if not keys:
        return None
    key = next(iter(keys.values())).to_base64()
    return {"key": key, "n": 1, "signature": me.sign(fallback_message(device, 1, key).encode()).to_base64()}


def _top_up(con, me, device: str, on_server: int) -> None:
    """서버의 일회용 키가 모자라면 새로 만들어 올린다. 올리기 전에 저장 — 응답을 못 받아도 같은 키로 다시.
    29일이 지난 묶음은 올리지 않고 버린다 — 그사이 서버가 쓴 키 기록을 지웠을 수 있다 (codex1 3c P1-4)."""
    row = con.execute("SELECT * FROM me").fetchone()
    if me.one_time_keys and not _fresh(row):
        me.mark_keys_as_published()             # 올리지 않은 채 '올린 것' 으로 — 이 비밀 키들은 쓰이지 않는다
        with _tx(con):
            con.execute("UPDATE me SET pickle=? WHERE id=1", (me.pickle(_key()),))
    if not me.one_time_keys and on_server >= KEYS_LOW:
        return
    if not me.one_time_keys:
        me.generate_one_time_keys(KEYS_TARGET - on_server)
        with _tx(con):
            con.execute("UPDATE me SET pickle=?, keys_since=? WHERE id=1", (me.pickle(_key()), time.time()))
    _api("POST", "keys", {"one_time_keys": _unpublished(me, device)})
    me.mark_keys_as_published()
    with _tx(con):
        con.execute("UPDATE me SET pickle=? WHERE id=1", (me.pickle(_key()),))


# ── 친구 ──

def find(handle: str) -> dict:
    return _api("POST", "find", {"handle": handle})


def friend(account_id: str, op: str) -> list[dict]:
    """op: request · accept · remove(거절·취소·끊기)."""
    _api("POST", "friend", {"account_id": account_id, "op": op})
    return friends()


def block(account_id: str, op: str) -> list[dict]:
    _api("POST", "block", {"account_id": account_id, "op": op})
    return friends()


def friends() -> list[dict]:
    """서버 목록을 받아 붙잡아 둔 신원키와 맞춘다. 신원키가 바뀐 친구는 다시 확인할 때까지 보내지 못한다."""
    data = _api("GET", "friends")
    with _one() as con:
        _remember(con, data["friends"])
        out = _list(con)
    blocked = {b["account_id"] for b in data.get("blocked", [])}
    # 차단한 사람은 '차단' 으로만 — 화면에서 풀 수 있게 (codex1 3d P2-5)
    return [f for f in out if f["account_id"] not in blocked] + [
        {"account_id": b["account_id"], "handle": b.get("handle"), "state": "blocked", "mine": True, "ready": False,
         "verified": False, "changed": False, "last": None} for b in data.get("blocked", [])]


def _remember(con, listed: list[dict]) -> None:
    seen = set()
    with _tx(con):
        for f in listed:
            peer, device, ed, curve = f["account_id"], f.get("device_id"), f.get("identity_ed"), f.get("identity_curve")
            seen.add(peer)
            if device and not signed(ed, device_message(peer, device, curve), f.get("identity_sig") or ""):
                device = ed = curve = None          # 신원키가 서명하지 않은 암호키는 받지 않는다
            if device:   # 지나간 기기도 남긴다 — 기기를 바꾸기 전에 보낸 메시지를 풀 수 있게 (codex1 3c P1-3)
                con.execute("INSERT OR IGNORE INTO peer_devices(device, account, ed, curve) VALUES (?,?,?,?)",
                            (device, peer, ed, curve))
            old = con.execute("SELECT * FROM peers WHERE account=?", (peer,)).fetchone()
            if old is not None and not device:      # 서버가 기기를 안 줬다(아직 친구 아님·해제) — 붙잡은 키는 둔다
                device, ed, curve = old["device"], old["ed"], old["curve"]
            con.execute(
                """INSERT INTO peers(account, handle, state, mine, generation, device, ed, curve) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(account) DO UPDATE SET handle=excluded.handle, state=excluded.state, mine=excluded.mine,
                  generation=excluded.generation, device=excluded.device, ed=excluded.ed, curve=excluded.curve""",
                (peer, f.get("handle"), f["state"], int(bool(f.get("mine"))), int(f["generation"]), device, ed, curve))
        for row in con.execute("SELECT account FROM peers").fetchall():
            if row["account"] not in seen:
                con.execute("UPDATE peers SET state='ended' WHERE account=?", (row["account"],))


def _list(con) -> list[dict]:
    out = []
    for p in con.execute("""SELECT p.*, (SELECT max(at) FROM messages m WHERE m.peer=p.account AND m.outgoing=0
            AND m.state='received') AS last, (SELECT at FROM seen s WHERE s.account=p.account) AS seen,
            (SELECT gid FROM group_members g WHERE g.account=p.account) AS grp FROM peers p
            WHERE p.state<>'ended' OR EXISTS (SELECT 1 FROM messages m WHERE m.peer=p.account)
            ORDER BY p.handle""").fetchall():   # 끝난 친구도 기록이 있으면 '지난 대화' 로 (codex1 3d P2-5)
        out.append({"account_id": p["account"], "handle": p["handle"], "state": p["state"], "mine": bool(p["mine"]),
                    "ready": bool(p["ed"]), "verified": bool(p["ed"]) and p["verified_ed"] == p["ed"],
                    "changed": bool(p["verified_ed"]) and p["verified_ed"] != p["ed"],
                    "last": p["last"], "seen": p["seen"],   # 받은 시각·읽은 시각 — 화면이 '안 읽음' 을 정한다
                    "group": p["grp"],
                    "keywords": _keywords(con, p["account"])})
    return out


# 멤버 그룹 (마스터 요청 2026-11-18) — 친구를 '구매팀' 처럼 묶는다. 이 PC·이 계정 안에만 있고 서버·상대에게 안 간다.
# 친구 하나는 그룹 하나에. 그룹을 지우면 친구는 '그룹 없음' 으로 돌아간다.
GROUP_NAME_MAX = 20
GROUPS_MAX = 30


def _group_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not 1 <= len(name) <= GROUP_NAME_MAX:
        raise MessengerError(f"그룹 이름은 1~{GROUP_NAME_MAX}자로 적어 주세요.")
    return name


@contextmanager
def _local():
    """그룹처럼 이 PC 표만 만지는 일 — 메신저 작업 잠금(_busy)을 기다리지 않는다. 받기(sync)가 서버를 기다리는 동안
    그룹 읽기가 최대 30초 줄을 서서 화면이 멈춘 것처럼 보였다 (마스터 2026-11-19). 동시 쓰기는 SQLite 잠금이 맡는다."""
    _live()
    con = _db()
    try:
        yield con
    finally:
        con.close()


def groups() -> list[dict]:
    with _local() as con:
        return [{"id": r["id"], "name": r["name"], "count": r["n"]} for r in con.execute(
            "SELECT g.id, g.name, (SELECT count(*) FROM group_members m WHERE m.gid=g.id) AS n FROM groups g ORDER BY g.name")]


def group_save(name: str, gid: str = "") -> list[dict]:
    """gid 가 없으면 새 그룹, 있으면 이름 바꾸기."""
    name = _group_name(name)
    with _local() as con, _tx(con):
        if con.execute("SELECT 1 FROM groups WHERE name=? AND id<>?", (name, gid or "")).fetchone():
            raise MessengerError("같은 이름의 그룹이 있어요.")
        if gid:
            if not con.execute("UPDATE groups SET name=? WHERE id=?", (name, gid)).rowcount:
                raise MessengerError("그 그룹을 찾지 못했어요.")
        elif con.execute("SELECT count(*) FROM groups").fetchone()[0] >= GROUPS_MAX:
            raise MessengerError(f"그룹은 {GROUPS_MAX}개까지 만들 수 있어요.")
        else:
            con.execute("INSERT INTO groups(id, name) VALUES (?,?)", (uuid.uuid4().hex, name))
    return groups()


def group_delete(gid: str) -> list[dict]:
    with _local() as con, _tx(con):
        con.execute("DELETE FROM group_members WHERE gid=?", (gid,))
        con.execute("DELETE FROM groups WHERE id=?", (gid,))
    return groups()


def group_assign(account_id: str, gid: str = "") -> list[dict]:
    """친구를 그룹에 넣는다. gid 가 비면 그룹에서 뺀다."""
    with _local() as con, _tx(con):
        if not con.execute("SELECT 1 FROM peers WHERE account=?", (account_id,)).fetchone():
            raise MessengerError("그 친구를 찾지 못했어요.")
        con.execute("DELETE FROM group_members WHERE account=?", (account_id,))
        if gid:
            if not con.execute("SELECT 1 FROM groups WHERE id=?", (gid,)).fetchone():
                raise MessengerError("그 그룹을 찾지 못했어요.")
            con.execute("INSERT INTO group_members(account, gid) VALUES (?,?)", (account_id, gid))
    return groups()


# 대화 키워드 — 메인 그래프에 친구 옆 작은 노드로 (마스터 요청 2026-11-10). 이 PC 안에서만 센다 — 서버·로그로 안 나간다.
# ponytail: 조사 떼기 + 빈도. 형태소 분석기 없이 — 엉뚱한 낱말이 자주 뜨면 그때 분석기를 붙인다.
_WORD = re.compile(r"[가-힣A-Za-z0-9]{2,}")
_JOSA = ("에서는", "으로는", "에게서", "이에요", "예요", "입니다", "습니다", "니다", "에서", "에게", "으로", "까지", "부터",
         "하고", "이랑", "처럼", "보다", "은", "는", "이", "가", "을", "를", "에", "의", "도", "로", "와", "과", "요", "만")
_STOP = {"안녕하세요", "감사합니다", "고맙습니다", "네네", "그리고", "그런데", "그래서", "지금", "오늘", "내일", "확인",
         "부탁드립니다", "부탁해요", "알겠습니다", "수고하세요", "수고", "보냈어", "보냈어요", "있어", "없어", "그럼", "근데",
         "the", "and", "you", "for", "ok", "ㅎㅎ", "ㅋㅋ", "시험"}


def _stem(w: str) -> str:
    for j in _JOSA:
        if w.endswith(j) and len(w) - len(j) >= 2:
            return w[: -len(j)]
    return w


_CODE = re.compile(r"(?=.*[a-z])(?=.*\d)[a-z0-9]{6,}")   # 공유 번호·해시처럼 영문+숫자가 섞인 긴 토막 — 사람이 쓴 낱말이 아니다


def _keywords(con, peer: str, top: int = 3) -> list[str]:
    """사람이 주고받은 글에서만 — 할 일 공유 알림(앱끼리의 데이터)은 빼고, 번호 같은 토막도 뺀다 (마스터 2026-11-19)."""
    from vader.core.todo import SHARE_MARK
    count: dict[str, int] = {}
    for r in con.execute("SELECT text FROM messages WHERE peer=? AND text IS NOT NULL ORDER BY at DESC LIMIT 300", (peer,)):
        text = _unseal(r["text"])
        if text.startswith(SHARE_MARK):
            continue
        for w in _WORD.findall(text):
            w = _stem(w.lower())
            if w not in _STOP and not w.isdigit() and not _CODE.fullmatch(w):
                count[w] = count.get(w, 0) + 1
    return [w for w, _ in sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]


def mark_seen(account_id: str, at: float) -> bool:
    """화면에 그린 받은 메시지의 시각까지 읽음. 앱을 껐다 켜도 남는다 (마스터 지적 2026-11-09 — 화면 저장소는 끄면 지워졌다)."""
    with _one() as con:
        con.execute("INSERT INTO seen(account, at) VALUES (?, ?) ON CONFLICT(account) DO UPDATE SET at=max(at, excluded.at)",
                    (str(account_id), float(at)))
    return True


def _number(con, acct: str, me, account_id: str):
    p = con.execute("SELECT * FROM peers WHERE account=?", (account_id,)).fetchone()
    if me is None or p is None or not p["ed"]:
        raise MessengerError("아직 안전 번호를 만들 수 없습니다. 친구가 메신저를 열면 생깁니다.")
    return safety_number(acct, me.ed25519_key.to_base64(), account_id, p["ed"]), p


def safety(account_id: str) -> dict:
    """안전 번호. 두 사람이 다른 길(전화·대면)로 같은 숫자인지 확인한 뒤 verify 를 부른다."""
    acct, _ = _who()
    with _one() as con:
        number, p = _number(con, acct, _me(con)[1], account_id)
        return {"number": number, "verified": p["verified_ed"] == p["ed"]}


def verify(account_id: str, number: str) -> dict:
    """사람이 같은 숫자를 확인했다고 누른 뒤. 화면에 보인 번호와 지금 키가 같을 때만, 그 키를 확인된 것으로 —
    비교와 저장을 한 잠금·한 거래에서 (codex1 3c P1-1)."""
    acct, _ = _who()
    with _one() as con, _tx(con):
        current, p = _number(con, acct, _me(con)[1], account_id)
        if number != current:
            raise MessengerError("그사이 키가 바뀌었습니다. 안전 번호를 다시 확인해 주세요.")
        con.execute("UPDATE peers SET verified_ed=? WHERE account=? AND ed=?", (p["ed"], account_id, p["ed"]))
        con.execute("UPDATE peer_devices SET verified=1 WHERE device=? AND ed=?", (p["device"], p["ed"]))
    return {"verified": True}


# ── 보내기 ──

def send(account_id: str, text: str) -> dict:
    text = (text or "").strip()
    if not text or len(text) > MAX_TEXT:
        raise MessengerError(f"메시지는 1~{MAX_TEXT}자로 보내 주세요.")
    with _one() as con:
        row, me = _me(con)
        p = con.execute("SELECT * FROM peers WHERE account=?", (account_id,)).fetchone()
        if me is None or not row["registered"]:
            raise MessengerError("메신저를 먼저 여세요.")
        if p is None or p["state"] != "accepted" or not p["ed"]:
            raise MessengerError("친구에게만 보낼 수 있습니다.")
        if p["verified_ed"] != p["ed"]:
            raise MessengerError("안전 번호를 확인한 뒤에 보낼 수 있습니다.")
        session = _latest(con, p["device"])
        if session is None:
            session = _open_session(me, p)
        kind, body = session.encrypt(text.encode()).to_parts()
        client_id, now = str(uuid.uuid4()), time.time()
        with _tx(con):
            con.execute("INSERT OR REPLACE INTO sessions(sid, device, pickle, used) VALUES (?,?,?,?)",
                        (session.session_id, p["device"], session.pickle(_key()), now))
            con.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)", (client_id, row["device"], account_id,
                        p["device"], p["generation"], kind, base64.b64encode(body).decode(), now))
            con.execute("INSERT INTO messages VALUES (?,?,1,?,?,'queued',?,1)",
                        (client_id, account_id, _seal(text), now, p["device"]))
        _flush(con, row["device"])
        return _message(con.execute("SELECT * FROM messages WHERE id=?", (client_id,)).fetchone())


def _latest(con, device: str):
    row = con.execute("SELECT pickle FROM sessions WHERE device=? ORDER BY used DESC LIMIT 1", (device,)).fetchone()
    return _vz().Session.from_pickle(row["pickle"], _key()) if row else None


def _open_session(me, p):
    """상대 기기의 일회용 키 하나로 첫 세션을 연다. 서버가 준 키가 붙잡아 둔 신원키로 서명됐는지 확인한다."""
    vz = _vz()
    got = _api("POST", "claim", {"account_id": p["account"], "device_id": p["device"]})
    if (got["device_id"], got["identity_ed"], got["identity_curve"]) != (p["device"], p["ed"], p["curve"]):
        raise MessengerError("상대의 키가 바뀌었습니다. 친구 목록을 새로 받고 안전 번호를 다시 확인해 주세요.")
    k = got["key"]
    message = (fallback_message(p["device"], int(k.get("n", 0)), k["key"]) if k["fallback"]
               else key_message(p["device"], k["id"], k["key"]))
    if not signed(p["ed"], message, k["signature"]):
        raise MessengerError("상대 기기의 키 서명이 맞지 않아 보내지 않았습니다.")
    return me.create_outbound_session(vz.Curve25519PublicKey.from_base64(p["curve"]),
                                      vz.Curve25519PublicKey.from_base64(k["key"]))


def _flush(con, my_device: str) -> None:
    """저장해 둔 암호문을 순서대로 올린다. 연결이 안 되면 멈추고 다음에 같은 바이트로 다시."""
    for o in con.execute("SELECT * FROM outbox ORDER BY created").fetchall():
        state = "sent"
        if o["sender"] != my_device or time.time() - o["created"] > RETRY_SECONDS:
            state = "failed"     # 다른(옛) 기기가 만든 암호문이거나 서버 기록 기한이 지났다
        else:
            try:
                _api("POST", "send", {"client_id": o["client_id"], "recipient_account": o["peer"],
                                      "recipient_device": o["device"], "generation": o["generation"],
                                      "kind": o["kind"], "body": o["body"]})
            except account.AccountError as exc:
                if exc.status not in (400, 403, 409):
                    return               # 연결·제한(429)·아직 다시 묶기 전(412)·서버 사정 — 다음에
                state = "failed"         # 친구가 아님·기기가 바뀜 — 같은 바이트로는 영영 안 된다
        with _tx(con):
            con.execute("DELETE FROM outbox WHERE client_id=?", (o["client_id"],))
            con.execute("UPDATE messages SET state=? WHERE id=?", (state, o["client_id"]))


# ── 받기 ──

def sync() -> dict:
    """화면이 열려(잠기지 않은) 동안 몇 초마다 부른다. 다른 메신저 일이 돌고 있으면 이번은 건너뛴다."""
    with _one(wait=False) as con:
        if con is None:
            return {"skipped": True}
        row, me = _me(con)
        if me is None or not row["registered"]:
            return {"received": 0}
        _flush(con, row["device"])
        got, refreshed, used_keys = 0, False, False
        while True:
            messages = _api("GET", "inbox")["messages"]
            if not messages:
                break
            done = []
            for m in messages:
                known = _sender(con, m)
                if known is None and not refreshed:
                    _remember(con, _api("GET", "friends")["friends"])    # 새 친구·기기 — 한 번만 새로 받는다
                    refreshed = True
                    known = _sender(con, m)
                used_keys |= m["kind"] == 0
                fresh = _receive(con, me, known, m)
                got += fresh
                done.append(m["id"])
            _api("POST", "ack", {"ids": done})        # 저장한 뒤에만. 잠겼으면 여기서 멈추고 다음에 다시 받는다
            _apply_shares(con)
            if len(messages) < 100:
                break
        if used_keys:
            _top_up(con, me, row["device"], int(_api("GET", "me")["one_time_keys"]))
        waiting = _apply_shares(con)   # 지난번에 적용 못 한 것도 — 받은 글이 없어도 다시 시도
        return {"received": got, **({"shares_waiting": waiting} if waiting else {})}


def _apply_shares(con) -> int:
    """적용 대기 중인 할 일 공유를 적용한다. 성공(또는 우리 글이 아님·친구 아님)만 지우고, 실패는 남겨 다음 받기 때 다시. 남은 수."""
    from vader.core import todo
    for r in con.execute("SELECT message, account FROM share_pending").fetchall():
        msg = con.execute("SELECT text FROM messages WHERE id=?", (r["message"],)).fetchone()
        p = con.execute("SELECT state FROM peers WHERE account=?", (r["account"],)).fetchone()
        try:
            if msg and msg["text"] is not None and p and p["state"] == "accepted":
                todo.receive_share(r["account"], _unseal(msg["text"]), r["message"])
        except Exception:   # noqa: BLE001 — 할 일 저장소 오류 등: 남겨 두고 다음에
            continue
        con.execute("DELETE FROM share_pending WHERE message=?", (r["message"],))
    return con.execute("SELECT count(*) FROM share_pending").fetchone()[0]


def _sender(con, m):
    """보낸 기기를 우리가 서명으로 확인해 둔 기기 중에서 찾는다 — 지금 기기가 아니어도 (codex1 3c P1-3)."""
    return con.execute("SELECT * FROM peer_devices WHERE device=? AND account=?",
                       (m["sender_device"], m["sender_account"])).fetchone()


def _receive(con, me, known, m) -> int:
    """하나를 풀어 저장한다. 1 이면 새 메시지. 이미 받은 것·못 푸는 것도 기록해 다시 오지 않게 한다.
    풀기(형식·암호)가 실패하면 '읽을 수 없음' 으로 남기고 넘어간다. 저장이 실패하면 예외 — ack 하지 않는다."""
    if con.execute("SELECT 1 FROM messages WHERE id=?", (m["id"],)).fetchone():
        return 0
    at = time.time()
    found = _decrypt(con, me, known, m) if known is not None else None
    with _tx(con):
        if found is None:      # 모르는 기기이거나 풀 수 없다 — 지우지 않고 '읽을 수 없음' 으로 남긴다
            con.execute("INSERT INTO messages VALUES (?,?,0,NULL,?,'unreadable',?,0)",
                        (m["id"], m["sender_account"], at, m["sender_device"]))
            return 0
        session, raw, account_changed = found
        # 암호는 풀렸으니 세션·계정 상태는 글자가 아니어도 저장한다 — 다음 메시지를 풀 수 있게 (codex1 3c NEW-P1-2)
        con.execute("INSERT OR REPLACE INTO sessions(sid, device, pickle, used) VALUES (?,?,?,?)",
                    (session.session_id, known["device"], session.pickle(_key()), at))
        if account_changed:
            con.execute("UPDATE me SET pickle=? WHERE id=1", (me.pickle(_key()),))
        try:
            plain = raw.decode()
        except UnicodeDecodeError:
            plain = None
        con.execute("INSERT INTO messages VALUES (?,?,0,?,?,?,?,?)",
                    (m["id"], known["account"], _seal(plain) if plain is not None else None, at,
                     "received" if plain is not None else "unreadable", known["device"], int(known["verified"])))
        from vader.core.todo import SHARE_MARK
        if plain is not None and known["verified"] and plain.startswith(SHARE_MARK):
            # 할 일 공유 글 — 메시지와 같은 트랜잭션에서 적용 대기로 (codex1 todo-share 회신 2: 저장과 대기 사이에 죽어도 안 잃는다)
            con.execute("INSERT OR IGNORE INTO share_pending(message, account) VALUES (?,?)", (m["id"], known["account"]))
    return int(plain is not None)


def _decrypt(con, me, known, m):
    """(세션, 푼 바이트, 계정이 바뀌었나) 또는 None. 형식·암호 오류는 None (codex1 3c P2-2). 글자 확인은 부르는 쪽."""
    vz = _vz()
    try:
        kind = int(m["kind"])
        message = vz.AnyOlmMessage.from_parts(kind, base64.b64decode(m["body"], validate=True))
        pre = message.to_pre_key() if kind == 0 else None
    except Exception:
        return None
    for row in con.execute("SELECT pickle FROM sessions WHERE device=? ORDER BY used DESC", (known["device"],)).fetchall():
        session = vz.Session.from_pickle(row["pickle"], _key())
        if pre is not None and not session.session_matches(pre):
            continue
        try:
            return session, session.decrypt(message), False
        except Exception:        # 이 세션의 것이 아니다 — 다음 세션
            continue
    if pre is None:
        return None
    try:
        session, plain = me.create_inbound_session(vz.Curve25519PublicKey.from_base64(known["curve"]), pre)
        return session, plain, True
    except Exception:
        return None


# ── 기록 ──

def history(account_id: str, limit: int = 200) -> list[dict]:
    with _one() as con:
        rows = con.execute("SELECT * FROM messages WHERE peer=? ORDER BY at DESC LIMIT ?",
                           (account_id, max(1, min(int(limit), 500)))).fetchall()
        return [_message(r) for r in reversed(rows)]


def _message(r) -> dict:
    """trusted: 받을 때 보낸 사람의 신원키를 안전 번호로 확인해 둔 상태였나. 내가 보낸 것은 늘 참."""
    return {"id": r["id"], "outgoing": bool(r["outgoing"]), "at": r["at"], "state": r["state"],
            "trusted": bool(r["trusted"]), "text": _unseal(r["text"]) if r["text"] is not None else None}
