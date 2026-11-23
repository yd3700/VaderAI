"""실행 일지. "이 행은 왜 빠졌어?" 에 답하는 바닥.

지켜야 하는 것은 두 줄이다.

- **디스크에는 그대로, LLM 에는 숫자만.** `data-map.md` 의 결과 줄이 이미
  정했다 — 산출물 내용은 안 나간다. 품명이 산출물 내용이다
- **일지가 Job 을 멈추지 않는다.** 못 쓰면 거짓을 돌려주고 만다
"""
from tests.private import needs
from datetime import date

import pytest

from vader.core import journal


def _entry(**over):
    base = dict(
        at=journal.now(),
        job_id="j1",
        rune_id="eonm_ipgo",
        level="info",
        message="한 줄",
        fields={},
    )
    base.update(over)
    return journal.Entry(**base)


def test_a_line_comes_back_as_it_went_in(isolated_settings):
    assert journal.record(_entry(message="건너뜁니다", fields={"count": 2}))

    got = journal.read(rune_id="eonm_ipgo")

    assert [(e.message, e.fields) for e in got] == [("건너뜁니다", {"count": 2})]


def test_another_rune_is_not_mixed_in(isolated_settings):
    journal.record(_entry(rune_id="weather"))
    journal.record(_entry(rune_id="eonm_ipgo", message="내 것"))

    assert [e.message for e in journal.read(rune_id="eonm_ipgo")] == ["내 것"]


def test_a_torn_line_does_not_swallow_the_next_one(isolated_settings):
    """죽는 순간에 반쯤 쓰인 줄이 있어도 일지는 계속 읽힌다."""
    journal.record(_entry(message="첫 줄"))
    path = journal._path(date.today())
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"at": "2026-08-08T10:00:00", "job_i')
    journal.record(_entry(message="셋째 줄"))

    assert [e.message for e in journal.read()] == ["첫 줄", "셋째 줄"]


def test_an_unwritable_journal_never_raises(isolated_settings, monkeypatch):
    """일지를 못 쓰는 것이 도는 Job 을 멈추면 안 된다."""
    monkeypatch.setattr(journal, "_path", lambda day: (_ for _ in ()).throw(OSError))

    assert journal.record(_entry()) is False


def test_an_unreadable_journal_is_an_empty_list(isolated_settings, monkeypatch):
    monkeypatch.setattr(journal, "_path", lambda day: (_ for _ in ()).throw(OSError))

    assert journal.read() == []


# --- 나가는 것 -----------------------------------------------------------------


def test_item_names_never_reach_the_model(isolated_settings):
    """품명은 산출물 내용이다. `data-map.md` 결과 줄이 정한 선."""
    journal.record(
        _entry(
            message="보험코드가 없지만 금액이 있어 그대로 진행합니다",
            fields={"count": 2, "names": ["헤파린캡", "스피릿 청진기"]},
        )
    )

    said = journal.explain("eonm_ipgo")

    assert "헤파린캡" not in said
    assert "스피릿 청진기" not in said


def test_the_counts_do_reach_the_model(isolated_settings):
    """숫자를 다 지우면 "왜 빠졌어" 에 답할 수 없다. 지우는 것은 값이다."""
    journal.record(
        _entry(
            level="error",
            message="명세서 합계와 뽑아낸 금액이 다릅니다",
            fields={"page": 3, "stated": 458000, "counted": 339000, "items": 5},
        )
    )

    said = journal.explain("eonm_ipgo")

    assert "명세서 합계와 뽑아낸 금액이 다릅니다" in said
    assert "458000" in said and "339000" in said
    assert "page=3" in said


def test_a_dropped_string_leaves_its_count_behind(isolated_settings):
    """무엇이 빠졌는지는 못 말해도 **몇 개**인지는 말할 수 있어야 한다."""
    journal.record(_entry(fields={"names": ["가", "나", "다"]}))

    assert "names_수=3" in journal.explain()


def test_an_empty_journal_says_so(isolated_settings):
    assert "없습니다" in journal.explain("eonm_ipgo")


def test_only_the_recent_lines_go_out(isolated_settings):
    """긴 배치가 프롬프트를 일지로 채우면 안 된다."""
    for index in range(journal.MAX_LINES + 20):
        journal.record(_entry(message=f"{index}번"))

    said = journal.explain()

    numbers = [line.split("] ")[1] for line in said.splitlines()]

    assert len(numbers) == journal.MAX_LINES
    assert numbers[0] == "20번", "앞에서 잘린다"
    assert numbers[-1] == f"{journal.MAX_LINES + 19}번"


