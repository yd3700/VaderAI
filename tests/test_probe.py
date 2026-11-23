"""
Milestone 1 — `_probe` 룬 하나가 **설치된 상태로** 끝까지 실행된다.

`tests/test_runner.py`는 합성 룬을 그 자리에서 만들어 넘긴다. 그건 러너를
검증하지 실행 인프라를 검증하지 않는다. 여기서는 룬을 디스크에 패키지로 깔고
로더가 찾게 한다 — 소스 트리에서 import하면 이 마일스톤을 충족하지 않는다.

`runes/`에 심지 않는 이유는 D8이다. 설치 룬은 소스 트리 밖에 살고,
`test_dependency_direction.py`가 `runes/`가 비어 있는 것을 지킨다.
`_probe`는 배포에도 포함하지 않는다.
"""
import threading

import pytest

from vader.core import seal
from vader.core.checkpoint import CheckpointStore
from vader.core.events import Finished, LogLine, Progress, UserActionRequested
from vader.core.job import JobState
from vader.core.loader import discover
from vader.core.runner import JobRunner
from vader.sdk import Input, UserActionResponse

TIMEOUT = 5.0

#: 모든 ctx 메서드를 한 번씩 지나가는 룬. ins_singo에 인위적 기능을 넣지 않고도
#: 실행 인프라를 검증하려고 둔다.
PROBE = '''
from vader.sdk import RuneManifest, RuneResult, UserActionRequest


class Probe:
    manifest = RuneManifest(id="_probe", name="계약 검증용")

    def run(self, ctx, inputs):
        ctx.log("info", "probe 시작", 입력=len(inputs))
        ctx.report_progress(0, 2, "1단계")

        resumed = ctx.load_checkpoint()
        if resumed is None:
            answer = ctx.request_user_action(
                UserActionRequest(kind="confirm", prompt="계속할까요?")
            )
            ctx.save_checkpoint({"단계": 1, "답": answer.action})
        else:
            answer = None

        ctx.check_cancelled()
        ctx.report_progress(1, 2, "2단계")
        return RuneResult(
            ok=True,
            summary={
                "재개": resumed,
                "답": answer.action if answer else None,
                "dry_run": ctx.dry_run,
                "workdir": str(ctx.workdir),
                "params": ctx.params,
            },
        )


rune = Probe()
'''


@pytest.fixture
def installed(tmp_path):
    """`_probe`를 룬 디렉터리에 설치하고 로더로 찾는다."""
    rune_dir = tmp_path / "runes"
    (rune_dir / "_probe").mkdir(parents=True)
    (rune_dir / "_probe" / "__init__.py").write_text(PROBE, encoding="utf-8")
    seal.trust("_probe", rune_dir / "_probe")

    loaded, failures = discover(rune_dir)
    assert failures == [], f"설치한 룬이 로드되지 않았다: {failures}"
    assert len(loaded) == 1
    return loaded[0]


@pytest.fixture
def runner(tmp_path):
    return JobRunner(checkpoints=CheckpointStore(tmp_path / "cp"), data_dir=tmp_path)


def wait_for_ask(runner, job, seen=None):
    """질문 이벤트가 큐에 들어올 때까지 기다린다.

    drain_events()는 큐를 비운다. 나중에 로그·진행률도 확인하려면 지나간 것을
    모아 둬야 한다 — `seen` 리스트를 주면 거기 쌓는다.
    """
    tick = threading.Event()
    found = None
    for _ in range(int(TIMEOUT / 0.02)):
        for event in runner.drain_events():
            if seen is not None:
                seen.append(event)
            if isinstance(event, UserActionRequested):
                found = event
        if found is not None or runner.get(job.id).is_terminal:
            return found
        tick.wait(0.02)
    return found


def test_probe_is_discovered_as_an_installed_rune(installed):
    assert installed.manifest.id == "_probe"
    assert installed.manifest.name == "계약 검증용"


def test_full_path_from_install_to_result(installed, runner, tmp_path):
    """Milestone 1 완료 조건 1~4·7을 한 번에 지난다."""
    src = tmp_path / "입력.txt"
    src.write_text("내용", encoding="utf-8")

    seen = []
    job = runner.submit(installed.rune, [Input(path=src)], params={"열쇠": "값"})

    ask = wait_for_ask(runner, job, seen)
    assert ask is not None, "룬이 사람에게 묻지 않았다"
    assert job.state is JobState.WAITING_USER

    assert runner.respond(job.id, ask.correlation_id, UserActionResponse(action="ok"))
    assert runner.join(job.id, TIMEOUT)

    assert job.state is JobState.SUCCEEDED
    assert job.result.summary["답"] == "ok"
    assert job.result.summary["params"] == {"열쇠": "값"}
    assert job.result.summary["dry_run"] is False
    assert job.workdir.exists()

    kinds = [type(e) for e in seen + runner.drain_events()]
    assert LogLine in kinds and Progress in kinds and Finished in kinds


def test_a_second_job_resumes_from_the_checkpoint(installed, runner, tmp_path):
    """완료 조건 6. 재시도는 새 Job이지만 이어서 실행된다."""
    src = tmp_path / "입력.txt"
    src.write_text("같은 내용", encoding="utf-8")
    inputs = [Input(path=src)]

    first = runner.submit(installed.rune, inputs)
    ask = wait_for_ask(runner, first)
    runner.respond(first.id, ask.correlation_id, UserActionResponse(action="ok"))
    runner.join(first.id, TIMEOUT)

    second = runner.submit(installed.rune, inputs)
    assert runner.join(second.id, TIMEOUT)

    assert first.id != second.id
    assert second.state is JobState.SUCCEEDED
    # 두 번째는 묻지 않는다 — 체크포인트가 있으므로 그 단계를 건너뛴다.
    assert second.result.summary["재개"] == {"단계": 1, "답": "ok"}


def test_cancel_while_waiting_releases_the_worker(installed, runner, tmp_path):
    """완료 조건 5. 응답을 기다리는 중에도 취소가 통한다."""
    src = tmp_path / "입력.txt"
    src.write_text("내용", encoding="utf-8")

    job = runner.submit(installed.rune, [Input(path=src)])
    assert wait_for_ask(runner, job) is not None

    runner.cancel(job.id)
    assert runner.join(job.id, TIMEOUT), "워커가 해제되지 않았다"
    assert job.state is JobState.CANCELLED


def test_dry_run_reaches_the_installed_rune(installed, runner, tmp_path):
    src = tmp_path / "입력.txt"
    src.write_text("내용", encoding="utf-8")

    job = runner.submit(installed.rune, [Input(path=src)], dry_run=True)
    ask = wait_for_ask(runner, job)
    runner.respond(job.id, ask.correlation_id, UserActionResponse(action="ok"))
    runner.join(job.id, TIMEOUT)

    assert job.result.summary["dry_run"] is True


def test_probe_is_not_shipped():
    """배포 트리에 `_probe`가 없어야 한다. 있으면 룬 0개 상태가 깨진다."""
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    assert not (repo / "runes" / "_probe").exists()
