"""
화면 상태 테스트.

창을 띄우지 않는다. `ui.py` 는 여기서 만든 상태를 위젯에 옮기기만 하므로,
틀릴 수 있는 것은 전부 여기 있다 — 진행률 계산, 취소 버튼 활성 조건,
취소 요청 중이라는 사실을 사용자에게 어떻게 말하는가.
"""
import types

import pytest

from pathlib import Path

from vader.core.events import Finished, LogLine, Progress, StateChanged, UserActionRequested
from vader.core.job import JobState
from vader.core.ocr import Assignment, Candidate, OcrProviderUnavailable
from vader.core.ui import AssignmentRow, assignment_rows
from vader.core.view import checkpoint_key_for, is_resumable, MAX_LOGS, FieldError, JobView, apply, apply_all, coerce_field
from vader.sdk import RuneField, UserActionRequest

JOB = "job-1"


class TestAssignmentRows:

    def test_automatic_rows_keep_candidate_numbers_and_block_reason(self):
        looked = {
            "infer": [
                Candidate("claude", 1400, 1400, "오늘 한도를 다 썼습니다 (1400/1400)"),
                Candidate("gemini", 412, 1400, None),
            ]
        }

        rows = assignment_rows(
            ["infer"],
            candidate_lookup=lambda role: looked[role],
            resolver=lambda role: Assignment("gemini", None, True),
        )

        assert rows == [
            AssignmentRow(
                "infer", "claude", "1400 / 1400", "오늘 한도를 다 썼습니다 (1400/1400)"
            ),
            AssignmentRow("", "gemini", "412 / 1400", "사용 중 (자동)"),
        ]

    def test_manual_role_uses_resolve_without_inventing_usage(self):
        rows = assignment_rows(
            ["extract"],
            candidate_lookup=lambda role: [],
            resolver=lambda role: Assignment("gemini", "flash", False),
        )

        assert [(r.role, r.provider, r.usage, r.status) for r in rows] == [
            ("extract", "gemini", "—", "사용 중")
        ]

    def test_all_blocked_candidates_are_still_visible(self):
        looked = [
            Candidate("claude", 2, 2, "오늘 한도를 다 썼습니다 (2/2)"),
            Candidate("gemini", 3, 3, "패키지나 API 키가 없습니다"),
        ]

        def unavailable(_role):
            raise OcrProviderUnavailable("전부 못 씀")

        rows = assignment_rows(
            ["infer"], candidate_lookup=lambda role: looked, resolver=unavailable
        )

        assert [r.provider for r in rows] == ["claude", "gemini"]
        assert [r.status for r in rows] == [c.blocked for c in looked]


class TestProgress:

    def test_percent_from_current_and_total(self):
        view = JobView()
        apply(view, Progress(JOB, 1, 4, "1단계"))

        assert view.percent == 25
        assert view.message == "1단계"

    def test_unknown_total_has_no_percent(self):
        """total 이 None 이면 개수를 모르는 작업이다. 불확정으로 그려야 한다."""
        view = JobView()
        apply(view, Progress(JOB, 3, None))

        assert view.percent is None

    def test_zero_total_does_not_divide_by_zero(self):
        view = JobView()
        apply(view, Progress(JOB, 0, 0))

        assert view.percent is None

    def test_percent_never_exceeds_100(self):
        view = JobView()
        apply(view, Progress(JOB, 7, 4))

        assert view.percent == 100


class TestCancelButton:

    def test_off_before_anything_starts(self):
        assert JobView().can_cancel is False

    def test_on_while_running(self):
        view = JobView()
        apply(view, StateChanged(JOB, JobState.RUNNING))

        assert view.can_cancel is True

    def test_on_while_waiting_for_the_user(self):
        """응답 대기 중에도 취소는 통해야 한다."""
        view = JobView()
        apply(view, StateChanged(JOB, JobState.WAITING_USER))

        assert view.can_cancel is True

    def test_off_once_terminal(self):
        for terminal in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
            view = JobView()
            apply(view, StateChanged(JOB, terminal))

            assert view.can_cancel is False
            assert view.is_done is True