# --- 끝에서 끝까지 --------------------------------------------------------------


def test_a_run_leaves_an_explainable_trail(isolated_settings, tmp_path):
    """룬이 `ctx.log` 로 남긴 판단이 앱을 닫아도 남아야 한다.

    전에는 `JobView.logs` 뿐이었다 — 메모리 500줄, 닫으면 사라짐.
    **어제 실행을 오늘 설명할 수 없었다.**
    """
    import threading
    from queue import Queue

    from vader.core.checkpoint import CheckpointStore
    from vader.core.context import JobContext
    from vader.core.job import Job
    from vader.sdk import CheckpointKey

    ctx = JobContext(
        job=Job(rune_id="eonm_ipgo", inputs=[], workdir=tmp_path, id="j9"),
        events=Queue(),
        cancel=threading.Event(),
        params={},
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        checkpoint_key=CheckpointKey(rune_id="eonm_ipgo", input_identity="i"),
    )

    ctx.log("error", "명세서 합계와 뽑아낸 금액이 다릅니다", page=3, stated=458000)

    assert [(e.rune_id, e.fields["stated"]) for e in journal.read()] == [
        ("eonm_ipgo", 458000)
    ]


def test_the_pet_reads_the_journal_through_its_own_tool(isolated_settings):
    """정보 룬은 일지를 못 읽는다 — `http_get` 과 `log` 밖에 못 받는다.
    그러니 이건 코어의 도구여야 한다. 시계와 같은 자리."""
    from types import SimpleNamespace

    from vader.core import chat, pet as pet_module

    journal.record(_entry(level="error", message="합계가 다릅니다", fields={"page": 3}))
    session = chat.ConversationSession(pet=pet_module.Pet(id="p", name="바다"))
    call = SimpleNamespace(
        id="c1", name="explain_run", arguments={"rune_id": "eonm_ipgo"}
    )

    said = session._run_tool(call)["content"]

    assert "합계가 다릅니다" in said
    assert "page=3" in said


@pytest.mark.parametrize(
    "fields",
    [
        {"item_name": "헤파린캡"},
        {"names": ["헤파린캡"]},
        {"vendor": "대한약품", "count": 2},
        {"detail": {"item": "헤파린캡"}},
        {"page": "3쪽"},
    ],
)
def test_no_string_value_ever_leaves(isolated_settings, fields):
    """한 가지 모양만 막으면 다음 모양으로 샌다.

    `page` 가 문자열로 와도 통과시키지 않는다 — 거기서 예외를 만들면 선이
    무너진다. 숫자여야 할 것이 문자열이면 그건 룬을 고칠 일이다.
    """
    journal.record(_entry(fields=fields))

    said = journal.explain()

    for value in fields.values():
        if isinstance(value, str):
            assert value not in said


@needs("wiki")
def test_the_data_map_says_where_the_journal_lives(isolated_settings):
    """약속을 조용히 바꾸지 않는다. 코드가 바뀌면 지도도 바뀌어야 한다."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "wiki/architecture/data-map.md").read_text(
        encoding="utf-8"
    )

    assert "실행 일지" in text
    assert "journal.explain" in text


def test_run_stars_come_only_from_finished_runs_and_search_lights_only_matches(isolated_settings):
    """우리은하 1단계 — 실행 하나 = 별 하나, 찾은 것만 빛난다. 실패 사유는 화면으로 안 나간다."""
    journal.record(_entry(job_id="a", rune_id="eonm_ipgo", message="중간 판단"))
    journal.record(_entry(job_id="a", rune_id="eonm_ipgo", message="실행이 SUCCEEDED 로 끝났습니다"))
    journal.record(_entry(job_id="b", rune_id="ins_singo", level="error", message="실행이 FAILED 로 끝났습니다: 가나메디칼 거즈 행"))
    runs = journal.runs()
    assert [(r["job_id"], r["state"], r["failed"]) for r in runs] == [("b", "failed", True), ("a", "succeeded", False)]
    assert "가나메디칼" not in str(runs)
    assert journal.search_runs("최근 실패")["job_ids"] == ["b"]
    assert journal.search_runs("오늘 입고", names={"eonm_ipgo": "이온엠 입고"})["job_ids"] == ["a"]
    empty = journal.search_runs("어제 실패")
    assert empty["job_ids"] == [] and empty["checked"] == 2 and not empty["truncated"]
    assert [r["job_id"] for r in journal.search_runs("실패")["runs"]] == ["b"]   # 목록도 같은 결과로
