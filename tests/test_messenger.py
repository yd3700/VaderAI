"""3c 메신저 앱 쪽 — 두 PC(가나메디칼·다라상사)를 각자 작업공간에 두고, 메모리 속 가짜 서버로 주고받는다.

가짜 서버는 web/server/messenger.ts 의 계약(서명 확인·같은 바이트 재전송·받은 뒤 ack·키 상한 50·해제·412)만 흉내 낸다.
서명 문장이 서버와 같은지는 tests/fixtures/messenger_vectors.json 을 웹 시험(messenger.spec.ts)이 확인한다.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import time
import uuid

import pytest

pytest.importorskip("vodozemac")

from vader.core import account, config, messenger  # noqa: E402


def refuse(status, message="서버가 거절했습니다."):
    error = account.AccountError(message)
    error.status = status
    raise error


class Server:
    def __init__(self):
        self.accounts = {}       # id → {handle, device(active)}
        self.devices = {}        # id → {account, ed, curve, sig, keys: {id: (key, sig, claimed)}, fallback, session, revoked}
        self.friends = {}        # frozenset → {state, by, generation}
        self.inbox = []
        self.sent = {}           # (device, client_id) → (id, digest)
        self.fail_on = None      # 이 동작은 처리하기 전에 끊긴다
        self.drop_reply = None   # 이 동작은 처리하고 답만 잃는다
        self.last = {}           # 동작별 마지막 요청 본문

    def call(self, who, method, path, body):
        assert path.startswith(messenger.API) and path.endswith("/"), path   # 웹 trailingSlash — 없으면 308
        action = path.removeprefix(messenger.API).removesuffix("/")
        b = json.loads(body) if body else {}
        self.last[action] = b
        if self.fail_on == action:
            self.fail_on = None
            refuse(None, "웹에 연결하지 못했습니다.")
        result = getattr(self, f"{method.lower()}_{action}")(who, b)
        if self.drop_reply == action:
            self.drop_reply = None
            refuse(None, "웹에 연결하지 못했습니다.")
        return result

    def release(self, acct):
        """웹 '메신저 해제'."""
        dev = self.accounts[acct].pop("device")
        self.devices[dev]["revoked"] = True
        self.inbox = [m for m in self.inbox if m["recipient_device"] != dev]

    def post_move(self, who, b):
        """이 PC 로 옮기기 — 한 계정 한 PC 라 옛 기기의 로그인은 이미 끊겼다고 본다(서버 시험이 그 조건을 따로 본다)."""
        d = self.accounts[who["account"]].get("device")
        if not d or self.devices[d]["session"] == who["session"]:
            return {"moved": False}
        self.release(who["account"])
        return {"moved": True}

    def _bound(self, who):
        d = self.accounts[who["account"]].get("device")
        if not (d and self.devices[d]["session"] == who["session"]):
            refuse(412, "이 PC 를 메신저 기기로 먼저 등록해 주세요.")
        return d

    def _free(self, dev):
        return sum(1 for k in self.devices[dev]["keys"].values() if not k[2])

    def get_me(self, who, b):
        a = self.accounts[who["account"]]
        d = a.get("device")
        bound = d if d and self.devices[d]["session"] == who["session"] else None
        return {"joined": True, "handle": a["handle"], "device": bound, "active_device": d,
                "one_time_keys": self._free(bound) if bound else 0}

    def post_register(self, who, b):
        acct, dev = who["account"], b["device_id"]
        if dev in self.devices:
            d = self.devices[dev]
            if d["revoked"] or d["account"] != acct:
                refuse(409, "해제된 메신저 기기입니다.")
            assert messenger.signed(d["ed"], messenger.bind_message(acct, who["session"], dev, d["curve"]), b["proof"])
            d["session"] = who["session"]
            return {"device": dev, "created": False}
        if self.accounts[acct].get("device"):
            refuse(409, "다른 PC 에서 메신저를 쓰고 있습니다.")
        ed, curve = b["identity_ed"], b["identity_curve"]
        assert messenger.signed(ed, messenger.bind_message(acct, who["session"], dev, curve), b["proof"])
        assert messenger.signed(ed, messenger.device_message(acct, dev, curve), b["identity_sig"])
        self.devices[dev] = {"account": acct, "ed": ed, "curve": curve, "sig": b["identity_sig"], "keys": {},
                             "fallback": b.get("fallback"), "session": who["session"], "revoked": False}
        self.accounts[acct]["device"] = dev
        self._store(dev, b["one_time_keys"])
        return {"device": dev, "created": True}

    def _store(self, dev, keys):
        d = self.devices[dev]
        added = []
        for k in keys:
            assert messenger.signed(d["ed"], messenger.key_message(dev, k["id"], k["key"]), k["signature"])
            if k["id"] in d["keys"]:
                assert d["keys"][k["id"]][:2] == (k["key"], k["signature"])
                continue
            d["keys"][k["id"]] = (k["key"], k["signature"], False)
            added.append(k["id"])
        if self._free(dev) > 50:
            for key_id in added:
                d["keys"].pop(key_id)              # 서버는 거래째 되돌린다
            refuse(400, "키가 너무 많습니다.")

    def post_keys(self, who, b):
        dev = self._bound(who)
        self._store(dev, b.get("one_time_keys", []))
        return {"one_time_keys": self._free(dev)}

    def post_find(self, who, b):
        for acct, a in self.accounts.items():
            if a["handle"] == b["handle"].lstrip("@") and acct != who["account"]:
                return {"found": True, "account_id": acct, "handle": a["handle"]}
        return {"found": False}

    def post_friend(self, who, b):
        pair = frozenset((who["account"], b["account_id"]))
        f = self.friends.get(pair)
        if b["op"] == "request":
            if f and f["state"] == "pending" and f["by"] != who["account"]:
                f["state"] = "accepted"
            elif not f or f["state"] == "ended":
                self.friends[pair] = {"state": "pending", "by": who["account"],
                                      "generation": (f["generation"] + 1) if f else 1}
        elif b["op"] == "accept":
            f["state"] = "accepted"
        else:
            f["state"] = "ended"
        return {"state": self.friends[pair]["state"]}

    def get_friends(self, who, b):
        out = []
        for pair, f in self.friends.items():
            if who["account"] not in pair or f["state"] == "ended":
                continue
            other = next(iter(pair - {who["account"]}))
            dev = self.accounts[other].get("device") if f["state"] == "accepted" else None
            d = self.devices.get(dev) if dev else None
            out.append({"account_id": other, "handle": self.accounts[other]["handle"], "state": f["state"],
                        "mine": f["by"] == who["account"], "generation": f["generation"], "device_id": dev,
                        "identity_ed": d and d["ed"], "identity_curve": d and d["curve"],
                        "identity_sig": d and d["sig"]})
        return {"friends": out, "blocked": []}

    def post_claim(self, who, b):
        self._bound(who)
        d = self.devices[b["device_id"]]
        f = self.friends[frozenset((who["account"], b["account_id"]))]
        free = sorted(k for k, v in d["keys"].items() if not v[2])
        if free:
            key, sig, _ = d["keys"][free[0]]
            d["keys"][free[0]] = (key, sig, True)
            k = {"id": free[0], "key": key, "signature": sig, "fallback": False}
        else:
            k = {"id": "fallback", **d["fallback"], "fallback": True}
        return {"device_id": b["device_id"], "identity_ed": d["ed"], "identity_curve": d["curve"],
                "identity_sig": d["sig"], "generation": f["generation"], "key": k}

    def post_send(self, who, b):
        dev = self._bound(who)
        digest = json.dumps([b["recipient_account"], b["recipient_device"], b["generation"], b["kind"], b["body"]])
        before = self.sent.get((dev, b["client_id"]))
        if before:
            if before[1] != digest:
                refuse(409, "같은 번호로 다른 내용을 보냈습니다.")
            return {"id": before[0], "duplicate": True}
        f = self.friends.get(frozenset((who["account"], b["recipient_account"])))
        if not f or f["state"] != "accepted":
            refuse(403, "친구에게만 보낼 수 있습니다.")
        if self.accounts[b["recipient_account"]].get("device") != b["recipient_device"]:
            refuse(409, "상대의 메신저 기기가 바뀌었습니다.")
        mid = str(uuid.uuid4())
        self.sent[(dev, b["client_id"])] = (mid, digest)
        self.inbox.append({"id": mid, "sender_account": who["account"], "sender_device": dev,
                           "recipient_device": b["recipient_device"], "kind": b["kind"], "body": b["body"],
                           "generation": b["generation"]})
        return {"id": mid, "duplicate": False}

    def get_inbox(self, who, b):
        dev = self._bound(who)
        return {"messages": [m for m in self.inbox if m["recipient_device"] == dev][:100]}

    def post_ack(self, who, b):
        dev = self._bound(who)
        before = len(self.inbox)
        self.inbox = [m for m in self.inbox if not (m["recipient_device"] == dev and m["id"] in b["ids"])]
        return {"removed": before - len(self.inbox)}


@pytest.fixture
def world(tmp_path, monkeypatch):
    messenger.set_alive(None)
    server = Server()
    people = {}
    state = {"now": None}

    def token(p):
        claim = base64.urlsafe_b64encode(json.dumps({"a": p["account"], "d": p["session"]}).encode()).decode()
        return claim.rstrip("=") + ".sig"

    def be(name):
        p = people[name]
        state["now"] = p
        monkeypatch.setattr(config, "_workspace", p["folder"])
        return p

    for name, handle in (("gana", "ganamed"), ("dara", "darasangsa")):
        folder = tmp_path / name
        folder.mkdir()
        acct = str(uuid.uuid4())
        people[name] = {"account": acct, "session": str(uuid.uuid4()), "folder": folder}
        server.accounts[acct] = {"handle": handle}

    monkeypatch.setattr(account, "access_token", lambda: token(state["now"]))
    monkeypatch.setattr(account, "call", lambda method, path, body=None, content_type=None:
                        server.call(state["now"], method, path, body))
    yield server, people, be
    messenger.set_alive(None)


def befriend(be, people):
    be("gana")
    assert messenger.connect()["state"] == "ready"
    be("dara")
    assert messenger.connect()["state"] == "ready"
    be("gana")
    assert messenger.find("@darasangsa")["account_id"] == people["dara"]["account"]
    messenger.friend(people["dara"]["account"], "request")
    be("dara")
    listed = messenger.friend(people["gana"]["account"], "accept")
    assert listed[0]["ready"] and not listed[0]["verified"]
    be("gana")
    messenger.friends()


def verify_both(be, people):
    numbers = []
    for me, other in (("gana", "dara"), ("dara", "gana")):
        be(me)
        messenger.friends()
        numbers.append(messenger.safety(people[other]["account"])["number"])
        messenger.verify(people[other]["account"], numbers[-1])
    assert numbers[0] == numbers[1] and len(numbers[0].replace(" ", "")) == 60   # 두 화면이 같은 숫자


@pytest.fixture
def talking(world):
    server, people, be = world
    befriend(be, people)
    verify_both(be, people)
    return world


def db(person):
    con = sqlite3.connect(person["folder"] / "messenger.sqlite3")
    con.row_factory = sqlite3.Row
    return con


def texts(account_id):
    return [m["text"] for m in messenger.history(account_id)]


def test_two_pcs_talk_only_after_checking_safety_numbers(world):
    server, people, be = world
    befriend(be, people)
    dara = people["dara"]["account"]
    with pytest.raises(messenger.MessengerError, match="안전 번호"):
        messenger.send(dara, "안녕하세요")
    verify_both(be, people)
    be("gana")
    assert messenger.send(dara, "안녕하세요 가나메디칼입니다")["state"] == "sent"
    assert "가나메디칼" not in json.dumps(server.inbox, ensure_ascii=False)      # 서버엔 암호문뿐
    be("dara")
    assert messenger.sync() == {"received": 1}
    got = messenger.history(people["gana"]["account"])
    assert [m["text"] for m in got] == ["안녕하세요 가나메디칼입니다"] and got[0]["trusted"]
    messenger.send(people["gana"]["account"], "네 다라상사입니다")
    be("gana")
    messenger.sync()
    assert [(m["outgoing"], m["text"]) for m in messenger.history(dara)] == [
        (True, "안녕하세요 가나메디칼입니다"), (False, "네 다라상사입니다")]
    assert server.inbox == []                                                   # 받은 것은 서버에서 지워졌다
    raw = (people["gana"]["folder"] / "messenger.sqlite3").read_bytes()
    assert "가나메디칼".encode() not in raw and "다라상사".encode() not in raw     # PC 기록도 암호화


def test_messages_received_before_checking_are_marked_untrusted(world):
    server, people, be = world
    befriend(be, people)
    be("dara")                                  # 다라상사만 확인하고 보낸다
    messenger.friends()
    messenger.verify(people["gana"]["account"], messenger.safety(people["gana"]["account"])["number"])
    messenger.send(people["gana"]["account"], "확인 전에 받은 말")
    be("gana")
    messenger.sync()
    messenger.verify(people["dara"]["account"], messenger.safety(people["dara"]["account"])["number"])
    got = messenger.history(people["dara"]["account"])
    assert got[0]["text"] == "확인 전에 받은 말" and not got[0]["trusted"]  # 나중에 확인해도 지난 것은 그대로


def test_a_lost_send_reply_resends_the_same_bytes_and_arrives_once(talking):
    server, people, be = talking
    be("gana")
    server.drop_reply = "send"                  # 서버는 받았는데 답이 끊겼다
    assert messenger.send(people["dara"]["account"], "한 번만")["state"] == "queued"
    assert len(server.inbox) == 1
    messenger.sync()                            # 같은 바이트로 다시 — 서버는 같은 메시지라고 답한다
    assert messenger.history(people["dara"]["account"])[0]["state"] == "sent"
    be("dara")
    messenger.sync()
    assert texts(people["gana"]["account"]) == ["한 번만"]


def test_a_lost_ack_does_not_show_a_message_twice(talking):
    server, people, be = talking
    be("gana")
    messenger.send(people["dara"]["account"], "중복 없이")
    be("dara")
    server.fail_on = "ack"                      # 저장은 했는데 ack 가 서버에 닿지 않았다 → 서버가 다시 준다
    with pytest.raises(account.AccountError):
        messenger.sync()
    assert len(server.inbox) == 1
    messenger.sync()
    assert server.inbox == []
    assert texts(people["gana"]["account"]) == ["중복 없이"]


def test_a_garbled_message_does_not_block_the_ones_after_it(talking):
    server, people, be = talking
    gana_dev = server.accounts[people["gana"]["account"]]["device"]
    dara_dev = server.accounts[people["dara"]["account"]]["device"]
    for kind, body in ((0, base64.b64encode(b"not olm at all").decode()), (1, "QUJD")):
        server.inbox.append({"id": str(uuid.uuid4()), "sender_account": people["gana"]["account"],
                             "sender_device": gana_dev, "recipient_device": dara_dev, "kind": kind, "body": body,
                             "generation": 1})
    be("gana")
    messenger.send(people["dara"]["account"], "뒤에 온 정상 메시지")
    be("dara")
    assert messenger.sync() == {"received": 1}
    assert server.inbox == []
    got = messenger.history(people["gana"]["account"])
    assert sorted(m["state"] for m in got) == ["received", "unreadable", "unreadable"]
    assert [m["text"] for m in got if m["state"] == "received"] == ["뒤에 온 정상 메시지"]


def test_a_changed_identity_stops_sending_until_checked_again(talking):
    server, people, be = talking
    dara = people["dara"]["account"]
    be("gana")
    shown = messenger.safety(dara)["number"]
    # 서버가 다라상사 계정에 다른 기기를 끼운다 (또는 다라상사가 PC 를 바꿨다)
    be("dara")
    server.release(dara)
    assert messenger.connect()["state"] == "ready"
    be("gana")
    listed = messenger.friends()
    assert listed[0]["changed"] and not listed[0]["verified"]
    with pytest.raises(messenger.MessengerError, match="안전 번호"):
        messenger.send(dara, "바뀐 키로는 안 간다")
    with pytest.raises(messenger.MessengerError, match="키가 바뀌었습니다"):
        messenger.verify(dara, shown)          # 바뀌기 전에 본 번호로는 새 키를 확인할 수 없다 (codex1 3c P1-1)
    assert messenger.friends()[0]["changed"]


def test_messages_sent_before_a_pc_change_still_arrive(talking):
    """다라상사가 보낸 뒤 PC 를 바꿔도, 가나메디칼은 옛 기기에서 온 메시지를 확인해 둔 키로 푼다 (codex1 3c P1-3)."""
    server, people, be = talking
    be("dara")
    messenger.send(people["gana"]["account"], "바꾸기 전에 보냄")
    server.release(people["dara"]["account"])
    messenger.connect()
    be("gana")
    messenger.friends()                         # 이제 서버 목록에는 새 기기만 있다
    assert messenger.sync() == {"received": 1}
    got = messenger.history(people["dara"]["account"])
    assert got[0]["text"] == "바꾸기 전에 보냄" and got[0]["trusted"]


def test_queued_messages_from_a_released_pc_are_not_sent_by_the_new_one(talking):
    server, people, be = talking
    be("gana")
    server.fail_on = "send"
    assert messenger.send(people["dara"]["account"], "옛 기기에서 줄 선 것")["state"] == "queued"
    server.release(people["gana"]["account"])   # 웹에서 이 PC 를 해제했다
    messenger.connect()                         # 새 기기로 — 옛 암호문은 실패로 끝낸다 (codex1 3c P1-2)
    messenger.sync()
    assert server.inbox == []
    assert messenger.history(people["dara"]["account"])[0]["state"] == "failed"


def test_a_relogin_waits_instead_of_failing_queued_messages(talking):
    server, people, be = talking
    be("gana")
    server.fail_on = "send"
    messenger.send(people["dara"]["account"], "다시 로그인 뒤에 간다")
    people["gana"]["session"] = str(uuid.uuid4())   # 다시 로그인 — 아직 새 로그인에 안 묶임 → 서버는 412
    with pytest.raises(account.AccountError):
        messenger.sync()
    assert messenger.history(people["dara"]["account"])[0]["state"] == "queued"
    messenger.connect()
    messenger.sync()
    assert messenger.history(people["dara"]["account"])[0]["state"] == "sent"


def test_a_key_the_identity_did_not_sign_is_refused(talking):
    server, people, be = talking
    dara_dev = server.accounts[people["dara"]["account"]]["device"]
    for k, (key, sig, used) in server.devices[dara_dev]["keys"].items():   # 서버가 일회용 키를 바꿔 끼운다
        server.devices[dara_dev]["keys"][k] = (base64.b64encode(bytes(32)).decode().rstrip("="), sig, used)
    be("gana")
    with pytest.raises(messenger.MessengerError, match="서명"):
        messenger.send(people["dara"]["account"], "가짜 키로는 안 간다")


def test_an_encryption_key_the_identity_did_not_sign_is_not_used(talking):
    server, people, be = talking
    dara_dev = server.accounts[people["dara"]["account"]]["device"]
    server.devices[dara_dev]["curve"] = server.devices[server.accounts[people["gana"]["account"]]["device"]]["curve"]
    be("gana")
    messenger.friends()                       # 서버가 암호키를 바꿔 끼웠다 — 서명이 안 맞으니 붙잡은 키를 그대로 둔다
    with pytest.raises(messenger.MessengerError, match="키가 바뀌었습니다"):
        messenger.send(people["dara"]["account"], "바꿔 낀 키로는 안 간다")


def test_relogin_rebinds_the_same_device_without_adding_keys(world):
    server, people, be = world
    be("gana")
    first = messenger.connect()["device"]
    people["gana"]["session"] = str(uuid.uuid4())         # 로그아웃 뒤 다시 로그인 — 새 로그인 행
    assert messenger.connect()["device"] == first          # 키 40개가 있는데 40개를 더 올리면 서버가 거절한다 (codex1 3c P2-1)
    assert server.devices[first]["session"] == people["gana"]["session"]
    assert server._free(first) == messenger.KEYS_TARGET


def test_one_time_keys_are_topped_up_after_they_are_used(talking):
    server, people, be = talking
    dara_dev = server.accounts[people["dara"]["account"]]["device"]
    keys = server.devices[dara_dev]["keys"]
    for k in list(keys)[: messenger.KEYS_TARGET - messenger.KEYS_LOW + 1]:
        keys[k] = (*keys[k][:2], True)                     # 다른 사람들이 키를 가져갔다
    be("gana")
    messenger.send(people["dara"]["account"], "첫 연락")
    be("dara")
    messenger.sync()                                       # 첫 연락(pre-key)을 받으면 키를 채운다
    assert server._free(dara_dev) == messenger.KEYS_TARGET


def test_a_key_batch_older_than_29_days_is_never_uploaded(talking):
    server, people, be = talking
    dara_dev = server.accounts[people["dara"]["account"]]["device"]
    keys = server.devices[dara_dev]["keys"]
    for k in list(keys)[:30]:
        keys[k] = (*keys[k][:2], True)
    be("dara")
    server.fail_on = "keys"                                # 새 묶음을 올리다 끊겼다
    with pytest.raises(account.AccountError):
        messenger.connect()
    stale = {k["id"] for k in server.last["keys"]["one_time_keys"]}
    with db(people["dara"]) as con:
        con.execute("UPDATE me SET keys_since=?", (time.time() - 30 * 86400,))
    messenger.connect()                                    # 오래된 묶음은 버리고 새로 만든다 (codex1 3c P1-4)
    fresh = {k["id"] for k in server.last["keys"]["one_time_keys"]}
    assert stale and fresh and not (fresh & stale) and not (set(keys) & stale)


def test_locking_stops_messenger_work_at_the_next_step(talking):
    server, people, be = talking
    be("gana")
    messenger.send(people["dara"]["account"], "잠기기 직전")
    be("dara")
    calls = iter([True, True])                             # 시작·받기까지 열려 있고, ack 직전에 잠겼다
    messenger.set_alive(lambda: next(calls, False))
    with pytest.raises(messenger.Stopped):
        messenger.sync()
    assert len(server.inbox) == 1                          # ack 하지 않았다 — 풀면 다시 받는다
    messenger.set_alive(lambda: False)
    with pytest.raises(messenger.Stopped):
        messenger.send(people["gana"]["account"], "잠긴 뒤에는 안 간다")
    messenger.set_alive(None)
    messenger.sync()
    assert texts(people["gana"]["account"]) == ["잠기기 직전"] and server.inbox == []


def test_signing_vectors_match_the_server():
    """tests/fixtures/messenger_vectors.json — 웹 시험이 같은 파일을 서버 함수로 확인한다."""
    from pathlib import Path
    data = json.loads((Path(__file__).parent / "fixtures" / "messenger_vectors.json").read_text(encoding="utf-8"))
    assert messenger.bind_message(data["account"], data["session"], data["device"], data["curve"]) == data["bind"]
    assert messenger.device_message(data["account"], data["device"], data["curve"]) == data["device_text"]
    assert messenger.key_message(data["device"], data["key_id"], data["key"]) == data["key_text"]
    assert messenger.fallback_message(data["device"], 1, data["key"]) == data["fallback_text"]
    for text, sig in ((data["bind"], data["bind_sig"]), (data["device_text"], data["device_sig"]),
                      (data["key_text"], data["key_sig"]), (data["fallback_text"], data["fallback_sig"])):
        assert messenger.signed(data["ed"], text, sig)
    number = messenger.safety_number(data["account"], data["ed"], data["peer"], data["peer_ed"])
    assert number == data["safety"] and "00000 00000 00000 00000" not in number
    assert messenger.safety_number(data["account"], data["peer_ed"], data["peer"], data["ed"]) != number


def test_the_desktop_host_stops_messenger_work_when_locked_and_leaves_no_check_behind(monkeypatch):
    from unittest.mock import MagicMock
    from vader.desktop import host
    api = host.DesktopAPI(controller=MagicMock())
    seen = []

    def sync():
        seen.append(messenger._guard.check())
        api._gen += 1                          # 도는 사이 잠겼다
        seen.append(messenger._guard.check())
        return {"received": 0}
    monkeypatch.setattr(messenger, "sync", sync)
    assert api.call("messenger_sync") == host._LOCKED
    assert seen == [True, False]
    assert getattr(messenger._guard, "check", None) is None


def test_connect_reads_server_state_only_while_holding_the_lock(world, monkeypatch):
    """먼저 본 '기기 없음' 으로 다른 요청이 방금 등록한 기기를 지우지 않게 (codex1 3c NEW-P1-1)."""
    server, people, be = world
    be("gana")
    real, held = server.get_me, []
    monkeypatch.setattr(server, "get_me", lambda who, b: held.append(messenger._busy.locked()) or real(who, b))
    messenger.connect()
    messenger.connect()
    assert held and all(held)


def test_a_non_text_first_message_keeps_the_session_for_the_next_one(talking):
    """암호는 맞는데 글자가 아닌 첫 메시지 — 세션은 저장해 뒤의 정상 메시지를 푼다 (codex1 3c NEW-P1-2)."""
    server, people, be = talking
    vz = messenger._vz()
    gana_dev = server.accounts[people["gana"]["account"]]["device"]
    be("dara")
    con = messenger._db()
    row, dara = messenger._me(con)
    con.close()
    claimed = server.post_claim({"account": people["dara"]["account"], "session": people["dara"]["session"]},
                                {"account_id": people["gana"]["account"], "device_id": gana_dev})
    session = dara.create_outbound_session(vz.Curve25519PublicKey.from_base64(claimed["identity_curve"]),
                                           vz.Curve25519PublicKey.from_base64(claimed["key"]["key"]))
    for plain in (b"\xff\xfe\xfd", "정상 메시지".encode()):
        kind, body = session.encrypt(plain).to_parts()
        server.inbox.append({"id": str(uuid.uuid4()), "sender_account": people["dara"]["account"],
                             "sender_device": row["device"], "recipient_device": gana_dev, "kind": kind,
                             "body": base64.b64encode(body).decode(), "generation": 1})
    be("gana")
    assert messenger.sync() == {"received": 1}
    got = messenger.history(people["dara"]["account"])
    assert [(m["state"], m["text"]) for m in got] == [("unreadable", None), ("received", "정상 메시지")]


def test_my_own_new_identity_asks_to_check_safety_numbers_again(talking):
    server, people, be = talking
    be("gana")
    server.release(people["gana"]["account"])
    messenger.connect()                                    # 이 PC 의 신원키가 새로 생겼다 (codex1 3c NEW-P2-1)
    messenger.friends()
    assert not messenger.safety(people["dara"]["account"])["verified"]
    with pytest.raises(messenger.MessengerError, match="안전 번호"):
        messenger.send(people["dara"]["account"], "다시 확인 전")


def test_a_late_message_from_an_old_checked_device_stays_trusted(talking):
    """옛 기기를 확인했고 새 기기도 확인한 뒤, 옛 기기에서 늦게 온 메시지도 확인된 것으로 (codex1 3c NEW-P2-2)."""
    server, people, be = talking
    be("dara")
    messenger.send(people["gana"]["account"], "옛 기기에서")
    server.release(people["dara"]["account"])
    messenger.connect()
    be("gana")
    messenger.friends()
    messenger.verify(people["dara"]["account"], messenger.safety(people["dara"]["account"])["number"])
    messenger.sync()
    got = messenger.history(people["dara"]["account"])
    assert got[0]["text"] == "옛 기기에서" and got[0]["trusted"]


def test_an_outbox_entry_made_by_another_device_is_never_sent(talking):
    server, people, be = talking
    be("gana")
    with messenger._tx(messenger._db()) as con:
        con.execute("INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), str(uuid.uuid4()),
                    people["dara"]["account"], server.accounts[people["dara"]["account"]]["device"], 1, 1, "QUJD",
                    time.time()))
    messenger.sync()
    assert server.inbox == [] and "send" not in server.last


def test_a_lost_register_reply_then_a_web_release_starts_a_new_device(world):
    server, people, be = world
    be("gana")
    server.drop_reply = "register"                         # 등록은 됐는데 답을 못 받았다
    with pytest.raises(account.AccountError):
        messenger.connect()
    first = server.accounts[people["gana"]["account"]]["device"]
    server.release(people["gana"]["account"])              # 그 뒤 웹에서 해제
    people["gana"]["session"] = str(uuid.uuid4())
    assert messenger.connect()["state"] == "ready"
    assert server.accounts[people["gana"]["account"]]["device"] not in (None, first)


def test_the_desktop_host_shows_only_its_own_messenger_error_text(monkeypatch):
    from unittest.mock import MagicMock
    from vader.desktop import host
    api = host.DesktopAPI(controller=MagicMock())
    monkeypatch.setattr(messenger, "history", lambda **_: (_ for _ in ()).throw(ValueError("받은 바이트 \xff")))
    result = api.call("messenger_history", {"account_id": "x"})
    assert result == {"ok": False, "error": "메신저에서 문제가 생겼습니다. 잠시 후 다시 시도해 주세요."}
    monkeypatch.setattr(messenger, "history", lambda **_: (_ for _ in ()).throw(messenger.MessengerError("친구에게만")))
    assert api.call("messenger_history", {"account_id": "x"})["error"] == "친구에게만"


def test_read_mark_survives_restart_and_only_moves_forward(talking):
    """읽음은 코어 저장소에 — 앱을 껐다 켜도(화면 저장소가 비어도) '새 메시지' 로 돌아가지 않는다 (2026-11-09)."""
    server, people, be = talking
    gana = people["gana"]["account"]
    be("gana")
    messenger.send(people["dara"]["account"], "명세서 보냈어요")
    be("dara")
    messenger.sync()
    last = messenger.friends()[0]["last"]
    assert last and messenger.friends()[0]["seen"] is None
    messenger.mark_seen(gana, last)
    messenger.mark_seen(gana, last - 100)                    # 늦게 온 예전 표시는 되돌리지 않는다
    assert messenger.friends()[0]["seen"] == last


def test_keywords_come_from_the_conversation_without_particles(talking):
    """대화 키워드 — 조사를 떼고 자주 나온 낱말부터. 인사말은 뺀다 (2026-11-10)."""
    server, people, be = talking
    be("gana")
    for text in ("안녕하세요 명세서 보냈어요", "명세서를 확인해 주세요", "거즈 단가는 명세서에 있어요", "거즈 수량도요"):
        messenger.send(people["dara"]["account"], text)
    assert messenger.friends()[0]["keywords"] == ["명세서", "거즈", "단가"]


def test_member_groups_stay_on_this_pc_and_one_friend_is_in_one_group(world):
    """멤버 그룹 (마스터 2026-11-18) — 서버에 안 가고, 친구 하나는 그룹 하나, 그룹을 지우면 '그룹 없음'."""
    server, people, be = world
    befriend(be, people)
    be("gana")
    calls = len(server.log) if hasattr(server, "log") else None
    b = people["dara"]["account"]
    [g] = messenger.group_save("구매팀")
    assert g["name"] == "구매팀" and g["count"] == 0
    with pytest.raises(messenger.MessengerError):
        messenger.group_save(" 구매팀 ")                       # 같은 이름(공백 정리 뒤)
    with pytest.raises(messenger.MessengerError):
        messenger.group_save("가" * (messenger.GROUP_NAME_MAX + 1))
    other = [x for x in messenger.group_save("원무과") if x["name"] == "원무과"][0]
    messenger.group_assign(b, g["id"])
    messenger.group_assign(b, other["id"])                    # 옮기면 앞 그룹에서 빠진다
    assert {x["name"]: x["count"] for x in messenger.groups()} == {"구매팀": 0, "원무과": 1}
    assert [f["group"] for f in messenger._list(db(people["gana"]))  if f["account_id"] == b] == [other["id"]]
    with pytest.raises(messenger.MessengerError):
        messenger.group_assign("nobody", g["id"])
    messenger.group_save("원무팀", other["id"])
    messenger.group_delete(other["id"])
    assert [x["name"] for x in messenger.groups()] == ["구매팀"]
    assert [f["group"] for f in messenger._list(db(people["gana"])) if f["account_id"] == b] == [None]
    if calls is not None:
        assert len(server.log) == calls                       # 서버에 아무것도 보내지 않았다


def test_a_shared_todo_reaches_the_friend_and_done_comes_back(talking):
    """할 일 멤버 공유 (2026-11-18) — 제목·날짜만 종단간으로, 받은 쪽은 미루기 못 함, 한쪽이 끝내면 다른 쪽도."""
    pytest.importorskip("cryptography")
    from datetime import datetime, timedelta
    from vader.core import todo
    server, people, be = talking
    dara = people["dara"]["account"]
    be("gana")
    card = todo.add("TEST 발주 확인")["id"]
    todo.note_body(card, card, "비밀 메모")                        # 포스트잇은 가지 않는다
    todo.share(card, [dara])
    assert [c["shared_with"] for c in todo.cards() if c["id"] == card] == [[dara]]
    be("dara")
    messenger.sync()
    [got] = todo.cards()
    assert got["title"] == "TEST 발주 확인" and got["owner"] == people["gana"]["account"]
    assert todo.note(got["id"])["body"] == ""
    with pytest.raises(todo.TodoError):
        todo.snooze(got["id"], (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds"))
    with pytest.raises(todo.TodoError):
        todo.share(got["id"], [people["gana"]["account"]])
    todo.done(got["id"])                                           # 받은 쪽에서 끝냄 → 보낸 쪽도
    be("gana")
    messenger.sync()
    assert [c["state"] for c in todo.cards() if c["id"] == card] == ["done"]
    todo.done(card, undo=True)                                     # 되돌리면 받은 쪽도 다시 열린다
    be("dara")
    messenger.sync()
    assert [c["state"] for c in todo.cards()] == ["open"]


def test_share_messages_from_someone_else_cannot_touch_my_cards(talking):
    pytest.importorskip("cryptography")
    from vader.core import todo
    server, people, be = talking
    be("gana")
    card = todo.add("TEST 내 카드")["id"]
    todo.share(card, [people["dara"]["account"]])
    sid = [c for c in todo._db().execute("SELECT share_id FROM cards WHERE id=?", (card,))][0][0]
    assert todo.receive_share("someone-else", todo.SHARE_MARK + json.dumps({"op": "done", "id": sid}))
    assert [c["state"] for c in todo.cards() if c["id"] == card] == ["open"]   # 공유하지 않은 사람의 끝냄은 무시
    assert todo.receive_share("x", "그냥 메시지") is False


def test_a_share_that_fails_to_apply_waits_and_is_applied_on_the_next_sync(talking, monkeypatch):
    pytest.importorskip("cryptography")
    from vader.core import todo
    server, people, be = talking
    be("gana")
    todo.share(todo.add("TEST 재시도")["id"], [people["dara"]["account"]])
    be("dara")
    real = todo.receive_share
    monkeypatch.setattr(todo, "receive_share", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    assert messenger.sync()["shares_waiting"] == 1 and todo.cards() == []
    monkeypatch.setattr(todo, "receive_share", real)
    assert "shares_waiting" not in messenger.sync()
    assert [c["title"] for c in todo.cards()] == ["TEST 재시도"]


def test_received_shares_are_capped_per_friend(talking, monkeypatch):
    pytest.importorskip("cryptography")
    from vader.core import todo
    server, people, be = talking
    be("dara")
    monkeypatch.setattr(todo, "SHARED_PER_SENDER", 2)
    for i in range(3):
        todo.receive_share("g1", todo.SHARE_MARK + json.dumps({"op": "share", "id": f"s{i}", "title": f"T{i}", "due": "2026-11-18"}))
    assert sorted(c["title"] for c in todo.cards()) == ["T0", "T1"]
    todo.receive_share("g1", todo.SHARE_MARK + json.dumps({"op": "share", "id": "s1", "title": "T1b", "due": "2026-11-18"}))
    assert sorted(c["title"] for c in todo.cards()) == ["T0", "T1b"]          # 같은 카드 고치기는 상한과 무관


def test_a_share_saved_but_not_acked_is_still_applied_after_redelivery(talking, monkeypatch):
    """메시지를 저장한 바로 뒤 멈춰도(재전달되면 '이미 받음') 공유는 적용된다 — 대기는 메시지와 같은 트랜잭션 (codex1 회신 2)."""
    pytest.importorskip("cryptography")
    from vader.core import todo
    server, people, be = talking
    be("gana")
    todo.share(todo.add("TEST 재전달")["id"], [people["dara"]["account"]])
    be("dara")
    real = messenger._receive

    def crash(*a):
        real(*a)
        raise RuntimeError("저장 직후 멈춤")
    monkeypatch.setattr(messenger, "_receive", crash)
    with pytest.raises(RuntimeError):
        messenger.sync()
    monkeypatch.setattr(messenger, "_receive", real)
    messenger.sync()                                   # 재전달 — _receive 는 '이미 받음' 으로 0
    assert [c["title"] for c in todo.cards()] == ["TEST 재전달"]


def test_a_share_applied_twice_after_a_crash_queues_the_fanout_once(talking):
    """적용 뒤 대기를 지우기 전에 멈춰 다시 적용돼도 한 번만 — 메시지 id 로 (codex1 회신 3)."""
    pytest.importorskip("cryptography")
    from vader.core import todo
    server, people, be = talking
    be("gana")
    card = todo.add("TEST 한 번만")["id"]
    todo.share(card, [people["dara"]["account"], "other-member"])
    sid = todo._db().execute("SELECT share_id FROM cards WHERE id=?", (card,)).fetchone()[0]
    con = todo._db()
    con.execute("DELETE FROM share_out")
    text = todo.SHARE_MARK + json.dumps({"op": "done", "id": sid})
    for _ in range(2):
        todo.receive_share(people["dara"]["account"], text, "m-1")
    assert con.execute("SELECT count(*) FROM share_out").fetchone()[0] == 1      # other-member 에게 한 번
    assert [c["state"] for c in todo.cards() if c["id"] == card] == ["done"]


def test_a_resent_notice_with_a_new_message_id_is_applied_once(talking):
    """보낸 쪽이 보낸 뒤 큐를 지우기 전에 멈춰 다시 보내도(새 메시지 id) 같은 ev 라 한 번만 (codex1 회신 4)."""
    pytest.importorskip("cryptography")
    from vader.core import todo
    server, people, be = talking
    be("gana")
    card = todo.add("TEST 다시 보냄")["id"]
    todo.share(card, [people["dara"]["account"], "other-member"])
    sid = todo._db().execute("SELECT share_id FROM cards WHERE id=?", (card,)).fetchone()[0]
    con = todo._db()
    con.execute("DELETE FROM share_out")
    text = todo.SHARE_MARK + json.dumps({"op": "done", "id": sid, "ev": "e1"})
    todo.receive_share(people["dara"]["account"], text, "m-1")
    todo.receive_share(people["dara"]["account"], text, "m-2")
    assert con.execute("SELECT count(*) FROM share_out").fetchone()[0] == 1


def test_an_old_queue_row_without_ev_gets_one_before_it_is_sent(talking):
    pytest.importorskip("cryptography")
    from vader.core import todo
    server, people, be = talking
    be("gana")
    con = todo._db()
    con.execute("INSERT INTO share_out(peer, payload) VALUES (?,?)", ("p1", todo._seal(json.dumps({"op": "done", "id": "s"}))))
    sent = []
    todo.flush_shares(send=lambda peer, text: (sent.append(text), (_ for _ in ()).throw(OSError()))[1])   # 보내고 실패 → 남는다
    todo.flush_shares(send=lambda peer, text: sent.append(text))
    assert len(sent) == 2 and sent[0] == sent[1] and '"ev"' in sent[0]                              # 두 번 보내도 같은 ev


def test_unsharing_removes_the_friends_copy_and_restores_mine(talking):
    """공유 취소 (2026-11-19) — 받은 쪽 카드는 지워지고 내 카드는 공유 전 모양. 주인이 아닌 사람의 취소는 무시."""
    pytest.importorskip("cryptography")
    from vader.core import todo
    server, people, be = talking
    dara, gana = people["dara"]["account"], people["gana"]["account"]
    be("gana")
    card = todo.add("TEST 취소")["id"]
    todo.share(card, [dara])
    be("dara")
    messenger.sync()
    [got] = todo.cards()
    sid = todo._db().execute("SELECT share_id FROM cards WHERE id=?", (got["id"],)).fetchone()[0]
    todo.receive_share("someone-else", todo.SHARE_MARK + json.dumps({"op": "unshare", "id": sid, "ev": "x"}))
    assert len(todo.cards()) == 1                                     # 주인 아님 → 그대로
    be("gana")
    todo.unshare(card, [dara])
    [mine] = [c for c in todo.cards() if c["id"] == card]
    assert mine["shared_with"] == [] and not todo._db().execute("SELECT share_id FROM cards WHERE id=?", (card,)).fetchone()[0]
    be("dara")
    messenger.sync()
    assert todo.cards() == []
    be("gana")
    todo.share(card, [dara])                                          # 다시 공유하면 다시 간다
    be("dara")
    messenger.sync()
    assert [c["title"] for c in todo.cards()] == ["TEST 취소"]


def test_keywords_skip_todo_share_notices_and_id_like_tokens(talking):
    pytest.importorskip("cryptography")
    from vader.core import todo
    server, people, be = talking
    be("gana")
    card = todo.add("발주서 확인")["id"]
    todo.share(card, [people["dara"]["account"]])
    messenger.send(people["dara"]["account"], "발주서 발주서 단가표 7f566781a2c9 보냈어")
    be("dara")
    messenger.sync()
    [f] = [f for f in messenger.friends() if f["account_id"] == people["gana"]["account"]]
    assert "발주서" in f["keywords"] and not any(k in ("due", "op", "share", "title") or any(c.isdigit() for c in k) for k in f["keywords"])


def test_moving_the_messenger_to_a_new_pc_after_confirming(world, tmp_path):
    """한 계정 한 PC (2026-11-25): 새 PC 에서는 '다른 PC' 로 멈추고, 사람이 확인해 옮기면 새 키로 새 기기가 된다."""
    server, people, be = world
    be("gana")
    assert messenger.connect()["state"] == "ready"
    old = server.accounts[people["gana"]["account"]]["device"]
    new_pc = tmp_path / "gana-new-pc"
    new_pc.mkdir()
    people["gana"].update(folder=new_pc, session=str(uuid.uuid4()))      # 같은 계정, 다른 PC·새 로그인
    be("gana")
    assert messenger.connect()["state"] == "other_pc"                    # 확인 전에는 아무것도 지우지 않는다
    assert server.accounts[people["gana"]["account"]]["device"] == old
    assert messenger.move()["state"] == "ready"
    now = server.accounts[people["gana"]["account"]]["device"]
    assert now not in (None, old) and server.devices[old]["revoked"]