class TestStatusText:

    def test_cancel_requested_explains_why_it_is_still_running(self):
        """취소를 눌러도 즉시 멈추지 않는다. 왜인지 말해줘야 한다."""
        view = JobView()
        apply(view, StateChanged(JOB, JobState.CANCEL_REQUESTED))

        assert "현재 항목 완료 후 중단" in view.status_text

    def test_waiting_user_says_so(self):
        view = JobView()
        apply(view, StateChanged(JOB, JobState.WAITING_USER))

        assert view.status_text == "사용자 입력 대기"

    def test_failure_reason_is_shown(self):
        view = JobView(failure="파일을 읽을 수 없습니다")
        apply(view, StateChanged(JOB, JobState.FAILED))

        assert "파일을 읽을 수 없습니다" in view.status_text

    def test_running_shows_the_progress_message(self):
        view = JobView()
        apply_all(view, [StateChanged(JOB, JobState.RUNNING), Progress(JOB, 1, 2, "2단계")])

        assert view.status_text == "2단계"


class TestLogs:

    def test_level_is_kept(self):
        view = JobView()
        apply(view, LogLine(JOB, "error", "터졌다"))

        assert view.logs == ["[error] 터졌다"]

    def test_logs_are_capped(self):
        """긴 배치에서 메모리가 늘지 않아야 한다."""
        view = JobView()
        apply_all(view, [LogLine(JOB, "info", str(i)) for i in range(MAX_LOGS + 50)])

        assert len(view.logs) == MAX_LOGS
        assert view.logs[-1] == f"[info] {MAX_LOGS + 49}"


class TestUnknownEvents:

    def test_are_ignored(self):
        """모르는 이벤트가 화면을 깨뜨리면 안 된다."""
        view = JobView()
        apply(view, UserActionRequested(JOB, "corr-1", UserActionRequest(kind="confirm", prompt="?")))

        assert view.state is None
        assert view.logs == []


class TestFinished:

    def test_sets_the_final_state(self):
        view = JobView()
        apply_all(view, [StateChanged(JOB, JobState.RUNNING), Finished(JOB, JobState.SUCCEEDED)])

        assert view.state is JobState.SUCCEEDED
        assert view.is_done is True


