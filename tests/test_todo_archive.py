"""완료 기록·통계·엑셀·md 가져오기 (2026-11-26)."""
from datetime import date, datetime, timedelta

import pytest

pytest.importorskip("cryptography")

from vader.core import todo, todo_archive  # noqa: E402

MD = """# 구매팀 정리
팀 공용 메모
- [ ]
- [x] 가나메디칼 명세서 확인
  - 단가 대조
    - 3월분
- [ ] 거래처 연락처 정리
- 재고 실사 보고
  메모 한 줄

## 회의
회의록은 따로 보관
"""


def _done_on(title, day, created=None):
    card = todo.add(title)
    todo.done(card["id"])
    con = todo._db()
    con.execute("UPDATE cards SET done_at=?, created_at=? WHERE id=?",
                (f"{day}T10:00:00", f"{created or day}T09:00:00", card["id"]))
    return card["id"]


def test_archive_finds_past_done_cards_by_period_and_title_with_stats(isolated_settings):
    _done_on("보고서 제출", "2026-11-02", "2026-10-30")
    _done_on("명세서 확인", "2026-11-10")
    _done_on("오래된 일", "2026-01-05")
    todo.add("아직 열린 일")
    got = todo_archive.archive("2026-11-01", "2026-11-30")
    assert [c["title"] for c in got["items"]] == ["명세서 확인", "보고서 제출"]
    assert got["stats"]["done"] == 2 and got["stats"]["avg_days"] == 1.5
    assert sum(w["n"] for w in got["stats"]["weeks"]) == 2 and got["stats"]["months"] == [{"month": "2026-11", "n": 2}]
    assert [c["title"] for c in todo_archive.archive("2026-11-01", "2026-11-30", "명세서")["items"]] == ["명세서 확인"]
    with pytest.raises(todo.TodoError):
        todo_archive.archive("2026-11-30", "2026-11-01")


def test_markdown_items_become_cards_once_with_sub_items_and_without_dates(isolated_settings, tmp_path):
    src = tmp_path / "정리.md"
    src.write_text(MD, encoding="utf-8")
    pre = todo_archive.import_preview(src)
    titles = [(x["title"], x["state"], x["children"]) for x in pre["items"]]
    assert titles == [("구매팀 정리", None, 0), ("가나메디칼 명세서 확인", "done", 2), ("거래처 연락처 정리", "open", 0),
                      ("재고 실사 보고", None, 0), ("회의", None, 0)]   # 제목 아래 글도 사라지지 않는다, '- [ ]' 빈 줄은 건이 아니다
    assert todo.cards() == []                                     # 미리보기는 아무것도 저장하지 않는다

    assert todo_archive.import_apply(pre["token"], [1, 2, 3, 4], plain="done") == {"added": 4, "skipped": 0}
    assert [c["title"] for c in todo.cards()] == ["거래처 연락처 정리"]   # 열린 것만 오늘 목록에
    kept = todo_archive.archive("2000-01-01", "2099-12-31", imported=True)
    assert {c["title"] for c in kept["items"]} == {"가나메디칼 명세서 확인", "재고 실사 보고", "회의"}
    assert todo_archive.archive("2000-01-01", "2099-12-31")["stats"]["done"] == 0   # 날짜를 믿지 않는다 — 통계에 없다
    first = next(c for c in kept["items"] if c["title"] == "가나메디칼 명세서 확인")
    note = todo.note(first["id"])
    assert note["body"] == "[분류] 구매팀 정리" and note["children"][0]["title"] == "단가 대조"
    assert todo.note(first["id"], note["children"][0]["id"])["children"][0]["title"] == "3월분"

    again = todo_archive.import_preview(src)
    assert again["counts"]["already"] == 4
    assert todo_archive.import_apply(again["token"], [1], "done") == {"added": 0, "skipped": 1}   # 같은 파일 같은 줄은 한 번만


def test_a_file_changed_after_preview_is_not_imported(isolated_settings, tmp_path):
    src = tmp_path / "a.md"
    src.write_text("- 하나\n", encoding="utf-8")
    pre = todo_archive.import_preview(src)
    src.write_text("- 하나\n- 둘\n", encoding="utf-8")
    with pytest.raises(todo.TodoError, match="바뀌었어요"):
        todo_archive.import_apply(pre["token"], [0])
    with pytest.raises(todo.TodoError):
        todo_archive.import_apply("없는표", [0])


