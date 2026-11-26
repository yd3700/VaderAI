"""그룹 채팅방 앱 쪽 (2026-11-26) — 세 PC 를 각자 작업공간에 두고 가짜 서버로. 서버 계약은 web/tests/messenger-rooms.spec.ts 가 본다.

방 초대만으로 대화 허용: 친구가 아닌 멤버끼리도 방 안에서 주고받고, 확인 안 된 멤버의 글은 trusted=False 로 남는다.
"""
from __future__ import annotations

import base64
import json
import uuid

import pytest

pytest.importorskip("vodozemac")

from vader.core import account, config, messenger, messenger_rooms  # noqa: E402

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_messenger import Server, refuse  # noqa: E402


class RoomServer(Server):
    """가짜 서버에 방 — 실제 서버처럼 사본 집합이 joined 멤버의 room1 기기와 정확히 같아야 받는다."""

    def __init__(self):
        super().__init__()
        self.rooms = {}          # id → {owner, generation, seq, members: {acct: {state, read}}}
        self.ledger = {}         # (device, client_id) → 응답

    def _live_device(self, acct):
        d = self.accounts[acct].get("device")
        return d if d and "room1" in self.devices[d].get("features", []) else None

    def get_rooms(self, who, b):
        out = []
        for rid, r in self.rooms.items():
            mine = r["members"].get(who["account"])
            if not mine or mine["state"] not in ("joined", "invited"):
                continue
            if mine["state"] == "invited":
                out.append({"id": rid, "state": "invited", "invited_by": r["owner"], "invited_by_handle": self.accounts[r["owner"]]["handle"],
                            "joined": sum(1 for m in r["members"].values() if m["state"] == "joined")})
                continue
            members = []
            for acct, m in r["members"].items():
                dev = self.accounts[acct].get("device") if m["state"] == "joined" else None
                d = self.devices.get(dev) if dev else None
                members.append({"account_id": acct, "handle": self.accounts[acct]["handle"], "state": m["state"], "read_seq": m["read"],
                                "device_id": dev, "identity_ed": d and d["ed"], "identity_curve": d and d["curve"],
                                "identity_sig": d and d["sig"], "features": d and d.get("features", [])})
            out.append({"id": rid, "state": "joined", "owner": r["owner"], "generation": r["generation"], "seq": r["seq"], "members": members})
        return {"rooms": out}

    def post_room_create(self, who, b):
        rid = str(uuid.uuid4())
        self.rooms[rid] = {"owner": who["account"], "generation": 1, "seq": 0,
                           "members": {who["account"]: {"state": "joined", "read": 0},
                                       **{a: {"state": "invited", "read": 0} for a in b["accounts"]}}}
        return {"id": rid}

    def post_room_accept(self, who, b):
        r = self.rooms[b["room"]]
        r["members"][who["account"]] = {"state": "joined", "read": r["seq"]}
        r["generation"] += 1
        return {"joined": True}

    def post_room_leave(self, who, b):
        r = self.rooms[b["room"]]
        r["members"].pop(who["account"])
        r["generation"] += 1
        return {"left": True}

    def post_room_claim(self, who, b):
        self._bound(who)
        r = self.rooms[b["room"]]
        if r["members"].get(b["account_id"], {}).get("state") != "joined":
            refuse(403, "이 방 멤버가 아니에요.")
        d = self.devices[b["device_id"]]
        free = sorted(k for k, v in d["keys"].items() if not v[2])
        key, sig, _ = d["keys"][free[0]]
        d["keys"][free[0]] = (key, sig, True)
        return {"device_id": b["device_id"], "identity_ed": d["ed"], "identity_curve": d["curve"], "identity_sig": d["sig"],
                "key": {"id": free[0], "key": key, "signature": sig, "fallback": False}}

    def post_room_send(self, who, b):
        dev = self._bound(who)
        if (dev, b["client_id"]) in self.ledger:
            return {**self.ledger[(dev, b["client_id"])], "duplicate": True}
        r = self.rooms[b["room"]]
        if r["generation"] != b["generation"]:
            refuse(409, "방 멤버가 바뀌었어요.")
        others = [a for a, m in r["members"].items() if m["state"] == "joined" and a != who["account"]]
        want = {self._live_device(a): a for a in others if self._live_device(a)}
        if sorted(c["recipient_device"] for c in b["copies"]) != sorted(want):
            refuse(409, "방 멤버가 바뀌었어요.")
        r["seq"] += 1
        mid = str(uuid.uuid4())
        if b.get("attachment_id"):
            self.attachments[b["attachment_id"]]["tied"] = True
        for c in b["copies"]:
            self.inbox.append({"id": str(uuid.uuid4()), "sender_account": who["account"], "sender_device": dev,
                               "recipient_device": c["recipient_device"], "kind": c["kind"], "body": c["body"], "generation": b["generation"],
                               "room": b["room"], "room_message": mid, "room_seq": r["seq"], "created_at": "2026-11-26T10:00:00Z"})
        got = {"id": mid, "seq": r["seq"], "at": "2026-11-26T10:00:00Z", "recipients": sorted(want.values()),
               "missed": [a for a in others if a not in want.values()]}
        self.ledger[(dev, b["client_id"])] = got
        return {**got, "duplicate": False}

    def post_room_result(self, who, b):
        got = self.ledger.get((self._bound(who), b["client_id"]))
        if got is None:
            refuse(404, "찾을 수 없습니다.")
        return got

    def post_room_read(self, who, b):
        m = self.rooms[b["room"]]["members"][who["account"]]
        m["read"] = max(m["read"], int(b["seq"]))
        return {"ok": True}

    def post_room_attach(self, who, b):
        aid = str(uuid.uuid4())
        self.attachments[aid] = {"recipient": None, "room": b["room"], "data": b["data"], "tied": False, "got": set()}
        return {"id": aid}

    def post_room_attachment(self, who, b):
        a = self.attachments.get(b["id"])
        if not a or not a["tied"] or a.get("room") is None:
            refuse(404, "찾을 수 없습니다.")
        return {"data": a["data"]}

    def post_room_attachment_ack(self, who, b):
        for i in b["ids"]:
            self.attachments.get(i, {}).get("got", set()).add(who["account"])
        return {"ok": True}


