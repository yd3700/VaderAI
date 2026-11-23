"""
Job 상태 전이 테스트.

지키려는 것: "취소했는데 성공으로 끝난" 같은 상태가 조용히 생기지 않는다.
상태를 문자열로 아무 데서나 바꾸면 그런 것이 생기고, 생기고 나면 로그만 보고는
무엇이 일어났는지 알 수 없다.
"""
from pathlib import Path

import pytest

from vader.core.job import TERMINAL, InvalidTransition, Job, JobState


def job() -> Job:
    return Job(rune_id="_probe", inputs=[], workdir=Path("/tmp/x"))


class TestTransitions:

    def test_starts_queued(self):
        assert job().state is JobState.QUEUED

    def test_normal_success_path(self):
        j = job()
        j.transition(JobState.RUNNING)
        j.transition(JobState.SUCCEEDED)
        assert j.state is JobState.SUCCEEDED

    def test_queued_cannot_jump_to_succeeded(self):
        j = job()
        with pytest.raises(InvalidTransition):
            j.transition(JobState.SUCCEEDED)

    def test_terminal_states_are_final(self):
        for terminal in TERMINAL:
            j = job()
            j.transition(JobState.RUNNING)
            j.transition(JobState.CANCEL_REQUESTED)
            j.transition(JobState.CANCELLED)
            with pytest.raises(InvalidTransition):
                j.transition(JobState.RUNNING)

    def test_waiting_user_returns_to_running(self):
        j = job()
        j.transition(JobState.RUNNING)
        j.transition(JobState.WAITING_USER)
        j.transition(JobState.RUNNING)
        assert j.state is JobState.RUNNING

    def test_cancel_is_accepted_while_waiting_for_the_user(self):
        j = job()
        j.transition(JobState.RUNNING)
        j.transition(JobState.WAITING_USER)
        j.transition(JobState.CANCEL_REQUESTED)
        assert j.state is JobState.CANCEL_REQUESTED

    def test_cancel_requested_can_still_fail(self):
        """취소를 요청했다고 결과를 취소로 덮으면 무엇이 일어났는지 잃는다."""
        j = job()
        j.transition(JobState.RUNNING)
        j.transition(JobState.CANCEL_REQUESTED)
        j.transition(JobState.FAILED)
        assert j.state is JobState.FAILED

    def test_cancel_requested_can_still_succeed(self):
        """안전 지점에 닿기 전에 룬이 정상적으로 끝날 수 있다."""
        j = job()
        j.transition(JobState.RUNNING)
        j.transition(JobState.CANCEL_REQUESTED)
        j.transition(JobState.SUCCEEDED)
        assert j.state is JobState.SUCCEEDED

    def test_queued_can_be_cancelled_before_it_starts(self):
        j = job()
        j.transition(JobState.CANCELLED)
        assert j.is_terminal


class TestTryTransition:

    def test_returns_false_instead_of_raising(self):
        j = job()
        assert j.try_transition(JobState.SUCCEEDED) is False
        assert j.state is JobState.QUEUED

    def test_returns_true_on_a_legal_move(self):
        j = job()
        assert j.try_transition(JobState.RUNNING) is True


class TestIdentity:

    def test_each_job_gets_its_own_id(self):
        assert job().id != job().id