class TestCoerceField:
    """마법사는 어떤 위젯이든 문자열을 준다. kind 가 그걸 무엇으로 읽을지 정한다."""

    def test_number_becomes_int(self):
        assert coerce_field(RuneField(name="선납", kind="number"), " 1 ") == 1

    def test_number_rejects_non_numeric_with_a_readable_message(self):
        with pytest.raises(FieldError, match="숫자"):
            coerce_field(RuneField(name="선납", kind="number"), "하나")

    def test_text_is_stripped(self):
        assert coerce_field(RuneField(name="구입처"), "  가나상사  ") == "가나상사"

    def test_blank_is_rejected(self):
        """빈 값을 저장하면 룬의 필수값 검사가 통과해 버린다."""
        with pytest.raises(FieldError, match="값이 필요"):
            coerce_field(RuneField(name="구입처"), "   ")

    def test_choice_must_be_one_of_the_options(self):
        field = RuneField(name="양도양수여부", kind="choice", options=["N", "Y"])

        assert coerce_field(field, "Y") == "Y"
        with pytest.raises(FieldError, match="N, Y"):
            coerce_field(field, "Z")

    def test_label_is_used_in_the_message_when_present(self):
        field = RuneField(name="vendor", kind="number", label="구입처 코드")

        with pytest.raises(FieldError, match="구입처 코드"):
            coerce_field(field, "x")

    def test_unknown_kind_is_treated_as_text(self):
        """룬이 오타를 냈다고 설치를 막지 않는다. 텍스트로 받는다."""
        assert coerce_field(RuneField(name="x", kind="바나나"), "값") == "값"

    def test_path_must_point_at_a_file_that_exists(self, tmp_path):
        field = RuneField(name="product_db", kind="path", label="상품 DB")
        csv = tmp_path / "product_db.csv"
        csv.write_text("code,price\n", encoding="utf-8")

        assert coerce_field(field, f" {csv} ") == str(csv)
        with pytest.raises(FieldError, match="찾을 수 없습니다"):
            coerce_field(field, str(tmp_path / "없는파일.csv"))

    def test_path_rejects_a_directory(self, tmp_path):
        """폴더를 저장하면 룬이 열다가 실패한다. 파일만 받는다."""
        with pytest.raises(FieldError, match="찾을 수 없습니다"):
            coerce_field(RuneField(name="db", kind="path"), str(tmp_path))

    def test_dir_must_point_at_a_folder_that_exists(self, tmp_path):
        """룬이 사용자 파일을 여기로 옮긴다. 오타를 저장하면 안 된다."""
        field = RuneField(name="done_dir", kind="dir", label="완료 폴더")

        assert coerce_field(field, f" {tmp_path} ") == str(tmp_path)
        with pytest.raises(FieldError, match="폴더를 찾을 수 없습니다"):
            coerce_field(field, str(tmp_path / "없는폴더"))

    def test_dir_rejects_a_file(self, tmp_path):
        f = tmp_path / "a.csv"
        f.write_text("x", encoding="utf-8")
        with pytest.raises(FieldError, match="폴더를 찾을 수 없습니다"):
            coerce_field(RuneField(name="d", kind="dir"), str(f))


def test_window_title_with_a_timestamp_is_refused():
    """창 목록에서 고른 제목을 그대로 저장하면 기동 시각이 같이 박힌다.

    드라이버가 제목 전체를 `title_re=".*{...}.*"` 로 넣으므로 대상 앱을 다시
    띄우는 순간 못 찾는다. **오늘은 되고 내일은 안 되는 설정**이라 저장 시점에
    막는다. 실제로 이온엠 창 제목이 이랬다.
    """
    field = RuneField(name="window_title", kind="window", label="창 제목")
    real = (
        "이영돈 [입고/반납등록(임의)]   Version=1.0.0.6 - "
        "의료법인) 가나병원 (10000001) Version = 20260211 오전 11:15:31"
    )

    with pytest.raises(FieldError, match="날짜나 시각"):
        coerce_field(field, real)

    assert coerce_field(field, "입고/반납등록") == "입고/반납등록"


def test_a_stable_number_in_the_title_is_not_mistaken_for_a_timestamp():
    """기관코드 `(10000001)` 는 8자리지만 변하지 않는다. 막으면 오탐이다."""
    field = RuneField(name="window_title", kind="window")

    assert coerce_field(field, "가나병원 (10000001)") == "가나병원 (10000001)"


def test_an_empty_checkpoint_is_not_resumable():
    """성공한 실행 뒤에도 "이어서 실행합니다" 가 뜨던 버그.

    룬은 파일을 끝내면 진행 목록에서 빼고 남은 것을 저장한다. 마지막 파일이
    끝나면 `{}` 가 저장되는데, UI 가 `is not None` 으로 보는 바람에 이어받을 게
    없는데도 이어받는다고 안내했다. 실제 운영 실행 뒤 디스크에서 확인했다.
    """
    assert not is_resumable({})
    assert not is_resumable(None)
    assert is_resumable({"scan.pdf": ["Page1_2026-07-03"]})


# ── 2026-08-05 연속 실행 중단 회귀 ──────────────────────────────