def test_export_writes_text_cells_that_are_never_formulas_with_notes_on_their_own_rows(isolated_settings, tmp_path):
    from openpyxl import load_workbook
    cid = _done_on("=HYPERLINK(\"http://x\")", date.today().isoformat())
    _done_on("다른 일", date.today().isoformat())
    todo.note_body(cid, cid, "세부")
    for n in range(9):                                            # 4000자 메모 아홉 개 — 한 칸(32767자)에 몰지 않는다
        kid = next(k["id"] for k in todo.note_add(cid, cid, f"항목 {n}")["children"] if k["title"] == f"항목 {n}")
        todo.note_body(cid, kid, "가" * 4000)
    span = ((date.today() - timedelta(days=1)).isoformat(), date.today().isoformat())
    out = todo_archive.export(tmp_path / "out.xlsx", *span, query="HYPER")   # 보이는 검색 그대로
    assert out["rows"] == 1 and out["notes"] == 10
    book = load_workbook(tmp_path / "out.xlsx")
    cell = book["완료 기록"]["C2"]
    assert cell.data_type == "s" and cell.value.startswith("=HYPERLINK") and book["완료 기록"].max_row == 2
    notes = book["세부 내용"]
    assert notes["E2"].value == "세부" and all(len(notes.cell(r, 5).value) == 4000 for r in range(3, 12))


def test_preview_shows_what_will_not_fit_and_saves_the_same_shape(isolated_settings, tmp_path):
    src = tmp_path / "big.md"
    lines = ["- 큰 일"] + [f"  - 하위 {n}" for n in range(52)] + ["- 깊은 일", "  - 깊이1", "    - 깊이2", "      - 깊이3", "        - 깊이4"]
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    big, deep = todo_archive.import_preview(src)["items"]
    assert (big["children"], big["lost_items"]) == (50, 2)         # 한 자리 50개까지 — 51·52번째는 빠진다고 미리 보인다
    assert (deep["children"], deep["lost_items"]) == (3, 1)        # 3단까지 — 4단은 빠진다
    pre = todo_archive.import_preview(src)
    todo_archive.import_apply(pre["token"], [0, 1])
    cards = {c["title"]: c for c in todo_archive.archive("2000-01-01", "2099-12-31", imported=True)["items"]}
    assert len(todo.note(cards["큰 일"]["id"])["children"]) == 50


def test_a_closed_preview_cannot_be_applied(isolated_settings, tmp_path):
    src = tmp_path / "a.md"
    src.write_text("- 하나\n", encoding="utf-8")
    pre = todo_archive.import_preview(src)
    todo_archive.drop_pending()                                  # 창을 닫거나 잠갔다
    with pytest.raises(todo.TodoError, match="미리보기가 끝났어요"):
        todo_archive.import_apply(pre["token"], [0])


def test_a_long_sub_title_keeps_its_full_text_and_imported_search_has_no_period_limit(isolated_settings, tmp_path, monkeypatch):
    src = tmp_path / "long.md"
    long = "가" * 240
    src.write_text("- 위\n  - " + long + "\n- 아래\n", encoding="utf-8")
    pre = todo_archive.import_preview(src)
    assert pre["items"][0]["lost_items"] == 0 and pre["items"][0]["lost_body"] == 0
    todo_archive.import_apply(pre["token"], [0, 1])
    cards = {c["title"]: c for c in todo_archive.archive("2000-01-01", "2099-12-31", imported=True)["items"]}
    kid = todo.note(cards["위"]["id"])["children"][0]
    assert len(kid["title"]) == 200 and todo.note(cards["위"]["id"], kid["id"])["body"] == long   # 잘린 제목의 원문은 그 항목 안에
    monkeypatch.setattr(todo_archive, "SCAN_MAX", 1)
    got = todo_archive.archive("2000-01-01", "2099-12-31", "아래", imported=True)   # 가져온 기록은 기간으로 못 좁힌다 — 상한 없이 찾는다
    assert [c["title"] for c in got["items"]] == ["아래"] and not got["limited"]


def test_a_sub_title_longer_than_a_note_is_cut_to_the_note_limit_and_counted(isolated_settings, tmp_path):
    src = tmp_path / "huge.md"
    src.write_text("- 위\n  - " + "가" * 33000 + "\n", encoding="utf-8")
    pre = todo_archive.import_preview(src)
    assert pre["items"][0]["lost_body"] == 33000 - todo.BODY_MAX          # 엑셀 셀(32,767)에서 몰래 잘리지 않고 미리 보인다
    todo_archive.import_apply(pre["token"], [0])
    card = todo_archive.archive("2000-01-01", "2099-12-31", imported=True)["items"][0]
    kid = todo.note(card["id"])["children"][0]
    assert len(todo.note(card["id"], kid["id"])["body"]) == todo.BODY_MAX
