"""폰에서 할 일 보기 — 연결 코드로 증명한 폰만 붙잡고, 그 키로만 잠그며, 서명·nonce·판 번호가 맞는 변경만 적용한다."""
import json
from datetime import datetime, timedelta

import pytest

pytest.importorskip("cryptography")

from vader.core import account, phone, todo  # noqa: E402


class FakePhone:
    """폰 브라우저 흉내 — 같은 식으로 키·증명·서명을 만든다."""

    def __init__(self, code):
        from cryptography.hazmat.primitives.asymmetric import ec
        self.ecdh = ec.generate_private_key(ec.SECP256R1())
        self.sign = ec.generate_private_key(ec.SECP256R1())
        self.ecdh_key, self.sign_key = phone._raw(self.ecdh.public_key()), phone._raw(self.sign.public_key())
        self.k = phone._pair_key(code, self.ecdh_key)

    def proof(self):
        return phone._mac(self.k, f"{phone.PAIR}|phone|{self.ecdh_key}|{self.sign_key}")

    def op(self, pid, body):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        text = json.dumps(body)
        r, s = decode_dss_signature(self.sign.sign(f"vader-phone-op-v1|{text}".encode(), ec.ECDSA(hashes.SHA256())))
        return {"body": text, "sig": phone._b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))}

    def open(self, pid, box, pc_key):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        body, sig = box.split(".")
        assert phone._verify(pc_key, sig, f"vader-phone-box-v1|{pid}|{body}")
        raw = phone._unb64u(body)
        info = phone.INFO + pid.encode()
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw[:65])
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(self.ecdh.exchange(ec.ECDH(), peer))
        return json.loads(AESGCM(key).decrypt(raw[65:77], raw[77:], info))


@pytest.fixture
def server(isolated_settings, monkeypatch):
    srv = {"phones": [], "ops": [], "puts": [], "acks": [], "approved": {}}

    def fake(method, action, body=None):
        if action == "pc":
            return {"phones": [dict(p) for p in srv["phones"]], "ops": list(srv["ops"])}
        if action == "approve":
            srv["approved"][body["phone_id"]] = body
            for p in srv["phones"]:
                if p["id"] == body["phone_id"]:
                    p["state"] = "approved"
        if action == "put":
            srv["puts"].append(body)
        if action == "ack":
            srv["acks"] += body["ids"]
            srv["ops"] = [o for o in srv["ops"] if o["id"] not in body["ids"]]
        return {}

    monkeypatch.setattr(phone, "_call", fake)
    monkeypatch.setattr(account, "signed_in", lambda: True)
    monkeypatch.setattr(account, "site", lambda: "https://example.test")
    phone._pairing = None
    return srv


def _pending(fp, pid, proof=None):
    return {"id": pid, "label": "iPhone", "ecdh_key": fp.ecdh_key, "sign_key": fp.sign_key,
            "proof": proof or fp.proof(), "state": "pending"}


def test_only_a_phone_that_proved_the_code_is_connected_and_the_pc_proves_itself_back(server):
    code = phone.start()["code"]
    real, stranger = FakePhone(code), FakePhone("ZZZZZZZZ")
    server["phones"] = [_pending(stranger, "p0"), _pending(real, "p1")]
    out = phone.phones()
    assert [p["id"] for p in out["phones"]] == ["p1"] and out["code"] is None and out["waiting"] == 1
    sent = server["approved"]["p1"]
    want = phone._mac(real.k, f"{phone.PAIR}|pc|p1|{real.ecdh_key}|{real.sign_key}|{sent['pc_key']}")
    assert sent["pc_proof"] == want                                   # 폰이 같은 코드로 PC 키를 확인할 수 있다
    assert "p0" not in server["approved"]                             # 코드를 모르는 요청은 승인하지 않는다


