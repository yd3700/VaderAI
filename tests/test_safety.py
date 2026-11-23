"""Phase 1 안전장치의 책임과 인터페이스 테스트."""
import threading

import pytest

from vader.core.events import Finished, LogLine
from vader.core.job import JobState
from vader.core.runner import JobRunner
from vader.core.safety import check_target_app
from vader.sdk import CheckpointKey, Failure, Input, RuneField, RuneManifest, RuneResult

TIMEOUT = 5.0


class FakeStore:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict] = {}

    def load(self, key: CheckpointKey, rune_version: str = "") -> dict | None:
        return self.data.get((key.rune_id, key.input_identity))

    def save(self, key: CheckpointKey, data: dict, rune_version: str = "") -> None:
        self.data[(key.rune_id, key.input_identity)] = data

    def clear(self, key: CheckpointKey) -> None:
        self.data.pop((key.rune_id, key.input_identity), None)


def rune_from(fn, rune_id: str = "_safety_probe"):
    class _Rune:
        manifest = RuneManifest(id=rune_id, name="안전장치 테스트")

        def run(self, ctx, inputs):
            return fn(ctx, inputs)

    return _Rune()


@pytest.fixture
def runner(tmp_path):
    return JobRunner(checkpoints=FakeStore(), data_dir=tmp_path)


def test_emergency_stop_cancels_every_running_job(runner):
    started = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]

    def blocking_rune(index):
        def run(ctx, inputs):
            started[index].set()
            release[index].wait(TIMEOUT)
            ctx.check_cancelled()
            return RuneResult(ok=True)

        return rune_from(run, rune_id=f"probe_{index}")

    jobs = [runner.submit(blocking_rune(index), []) for index in range(2)]
    assert all(event.wait(TIMEOUT) for event in started)

    assert runner.emergency_stop() == 2
    assert runner.is_stopped()
    assert all(job.state is JobState.CANCEL_REQUESTED for job in jobs)

    for event in release:
        event.set()
    assert all(runner.join(job.id, TIMEOUT) for job in jobs)
    assert all(job.state is JobState.CANCELLED for job in jobs)


def test_submit_is_rejected_while_emergency_stop_is_active(runner, tmp_path):
    assert runner.emergency_stop() == 0

    with pytest.raises(RuntimeError, match="비상정지"):
        runner.submit(rune_from(lambda ctx, inputs: RuneResult(ok=True)), [])

    assert not (tmp_path / "jobs").exists(), "거절된 submit은 Job 폴더도 만들면 안 된다"


def test_resume_allows_submit_again(runner):
    runner.emergency_stop()
    runner.resume()

    job = runner.submit(rune_from(lambda ctx, inputs: RuneResult(ok=True)), [])

    assert not runner.is_stopped()
    assert runner.join(job.id, TIMEOUT)
    assert job.state is JobState.SUCCEEDED


def test_dry_run_reaches_context_and_emits_visible_log(runner):
    seen = {}

    def run(ctx, inputs):
        seen["dry_run"] = ctx.dry_run
        return RuneResult(ok=True)

    job = runner.submit(rune_from(run), [], dry_run=True)
    assert runner.join(job.id, TIMEOUT)

    logs = [
        event
        for event in runner.drain_events()
        if isinstance(event, LogLine)
    ]
    assert seen["dry_run"] is True
    assert any(log.level == "warning" and "DRY-RUN" in log.message for log in logs)


def test_dry_run_is_off_by_default(runner):
    seen = {}

    def run(ctx, inputs):
        seen["dry_run"] = ctx.dry_run
        return RuneResult(ok=True)

    job = runner.submit(rune_from(run), [])
    assert runner.join(job.id, TIMEOUT)
    assert seen["dry_run"] is False


def test_empty_target_apps_means_unrestricted():
    manifest = RuneManifest(id="r", name="룬")

    assert check_target_app(manifest, "어떤 창이든 허용")


def test_target_app_exact_match_is_allowed():
    manifest = RuneManifest(id="r", name="룬", target_apps=["EONM 입고"])

    assert check_target_app(manifest, "EONM 입고")


def test_target_app_mismatch_is_rejected():
    manifest = RuneManifest(id="r", name="룬", target_apps=["EONM 입고"])

    assert not check_target_app(manifest, "메모장")


def test_target_app_matches_a_longer_real_window_title():
    """실제 창 제목은 설정에 적는 값보다 길다.

    IchorAI 는 `"입고/반품등록(수기)"` 로 창을 찾는데
    (`eonm_driver.py:31` 이 `title_re=".*{...}.*"` 로 연결한다),
    정확 일치로 두면 Phase 5 에서 실제 창을 전부 거부한다.
    """
    manifest = RuneManifest(id="r", name="룬", target_apps=["입고/반품등록(수기)"])

    assert check_target_app(manifest, "입고/반품등록(수기) - 이온엠 [원장]")


def test_target_app_accepts_any_one_of_several():
    manifest = RuneManifest(id="r", name="룬", target_apps=["이온엠", "EMR"])

    assert check_target_app(manifest, "EMR 조회 화면")
    assert not check_target_app(manifest, "메모장")


# ── 배선 — 위 검사 함수를 실제로 부르는 자리 ─────────────────────

def _window_rune(rune_id="_target_probe"):
    """`kind="window"` 필드와 `target_apps` 를 가진 룬."""
    calls = []

    class _Rune:
        manifest = RuneManifest(
            id=rune_id,
            name="창 표적 테스트",
            target_apps=["이온엠"],
            required_fields=[RuneField(name="window_title", kind="window")],
        )

        def run(self, ctx, inputs):
            calls.append(ctx.params.get("window_title"))
            return RuneResult(ok=True)

    return _Rune(), calls