@pytest.fixture
def three(tmp_path, monkeypatch):
    messenger.set_alive(None)
    server = RoomServer()
    people, state = {}, {"now": None}

    def token(p):
        claim = base64.urlsafe_b64encode(json.dumps({"a": p["account"], "d": p["session"]}).encode()).decode()
        return claim.rstrip("=") + ".sig"

    def be(name):
        state["now"] = people[name]
        monkeypatch.setattr(config, "_workspace", people[name]["folder"])
        return people[name]

    for name, handle in (("gana", "ganamed"), ("dara", "darasangsa"), ("mara", "marabio")):
        folder = tmp_path / name
        folder.mkdir()
        acct = str(uuid.uuid4())
        people[name] = {"account": acct, "session": str(uuid.uuid4()), "folder": folder}
        server.accounts[acct] = {"handle": handle}
    monkeypatch.setattr(account, "access_token", lambda: token(state["now"]))
    monkeypatch.setattr(account, "call", lambda method, path, body=None, content_type=None, limit=None:
                        server.call(state["now"], method, path, body))
    for name in people:
        be(name)
        assert messenger.connect()["state"] == "ready"
    # gana 는 둘과 친구 (방장은 친구만 초대), dara·mara 는 서로 친구가 아니다
    for other in ("dara", "mara"):
        be("gana")
        messenger.friend(people[other]["account"], "request")
        be(other)
        messenger.friend(people["gana"]["account"], "accept")
    be("gana")
    room = messenger_rooms.create([people["dara"]["account"], people["mara"]["account"]], "구매팀")["id"]
    for other in ("dara", "mara"):
        be(other)
        [invited] = messenger_rooms.rooms()
        assert invited["state"] == "invited" and invited["invited_by_handle"] == "ganamed"
        messenger_rooms.accept(room)
    yield server, people, be, room
    messenger.set_alive(None)


