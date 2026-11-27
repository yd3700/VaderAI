"""말로 정리 — AI 는 제안만 하고, 코어가 검사하며, 고른 것만 적용된다. 지우기는 제안할 수 없다."""
import json
from datetime import datetime, timedelta

import pytest

pytest.importorskip("cryptography")

from vader.core import todo, todo_ai  # noqa: E402


def test_ai_proposes_checked_changes_and_only_applied_ones_happen(isolated_settings, monkeypatch):
    a = todo.add("가나메디칼 명세서 확인")
    b = todo.add("다라상사 견적 보내기")
    later = (datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    sent = {}

    def fake_ask(text, now, open_cards, mine=None, runes=None):
        sent["titles"] = [c["title"] for c in open_cards]
        return "설명 " + json.dumps({"changes": [
            {"op": "snooze", "ref": "c1", "until": later.strftime("%Y-%m-%dT%H:%M")},
            {"op": "done", "ref": "c2"},
            {"op": "add", "title": "마바물산 전화"},
            {"op": "delete", "ref": "c1"},                       # 지우기는 할 수 없다
            {"op": "done", "ref": "c9"},                         # 없는 카드
            {"op": "snooze", "ref": "c2", "until": "2000-01-01T09:00"},   # 지난 시각
        ], "note": "하나 미루고 하나 끝냈어요"}, ensure_ascii=False)

    monkeypatch.setattr(todo_ai, "_ask", fake_ask)
    plan = todo_ai.propose("명세서는 내일 아침으로, 견적은 끝냈어")
    assert sent["titles"] == ["가나메디칼 명세서 확인", "다라상사 견적 보내기"]
    assert [c["op"] for c in plan["changes"]] == ["snooze", "done", "add"] and plan["skipped"] == 3
    assert {c["id"]: c["state"] for c in todo.cards()} == {a["id"]: "open", b["id"]: "open"}   # 제안만으로는 안 바뀐다

    snooze, finish, new = plan["changes"]
    todo.done(b["id"])                                           # 제안을 본 사이 손으로 바꿨다
    out = todo_ai.apply([snooze, finish, new, {"op": "delete", "card_id": a["id"]}])
    assert [r["ok"] for r in out["results"]] == [True, False, True, False]
    cards = {c["title"]: c for c in out["cards"]}
    assert cards["가나메디칼 명세서 확인"]["remind_at"].startswith(later.strftime("%Y-%m-%dT%H:%M"))
    assert "마바물산 전화" in cards


def test_unreadable_answer_changes_nothing(isolated_settings, monkeypatch):
    todo.add("가나메디칼 명세서 확인")
    monkeypatch.setattr(todo_ai, "_ask", lambda *a: "모르겠어요")
    with pytest.raises(todo_ai.TodoAiError):
        todo_ai.propose("정리해 줘")
    assert len(todo.cards()) == 1


def test_a_proposal_for_an_older_revision_is_not_applied_even_if_the_card_looks_the_same(isolated_settings, monkeypatch):
    a = todo.add("가나메디칼 명세서 확인")
    monkeypatch.setattr(todo_ai, "_ask", lambda *x: json.dumps({"changes": [{"op": "done", "ref": "c1"}]}))
    [finish] = todo_ai.propose("끝냈어")["changes"]
    todo.done(a["id"]); todo.done(a["id"], undo=True)             # 끝냈다 되돌림 — 모양은 같지만 판 번호가 올랐다
    assert todo_ai.apply([finish])["results"] == [{"ok": False, "error": "그 사이 할 일이 바뀌어 적용하지 않았습니다."}]
    assert todo_ai.apply([{**finish, "mark": None}])["results"][0]["ok"] is False   # 판 번호 없이는 적용하지 않는다


def test_ai_can_propose_an_order_and_unlisted_cards_keep_their_place_after(isolated_settings, monkeypatch):
    a = todo.add("가나메디칼 명세서 확인")
    b = todo.add("다라상사 견적 보내기")
    c = todo.add("마바물산 전화")
    monkeypatch.setattr(todo_ai, "_ask", lambda *x: json.dumps({"changes": [{"op": "order", "refs": ["c3", "c1"]}]}))
    [order] = todo_ai.propose("빠른 순으로")["changes"]
    assert order["op"] == "order" and order["titles"] == ["마바물산 전화", "가나메디칼 명세서 확인"]
    out = todo_ai.apply([order])
    assert out["results"] == [{"ok": True}]
    assert [x["id"] for x in out["cards"] if x["state"] == "open"] == [c["id"], a["id"], b["id"]]
    monkeypatch.setattr(todo_ai, "_ask", lambda *x: json.dumps({"changes": [{"op": "order", "refs": ["c1", "c1"]}, {"op": "order", "refs": ["c9"]}]}))
    assert todo_ai.propose("정렬")["skipped"] == 2   # 겹치거나 없는 카드는 제안하지 않는다


def test_a_past_question_is_looked_up_on_this_pc_and_the_records_never_reach_the_ai(isolated_settings, monkeypatch):
    """비서 정리 + 지난 일 묻기 (2026-11-26) — AI 는 찾을 조건만, 날짜·제목·건수는 이 PC 의 완료 기록에서."""
    old = todo.add("가나메디칼 단가표 확인")
    todo.done(old["id"])
    todo.add("다라상사 견적 보내기")
    today = datetime.now().date()
    sent = {}

    def fake_ask(text, now, open_cards, mine=None, runes=None):
        sent["titles"] = [c["title"] for c in open_cards]
        return json.dumps({"changes": [], "note": "단가표를 찾아볼게요",
                           "lookup": {"start": (today - timedelta(days=7)).isoformat(), "end": (today + timedelta(days=30)).isoformat(),
                                      "query": "단가표"}}, ensure_ascii=False)
    monkeypatch.setattr(todo_ai, "_ask", fake_ask)
    plan = todo_ai.propose("지난주에 단가표 확인 언제 했지?")
    assert sent["titles"] == ["다라상사 견적 보내기"]                       # 끝낸 기록은 AI 로 안 간다
    got = plan["lookup"]
    assert got["end"] == today.isoformat()                                  # 오늘보다 뒤는 잘라 낸다
    assert got["total"] == 1 and [i["title"] for i in got["items"]] == ["가나메디칼 단가표 확인"] and got["items"][0]["done_at"]
    monkeypatch.setattr(todo_ai, "_ask", lambda *a: json.dumps({"changes": [], "lookup": {"start": "어제", "end": "오늘"}}))
    assert "error" in todo_ai.propose("언제였지?")["lookup"]


def test_room_cards_from_others_can_only_be_requested_done_and_never_snoozed(isolated_settings, monkeypatch):
    now = datetime.now().replace(microsecond=0)
    con = todo._db()
    con.execute("INSERT INTO cards(id,title,due,created_at,share_id,owner,share_room) VALUES ('r1',?,?,?,'s1','owner','room')",
                (todo._seal("방 카드"), now.date().isoformat(), now.isoformat()))
    card = next(c for c in todo.cards(now) if c["id"] == "r1")
    by_id = {"r1": card}
    done = todo_ai.check({"op": "done", "card_id": "r1", "mark": todo.mark(card)}, by_id, now)
    assert done.get("request") is True
    with pytest.raises(todo_ai.TodoAiError):
        todo_ai.check({"op": "snooze", "card_id": "r1", "mark": todo.mark(card),
                       "until": (now + timedelta(days=1)).isoformat(timespec="minutes")}, by_id, now)


def test_the_named_destination_is_the_provider_that_actually_receives_the_question(isolated_settings, monkeypatch):
    """화면이 보이는 '보낼 AI' 와 실제로 받는 공급자가 같다 — 기본 AI 를 A→B 로 바꾼 뒤에도 (codex1 next3 회신 2 #4)."""
    from vader.core import ai_choice
    from vader.core.ocr.assign import Assignment
    picked, got = {"name": "prov-a"}, []

    class Fake:
        def __init__(self, name):
            self.name = name

        def supports(self, role):
            return True

        def chat(self, messages):
            got.append(self.name)
            return json.dumps({"changes": [], "note": ""})
    monkeypatch.setattr(todo_ai, "resolve", lambda role: Assignment(picked["name"], None, True))
    monkeypatch.setattr(todo_ai, "provider_factory", lambda name: lambda model=None: Fake(name))
    monkeypatch.setattr(ai_choice, "label_for", lambda name: f"label-{name}")
    for name in ("prov-a", "prov-b"):
        picked["name"] = name
        assert todo_ai.target() == {"label": f"label-{name}"}
        todo_ai.propose("정리해줘")
        assert got[-1] == name


def test_the_assistant_can_suggest_a_rune_by_name_only_and_linking_happens_on_press(isolated_settings, monkeypatch):
    """룬 추천 (2026-11-27) — AI 에는 룬 이름만(임시 번호 u1…), id 는 안 간다. 지금 설치된 룬만, 누른 것만 이 PC 에 연결."""
    from vader.core.ocr.assign import Assignment
    card = todo.add("TEST 명세서 정리")
    monkeypatch.setattr(todo_ai, "installed_runes", lambda: {"rune-id-secret-1": "TEST 명세서 룬", "rune-id-2": "TEST 다른 룬"})
    sent = []

    class Fake:
        def supports(self, role):
            return True

        def chat(self, messages):
            sent.append(json.dumps(messages, ensure_ascii=False))
            return json.dumps({"changes": [{"op": "link", "ref": "c1", "rune": "u1"}, {"op": "link", "ref": "c1", "rune": "u9"}], "note": ""})
    monkeypatch.setattr(todo_ai, "resolve", lambda role: Assignment("fake", None, True))
    monkeypatch.setattr(todo_ai, "provider_factory", lambda name: lambda model=None: Fake())
    todo_ai.propose("그냥 정리해줘")
    assert "TEST 명세서 룬" not in sent[0], "[룬 추천] 을 누르지 않으면 룬 이름은 안 간다"
    got = todo_ai.propose("이 카드에 맞는 룬 붙여줘", runes=True)
    assert "TEST 명세서 룬" in sent[1] and "rune-id-secret-1" not in sent[1]
    assert [(c["op"], c["rune_name"]) for c in got["changes"]] == [("link", "TEST 명세서 룬")] and got["skipped"] == 1
    assert todo.rune_links() == {}, "제안만 — 아직 안 바뀜"
    monkeypatch.setattr(todo_ai, "installed_runes", lambda: {"rune-id-2": "TEST 다른 룬"})   # 그 사이 룬을 지웠다
    assert todo_ai.apply(got["changes"])["results"][0]["ok"] is False
    monkeypatch.setattr(todo_ai, "installed_runes", lambda: {"rune-id-secret-1": "TEST 명세서 룬", "rune-id-2": "TEST 다른 룬"})
    todo.link_rune(card["id"], "rune-id-2")                     # 추천을 받은 뒤 사람이 손으로 다른 룬을 연결했다
    assert todo_ai.apply(got["changes"])["results"][0]["ok"] is False
    assert todo.rune_links() == {card["id"]: "rune-id-2"}, "손으로 고른 연결을 옛 추천이 덮지 않는다"
    todo.link_rune(card["id"], "")                              # 다시 풀면(추천 때와 같은 '연결 없음') 적용된다
    r = todo_ai.apply(got["changes"])
    assert r["results"][0]["ok"] and r["runes"] == {card["id"]: "rune-id-secret-1"}


def test_a_rune_suggestion_is_not_applied_to_a_card_finished_in_the_meantime(isolated_settings, monkeypatch):
    """룬 추천 적용은 쓰는 거래 안에서 카드가 열려 있고 판 번호가 같은지도 본다 (codex1 next4 회신 3 #4)."""
    card = todo.add("TEST 끝낼 일")
    monkeypatch.setattr(todo_ai, "installed_runes", lambda: {"rune-a": "TEST 룬"})
    change = todo_ai.check({"op": "link", "card_id": card["id"], "mark": todo.mark(card), "rune_id": "rune-a"},
                           {card["id"]: card}, datetime.now())
    real = todo_ai.check
    monkeypatch.setattr(todo_ai, "check", lambda c, by_id, now: (todo.done(card["id"]), real(c, {card["id"]: card}, now))[1])
    got = todo_ai.apply([change])                          # 검사와 쓰기 사이에 카드가 끝났다
    assert got["results"][0]["ok"] is False and todo.rune_links() == {}
