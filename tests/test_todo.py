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


def test_a_linked_rune_stays_on_this_pc_and_never_rides_cards_shares_or_exports(isolated_settings, monkeypatch, tmp_path):
    """카드 ↔ 룬 (2026-11-27) — 연결은 따로 된 표에만. 카드 사전(폰·AI 가 쓰는 것)·공유 글·완료 기록 엑셀에 안 실린다 (codex1 next3 ③)."""
    from vader.core import todo_archive
    rune = "rune-test-zq9k7"
    card = todo.add("TEST 명세서 확인")
    assert todo.link_rune(card["id"], rune) == {card["id"]: rune}
    assert rune not in repr(todo.cards()) and all("rune" not in c for c in todo.cards())
    sent = []
    monkeypatch.setattr(todo, "_queue", lambda con, to, payload: sent.append(payload))
    monkeypatch.setattr(todo, "flush_shares", lambda: None)
    todo.share(card["id"], ["friend-1"])
    assert sent and rune not in repr(sent)
    todo.done(card["id"])
    day = datetime.now().date().isoformat()
    got = todo_archive.archive(day, day)
    assert got["total"] == 1 and rune not in repr(got)
    out = todo_archive.export(tmp_path / "x.xlsx", day, day)
    import zipfile
    with zipfile.ZipFile(out["path"]) as z:
        assert not any(rune.encode() in z.read(n) for n in z.namelist())
    assert todo.rune_links() == {card["id"]: rune}, "다시 열어도 이 PC 에서는 그대로"
    assert todo.link_rune(card["id"], "") == {}
    todo.link_rune(card["id"], rune)
    todo.delete(card["id"])
    assert todo.rune_links() == {}, "카드를 지우면 연결도"
    with pytest.raises(todo.TodoError):
        todo.link_rune("없는-카드", rune)


def test_a_withdrawn_shared_card_takes_its_local_rune_link_with_it(isolated_settings):
    """받은 카드에 룬을 연결한 뒤 주인이 공유를 거두면 — 연결도 같은 거래로 지운다 (codex1 next3 회신 2 #5)."""
    import json
    assert todo.receive_share("owner-1", todo.SHARE_MARK + json.dumps({"op": "share", "id": "s1", "title": "TEST 받은 일", "due": "2026-11-27"}))
    got = next(c for c in todo.cards() if c["owner"] == "owner-1")
    todo.link_rune(got["id"], "rune-test")
    assert todo.receive_share("owner-1", todo.SHARE_MARK + json.dumps({"op": "unshare", "id": "s1"}))
    assert todo.rune_links() == {}
    assert todo._db().execute("SELECT count(*) FROM card_runes").fetchone()[0] == 0


def test_rune_opens_are_counted_on_this_pc_and_shown_as_numbers_in_the_archive(isolated_settings):
    """룬 연결 효과 재기 (2026-11-27) — 연결된 카드의 [룬 열기] 만 세고, 완료 기록 통계에는 숫자만."""
    from vader.core import todo_archive
    linked, plain = todo.add("TEST 연결된 일"), todo.add("TEST 그냥 일")
    todo.link_rune(linked["id"], "rune-test-k3")
    assert todo.rune_opened(linked["id"], "rune-test-k3") and todo.rune_opened(linked["id"], "rune-test-k3")
    assert not todo.rune_opened(plain["id"], "rune-test-k3"), "연결 없는 카드는 세지 않는다"
    assert not todo.rune_opened(linked["id"], "다른-룬"), "화면이 연 룬과 지금 연결이 다르면 안 센다"
    todo.done(linked["id"])
    todo.done(plain["id"])
    todo.link_rune(linked["id"], "")                       # 끝낸 뒤 연결을 풀어도 지난 숫자는 그대로
    day = datetime.now().date().isoformat()
    stats = todo_archive.archive(day, day)["stats"]
    assert stats["rune_opens"] == 2 and stats["rune_done"] == 1
    later = todo.add("TEST 나중에 연결")
    todo.done(later["id"])
    todo.link_rune(later["id"], "rune-test-k3")            # 끝낸 뒤 연결해도 늘지 않는다
    assert todo_archive.archive(day, day)["stats"]["rune_done"] == 1
    todo.done(later["id"], undo=True)                      # 센 적 없는 끝냄을 되돌려도 숫자는 그대로
    assert todo_archive.archive(day, day)["stats"]["rune_done"] == 1
    todo.done(later["id"])                                 # 이제 연결된 채 끝냄 → 2, 되돌리면 → 1
    assert todo_archive.archive(day, day)["stats"]["rune_done"] == 2
    todo.done(later["id"], undo=True)
    assert todo_archive.archive(day, day)["stats"]["rune_done"] == 1
    assert "rune-test-k3" not in repr(todo._db().execute("SELECT * FROM rune_daily").fetchall())
