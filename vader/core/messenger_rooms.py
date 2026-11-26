"""메신저 그룹 채팅방 — 앱 쪽 (2026-11-26 마스터 요청, codex1 방 설계 회신 1~3).

방 초대만으로 대화 허용: 방 멤버는 친구가 아니어도, 안전 번호를 확인하지 않아도 방 안에서 글·사진·할 일을 주고받는다.
확인 안 된 멤버의 글은 화면이 경고로 표시한다(받을 때의 확인 상태를 글마다 남긴다). 1:1 의 규칙은 그대로다.

보내기는 1:1 과 같은 약속: 받는 기기마다 Olm 으로 잠근 사본 전부·세션 상태·보낼 상자·내 기록을 한 거래로 저장한 **뒤에** 한 번에 올린다.
서버가 '멤버가 바뀜(409)' 이면 그 글은 실패 — 화면의 [다시 보내기] 가 지금 멤버로 다시 잠근다(새 번호).
평문 앞에 방 번호를 붙여 잠근다 — 받는 쪽은 서버가 알려 준 방과 같을 때만 그 방 글로 둔다.
방 이름은 이 PC 에만(서버에 없음). 기기를 옮기면 기본 이름(멤버 핸들)으로 돌아간다.
"""
from __future__ import annotations

import base64
import json
import time
import uuid

from vader.core import account, messenger as M, messenger_image

MARK = "⁣vader-room:"
NAME_MAX = 30
MEMBERS_MAX = 20


def wrap(room: str, text: str) -> str:
    return f"{MARK}{room}\n{text}"


def unwrap(room: str, plain: str | None) -> str | None:
    """서버가 준 방과 평문 안의 방이 같을 때만 본문. 아니면 None(읽을 수 없음)."""
    head = f"{MARK}{room}\n"
    return plain[len(head):] if plain is not None and plain.startswith(head) else None


