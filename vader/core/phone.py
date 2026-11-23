"""폰에서 할 일 보기 — PC 쪽 (2026-11-11 마스터 결정: 폰에서 할 일 내용까지 보인다).

원본은 이 PC 의 할 일(`todo`)이다. 이 PC 가 연결한 폰마다 그 폰만 열 수 있게 잠그고 이 PC 가 서명해 웹 서버에 맡긴다.
- 연결: [폰 연결 시작] 이 1회용 코드(8자, 10분)를 보여 준다. 사람이 폰에 치면, 폰은 그 코드로 자기 키를 증명(proof)하고
  이 PC 는 그 증명이 맞는 폰만 승인하며 자기 서명 키를 같은 코드로 증명해 돌려준다. 코드는 서버로 가지 않는다.
  코드(40비트)는 PBKDF2(20만 번, 소금 = 그 폰의 잠금 공개키)로 늘려 쓴다. 이것은 비용으로 막는 것이다 — 증명값을 본 서버가
  코드를 맞히려면 연결마다 따로(미리 계산해 둘 수 없게) 후보 하나에 20만 번씩 계산해야 하지만, 시간 안에 못 한다고 보장하지는
  않는다 (codex1 round6). 승인 뒤엔 양쪽이 키를 붙잡으므로 나중에 코드를 알아내도 그 연결을 열거나 위조할 수 없다.
- 확인한 폰 키는 이 작업공간의 원장(todo DB 의 phones 표)에 붙잡는다. 동기화는 원장의 키만 쓴다 — 서버가 키를 바꿔 끼워도 따르지 않는다.
- 묶음: ECDH P-256(일회용 키) → HKDF-SHA256 → AES-256-GCM, 그 위에 이 PC 의 ECDSA 서명. 안에 순번(seq)이 있어 옛 묶음을 되돌려 보낼 수 없다.
- 폰 변경: 폰이 서명한 글(nonce·카드·판 번호·시각). 원장의 폰 서명 키로 확인하고, nonce 는 한 번만, 판 번호가 같을 때만 적용한다.
  결과(반영·충돌·거절)는 다음 묶음에 실어 폰에 알린다.
- 잠기면(문이 닫히면) 다음 단계에서 멈춘다 — 이미 보낸 요청은 거둘 수 없다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as pysecrets
import threading
import time
from datetime import datetime, timedelta

from vader.core import account, messenger, secrets, todo

API = "/api/phone/"
PAIR = "vader-phone-pair-v1"
INFO = b"vader-phone-todo-v1:"
SIGN_KEY = "phone.sign_key"
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
PAIR_SECONDS = 600
ITERATIONS = 200_000
BOX_MAX = 600_000
CARDS_MAX = 300
LEDGER = """CREATE TABLE IF NOT EXISTS phones(id TEXT PRIMARY KEY, label TEXT NOT NULL, ecdh_key TEXT NOT NULL,
  sign_key TEXT NOT NULL, seq INTEGER NOT NULL DEFAULT 0, sent TEXT);
CREATE TABLE IF NOT EXISTS phone_ops(nonce TEXT PRIMARY KEY, phone_id TEXT NOT NULL, result TEXT NOT NULL, at TEXT NOT NULL);"""
_pairing: dict | None = None        # {code, key, until} — 메모리에만. 앱을 끄면 다시 시작한다
_LOCK = threading.Lock()


class PhoneError(ValueError):
    pass


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _ledger():
    con = todo._db()
    con.executescript(LEDGER)
    return con


def _pair_key(code: str, ecdh_key: str) -> bytes:
    """소금에 그 폰의 공개키를 넣는다 — 연결마다 다르므로 후보 계산을 미리 해 두거나 돌려 쓸 수 없다."""
    return hashlib.pbkdf2_hmac("sha256", code.encode(), f"{PAIR}|{ecdh_key}".encode(), ITERATIONS, 32)


def _mac(key: bytes, text: str) -> str:
    return _b64u(hmac.new(key, text.encode(), hashlib.sha256).digest())


def _sign_key():
    """이 작업공간의 서명 키 (계정 금고). 처음이면 만든다."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    with _LOCK:
        stored = secrets.get(SIGN_KEY)
        if not stored:
            made = ec.generate_private_key(ec.SECP256R1())
            stored = base64.b64encode(made.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                         serialization.NoEncryption())).decode()
            secrets.put(SIGN_KEY, stored)
        return serialization.load_der_private_key(base64.b64decode(stored), None)