def test_a_consumed_input_has_no_checkpoint_key(tmp_path):
    """T1. 성공한 실행은 입력을 `done_dir` 로 옮긴다.

    완료 직후 같은 경로로 키를 다시 만들면 `FileNotFoundError` 다. 그 예외가
    UI 폴링을 통째로 죽여서 다음 두 작업의 로그와 버튼이 멎었다.
    """
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"x" * 100)
    done = tmp_path / "done"
    done.mkdir()

    assert checkpoint_key_for("eonm_ipgo", [scan]) is not None
    scan.rename(done / "scan_20260805.pdf")          # rune._move 와 같은 동작
    assert checkpoint_key_for("eonm_ipgo", [scan]) is None


def test_an_existing_input_still_gets_a_key(tmp_path):
    """T2. 과잉 차단을 막는다. 이걸 놓치면 이어하기가 통째로 죽는다."""
    scan = tmp_path / "scan.pdf"
    scan.write_bytes(b"x" * 100)

    key = checkpoint_key_for("eonm_ipgo", [scan])

    assert key is not None
    assert key.rune_id == "eonm_ipgo"
    # 키는 **내용**으로 만든다. 같은 내용이면 경로가 달라도 같은 자리다.
    twin = tmp_path / "다른이름.pdf"
    twin.write_bytes(b"x" * 100)
    assert checkpoint_key_for("eonm_ipgo", [twin]).input_identity == key.input_identity


def test_no_input_means_no_key(tmp_path):
    assert checkpoint_key_for("eonm_ipgo", []) is None


def test_the_pump_reschedules_even_when_the_tick_raises():
    """T4. 재예약이 본문 맨 끝에 있으면 예외 한 번에 폴링이 영구히 멎는다.

    Tk 없이 검사한다 — `_pump` 는 평범한 함수라 self 를 대신 넘길 수 있다.
    """
    from vader.core import ui

    scheduled = []
    boom = types.SimpleNamespace(
        root=types.SimpleNamespace(after=lambda ms, fn: scheduled.append(ms)),
        _pump_once=lambda: (_ for _ in ()).throw(FileNotFoundError("사라진 입력")),
        _pump=None,   # 재예약이 넘기는 콜백. 값은 보지 않는다
    )

    with pytest.raises(FileNotFoundError):
        ui.App._pump(boom)

    assert scheduled == [ui.POLL_MS], "예외가 나도 다음 틱을 예약해야 한다"


def test_the_pump_reschedules_on_the_happy_path():
    from vader.core import ui

    scheduled = []
    calm = types.SimpleNamespace(
        root=types.SimpleNamespace(after=lambda ms, fn: scheduled.append(ms)),
        _pump_once=lambda: None,
        _pump=None,
    )

    ui.App._pump(calm)

    assert scheduled == [ui.POLL_MS]


def test_buttons_and_status_recover_after_a_job_finishes():
    """T6. 실행 중에는 취소만, 끝나면 실행만. 그 전이가 화면 상태로 결정된다."""
    view = JobView()
    assert not view.can_cancel and not view.is_done      # 대기

    apply(view, StateChanged("j", JobState.RUNNING))
    assert view.can_cancel and not view.is_done          # 실행 중

    apply(view, Finished("j", JobState.SUCCEEDED))
    assert view.is_done and not view.can_cancel          # 완료 → 실행 버튼 복구
    assert view.status_text == "완료"


def test_the_waiting_provider_is_not_labelled_the_one_in_use():
    """화면은 "왜 이걸 쓰고 있나" 에 답해야 한다. 라벨이 뒤집히면 거꾸로 답한다."""
    looked = [Candidate("gemini", 0, 1400, None), Candidate("claude", 0, 1400, None)]

    rows = assignment_rows(
        ["extract"],
        candidate_lookup=lambda role: looked,
        resolver=lambda role: Assignment("gemini", None, True),
    )

    assert [(r.provider, r.status) for r in rows] == [
        ("gemini", "사용 중 (자동)"),
        ("claude", "대기"),
    ]


