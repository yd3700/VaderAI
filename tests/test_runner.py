"""
JobRunner 통합 테스트.

Milestone 1이 요구하는 경로를 전부 지나간다: 성공, 실패, 사용자 대기와 응답,
취소, 체크포인트 왕복. 여기 있는 룬들은 인위적인 합성물이고, 진짜 `_probe`는
설치된 룬으로 같은 경로를 다시 지나야 한다(TODO.md Milestone 1 참조).

**타이밍에 의존하지 않는다.** sleep으로 기다리는 테스트는 느린 기계에서
간헐적으로 깨진다. 대신 Event로 서로 신호를 주고받는다.
"""
import threading

import pytest

from vader.core.events import Finished, LogLine, Progress, StateChanged, UserActionRequested
from vader.core.job import JobState
from vader.core.runner import JobRunner
from vader.sdk import (
    CheckpointKey,
    Input,
    RuneManifest,
    RuneResult,
    UserActionRequest,
    UserActionResponse,
)

TIMEOUT = 5.0


class FakeStore:
    """메모리 체크포인트 저장소. 진짜 구현은 Codex가 만든다."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], tuple[dict, str]] = {}

    def load(self, key: CheckpointKey, *, rune_version: str = "") -> dict | None:
        stored = self.data.get((key.rune_id, key.input_identity))
        if stored is None:
            return None
        payload, version = stored
        if rune_version and version and version != rune_version:
            return None
        return payload

    def save(self, key: CheckpointKey, data: dict, *, rune_version: str = "") -> None:
        self.data[(key.rune_id, key.input_identity)] = (data, rune_version)

    def clear(self, key: CheckpointKey) -> None:
        self.data.pop((key.rune_id, key.input_identity), None)


def rune_from(fn, rune_id: str = "_probe"):
    """run()이 fn인 룬 하나를 만든다."""

    class _R:
        manifest = RuneManifest(id=rune_id, name="테스트용")

        def run(self, ctx, inputs):
            return fn(ctx, inputs)

    return _R()


@pytest.fixture
def runner(tmp_path):
    return JobRunner(checkpoints=FakeStore(), data_dir=tmp_path)


def events_of(runner, kind):
    return [e for e in runner.drain_events() if isinstance(e, kind)]


# ── 성공 ────────────────────────────────────────────────────────

class TestSuccess:

    def test_runs_to_completion(self, runner):
        job = runner.submit(rune_from(lambda ctx, i: RuneResult(ok=True)), [])
        assert runner.join(job.id, TIMEOUT)
        assert job.state is JobState.SUCCEEDED

    def test_gives_the_rune_a_private_workdir(self, runner):
        seen = {}

        def run(ctx, inputs):
            seen["dir"] = ctx.workdir
            return RuneResult(ok=True)

        job = runner.submit(rune_from(run), [])
        runner.join(job.id, TIMEOUT)

        assert seen["dir"].exists()
        assert job.id in str(seen["dir"])

    def test_passes_params_through_untouched(self, runner):
        seen = {}

        def run(ctx, inputs):
            seen["params"] = ctx.params
            return RuneResult(ok=True)

        job = runner.submit(rune_from(run), [], params={"어떤키": [1, 2]})
        runner.join(job.id, TIMEOUT)

        assert seen["params"] == {"어떤키": [1, 2]}

    def test_emits_log_and_progress(self, runner):
        def run(ctx, inputs):
            ctx.log("info", "시작", 항목=3)
            ctx.report_progress(1, 3, "1단계")
            return RuneResult(ok=True)

        job = runner.submit(rune_from(run), [])
        runner.join(job.id, TIMEOUT)
        drained = runner.drain_events()

        logs = [e for e in drained if isinstance(e, LogLine)]
        progress = [e for e in drained if isinstance(e, Progress)]
        assert logs[0].message == "시작"
        assert logs[0].fields == {"항목": 3}
        assert progress[0].current == 1 and progress[0].total == 3

    def test_ends_with_a_finished_event(self, runner):
        job = runner.submit(rune_from(lambda ctx, i: RuneResult(ok=True)), [])
        runner.join(job.id, TIMEOUT)

        finished = [e for e in runner.drain_events() if isinstance(e, Finished)]
        assert len(finished) == 1
        assert finished[0].state is JobState.SUCCEEDED


# ── 실패 ────────────────────────────────────────────────────────

class TestFailure:

    def test_ok_false_is_a_failure(self, runner):
        job = runner.submit(rune_from(lambda ctx, i: RuneResult(ok=False)), [])
        runner.join(job.id, TIMEOUT)
        assert job.state is JobState.FAILED

    def test_an_exception_does_not_escape_the_job(self, runner):
        def boom(ctx, inputs):
            raise ValueError("터졌다")

        job = runner.submit(rune_from(boom), [])
        assert runner.join(job.id, TIMEOUT)

        assert job.state is JobState.FAILED
        assert job.failure.kind == "ValueError"
        assert "터졌다" in job.failure.message

    def test_returning_the_wrong_type_is_a_contract_violation(self, runner):
        job = runner.submit(rune_from(lambda ctx, i: "문자열"), [])
        runner.join(job.id, TIMEOUT)

        assert job.state is JobState.FAILED
        assert job.failure.kind == "ContractViolation"


# ── 사용자 대기 ─────────────────────────────────────────────────

class TestUserAction:

    def test_blocks_until_the_user_answers(self, runner):
        asked = threading.Event()
        answered = {}

        def run(ctx, inputs):
            asked.set()
            answered["r"] = ctx.request_user_action(
                UserActionRequest(kind="confirm", prompt="계속할까요?")
            )
            return RuneResult(ok=True)

        job = runner.submit(rune_from(run), [])
        assert asked.wait(TIMEOUT)

        # 질문 이벤트가 올 때까지 기다린다 — 큐에 들어가는 시점이 미세하게 늦다.
        request = None
        deadline = threading.Event()
        while request is None and not deadline.wait(0.02):
            for e in runner.drain_events():
                if isinstance(e, UserActionRequested):
                    request = e
            if runner.get(job.id).is_terminal:
                break

        assert request is not None
        assert job.state is JobState.WAITING_USER

        assert runner.respond(job.id, request.correlation_id, UserActionResponse(action="ok"))
        assert runner.join(job.id, TIMEOUT)

        assert answered["r"].action == "ok"
        assert job.state is JobState.SUCCEEDED

    def test_a_late_answer_to_a_gone_question_is_rejected(self, runner):
        job = runner.submit(rune_from(lambda ctx, i: RuneResult(ok=True)), [])
        runner.join(job.id, TIMEOUT)

        assert runner.respond(job.id, "없는_correlation_id", UserActionResponse(action="ok")) is False


# ── 취소 ────────────────────────────────────────────────────────

class TestCancel:

    def test_stops_at_the_next_safe_point(self, runner):
        started = threading.Event()
        release = threading.Event()
        reached_second_point = []

        def run(ctx, inputs):
            started.set()
            release.wait(TIMEOUT)
            ctx.check_cancelled()          # 여기서 끊긴다
            reached_second_point.append(True)
            return RuneResult(ok=True)

        job = runner.submit(rune_from(run), [])
        assert started.wait(TIMEOUT)

        assert runner.cancel(job.id)
        assert job.state is JobState.CANCEL_REQUESTED   # 아직 돌고 있다
        release.set()

        assert runner.join(job.id, TIMEOUT)
        assert job.state is JobState.CANCELLED
        assert reached_second_point == [], "안전 지점을 지나 계속 실행되면 안 된다"

    def test_releases_a_worker_waiting_on_the_user(self, runner):
        """응답 대기 중 취소해도 워커가 매달려 있으면 안 된다."""
        asked = threading.Event()

        def run(ctx, inputs):
            asked.set()
            ctx.request_user_action(UserActionRequest(kind="confirm", prompt="?"))
            return RuneResult(ok=True)

        job = runner.submit(rune_from(run), [])
        assert asked.wait(TIMEOUT)

        runner.cancel(job.id)
        assert runner.join(job.id, TIMEOUT), "워커가 해제되지 않았다"
        assert job.state is JobState.CANCELLED

    def test_does_not_ask_again_after_cancel(self, runner):
        """취소를 눌렀는데 새 질문이 뜨면 안 된다."""
        asks = []

        def run(ctx, inputs):
            for _ in range(3):
                asks.append(1)
                ctx.request_user_action(UserActionRequest(kind="confirm", prompt="?"))
            return RuneResult(ok=True)

        job = runner.submit(rune_from(run), [])
        # 첫 질문이 뜰 때까지 기다렸다 취소
        while not runner.get(job.id).state is JobState.WAITING_USER:
            if runner.get(job.id).is_terminal:
                break
        runner.cancel(job.id)
        runner.join(job.id, TIMEOUT)

        assert job.state is JobState.CANCELLED
        assert len(asks) == 1

    def test_cancelling_a_finished_job_does_nothing(self, runner):
        job = runner.submit(rune_from(lambda ctx, i: RuneResult(ok=True)), [])
        runner.join(job.id, TIMEOUT)
        assert runner.cancel(job.id) is False


# ── 체크포인트 왕복 ─────────────────────────────────────────────

class TestCheckpoint:

    def test_a_second_job_with_the_same_input_resumes(self, runner, tmp_path):
        """재시도는 새 Job이다. 그래도 이어서 실행돼야 한다."""
        src = tmp_path / "입력.txt"
        src.write_text("같은 내용", encoding="utf-8")
        inputs = [Input(path=src)]
        seen: list[dict | None] = []

        def run(ctx, i):
            seen.append(ctx.load_checkpoint())
            ctx.save_checkpoint({"단계": 2})
            return RuneResult(ok=True)

        first = runner.submit(rune_from(run), inputs)
        runner.join(first.id, TIMEOUT)
        second = runner.submit(rune_from(run), inputs)
        runner.join(second.id, TIMEOUT)

        assert first.id != second.id
        assert seen == [None, {"단계": 2}]

    def test_different_input_does_not_share_a_checkpoint(self, runner, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text("A", encoding="utf-8")
        b = tmp_path / "b.txt"
        b.write_text("B", encoding="utf-8")
        seen: list[dict | None] = []

        def run(ctx, i):
            seen.append(ctx.load_checkpoint())
            ctx.save_checkpoint({"본것": 1})
            return RuneResult(ok=True)

        runner.join(runner.submit(rune_from(run), [Input(path=a)]).id, TIMEOUT)
        runner.join(runner.submit(rune_from(run), [Input(path=b)]).id, TIMEOUT)

        assert seen == [None, None]

    def test_clear_removes_it(self, runner, tmp_path):
        src = tmp_path / "입력.txt"
        src.write_text("x", encoding="utf-8")
        inputs = [Input(path=src)]
        seen: list[dict | None] = []

        def run(ctx, i):
            seen.append(ctx.load_checkpoint())
            ctx.save_checkpoint({"단계": 1})
            ctx.clear_checkpoint()
            return RuneResult(ok=True)

        runner.join(runner.submit(rune_from(run), inputs).id, TIMEOUT)
        runner.join(runner.submit(rune_from(run), inputs).id, TIMEOUT)

        assert seen == [None, None]


# ── 상태 이벤트 ─────────────────────────────────────────────────

class TestStateEvents:

    def test_state_changes_are_reported(self, runner):
        job = runner.submit(rune_from(lambda ctx, i: RuneResult(ok=True)), [])
        runner.join(job.id, TIMEOUT)

        states = [e.state for e in runner.drain_events() if isinstance(e, StateChanged)]
        assert JobState.RUNNING in states
        assert states[-1] is JobState.SUCCEEDED


# ── OCR ─────────────────────────────────────────────────────────

class TestOcr:

    def test_context_delegates_to_gateway(self, runner, tmp_path, monkeypatch):
        expected = {"rows": 1}
        seen = {}

        def extract(path, prompt, schema, *, log, assignment=None):
            seen.update(path=path, prompt=prompt, schema=schema, log=log)
            return expected

        monkeypatch.setattr("vader.core.ocr.extract", extract)

        def run(ctx, i):
            result = ctx.ocr(tmp_path / "x.pdf", "프롬프트", dict)
            return RuneResult(ok=True, summary={"ocr": result})

        job = runner.submit(rune_from(run), [])
        runner.join(job.id, TIMEOUT)

        assert job.state is JobState.SUCCEEDED
        assert job.result.summary["ocr"] == expected
        assert seen["path"] == tmp_path / "x.pdf"
        assert seen["prompt"] == "프롬프트"
        assert seen["schema"] is dict
        assert callable(seen["log"])


class TestRetryAfterFailure:
    """실패한 Job 을 다시 실행하면 남은 체크포인트에서 이어져야 한다.

    기존 체크포인트 테스트는 성공 → 성공만 덮는다. 사용자가 실제로 겪는 것은
    실패 → 재시도이고, 그때 이어지지 않으면 처음부터 다시 도는 셈이다.
    """

    def test_a_retry_resumes_from_where_it_failed(self, runner, tmp_path):
        src = tmp_path / "입력.txt"
        src.write_text("내용", encoding="utf-8")
        inputs = [Input(path=src)]
        attempts = []

        def run(ctx, i):
            resumed = ctx.load_checkpoint()
            attempts.append(resumed)
            if resumed is None:
                ctx.save_checkpoint({"완료한_항목": 2})
                raise RuntimeError("3번째 항목에서 터졌다")
            return RuneResult(ok=True, summary={"이어받음": resumed})

        first = runner.submit(rune_from(run), inputs)
        runner.join(first.id, TIMEOUT)
        assert first.state is JobState.FAILED

        second = runner.submit(rune_from(run), inputs)
        runner.join(second.id, TIMEOUT)

        assert second.state is JobState.SUCCEEDED
        assert attempts == [None, {"완료한_항목": 2}]
        assert second.result.summary["이어받음"] == {"완료한_항목": 2}

    def test_clearing_the_checkpoint_makes_the_retry_start_fresh(self, runner, tmp_path):
        """사용자가 '처음부터' 를 골랐을 때 실제로 처음부터 돌아야 한다."""
        src = tmp_path / "입력.txt"
        src.write_text("내용", encoding="utf-8")
        inputs = [Input(path=src)]
        attempts = []

        def run(ctx, i):
            attempts.append(ctx.load_checkpoint())
            ctx.save_checkpoint({"단계": 1})
            return RuneResult(ok=True)

        runner.join(runner.submit(rune_from(run), inputs).id, TIMEOUT)

        key = CheckpointKey.from_inputs("_probe", inputs)
        runner._checkpoints.clear(key)

        runner.join(runner.submit(rune_from(run), inputs).id, TIMEOUT)

        assert attempts == [None, None]


class TestDataDir:
    """룬 전용 영속 폴더. workdir 과 다르다 — Job 이 아니라 룬 단위다."""

    def test_is_separate_from_workdir(self, runner):
        seen = {}

        def run(ctx, inputs):
            seen["workdir"] = ctx.workdir
            seen["datadir"] = ctx.datadir
            return RuneResult(ok=True)

        job = runner.submit(rune_from(run), [])
        runner.join(job.id, TIMEOUT)

        assert seen["datadir"].exists()
        assert seen["datadir"] != seen["workdir"]
        assert "_probe" in str(seen["datadir"])

    def test_survives_across_jobs(self, runner):
        """학습 데이터가 사는 곳이다. Job 이 끝나도 남아야 한다."""
        dirs = []

        def run(ctx, inputs):
            dirs.append(ctx.datadir)
            (ctx.datadir / "learned.csv").write_text("x", encoding="utf-8")
            return RuneResult(ok=True)

        runner.join(runner.submit(rune_from(run), []).id, TIMEOUT)
        runner.join(runner.submit(rune_from(run), []).id, TIMEOUT)

        assert dirs[0] == dirs[1]
        assert (dirs[0] / "learned.csv").read_text(encoding="utf-8") == "x"

    def test_two_runes_do_not_share_it(self, runner):
        dirs = {}

        def make(rid):
            def run(ctx, inputs):
                dirs[rid] = ctx.datadir
                return RuneResult(ok=True)
            return rune_from(run, rune_id=rid)

        runner.join(runner.submit(make("첫번째"), []).id, TIMEOUT)
        runner.join(runner.submit(make("두번째"), []).id, TIMEOUT)

        assert dirs["첫번째"] != dirs["두번째"]


class TestInfer:
    """텍스트에서 구조화 추론. 파일이 없는 ocr() 이다."""

    def test_delegates_to_the_gateway(self, runner, monkeypatch):
        seen = {}

        def fake_infer(prompt, schema, *, log, assignment=None):
            seen.update(prompt=prompt, schema=schema, log=log)
            return {"code": "BA000819"}

        monkeypatch.setattr("vader.core.ocr.infer", fake_infer)

        def run(ctx, i):
            return RuneResult(ok=True, summary={"got": ctx.infer("후보 20개 중 어느 것?", dict)})

        job = runner.submit(rune_from(run), [])
        runner.join(job.id, TIMEOUT)

        assert job.state is JobState.SUCCEEDED
        assert job.result.summary["got"] == {"code": "BA000819"}
        assert seen["prompt"] == "후보 20개 중 어느 것?"
        assert seen["schema"] is dict
        assert callable(seen["log"])