def server_time(m: dict) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(m.get("created_at")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ── 목록 ──

def store(con, listed: list[dict]) -> None:
    """서버 방 목록을 맞춘다. 멤버 기기는 신원키 서명을 확인해 peer_devices 에 — **확인(verified) 은 올리지 않는다** (회신 1 P1-1)."""
    seen = set()
    now = time.time()
    with M._tx(con):
        for r in listed:
            rid = str(r["id"])
            seen.add(rid)
            if r["state"] == "invited":
                con.execute("""INSERT INTO rooms(id, state, invited_by, invited_handle, joined, updated) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET state=excluded.state, invited_by=excluded.invited_by,
                      invited_handle=excluded.invited_handle, joined=excluded.joined, updated=excluded.updated""",
                            (rid, "invited", r.get("invited_by"), r.get("invited_by_handle"), int(r.get("joined") or 0), now))
                con.execute("DELETE FROM room_members WHERE room=?", (rid,))
                continue
            con.execute("""INSERT INTO rooms(id, owner, generation, state, joined, updated) VALUES (?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, generation=excluded.generation, state=excluded.state,
                  joined=excluded.joined, updated=excluded.updated""",
                        (rid, r.get("owner"), int(r["generation"]), "joined",
                         sum(1 for x in r["members"] if x["state"] == "joined"), now))
            con.execute("DELETE FROM room_members WHERE room=?", (rid,))
            for x in r["members"]:
                peer, device, ed, curve = x["account_id"], x.get("device_id"), x.get("identity_ed"), x.get("identity_curve")
                if device and not M.signed(ed, M.device_message(peer, device, curve), x.get("identity_sig") or ""):
                    device = None                     # 신원키가 서명하지 않은 암호키는 받지 않는다
                if device:
                    con.execute("INSERT OR IGNORE INTO peer_devices(device, account, ed, curve) VALUES (?,?,?,?)",
                                (device, peer, ed, curve))
                features = ",".join(f for f in (x.get("features") or []) if isinstance(f, str)) if device else ""
                con.execute("INSERT INTO room_members(room, account, handle, state, read_seq, device, features) VALUES (?,?,?,?,?,?,?)",
                            (rid, peer, x.get("handle"), x["state"], x.get("read_seq"), device, features))
        for row in con.execute("SELECT id FROM rooms WHERE state IN ('joined','invited')").fetchall():
            if row["id"] not in seen:                 # 나왔거나 내보내졌거나 방이 닫혔다 — 기록은 남긴다
                con.execute("UPDATE rooms SET state='ended' WHERE id=?", (row["id"],))


def _default_name(con, rid: str, me: str) -> str:
    names = [r["handle"] or "멤버" for r in con.execute(
        "SELECT handle FROM room_members WHERE room=? AND account<>? AND state='joined' ORDER BY handle", (rid, me))]
    if not names:
        return "빈 방"
    return ", ".join("@" + n for n in names[:3]) + (f" 외 {len(names) - 3}명" if len(names) > 3 else "")


def _verified(con, account_id: str, device: str | None) -> bool:
    if not device:
        return False
    d = con.execute("SELECT verified FROM peer_devices WHERE device=? AND account=?", (device, account_id)).fetchone()
    p = con.execute("SELECT ed, verified_ed FROM peers WHERE account=? AND device=?", (account_id, device)).fetchone()
    return bool(d and d["verified"]) or bool(p and p["ed"] and p["verified_ed"] == p["ed"])


def _listing(con) -> list[dict]:
    me, _ = M._who()
    out = []
    for r in con.execute("""SELECT r.*, (SELECT seq FROM room_seen s WHERE s.room=r.id) AS seen,
            (SELECT max(room_seq) FROM messages m WHERE m.room=r.id AND m.outgoing=0) AS top,
            (SELECT max(at) FROM messages m WHERE m.room=r.id) AS last FROM rooms r
            WHERE r.state<>'ended' OR EXISTS (SELECT 1 FROM messages m WHERE m.room=r.id) ORDER BY last DESC, r.updated DESC""").fetchall():
        members = [{"account_id": x["account"], "handle": x["handle"], "state": x["state"], "me": x["account"] == me,
                    "verified": x["account"] == me or _verified(con, x["account"], x["device"]),
                    "ready": bool(x["device"]) and "room1" in (x["features"] or "").split(",")}
                   for x in con.execute("SELECT * FROM room_members WHERE room=? ORDER BY state DESC, handle", (r["id"],))]
        unread = con.execute("SELECT count(*) FROM messages WHERE room=? AND outgoing=0 AND state='received' AND room_seq>?",
                             (r["id"], r["seen"] or 0)).fetchone()[0]
        out.append({"id": r["id"], "name": M._unseal(r["name"]) if r["name"] else _default_name(con, r["id"], me),
                    "named": bool(r["name"]), "state": r["state"], "owner": r["owner"], "mine": r["owner"] == me,
                    "invited_by_handle": r["invited_handle"], "joined": r["joined"], "members": members,
                    "unread": unread, "last": r["last"]})
    return out


def rooms() -> list[dict]:
    data = M._api("GET", "rooms")
    with M._one() as con:
        store(con, data["rooms"])
        return _listing(con)


def _refresh(con) -> list[dict]:
    store(con, M._api("GET", "rooms")["rooms"])
    return _listing(con)


def _name(name) -> str:
    name = " ".join(str(name or "").split())
    if len(name) > NAME_MAX:
        raise M.MessengerError(f"방 이름은 {NAME_MAX}자까지예요.")
    return name


def create(account_ids: list[str], name: str = "") -> dict:
    """친구(들)을 초대해 방을 만든다. 이름은 이 PC 에만."""
    if not isinstance(account_ids, list) or not account_ids or len(account_ids) >= MEMBERS_MAX:
        raise M.MessengerError(f"초대할 친구를 1~{MEMBERS_MAX - 1}명 골라 주세요.")
    name = _name(name)
    got = M._api("POST", "room_create", {"accounts": [str(a) for a in account_ids]})
    with M._one() as con:
        with M._tx(con):
            con.execute("INSERT OR IGNORE INTO rooms(id, state, updated) VALUES (?, 'joined', ?)", (got["id"], time.time()))
            if name:
                con.execute("UPDATE rooms SET name=? WHERE id=?", (M._seal(name), got["id"]))
        return {"id": got["id"], "rooms": _refresh(con)}


def rename(room: str, name: str) -> list[dict]:
    name = _name(name)
    with M._one() as con:
        con.execute("UPDATE rooms SET name=? WHERE id=?", (M._seal(name) if name else None, str(room)))
        return _listing(con)


def _act(action: str, body: dict) -> list[dict]:
    M._api("POST", action, body)
    with M._one() as con:
        return _refresh(con)


def invite(room: str, account_ids: list[str]) -> list[dict]:
    return _act("room_invite", {"room": str(room), "accounts": [str(a) for a in account_ids]})


def accept(room: str) -> list[dict]:
    return _act("room_accept", {"room": str(room)})


def decline(room: str) -> list[dict]:
    return _act("room_decline", {"room": str(room)})


def leave(room: str) -> list[dict]:
    return _act("room_leave", {"room": str(room)})


def remove(room: str, account_id: str) -> list[dict]:
    return _act("room_remove", {"room": str(room), "account_id": str(account_id)})


# ── 보내기 ──

def _room(con, room: str):
    r = con.execute("SELECT * FROM rooms WHERE id=?", (str(room),)).fetchone()
    if r is None or r["state"] != "joined":
        raise M.NotInRoom("들어가 있는 방에만 보낼 수 있어요.")
    return r


def _targets(con, room: str, me: str) -> list[dict]:
    """서버가 사본을 요구할 기기 — 나 아닌 joined 멤버 중 방 글을 받을 수 있는 기기(서버가 차단 관계는 이미 뺐다)."""
    out = []
    for x in con.execute("SELECT * FROM room_members WHERE room=? AND state='joined' AND account<>? AND device IS NOT NULL",
                         (room, me)).fetchall():
        if "room1" not in (x["features"] or "").split(","):
            continue
        d = con.execute("SELECT * FROM peer_devices WHERE device=? AND account=?", (x["device"], x["account"])).fetchone()
        if d is not None:
            out.append({"account": x["account"], "device": x["device"], "ed": d["ed"], "curve": d["curve"]})
    return out


def _queue(con, me_account, row, me, room_row, text: str, attachment: str | None = None, image: tuple | None = None,
           replaces: str | None = None, expect: int | None = None, client_id: str | None = None) -> dict:
    """expect — 사람이 고를 때 본 방 세대(할 일 공유). 다르면 지금 멤버에게 저절로 보내지 않고 실패로 남긴다 (impl 회신 2 P1-1)."""
    room = room_row["id"]
    if client_id and con.execute("SELECT 1 FROM messages WHERE id=?", (client_id,)).fetchone():
        return _one_message(con, client_id)          # 같은 번호의 글이 이미 있다 — 다시 만들지 않는다 (impl 회신 5)
    seen = room_row["generation"] if expect is None else expect
    plain = wrap(room, text).encode()
    copies, sessions = [], ([] if room_row["generation"] == seen else None)
    for attempt in (1, 2) if sessions is not None else ():
        copies, sessions = [], []
        try:
            for t in _targets(con, room, me_account):
                session = M._latest(con, t["device"]) or M._open_session(me, t, room)
                kind, body = session.encrypt(plain).to_parts()
                copies.append({"recipient_device": t["device"], "kind": kind, "body": base64.b64encode(body).decode()})
                sessions.append((session, t["device"]))
            break
        except account.AccountError as exc:
            if attempt == 2 or exc.status not in (403, 404):
                raise
            store(con, M._api("GET", "rooms")["rooms"])     # 키를 못 받았다 — 방을 한 번 새로 받는다
            room_row = _room(con, room)
            if room_row["generation"] != seen:   # 멤버가 바뀌었다 — 지금 멤버로 저절로 보내지 않는다. [다시 보내기]를 기다린다 (impl 회신 P1-4)
                sessions = None
                break
    client_id, now = client_id or str(uuid.uuid4()), time.time()
    with M._tx(con):       # 세션 전부·사본 전부·보낼 상자·내 기록을 한 거래로 (회신 1 P1-2). 겹친 같은 번호는 기본 키에서 막힌다
        if replaces:       # 다시 보내기 — 옛 글은 새 글을 적는 같은 거래에서만 지운다 (impl 회신 P2-6)
            con.execute("DELETE FROM messages WHERE id=?", (replaces,))
        for session, device in sessions or []:
            con.execute("INSERT OR REPLACE INTO sessions(sid, device, pickle, used) VALUES (?,?,?,?)",
                        (session.session_id, device, session.pickle(M._key()), now))
        if sessions is not None:
            con.execute("INSERT INTO room_outbox(client_id, sender, room, generation, copies, attachment, created) VALUES (?,?,?,?,?,?,?)",
                        (client_id, row["device"], room, room_row["generation"], json.dumps(copies), attachment, now))
        con.execute("INSERT INTO messages(id, peer, outgoing, text, at, state, device, trusted, room) VALUES (?,?,1,?,?,?,?,1,?)",
                    (client_id, me_account, M._seal(text), now, "queued" if sessions is not None else "failed", row["device"], room))
        if image is not None:
            jpeg, w, h = image
            con.execute("INSERT INTO images(id, data, state, w, h) VALUES (?,?,'ok',?,?)",
                        (client_id, M._seal(base64.b64encode(jpeg).decode()), w, h))
    flush(con, row["device"])
    return _one_message(con, client_id)


def flush(con, my_device: str) -> None:
    """보낼 상자의 방 글을 순서대로. 멤버가 바뀌었으면(409) 그 글은 실패, 연결 문제면 다음에 같은 바이트로."""
    for o in con.execute("SELECT * FROM room_outbox ORDER BY created").fetchall():
        # 기한이 지났거나 다른 기기가 만든 글 — 서버가 받아들였는지 모른다. 거절(4xx)과 구분해 'unknown' (impl 회신 6)
        done = {"state": "unknown"}
        if o["sender"] == my_device and time.time() - o["created"] > M.RETRY_SECONDS:
            try:   # 다시 보내지 않고 결과만 — 원장이 남아 있으면 실제 받은 사람을 되찾는다 (impl 회신 7)
                got = M._api("POST", "room_result", {"client_id": o["client_id"]})
                done = {"state": "sent", "server_at": server_time({"created_at": got.get("at")}), "room_seq": got.get("seq"),
                        "recipients": json.dumps(got.get("recipients") or []), "missed": json.dumps(got.get("missed") or [])}
            except account.AccountError as exc:
                if exc.status is None or exc.status == 429 or exc.status >= 500:
                    return               # 연결·제한 — 다음에 다시 묻는다
        elif o["sender"] == my_device and time.time() - o["created"] <= M.RETRY_SECONDS:
            try:
                got = M._api("POST", "room_send", {"room": o["room"], "generation": o["generation"], "client_id": o["client_id"],
                                                   "copies": json.loads(o["copies"]),
                                                   **({"attachment_id": o["attachment"]} if o["attachment"] else {})})
                done = {"state": "sent", "server_at": server_time({"created_at": got.get("at")}), "room_seq": got.get("seq"),
                        "recipients": json.dumps(got.get("recipients") or []), "missed": json.dumps(got.get("missed") or [])}
            except account.AccountError as exc:
                if exc.status not in (400, 403, 404, 409):
                    return               # 연결·제한·다시 묶기 전 — 다음에 같은 바이트로
                done = {"state": "failed"}   # 서버가 분명히 거절했다(원장에 없음)
        with M._tx(con):
            con.execute("DELETE FROM room_outbox WHERE client_id=?", (o["client_id"],))
            sets = ", ".join(f"{k}=?" for k in done)
            con.execute(f"UPDATE messages SET {sets} WHERE id=?", (*done.values(), o["client_id"]))


def generation(room: str) -> int:
    """이 PC 가 알고 있는 방 세대 — 할 일을 방에 공유할 때 고른 순간의 멤버를 묶어 둔다."""
    with M._one() as con:
        return _room(con, room)["generation"]


def message_state(message_id: str) -> str | None:
    """내 방 글의 지금 상태(queued·sent·failed). 없으면 None."""
    with M._one() as con:
        r = con.execute("SELECT state FROM messages WHERE id=? AND outgoing=1 AND room IS NOT NULL", (str(message_id),)).fetchone()
        return r["state"] if r else None


def send(room: str, text: str, generation: int | None = None, client_id: str | None = None) -> dict:
    text = (text or "").strip()
    if not text or len(text) > M.MAX_TEXT:
        raise M.MessengerError(f"메시지는 1~{M.MAX_TEXT}자로 보내 주세요.")
    me_account, _ = M._who()
    with M._one() as con:
        row, me = M._me(con)
        if me is None or not row["registered"]:
            raise M.MessengerError("메신저를 먼저 여세요.")
        return _queue(con, me_account, row, me, _room(con, room), text, expect=generation, client_id=client_id)


def resend(message_id: str) -> dict:
    """실패한 내 방 글을 지금 멤버로 다시 잠가 새 번호로 보낸다(옛 글은 지운다). 사진 글은 사진을 다시 골라야 한다."""
    me_account, _ = M._who()
    with M._one() as con:
        old = con.execute("SELECT * FROM messages WHERE id=? AND outgoing=1 AND state='failed' AND room IS NOT NULL",
                          (str(message_id),)).fetchone()
        if old is None or old["text"] is None:
            raise M.MessengerError("다시 보낼 글이 아니에요.")
        text = M._unseal(old["text"])
        if messenger_image.parse(text) is not None:
            raise M.MessengerError("사진은 다시 골라서 보내 주세요.")
        row, me = M._me(con)
        _refresh(con)
        room_row = _room(con, old["room"])
        return _queue(con, me_account, row, me, room_row, text, replaces=old["id"])


def send_image(room: str, token: str) -> dict:
    image = M._prepared.get(token)
    if image is None:
        raise M.MessengerError("사진을 다시 골라 주세요.")
    me_account, _ = M._who()
    with M._one() as con:
        row, me = M._me(con)
        if me is None or not row["registered"]:
            raise M.MessengerError("메신저를 먼저 여세요.")
        room_row = _room(con, room)
        jpeg, w, h = image
        cipher, meta = messenger_image.seal(jpeg)
        upload = {"upload_id": str(uuid.uuid4()), "room": room_row["id"], "generation": room_row["generation"],
                  "data": base64.b64encode(cipher).decode()}
        try:
            attachment = M._api("POST", "room_attach", upload)["id"]
        except account.AccountError as exc:
            if exc.status is not None:
                raise
            attachment = M._api("POST", "room_attach", upload)["id"]   # 응답을 잃었다 — 같은 번호·같은 바이트로 한 번 더
        M._prepared.pop(token, None)
        return _queue(con, me_account, row, me, room_row, messenger_image.payload(attachment, meta, w, h), attachment, (jpeg, w, h))


# ── 기록·읽음 ──

def _one_message(con, mid: str) -> dict:
    return _decorate(con, con.execute(M._MESSAGE_ROW + " WHERE m.id=?", (mid,)).fetchone())


def _decorate(con, r) -> dict:
    """1:1 말풍선 + 방에서 쓰는 것: 보낸 사람, 안 읽은 사람 수(발송 당시 받은 사람 중 지금 들어와 있고 아직 안 읽은), 못 받은 사람 수."""
    out = M._message(r)
    if out.get("image") and out["image"]["state"] == "unchecked":
        out["image"]["state"] = "waiting"     # 방 사진은 확인 안 된 멤버의 것도 받는다 (마스터 결정 4)
    out["sender"] = r["peer"]
    handle = con.execute("SELECT handle FROM room_members WHERE room=? AND account=?", (r["room"], r["peer"])).fetchone()
    out["handle"] = handle["handle"] if handle else None
    if r["outgoing"] and r["room_seq"] is not None:
        got = json.loads(r["recipients"] or "[]")
        unread = con.execute(f"""SELECT count(*) FROM room_members WHERE room=? AND state='joined' AND read_seq<?
            AND account IN ({','.join('?' * len(got)) or "''"})""", (r["room"], r["room_seq"], *got)).fetchone()[0]
        out.update(unread=unread, missed=len(json.loads(r["missed"] or "[]")))
    return out


def history(room: str, limit: int = 200) -> list[dict]:
    with M._one() as con:
        rows = con.execute(M._MESSAGE_ROW + " WHERE m.room=? ORDER BY m.at DESC LIMIT ?",
                           (str(room), max(1, min(int(limit), 500)))).fetchall()
        rows = sorted(rows, key=lambda r: (r["room_seq"] is None, r["room_seq"] or 0, r["at"]))
        return [_decorate(con, r) for r in rows]


def mark_read(room: str) -> bool:
    """화면에 그린 받은 방 글까지 읽음 — 서버에도 알린다(다른 멤버의 '안 읽음' 수가 준다)."""
    with M._one() as con:
        top = con.execute("SELECT max(room_seq) FROM messages WHERE room=? AND outgoing=0", (str(room),)).fetchone()[0] or 0
        seen = con.execute("SELECT seq FROM room_seen WHERE room=?", (str(room),)).fetchone()
        if seen and seen["seq"] >= top:
            return False
        r = con.execute("SELECT state FROM rooms WHERE id=?", (str(room),)).fetchone()
        if r and r["state"] == "joined" and top:
            M._api("POST", "room_read", {"room": str(room), "seq": int(top)})
        con.execute("INSERT INTO room_seen(room, seq) VALUES (?,?) ON CONFLICT(room) DO UPDATE SET seq=max(seq, excluded.seq)",
                    (str(room), int(top)))
        return True