def test_submit_refuses_a_window_title_outside_target_apps(runner):
    """설정 하나로 아무 창에나 키 입력이 들어가면 안 된다.

    `check_target_app` 은 Phase 4 부터 있었지만 부르는 곳이 테스트뿐이었다.
    막는 자리는 모든 실행이 지나는 `submit` 이다 — 룬마다 부르게 하면 안 부르는
    룬이 생기고, 룬은 `vader.sdk` 만 import 해야 해서 부를 수도 없다.
    """
    rune, calls = _window_rune()

    with pytest.raises(RuntimeError, match="대상 앱이 아닙니다"):
        runner.submit(rune, [], params={"window_title": "메모장"})

    assert calls == [], "거부된 실행이 룬 본체까지 갔다"


def test_submit_allows_a_real_window_title_that_contains_the_target(runner):
    rune, calls = _window_rune("_target_ok")

    job = runner.submit(
        rune, [], params={"window_title": "입고/반품등록(수기) - 이온엠 [원장]"}
    )
    assert runner.join(job.id, TIMEOUT)

    assert calls == ["입고/반품등록(수기) - 이온엠 [원장]"]


def test_dry_run_never_writes_a_checkpoint(runner):
    """리허설이 기록을 남기면 **다음 실제 실행이 하지도 않은 작업을 건너뛴다.**

    EONM 이면 입고가 통째로 빠진다. 지우는 것도 같은 이유로 막는다 — 리허설이
    진짜 재개 지점을 날리면 안 된다.
    """
    def run(ctx, inputs):
        ctx.save_checkpoint({"bucket": ["Page1"]})
        ctx.clear_checkpoint()
        return RuneResult(ok=True)

    store = runner._checkpoints
    store.data[("_dry", "seed")] = {"기존": "값"}

    job = runner.submit(rune_from(run, rune_id="_dry"), [], dry_run=True)
    assert runner.join(job.id, TIMEOUT)

    assert store.data == {("_dry", "seed"): {"기존": "값"}}, "dry-run 이 기록을 건드렸다"


def test_a_real_run_still_writes_the_checkpoint(runner):
    """게이트가 dry-run 에만 걸렸는지 확인한다. 아니면 재개가 통째로 죽는다."""
    def run(ctx, inputs):
        ctx.save_checkpoint({"bucket": ["Page1"]})
        return RuneResult(ok=True)

    job = runner.submit(rune_from(run, rune_id="_wet"), [], dry_run=False)
    assert runner.join(job.id, TIMEOUT)

    assert [v for v in runner._checkpoints.data.values()] == [{"bucket": ["Page1"]}]


def test_a_rune_that_returns_a_failure_puts_the_reason_on_the_job(runner):
    """룬이 얌전히 실패를 담아 돌려줘도 사유가 화면까지 가야 한다.

    `job.failure` 는 예외로 죽을 때만 채워지고 있었다. 그래서 룬이
    `RuneResult(ok=False, failures=[...])` 를 돌려주면 UI 가 "실패" 네 글자만
    띄우고 **왜인지는 어디에도 나오지 않았다** — 로그도 남기지 않는 경로면
    사용자에게 단서가 0이다. 실제로 그 상태로 신고가 들어왔다.
    """
    def run(ctx, inputs):
        return RuneResult(
            ok=False,
            failures=[Failure(kind="no_input", message="받는 확장자는 .pdf 입니다")],
        )

    job = runner.submit(rune_from(run, rune_id="_fail"), [])
    assert runner.join(job.id, TIMEOUT)

    assert job.state is JobState.FAILED
    assert job.failure is not None, "실패 사유가 Job 에 실리지 않았다"
    assert job.failure.kind == "no_input"
    assert "확장자" in job.failure.message


def test_three_runs_in_a_row_each_finish_and_emit_their_events(runner, tmp_path):
    """T5. 입력을 옮기는 룬을 연속으로 돌려도 매 실행이 이벤트를 낸다.

    2026-08-05 에 스캔 A·B·C 를 연속 처리하다 B 부터 로그가 멎고 C 는 버튼이
    비활성으로 남았다. A 가 입력을 옮긴 뒤 UI 가 사라진 파일을 다시 해시하다
    죽어 폴링이 멎은 것이었다. **워커와 이벤트 큐 자체는 멀쩡했다** — 그 사실을
    여기서 고정해 둔다. 화면 쪽 보증은 `test_view.py` 와 `_pump` 구조 검사다.
    """
    done = tmp_path / "done"
    done.mkdir()

    def run(ctx, inputs):
        for item in inputs:
            ctx.log("info", f"처리 {item.path.name}")
            item.path.rename(done / item.path.name)   # eonm 의 _move 와 같다
        return RuneResult(ok=True)

    rune = rune_from(run, rune_id="_moving")
    seen = []
    for index in range(3):
        scan = tmp_path / f"scan{index}.pdf"
        scan.write_bytes(f"내용{index}".encode())

        job = runner.submit(rune, [Input(path=scan)])
        assert runner.join(job.id, TIMEOUT), f"{index}번째 Job 이 끝나지 않았다"

        events = runner.drain_events()
        seen.append({
            "state": job.state,
            "로그": [e.message for e in events if isinstance(e, LogLine)],
            "끝남": any(isinstance(e, Finished) for e in events),
        })
        assert not scan.exists(), "룬이 입력을 옮겼어야 한다"

    assert [s["state"] for s in seen] == [JobState.SUCCEEDED] * 3
    assert all(s["끝남"] for s in seen), "매 실행이 Finished 를 내야 한다"
    assert [s["로그"] for s in seen] == [
        [f"처리 scan{index}.pdf"] for index in range(3)
    ], "두 번째부터 로그가 비면 그때 그 증상이다"
