"""선형 UI 시퀀스 가정 검증 (Phase 7).

PLAN 은 "선형 UI 시퀀스 가정 검증" 을 Phase 7 항목으로 뒀다. 검증할 가정은
이것이다 — **VaderAI 계약이 자동화를 "단계의 일렬 나열" 로 전제하는가.**

전제한다면 분기·반복·되돌아가기가 있는 업무는 룬으로 못 만들고, 그러면 병원 밖
업무에서 막힌다. 그게 이 Phase 의 위험이다.

에세이로 답하지 않는다. **일부러 비선형인 룬을 써서 계약이 감당하는지 본다.**
`_probe` 가 실행 인프라를 검증했던 것과 같은 방식이고, 배포에는 포함하지 않는다.

검증 대상 다섯:

  1. 전체 개수를 모르는 진행률 (total=None)
  2. 실행 중에 결정되는 반복 횟수
  3. 사용자 답에 따라 갈라지는 분기 (일렬이 아니라 트리)
  4. 분기 안의 반복 안의 질문 (중첩)
  5. 이미 지난 상태로 되돌아가기
"""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

from vader.core.checkpoint import CheckpointStore
from vader.core.events import Finished, LogLine, Progress, StateChanged
from vader.core.job import JobState
from vader.core.runner import JobRunner
from vader.core.view import JobView, apply_all
from vader.sdk import (
    Input,
    RuneManifest,
    RuneResult,
    UserActionRequest,
    UserActionResponse,
)

TIMEOUT = 5.0


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("이 테스트는 네트워크를 쓰지 않는다")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def runner(tmp_path):
    return JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )


def answer_all(runner, job_id, answers: list[UserActionResponse]) -> list[dict]:
    """룬이 물을 때마다 순서대로 답한다. 물은 내용을 돌려준다.

    실제 UI 가 `_pump` 에서 하는 일과 같다 — 이벤트를 비우고 질문에 답한다.
    """
    asked: list[dict] = []
    pending = list(answers)
    while True:
        for event in runner.drain_events():
            if isinstance(event, Finished):
                return asked
            correlation = getattr(event, "correlation_id", None)
            request = getattr(event, "request", None)
            if correlation and request is not None:
                asked.append({"kind": request.kind, "prompt": request.prompt})
                response = pending.pop(0) if pending else UserActionResponse(action="abort")
                runner.respond(job_id, correlation, response)
        if not runner.join(job_id, 0.05):
            continue
        # 워커가 끝났다. 남은 이벤트를 비우고 나간다.
        for event in runner.drain_events():
            if isinstance(event, Finished):
                break
        return asked


class _NonLinearRune:
    """일부러 비선형인 룬.

    - 전체 개수를 모른 채 시작한다
    - 반복 횟수를 실행 중에 정한다
    - 사용자 답으로 갈라진다
    - 분기 안에서 반복하고 그 안에서 또 묻는다
    - 되돌아가서 같은 상태를 다시 지난다
    """

    manifest = RuneManifest(id="_nonlinear", name="비선형 검증용")

    def run(self, ctx, inputs):
        visited: list[str] = []

        # 1. 전체 개수를 모른다. 계약이 total=None 을 받는가.
        ctx.report_progress(0, None, "탐색 중 — 총량 미정")
        visited.append("explore")

        # 2. 반복 횟수를 실행 중에 정한다.
        answer = ctx.request_user_action(
            UserActionRequest(
                kind="choice",
                prompt="몇 갈래로 갈까요?",
                options=["둘", "셋"],
            )
        )
        branches = 3 if answer.value == "셋" else 2
        visited.append(f"fanout:{branches}")

        # 3·4. 분기 안의 반복 안의 질문.
        for index in range(branches):
            ctx.check_cancelled()
            ctx.report_progress(index, branches, f"{index + 1}번 갈래")
            reply = ctx.request_user_action(
                UserActionRequest(
                    kind="confirm",
                    prompt=f"{index + 1}번 갈래를 처리할까요?",
                    context={"branch": index},
                )
            )
            if reply.action == "skip":
                visited.append(f"skip:{index}")
                continue
            visited.append(f"work:{index}")

            # 5. 되돌아간다. 이미 지난 상태를 다시 지나는 것이 계약에 문제인가.
            if index == 0:
                visited.append("back:explore")
                ctx.report_progress(0, None, "되돌아가 다시 탐색")
                ctx.save_checkpoint({"visited": list(visited)})

        ctx.report_progress(branches, branches, "끝")
        return RuneResult(ok=True, summary={"visited": visited})