def _raw(public) -> str:
    from cryptography.hazmat.primitives import serialization
    return _b64u(public.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))


def _sign(text: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    r, s = decode_dss_signature(_sign_key().sign(text.encode(), ec.ECDSA(hashes.SHA256())))
    return _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))   # WebCrypto 모양(r‖s)


def _verify(public_key: str, signature: str, text: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    try:
        raw = _unb64u(signature)
        if len(raw) != 64:
            return False
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _unb64u(public_key))
        key.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                   text.encode(), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False


def seal(public_key: str, phone_id: str, payload: bytes) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _unb64u(public_key))
    mine = ec.generate_private_key(ec.SECP256R1())
    info = INFO + phone_id.encode()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(mine.exchange(ec.ECDH(), peer))
    iv = os.urandom(12)
    epk = mine.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    body = _b64u(epk + iv + AESGCM(key).encrypt(iv, payload, info))
    return body + "." + _sign(f"vader-phone-box-v1|{phone_id}|{body}")


def _call(method: str, action: str, body: dict | None = None) -> dict:
    return account.call(method, API + action + "/", json.dumps(body).encode() if body is not None else None)


def start() -> dict:
    """[폰 연결 시작] — 1회용 연결 코드. 이 화면에만 보이고 서버로 가지 않는다."""
    global _pairing
    code = "".join(pysecrets.choice(ALPHABET) for _ in range(8))
    _pairing = {"code": code, "keys": {}, "until": time.time() + PAIR_SECONDS}
    return phones()


def _claim(pending: list[dict]) -> None:
    """연결 코드로 증명한 대기 폰만 승인한다. 한 코드에 폰 하나."""
    global _pairing
    if not _pairing or time.time() > _pairing["until"]:
        _pairing = None
        return
    for p in pending:
        cache = _pairing["keys"]      # 창이 3초마다 다시 물어도 폰마다 한 번만 계산한다
        key = cache.get(p["ecdh_key"]) or cache.setdefault(p["ecdh_key"], _pair_key(_pairing["code"], p["ecdh_key"]))
        want = _mac(key,f"{PAIR}|phone|{p['ecdh_key']}|{p['sign_key']}")
        if not hmac.compare_digest(want, str(p.get("proof", ""))):
            continue
        messenger._live()
        pc_key = _raw(_sign_key().public_key())
        pc_proof = _mac(key, f"{PAIR}|pc|{p['id']}|{p['ecdh_key']}|{p['sign_key']}|{pc_key}")
        _call("POST", "approve", {"phone_id": p["id"], "pc_key": pc_key, "pc_proof": pc_proof})
        _ledger().execute("INSERT OR REPLACE INTO phones(id,label,ecdh_key,sign_key) VALUES (?,?,?,?)",
                          (p["id"], p["label"], p["ecdh_key"], p["sign_key"]))
        _pairing = None
        return


def phones() -> dict:
    """폰 연결 창: 이 PC 가 연결한 폰(원장), 연결 코드(있으면), 확인 안 된 요청 수."""
    got = _call("GET", "pc")
    pending = [p for p in got.get("phones", []) if p["state"] == "pending"]
    _claim(pending)
    mine = [{"id": r["id"], "label": r["label"]} for r in _ledger().execute("SELECT id, label FROM phones")]
    active = _pairing and time.time() <= _pairing["until"]
    claimed = {m["id"] for m in mine}
    return {"url": account.site() + "/ko/todo/", "phones": mine,
            "code": _pairing["code"] if active else None,
            "left": int(_pairing["until"] - time.time()) if active else 0,
            "waiting": sum(1 for p in pending if p["id"] not in claimed)}


def revoke(phone_id: str) -> dict:
    try:
        _call("POST", "revoke", {"phone_id": phone_id})
    except account.AccountError as exc:
        if getattr(exc, "status", None) != 404:       # 서버에 이미 없으면 원장에서만 뺀다
            raise
    con = _ledger()
    con.execute("DELETE FROM phones WHERE id=?", (phone_id,))
    con.execute("DELETE FROM phone_ops WHERE phone_id=?", (phone_id,))
    return phones()


