"""할 일 카드 — 제목은 잠겨 저장되고, 미루기는 다시 알릴 시각이 있어야 하며, 지난 알림은 한 번만 알린다."""
from datetime import datetime, timedelta

import pytest

pytest.importorskip("cryptography")

from vader.core import config, todo  # noqa: E402


def test_cards_are_sealed_snoozed_with_a_time_and_reminded_once(isolated_settings):
    card = todo.add("가나메디칼 명세서 확인")
    raw = (config.data_dir() / "todo.sqlite3").read_bytes()
    assert "가나메디칼".encode() not in raw                     # 제목은 디스크에 평문으로 없다

    with pytest.raises(todo.TodoError):
        todo.snooze(card["id"], "")                              # 언제 다시 알릴지 없이 미루지 않는다
    with pytest.raises(todo.TodoError):
        todo.snooze(card["id"], (datetime.now() - timedelta(minutes=1)).isoformat())
    with pytest.raises(todo.TodoError):
        todo.snooze(card["id"], "2099-01-01T09:00:00+09:00")    # 시간대가 붙은 값은 받지 않는다

    soon = (datetime.now() + timedelta(minutes=5)).replace(microsecond=0)
    [c] = todo.snooze(card["id"], soon.isoformat())
    assert c["snoozed"] == 1 and c["due"] == soon.date().isoformat() and not c["ringing"]

    assert todo.due(soon - timedelta(minutes=1)) == []
    claim = todo.due(soon + timedelta(hours=8))
    assert claim == [[card["id"], soon.isoformat()]]             # 꺼져 있던 동안 지난 알림도 다음에 잡힌다
    assert todo.due(soon + timedelta(hours=8)) == claim          # 알림을 띄우고 ack 하기 전엔 남는다
    later = (datetime.now() + timedelta(minutes=30)).replace(microsecond=0)
    todo.snooze(card["id"], later.isoformat())                   # 알림을 띄우는 사이 다시 미뤘다
    todo.ack(claim)
    assert todo.due(later + timedelta(minutes=1)) == [[card["id"], later.isoformat()]]   # 옛 ack 가 새 예약을 지우지 않는다
    todo.ack(todo.due(later + timedelta(minutes=1)))
    assert todo.due(later + timedelta(hours=9)) == []            # 한 번만
    assert todo.cards(later + timedelta(minutes=1))[0]["ringing"]

    assert todo.done(card["id"])[0]["state"] == "done"
    assert todo.delete(card["id"]) == []


def test_first_cards_from_many_threads_share_one_key(isolated_settings):
    """처음 여러 요청이 한꺼번에 와도 열쇠는 하나 — 먼저 쓴 제목이 열리지 않게 되지 않는다 (codex1 P1-3)."""
    import threading
    threads = [threading.Thread(target=todo.add, args=(f"할 일 {i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(c["title"] for c in todo.cards()) == sorted(f"할 일 {i}" for i in range(8))


def test_empty_or_long_titles_are_refused(isolated_settings):
    for bad in ("", "  ", "가" * 201):
        with pytest.raises(todo.TodoError):
            todo.add(bad)
    assert todo.due() == []                                      # 카드를 만든 적 없으면 파일도 열쇠도 만들지 않는다
    assert not (config.data_dir() / "todo.sqlite3").exists()


def test_cards_keep_the_order_they_were_dragged_into_and_new_ones_go_last(isolated_settings):
    a = todo.add("가나메디칼 명세서 확인")
    b = todo.add("다라상사 견적 보내기")
    c = todo.add("마바물산 반품 접수")
    assert [x["title"] for x in todo.move([c["id"], a["id"], b["id"]])] == ["마바물산 반품 접수", "가나메디칼 명세서 확인", "다라상사 견적 보내기"]
    todo.add("사자로 견적서 받기")
    assert [x["title"] for x in todo.cards()][-1] == "사자로 견적서 받기"
    assert todo.cards()[0]["rev"] == c["rev"]                       # 순서는 내용이 아니다 — 판 번호 그대로
    with pytest.raises(todo.TodoError):
        todo.move([a["id"], a["id"]])


def test_post_it_drills_down_three_levels_and_deleting_takes_everything_below(isolated_settings):
    """세부 포스트잇 (2026-11-18) — 카드 → 항목 → 항목 → 항목, 4단째는 거절. 내용은 잠가 둔다. 지우면 밑까지."""
    card = todo.add("발주 정리")["id"]
    top = todo.note_body(card, card, "거래처 세 곳 확인")
    assert top["body"] == "거래처 세 곳 확인" and top["depth"] == 0 and top["path"][0]["title"] == "발주 정리"
    a = todo.note_add(card, card, "A 상사")["children"][0]["id"]
    b = todo.note_add(card, a, "단가표")["children"][0]["id"]
    c = todo.note_add(card, b, "3월분")["children"][0]["id"]
    deep = todo.note(card, c)
    assert [p["title"] for p in deep["path"]] == ["발주 정리", "A 상사", "단가표", "3월분"] and not deep["can_open"]
    with pytest.raises(todo.TodoError):
        todo.note_add(card, c, "더 깊이")
    todo.note_body(card, b, "비밀메모")
    raw = (config.data_dir() / "todo.sqlite3").read_bytes()
    assert "비밀메모".encode() not in raw and "단가표".encode() not in raw
    assert todo.note_done(card, b)["children"][0]["done"]
    assert todo.note(card, card)["children"][0]["count"] == 1
    with pytest.raises(todo.TodoError):
        todo.note(card, todo.add("다른 카드")["id"])          # 다른 카드의 자리는 못 연다
    assert todo.note_delete(card, a)["children"] == []
    with pytest.raises(todo.TodoError):
        todo.note(card, c)
    todo.delete(card)
    import sqlite3
    con = sqlite3.connect(config.data_dir() / "todo.sqlite3")
    assert con.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM bodies").fetchone()[0] == 0


def test_a_failed_notice_holds_back_later_notices_to_the_same_person(isolated_settings):
    """공유 → 취소 순서 — 공유 전송이 실패하면 취소도 그 사람에게는 보내지 않는다(다른 사람은 그대로) (codex1 unshare 회신 1)."""
    todo.add("TEST 순서")
    con = todo._db()
    con.execute("DELETE FROM share_out")
    for peer, op in (("p1", "share"), ("p2", "share"), ("p1", "unshare")):
        todo._queue(con, [peer], {"op": op, "id": "s"})
    sent = []

    def send(peer, text):
        if peer == "p1" and '"share"' in text and not sent.count("p1-retry"):
            sent.append("p1-retry")
            raise OSError("down")
        sent.append((peer, "unshare" if '"unshare"' in text else "share"))
    todo.flush_shares(send=send)
    assert ("p1", "unshare") not in sent and ("p2", "share") in sent
    todo.flush_shares(send=send)
    assert [s for s in sent if s != "p1-retry" and s[0] == "p1"] == [("p1", "share"), ("p1", "unshare")]