class TestContractDoesNotAssumeALinearSequence:

    def test_a_nonlinear_rune_runs_to_success(self, runner):
        job = runner.submit(_NonLinearRune(), [])
        answer_all(
            runner,
            job.id,
            [
                UserActionResponse(action="ok", value="셋"),
                UserActionResponse(action="ok"),
                UserActionResponse(action="skip"),
                UserActionResponse(action="ok"),
            ],
        )
        assert runner.join(job.id, TIMEOUT)

        assert job.state is JobState.SUCCEEDED
        assert job.result.summary["visited"] == [
            "explore",
            "fanout:3",
            "work:0",
            "back:explore",
            "skip:1",
            "work:2",
        ]

    def test_the_branch_taken_depends_on_the_answer(self, runner):
        """같은 룬이 답에 따라 다른 경로를 지난다 — 트리이지 일렬이 아니다."""
        job = runner.submit(_NonLinearRune(), [])
        answer_all(
            runner,
            job.id,
            [
                UserActionResponse(action="ok", value="둘"),
                UserActionResponse(action="skip"),
                UserActionResponse(action="skip"),
            ],
        )
        assert runner.join(job.id, TIMEOUT)

        assert job.result.summary["visited"] == ["explore", "fanout:2", "skip:0", "skip:1"]

    def test_unknown_total_becomes_indeterminate_progress(self, runner):
        """총량을 모르는 작업이 화면에서 깨지지 않아야 한다."""
        view = JobView()
        job = runner.submit(_NonLinearRune(), [])
        events: list = []
        answers = [
            UserActionResponse(action="ok", value="둘"),
            UserActionResponse(action="skip"),
            UserActionResponse(action="skip"),
        ]
        pending = list(answers)
        while not runner.join(job.id, 0.05):
            for event in runner.drain_events():
                events.append(event)
                correlation = getattr(event, "correlation_id", None)
                if correlation:
                    runner.respond(
                        job.id,
                        correlation,
                        pending.pop(0) if pending else UserActionResponse(action="abort"),
                    )
        events.extend(runner.drain_events())
        apply_all(view, [e for e in events if not hasattr(e, "correlation_id")])

        indeterminate = [
            e for e in events if isinstance(e, Progress) and e.total is None
        ]
        assert indeterminate, "total=None 진행률이 전달되지 않았다"
        # 마지막 진행률은 확정값이라 percent 가 나온다. 중간에 None 이 있어도
        # view 가 터지지 않는 것이 요점이다.
        assert view.percent is not None

    def test_revisiting_state_does_not_break_the_checkpoint(self, runner):
        job = runner.submit(_NonLinearRune(), [])
        answer_all(
            runner,
            job.id,
            [
                UserActionResponse(action="ok", value="둘"),
                UserActionResponse(action="ok"),
                UserActionResponse(action="skip"),
            ],
        )
        assert runner.join(job.id, TIMEOUT)

        assert job.state is JobState.SUCCEEDED
        assert "back:explore" in job.result.summary["visited"]


class TestWhereTheLinearAssumptionActuallyLives:
    """계약에는 없다. 남은 자리를 정확히 짚어 문서에 남긴다."""

    def test_the_contract_has_no_step_or_sequence_concept(self):
        """`run(ctx, inputs)` 하나뿐이다. 단계 목록도 상태 기계도 없다.

        룬은 평범한 파이썬을 쓴다. 그래서 분기·반복·되돌아가기가 자연히 된다 —
        위 테스트가 그것을 실제로 보여준다.
        """
        source = (
            Path(__file__).resolve().parent.parent / "vader" / "sdk" / "contract.py"
        ).read_text(encoding="utf-8")

        for forbidden in ("class Step", "steps:", "def next_step", "StateMachine"):
            assert forbidden not in source

    def test_progress_accepts_an_unknown_total(self):
        """유일한 선형 흔적은 `report_progress(current, total)` 다.

        total 이 None 을 받으므로 "셀 수 있는 단계" 를 강제하지는 않는다.
        """
        import inspect

        from vader.sdk import RuneContext

        signature = inspect.signature(RuneContext.report_progress)
        # 기본값은 없다 — 룬이 총량을 아는지 **명시적으로** 밝히게 만든다.
        # 대신 타입이 None 을 받는다. 그래서 셀 수 있는 단계를 강제하지 않는다.
        assert signature.parameters["total"].annotation == "int | None"
        assert signature.parameters["total"].default is inspect.Parameter.empty

    def test_user_action_kind_is_an_open_string_not_an_enum(self):
        """상호작용 어휘가 닫혀 있으면 새 업무에서 막힌다.

        `kind` 는 자유 문자열이고 UI 는 모르는 종류를 텍스트 입력으로 그린다
        (`ui.py` 의 `AskDialog`). 그래서 새 업무가 어휘를 늘려도 코어를 안 고친다.
        """
        request = UserActionRequest(kind="지도에서_지점_고르기", prompt="어디?")

        assert isinstance(request.kind, str)
        assert request.kind == "지도에서_지점_고르기"