def test_non_friends_talk_in_a_room_and_unchecked_senders_are_marked(three):
    server, people, be, room = three
    be("dara")
    messenger_rooms.rooms()
    sent = messenger_rooms.send(room, "마라님 안녕하세요")             # dara·mara 는 친구가 아니다 — 방 안에서만
    assert sent["state"] == "sent"
    assert sorted(json.loads(server.ledger[next(iter(server.ledger))]["recipients"].__repr__().replace("'", '"'))) == \
        sorted([people["gana"]["account"], people["mara"]["account"]])
    be("mara")
    messenger.sync()
    [got] = messenger_rooms.history(room)
    assert got["text"] == "마라님 안녕하세요" and got["handle"] == "darasangsa" and not got["trusted"]   # 확인 안 된 멤버
    assert messenger.history(people["dara"]["account"]) == []          # 1:1 기록에 섞이지 않는다
    [r] = messenger_rooms.rooms()
    assert r["unread"] == 1 and r["name"] == "@darasangsa, @ganamed"   # 이름은 만든 PC 에만 — 여기서는 기본 이름
    assert messenger_rooms.mark_read(room)
    assert messenger_rooms.rooms()[0]["unread"] == 0
    be("dara")
    messenger_rooms.rooms()
    [mine] = messenger_rooms.history(room)
    assert mine["unread"] == 1                                          # gana 가 아직 안 읽었다
    be("gana")
    assert messenger_rooms.rooms()[0]["name"] == "구매팀"


def test_a_message_for_another_room_is_not_shown_there(three):
    """서버가 사본을 다른 방으로 옮겨도 평문 안의 방 번호가 달라 그 방 글이 되지 않는다."""
    server, people, be, room = three
    be("dara")
    messenger_rooms.rooms()
    messenger_rooms.send(room, "비밀")
    for m in server.inbox:
        m["room"] = str(uuid.uuid4())
    be("mara")
    messenger.sync()
    assert messenger_rooms.history(room) == []


def test_a_member_change_fails_the_message_and_resend_uses_the_new_members(three):
    server, people, be, room = three
    be("dara")
    messenger_rooms.rooms()
    be("mara")
    messenger_rooms.leave(room)
    be("dara")
    sent = messenger_rooms.send(room, "새 멤버로")                     # 아직 mara 가 있는 줄 알았다 — 키 받기가 막혀 새로 받으니 멤버가 바뀌었다
    assert sent["state"] == "failed" and not server.ledger  # 저절로 지금 멤버에게 보내지 않는다 (impl 회신 P1-4)
    again = messenger_rooms.resend(sent["id"])                          # 사람이 [다시 보내기]를 눌러야 지금 멤버로
    assert again["state"] == "sent" and [m["text"] for m in messenger_rooms.history(room)] == ["새 멤버로"]
    be("gana")
    messenger_rooms.rooms()
    server.rooms[room]["generation"] += 1                               # 잠근 뒤 올리기 전에 멤버가 바뀌었다
    failed = messenger_rooms.send(room, "다시")
    assert failed["state"] == "failed"
    messenger_rooms.rooms()
    again = messenger_rooms.resend(failed["id"])
    assert again["state"] == "sent" and [m["text"] for m in messenger_rooms.history(room)] == ["다시"]


def test_a_resend_that_cannot_reach_the_server_keeps_the_failed_message(three, monkeypatch):
    server, people, be, room = three
    be("gana")
    messenger_rooms.rooms()
    server.rooms[room]["generation"] += 1
    failed = messenger_rooms.send(room, "남아야 한다")
    messenger_rooms.rooms()
    monkeypatch.setattr(messenger, "_open_session",
                        lambda *a, **k: (_ for _ in ()).throw(account.AccountError("끊김", None)))
    with messenger._one() as con:
        con.execute("DELETE FROM sessions")
    with pytest.raises(account.AccountError):
        messenger_rooms.resend(failed["id"])
    assert [(m["text"], m["state"]) for m in messenger_rooms.history(room)] == [("남아야 한다", "failed")]   # 옛 글이 사라지지 않는다 (impl 회신 P2-6)


