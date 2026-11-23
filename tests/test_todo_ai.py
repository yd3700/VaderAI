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

    def fake_ask(text, now, open_cards):
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