def test_sync_uses_only_pinned_keys_and_applies_signed_fresh_changes_once(server):
    a = todo.add("가나메디칼 명세서 확인")
    b = todo.add("다라상사 견적 보내기")
    code = phone.start()["code"]
    fp = FakePhone(code)
    server["phones"] = [_pending(fp, "p1")]
    phone.phones()
    pc_key = server["approved"]["p1"]["pc_key"]

    server["phones"][0]["ecdh_key"] = FakePhone("ZZZZZZZZ").ecdh_key   # 서버가 키를 바꿔 끼워도
    phone.sync()
    got = fp.open("p1", server["puts"][-1]["box"], pc_key)              # 붙잡은 진짜 폰 키로 잠갔다
    assert {c["title"] for c in got["cards"]} == {"가나메디칼 명세서 확인", "다라상사 견적 보내기"} and got["seq"] == 1
    phone.sync()
    assert len(server["puts"]) == 1                                     # 안 바뀌었으면 다시 안 올린다

    rev = {c["id"]: c["rev"] for c in got["cards"]}
    later = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    todo.done(b["id"]); todo.done(b["id"], undo=True)                  # PC 에서 끝냈다 되돌림 — 판 번호는 올라간다
    snooze = fp.op("p1", {"v": 1, "phone": "p1", "nonce": "n1", "card": a["id"], "op": "snooze",
                          "at": int(later.timestamp()), "rev": rev[a["id"]]})
    finish = fp.op("p1", {"v": 1, "phone": "p1", "nonce": "n2", "card": b["id"], "op": "done", "at": None, "rev": rev[b["id"]]})
    forged = FakePhone("ZZZZZZZZ").op("p1", {"v": 1, "phone": "p1", "nonce": "n3", "card": b["id"], "op": "done",
                                             "at": None, "rev": rev[b["id"]] + 2})
    server["ops"] = [{"id": "o1", "phone_id": "p1", **snooze}, {"id": "o2", "phone_id": "p1", **finish},
                     {"id": "o3", "phone_id": "p1", **forged}]
    assert phone.sync() == 1 and server["acks"] == ["o1", "o2", "o3"]
    cards = {c["id"]: c for c in todo.cards()}
    assert cards[a["id"]]["remind_at"].startswith(later.strftime("%Y-%m-%dT%H:%M"))
    assert cards[b["id"]]["state"] == "open"                            # 옛 판의 끝냄·위조 서명은 적용하지 않는다
    again = fp.open("p1", server["puts"][-1]["box"], pc_key)
    assert again["seq"] == 2 and again["results"] == {"n1": "applied", "n2": "conflict"}   # 위조 서명은 결과 목록에도 못 쓴다

    server["ops"] = [{"id": "o4", "phone_id": "p1", **snooze}]           # 같은 변경을 다시 보내도
    assert phone.sync() == 0

    server["phones"] = []                                               # 서버에서 끊겼으면 원장에서도 뺀다
    phone.sync()
    assert phone.phones()["phones"] == []


def test_a_lost_upload_never_reuses_its_sequence_number_and_totals_tell_the_phone_about_cut_cards(server, monkeypatch):
    todo.add("가나메디칼 명세서 확인")
    fp = FakePhone(phone.start()["code"])
    server["phones"] = [_pending(fp, "p1")]
    phone.phones()
    pc_key = server["approved"]["p1"]["pc_key"]
    real = phone._call

    def lost(method, action, body=None):
        if action == "put":
            server["puts"].append(body)              # 서버엔 들어갔는데 답을 못 받았다
            raise account.AccountError("웹에 연결하지 못했습니다.")
        return real(method, action, body)

    monkeypatch.setattr(phone, "_call", lost)
    with pytest.raises(account.AccountError):
        phone.sync()
    monkeypatch.setattr(phone, "_call", real)
    todo.add("다라상사 견적 보내기")
    phone.sync()
    first, second = (fp.open("p1", p["box"], pc_key) for p in server["puts"])
    assert (first["seq"], second["seq"]) == (1, 2) and len(second["cards"]) == 2
    phone.sync()
    assert fp.open("p1", server["puts"][-1]["box"], pc_key)["seq"] == 2   # 내용이 같으면 다시 안 보낸다

    monkeypatch.setattr(phone, "CARDS_MAX", 1)
    todo.add("마바물산 반품 접수")
    phone.sync()
    last = fp.open("p1", server["puts"][-1]["box"], pc_key)
    assert last["seq"] == 3 and len([c for c in last["cards"] if c["state"] == "open"]) == 1 and last["totals"]["open"] == 3