def test_photos_in_a_room_reach_every_member(three, monkeypatch):
    from vader.core import messenger_image
    server, people, be, room = three
    monkeypatch.setattr(messenger_image, "clean", lambda raw, received=False: (b"\xff\xd8jpeg", 4, 3))
    be("gana")
    messenger_rooms.rooms()
    token = messenger.prepare_image(base64.b64encode(b"raw").decode())["token"]
    messenger_rooms.send_image(room, token)
    for name in ("dara", "mara"):
        be(name)
        messenger.sync()
        [m] = messenger_rooms.history(room)
        assert m["image"]["state"] == "ok"
    [att] = [a for a in server.attachments.values() if a.get("room")]
    assert att["got"] == {people["dara"]["account"], people["mara"]["account"]}


def test_a_todo_shared_to_a_room_is_judged_by_its_owner(three):
    from vader.core import todo
    server, people, be, room = three
    be("gana")
    messenger_rooms.rooms()
    card = todo.add("단가표 정리")
    todo.share_room(card["id"], room)
    for name in ("dara", "mara"):
        be(name)
        messenger_rooms.rooms()
        messenger.sync()
        [got] = todo.cards()
        assert got["title"] == "단가표 정리" and got["owner"] == people["gana"]["account"] and got["share_room"] == room
    # dara 가 끝냄 → 요청만 방에. 주인 gana 의 PC 가 판정해 상태를 알린다 → mara 도 끝남
    be("dara")
    todo.done(todo.cards()[0]["id"])
    be("gana")
    messenger.sync()
    assert todo.cards()[0]["state"] == "done"
    todo.flush_shares()
    be("mara")
    messenger.sync()
    assert todo.cards()[0]["state"] == "done"
    # 주인이 거두면 받은 카드가 사라지고, 늦게 온 같은 공유는 다시 생기지 않는다
    be("gana")
    todo.done(card["id"], undo=True)
    todo.unshare_room(card["id"])
    for name in ("dara", "mara"):
        be(name)
        messenger.sync()
        assert todo.cards() == []


def test_a_done_request_from_someone_who_never_got_the_share_is_ignored(three):
    from vader.core import todo
    server, people, be, room = three
    be("gana")
    messenger_rooms.rooms()
    card = todo.add("비공개 아님")
    todo.share_room(card["id"], room)
    # mara 는 그 공유 글을 받지 못했다고 치자 — 주인 PC 의 받은 사람 기록에서 뺀다
    import sqlite3
    con = sqlite3.connect(people["gana"]["folder"] / "messenger.sqlite3")
    con.execute("UPDATE messages SET recipients=? WHERE outgoing=1", (json.dumps([people["dara"]["account"]]),))
    con.commit()
    con.close()
    be("mara")
    messenger_rooms.rooms()
    messenger.sync()
    sid = todo.cards()[0]["id"]
    todo.done(sid)                                                 # 요청은 가지만
    be("gana")
    messenger.sync()
    assert todo.cards()[0]["state"] == "open"                      # 주인은 받아들이지 않는다


def test_room_todo_requests_wait_for_the_owner_and_others_cannot_revoke_or_auto_reach_new_members(three):
    """impl 회신 P1-2·P1-4·P2-5."""
    from vader.core import todo
    server, people, be, room = three
    be("gana")
    messenger_rooms.rooms()
    card = todo.add("요청 대기")
    todo.share_room(card["id"], room)
    sid = todo._db().execute("SELECT share_id FROM cards WHERE id=?", (card["id"],)).fetchone()[0]
    be("dara")
    messenger_rooms.rooms()
    # 다른 멤버(mara)가 보낸 철회가 먼저 와도 주인(gana)의 공유는 막히지 않는다
    todo.receive_room_share(room, people["mara"]["account"], todo.SHARE_MARK + json.dumps({"op": "unshare", "id": sid}))
    messenger.sync()
    [got] = todo.cards()
    assert got["owner"] == people["gana"]["account"]
    # 끝냄은 요청만 — 주인의 상태가 오기 전엔 끝나지 않고 '기다리는 중'
    todo.done(got["id"])
    [got] = todo.cards()
    assert got["state"] == "open" and got["share_req"] == "done"
    be("gana")
    messenger.sync()
    todo.flush_shares()
    be("dara")
    messenger.sync()
    [got] = todo.cards()
    assert got["state"] == "done" and got["share_req"] is None
    # 처음 공유가 멤버 변경으로 실패하면 지금 멤버에게 저절로 가지 않고 공유가 풀린다
    be("gana")
    other = todo.add("새 공유")
    server.rooms[room]["generation"] += 1
    todo.share_room(other["id"], room)
    row = todo._db().execute("SELECT share_room, share_id FROM cards WHERE id=?", (other["id"],)).fetchone()
    assert tuple(row) == (None, None)
    assert todo._db().execute("SELECT count(*) FROM share_out").fetchone()[0] == 0