def test_a_secret_field_is_masked_in_the_setup_screen():
    """API 키와 토큰이다. 설정 화면은 어깨 너머로도 보인다."""
    import tkinter as tk
    from vader.core.ui import SetupDialog
    from vader.sdk import RuneField

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("화면 없는 환경")
    root.withdraw()
    try:
        var = tk.StringVar()
        secret = SetupDialog._widget(root, RuneField(name="k", kind="secret"), var)
        plain = SetupDialog._widget(root, RuneField(name="t", kind="text"), var)

        assert secret.cget("show") == "•"
        assert plain.cget("show") == ""
    finally:
        root.destroy()


def test_every_rune_declares_its_secrets_as_secrets():
    """비밀값을 text 로 두면 화면에 그대로 뜬다. 빠뜨리기 쉬운 자리다."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "rune-packages"
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "RuneField"):
                continue
            args = {kw.arg: kw.value for kw in node.keywords}
            name = getattr(args.get("name"), "value", "")
            kind = getattr(args.get("kind"), "value", "text")
            if any(word in name for word in ("key", "token", "secret", "password")):
                if kind != "secret":
                    offenders.append(f"{path.name}: {name} (kind={kind})")

    assert offenders == [], "비밀값이 가려지지 않습니다:\n  " + "\n  ".join(offenders)


def test_an_optional_field_may_be_left_empty():
    """비워 둬도 되는 설정이 있다. **빈 값이 곧 "그 기능을 안 쓴다" 다.**

    이온엠의 `exe_path` 가 그렇다 — 넣으면 프로그램을 대신 띄우고, 비우면
    사람이 띄운 창에 붙는다. 없다고 멀쩡한 입고를 막으면 안 된다.
    """
    from vader.sdk import RuneField

    field = RuneField(name="exe_path", kind="path", label="실행 파일", optional=True)

    assert coerce_field(field, "   ") == ""


def test_a_normal_field_still_demands_a_value():
    """`optional` 이 기본으로 켜지면 설정 검사가 통째로 무의미해진다."""
    from vader.sdk import RuneField

    field = RuneField(name="master_db", kind="path", label="품목 DB")

    with pytest.raises(FieldError, match="값이 필요합니다"):
        coerce_field(field, "")


def test_a_log_line_carries_its_fields_to_the_screen():
    """**여기가 비어 있어서 진단이 통째로 안 보였다.**

    룬이 `titles=[...]` 로 무엇을 봤는지 남겼는데 화면은 메시지만 그렸다.
    실측에서 마스터가 줄 수 있는 것은 "하나로 좁히지 못했습니다" 한 줄뿐이었고,
    그 줄로는 아무도 고칠 수 없다.
    """
    from vader.core.events import LogLine

    view = JobView()

    apply(view, LogLine("j1", "error", "본 창을 찾습니다", {"titles": ["가", "나"]}))

    assert "본 창을 찾습니다" in view.logs[0]
    assert "titles=" in view.logs[0]
    assert "가" in view.logs[0]


def test_a_log_line_without_fields_stays_clean():
    """대부분의 줄에는 필드가 없다. 거기에 구분자만 붙으면 지저분해진다."""
    from vader.core.events import LogLine

    view = JobView()

    apply(view, LogLine("j1", "info", "로그인했습니다", {}))

    assert view.logs[0] == "[info] 로그인했습니다"


def test_a_long_item_is_shortened_but_the_list_is_not_cut():
    """전에는 값 **전체**를 잘랐다. 그래서 창 목록이 다섯 개쯤에서 끊겼고
    **답이 그 뒤에 있었다** — 마스터 로그에서 이온엠 본 창이 잘려 나갔다.
    """
    from vader.core.events import LogLine

    view = JobView()

    apply(view, LogLine("j1", "info", "봤다", {"titles": ["창" * 500] + ["끝창"] * 20}))

    assert "…" in view.logs[0], "긴 항목은 줄인다"
    assert view.logs[0].count("끝창") == 20, "목록은 다 보여준다"