def _apply(phone: dict, op: dict) -> str:
    """서명을 확인한 폰 변경 하나. 결과: applied | conflict | rejected."""
    body = str(op.get("body") or "")
    if not _verify(phone["sign_key"], str(op.get("sig") or ""), f"vader-phone-op-v1|{body}"):
        return "rejected"
    try:
        o = json.loads(body)
    except ValueError:
        return "rejected"
    if not isinstance(o, dict) or o.get("phone") != phone["id"] or not isinstance(o.get("nonce"), str):
        return "rejected"
    con = _ledger()
    con.execute("BEGIN IMMEDIATE")   # 카드 변경과 결과 기록을 함께 — 사이에 꺼져도 '반영됐는데 충돌' 이 되지 않게 (codex1 P2-C)
    try:
        if con.execute("SELECT 1 FROM phone_ops WHERE nonce=?", (o["nonce"][:40],)).fetchone():
            con.execute("ROLLBACK")
            return ""                # 이미 처리한 것 — 다시 하지 않는다. 결과는 기록된 그대로 간다
        try:
            if o.get("op") == "done":
                todo.done(str(o.get("card")), rev=str(o.get("rev")), con=con)
                result = "applied"
            elif o.get("op") == "snooze" and isinstance(o.get("at"), int):
                until = datetime.fromtimestamp(o["at"]).replace(second=0, microsecond=0)   # 순간 → 이 PC 의 현지 시각
                todo.snooze(str(o.get("card")), until.isoformat(), rev=str(o.get("rev")), con=con)
                result = "applied"
            else:
                result = "rejected"
        except todo.TodoError as exc:
            result = "conflict" if "바뀌어" in str(exc) else "rejected"
        con.execute("INSERT INTO phone_ops(nonce, phone_id, result, at) VALUES (?,?,?,?)",
                    (o["nonce"][:40], phone["id"], result, datetime.now().isoformat(timespec="seconds")))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return result


def sync() -> int:
    """이 PC 가 연결한 폰의 변경을 받아 적용하고, 바뀐 할 일을 잠가 맡긴다. 반영한 변경 수를 돌려준다."""
    if not account.signed_in():
        return 0
    con = _ledger()
    ledger = {r["id"]: dict(r) for r in con.execute("SELECT * FROM phones")}
    if not ledger:
        return 0                     # 연결한 폰이 없으면 서버에 묻지도 않는다
    got = _call("GET", "pc")
    live = {p["id"] for p in got.get("phones", []) if p["state"] == "approved"}
    applied, done_ids = 0, []
    for op in got.get("ops", []):
        messenger._live()
        phone = ledger.get(op.get("phone_id"))
        if phone and _apply(phone, op) == "applied":
            applied += 1
        done_ids.append(op["id"])
    if done_ids:
        _call("POST", "ack", {"ids": done_ids})
    every = todo.cards()
    opened = [c for c in every if c["state"] == "open"]
    closed = [c for c in every if c["state"] == "done"]
    cards = opened[:CARDS_MAX] + closed[:50]
    totals = {"open": len(opened), "done": len(closed)}    # 잘랐으면 폰이 '몇 개 중 몇 개' 를 말한다 (codex1 P2-B)
    week = (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    for pid, phone in ledger.items():
        if pid not in live:          # 서버에서 끊겼다 — 다시 연결해야 한다
            con.execute("DELETE FROM phones WHERE id=?", (pid,))
            continue
        # 결과는 7일 동안 다 싣는다 — 한 번에 많이 처리해도 폰의 대기 표시가 남지 않게 (codex1 P2-A)
        con.execute("DELETE FROM phone_ops WHERE phone_id=? AND at < ?", (pid, week))
        results = {r["nonce"]: r["result"] for r in con.execute(
            "SELECT nonce, result FROM phone_ops WHERE phone_id=?", (pid,))}   # 7일 치 전부 — 묶음 크기 검사가 상한
        digest = hashlib.sha256(json.dumps([cards, results, totals], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if phone["sent"] == digest:
            continue
        messenger._live()
        # 순번은 보내기 전에 예약한다 — 보내다 실패하거나 답을 못 받아도 그 순번은 다시 쓰지 않는다 (codex1 P1-A)
        seq = con.execute("UPDATE phones SET seq=seq+1 WHERE id=? RETURNING seq", (pid,)).fetchone()[0]
        body = json.dumps({"v": 1, "phone": pid, "seq": seq, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                           "cards": cards, "totals": totals, "results": results}, ensure_ascii=False).encode()
        box = seal(phone["ecdh_key"], pid, body)
        if len(box) > BOX_MAX:
            raise PhoneError("할 일이 너무 많아 폰으로 보내지 못했어요.")
        _call("POST", "put", {"phone_id": pid, "box": box})
        con.execute("UPDATE phones SET sent=? WHERE id=?", (digest, pid))
    return applied