def test_a_first_room_share_cut_off_by_the_network_never_reaches_later_members(three, monkeypatch):
    """impl 회신 2 P1-1·P1-2 — 끊긴 시점이 키 받기 중이든 보낸 뒤든, 멤버가 바뀌면 공유를 푼다(앱을 다시 켜도)."""
    from vader.core import todo
    server, people, be, room = three
    be("gana")
    messenger_rooms.rooms()
    with messenger._one() as con:
        con.execute("DELETE FROM sessions")
    cut = lambda *a, **k: (_ for _ in ()).throw(account.AccountError("끊김"))   # noqa: E731
    # 1) 키 받기 중 끊김 → 보낼 통에 남는다 → 그사이 멤버가 바뀌었다 → 다음 flush 에서 지금 멤버에게 가지 않는다
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(messenger, "_open_session", cut)
        one = todo.add("키 받기 중 끊김")
        todo.share_room(one["id"], room)
    server.rooms[room]["generation"] += 1
    messenger_rooms.rooms()
    todo.flush_shares()
    assert todo._db().execute("SELECT share_room FROM cards WHERE id=?", (one["id"],)).fetchone()[0] is None
    assert not server.ledger
    # 2) 사본을 만든 뒤 보내기에서 끊김 → 메신저 상자에 queued → 멤버가 바뀌어 failed → 다시 켠 뒤 flush 가 공유를 푼다
    real = messenger._api
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(messenger, "_api", lambda m, path, *a, **k: cut() if path == "room_send" else real(m, path, *a, **k))
        two = todo.add("보낸 뒤 끊김")
        todo.share_room(two["id"], room)
        assert todo._db().execute("SELECT share_room FROM cards WHERE id=?", (two["id"],)).fetchone()[0] == room
    server.rooms[room]["generation"] += 1
    messenger_rooms.rooms()
    messenger.sync()
    todo.flush_shares()
    assert todo._db().execute("SELECT share_room FROM cards WHERE id=?", (two["id"],)).fetchone()[0] is None
    assert not server.ledger


def test_a_waiting_first_share_is_dropped_when_cancelled_or_the_room_is_left(three):
    """impl 회신 3 — 키 받기 중 끊긴 뒤 취소하거나 방을 나가면 그 처음 공유는 아무에게도 가지 않고 카드는 다시 공유할 수 있다."""
    from vader.core import todo
    server, people, be, room = three
    be("gana")
    messenger_rooms.rooms()
    with messenger._one() as con:
        con.execute("DELETE FROM sessions")
    cut = lambda *a, **k: (_ for _ in ()).throw(account.AccountError("끊김"))   # noqa: E731
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(messenger, "_open_session", cut)
        one = todo.add("취소한 공유")
        todo.share_room(one["id"], room)
        todo.unshare_room(one["id"])
        two = todo.add("나간 방")
        todo.share_room(two["id"], room)
    assert todo._db().execute("SELECT count(*) FROM share_out").fetchone()[0] == 2   # 취소한 것은 철회만 (번호를 이미 매겨 보내졌을 수도 있다 — 무해)
    messenger_rooms.leave(room)
    todo.flush_shares()
    todo.flush_shares()
    db = todo._db()
    assert db.execute("SELECT count(*) FROM share_out").fetchone()[0] == 0
    assert db.execute("SELECT share_room FROM cards WHERE id=?", (two["id"],)).fetchone()[0] is None
    assert not server.ledger


def test_a_cancel_that_lands_while_the_first_share_is_being_sent_still_revokes_it(three):
    """impl 회신 4 — 처음 공유가 서버에 닿은 직후, 기록하기 전에 취소가 끼어도 받은 사람의 카드가 사라진다."""
    from vader.core import todo
    server, people, be, room = three
    be("gana")
    messenger_rooms.rooms()
    with messenger._one() as con:
        con.execute("DELETE FROM sessions")
    cut = lambda *a, **k: (_ for _ in ()).throw(account.AccountError("끊김"))   # noqa: E731
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(messenger, "_open_session", cut)
        card = todo.add("겹친 취소")
        todo.share_room(card["id"], room)                  # 키 받기 중 끊겨 대기
    real_flush = todo.flush_shares

    def send_then_cancel(room_id, text, generation=None, client_id=None):
        got = messenger_rooms.send(room_id, text, generation=generation, client_id=client_id)   # 서버가 받아들였다
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(todo, "flush_shares", lambda *a, **k: 0)            # 다른 스레드의 취소 — 그 순간엔 보내지 않는다
            todo.unshare_room(card["id"])
        return got
    real_flush(room_send=send_then_cancel)
    todo.flush_shares()                                   # 예약된 철회
    assert len(server.ledger) == 2
    be("dara")
    messenger_rooms.rooms()
    messenger.sync()
    assert todo.cards() == []


def test_a_first_share_resumed_after_a_crash_or_by_an_overlapping_flush_is_sent_once(three):
    """impl 회신 5 — 서버가 받아들인 직후 멈춰도, flush 가 겹쳐도 같은 번호의 글 하나만. 멤버가 바뀌어도 이미 받은 사람의 공유는 유지."""
    from vader.core import todo
    server, people, be, room = three
    be("gana")
    messenger_rooms.rooms()
    with messenger._one() as con:
        con.execute("DELETE FROM sessions")
    cut = lambda *a, **k: (_ for _ in ()).throw(account.AccountError("끊김"))   # noqa: E731
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(messenger, "_open_session", cut)
        card = todo.add("한 번만")
        todo.share_room(card["id"], room)
    real_flush = todo.flush_shares

    def send_overlap_then_crash(room_id, text, generation=None, client_id=None):
        got = messenger_rooms.send(room_id, text, generation=generation, client_id=client_id)
        real_flush()                                     # 다른 스레드의 flush 가 겹친다
        raise RuntimeError("기록 전에 멈춤")              # 대기 행을 지우기 전에 앱이 멈췄다
    real_flush(room_send=send_overlap_then_crash)
    assert len(server.ledger) == 1
    be("mara")
    messenger_rooms.leave(room)                          # 그사이 멤버가 바뀌었다
    be("gana")
    messenger_rooms.rooms()
    todo.flush_shares()
    todo.flush_shares()
    assert len(server.ledger) == 1                       # 다시 보내지 않는다
    db = todo._db()
    assert db.execute("SELECT share_room FROM cards WHERE id=?", (card["id"],)).fetchone()[0] == room   # 주인 쪽도 공유 유지
    assert db.execute("SELECT count(*) FROM share_out").fetchone()[0] == 0
    be("dara")
    messenger_rooms.rooms()
    messenger.sync()
    assert [c["title"] for c in todo.cards()] == ["한 번만"]


def test_a_first_share_whose_answer_was_lost_past_the_retry_window_stays_revocable(three):
    """impl 회신 6 — 서버는 받았는데 답을 잃고 재시도 기한이 지나면 '모름'. 공유를 풀지 않아 주인이 철회하면 받은 사람 카드도 사라진다."""
    from vader.core import todo
    server, people, be, room = three
    be("gana")
    messenger_rooms.rooms()
    real = messenger._api

    def lose_answer(m, path, *a, **k):
        got = real(m, path, *a, **k)
        if path == "room_send":
            raise account.AccountError("답을 잃음")
        return got
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(messenger, "_api", lose_answer)
        card = todo.add("답을 잃은 공유")
        todo.share_room(card["id"], room)
    assert len(server.ledger) == 1
    with messenger._one() as con:
        con.execute("UPDATE room_outbox SET created=created-?", (messenger.RETRY_SECONDS + 1,))
    messenger.sync()
    todo.flush_shares()
    assert todo._db().execute("SELECT share_room FROM cards WHERE id=?", (card["id"],)).fetchone()[0] == room
    todo.unshare_room(card["id"])
    be("dara")
    messenger_rooms.rooms()
    messenger.sync()
    assert todo.cards() == []


def test_past_the_retry_window_the_ledger_restores_real_recipients_so_their_done_requests_count(three):
    """impl 회신 7 — 답을 잃은 처음 공유가 기한을 넘겨도, 원장이 남아 있으면 결과만 물어 받은 사람을 되찾는다. 원장이 없으면 '모름'."""
    from vader.core import todo
    server, people, be, room = three
    be("gana")
    messenger_rooms.rooms()
    real = messenger._api

    def lose_answer(m, path, *a, **k):
        got = real(m, path, *a, **k)
        if path == "room_send":
            raise account.AccountError("답을 잃음")
        return got
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(messenger, "_api", lose_answer)
        card = todo.add("원장으로 되찾기")
        todo.share_room(card["id"], room)
    with messenger._one() as con:
        con.execute("UPDATE room_outbox SET created=created-?", (messenger.RETRY_SECONDS + 1,))
    messenger.sync()
    todo.flush_shares()
    assert len(server.ledger) == 1                                     # 결과만 물었다 — 다시 보내지 않았다
    be("dara")
    messenger_rooms.rooms()
    messenger.sync()
    todo.done(todo.cards()[0]["id"])                                   # 실제 받은 사람의 끝냄 요청
    be("gana")
    messenger.sync()
    assert todo.cards()[0]["state"] == "done"
    todo.flush_shares()
    be("dara")
    messenger.sync()
    [got] = todo.cards()
    assert got["state"] == "done" and got["share_req"] is None
    # 원장이 없으면(보존 기간 지남) 모름 — 공유를 풀지 않는다
    be("gana")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(messenger, "_api", lose_answer)
        other = todo.add("원장 없음")
        todo.share_room(other["id"], room)
    server.ledger.clear()
    with messenger._one() as con:
        con.execute("UPDATE room_outbox SET created=created-?", (messenger.RETRY_SECONDS + 1,))
    messenger.sync()
    todo.flush_shares()
    assert todo._db().execute("SELECT share_room FROM cards WHERE id=?", (other["id"],)).fetchone()[0] == room
    assert not server.ledger


def test_a_done_request_that_arrives_while_the_result_lookup_is_retrying_is_kept_and_applied_once(three):
    """impl 회신 8 — 결과 묻기 첫 번째 429 → 그사이 끝냄 요청 도착 → 다음 묻기 성공 → 그때 판정해 한 번 적용."""
    from vader.core import todo
    server, people, be, room = three
    be("gana")
    messenger_rooms.rooms()
    real = messenger._api

    def lose_answer(m, path, *a, **k):
        got = real(m, path, *a, **k)
        if path == "room_send":
            raise account.AccountError("답을 잃음")
        return got
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(messenger, "_api", lose_answer)
        card = todo.add("묻는 중 요청")
        todo.share_room(card["id"], room)
    with messenger._one() as con:
        con.execute("UPDATE room_outbox SET created=created-?", (messenger.RETRY_SECONDS + 1,))
    be("dara")
    messenger_rooms.rooms()
    messenger.sync()
    todo.done(todo.cards()[0]["id"])
    be("gana")

    def busy(m, path, *a, **k):
        if path == "room_result":
            e = account.AccountError("잠시 뒤")
            e.status = 429
            raise e
        return real(m, path, *a, **k)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(messenger, "_api", busy)
        messenger.sync()                                   # 요청은 받았지만 받은 사람이 아직 확정 전
    assert todo.cards()[0]["state"] == "open"
    messenger.sync()
    messenger.sync()
    assert todo.cards()[0]["state"] == "done"
    todo.flush_shares()
    be("dara")
    messenger.sync()
    [got] = todo.cards()
    assert got["state"] == "done" and got["share_req"] is None
