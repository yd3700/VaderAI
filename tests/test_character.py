"""성격·성장 모델 테스트.

Phase 6 완료 기준은 "룬을 설치하지 않은 사용자에게도 관찰 데이터에 따른 성장
변화가 재현된다" 다. 마지막 클래스가 그것을 본다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from vader.core import observe
from vader.core.character import (
    EXP_PER_LEVEL,
    Character,
    Traits,
    derive,
)
from vader.core.observe import (
    FOREGROUND,
    JOB_FINISHED,
    SESSION_START,
    Observation,
    ObservationStore,
    SessionObserver,
)

NOW = datetime(2026, 7, 30, 14, 0, 0)


def seen(app: str, *, hour: int = 14, day: int = 30, count: int = 1) -> list[Observation]:
    """같은 앱을 `count` 번 본 관찰. 분 단위로 늘리되 시각은 유지한다.

    시간을 넘기면 night 축 판정이 흔들리니 초 단위로 늘린다 — 한 시간 안에
    3600건까지 같은 hour 에 머문다.
    """
    start = datetime(2026, 7, day, hour, 0, 0)
    return [
        Observation(FOREGROUND, start + timedelta(seconds=index), app)
        for index in range(count)
    ]


# ── 빈 상태 ────────────────────────────────────────────────────

class TestNoObservations:

    def test_starts_at_level_one_with_nothing_learned(self):
        character = derive([], now=NOW)

        assert character.level == 1
        assert character.exp == 0
        assert character.traits == Traits()
        assert character.traits.dominant == "unknown"
        assert "아직" in character.personality

    def test_percent_is_zero_not_a_crash(self):
        assert derive([], now=NOW).percent == 0


# ── 성장 ───────────────────────────────────────────────────────

class TestGrowth:

    def test_exp_comes_from_foreground_observations(self):
        assert derive(seen("a.exe", count=5), now=NOW).exp == 5

    def test_level_rises_at_the_threshold(self):
        just_under = derive(seen("a.exe", count=EXP_PER_LEVEL - 1), now=NOW)
        at_threshold = derive(seen("a.exe", count=EXP_PER_LEVEL), now=NOW)

        assert just_under.level == 1
        assert at_threshold.level == 2

    def test_percent_tracks_progress_inside_the_level(self):
        character = derive(seen("a.exe", count=EXP_PER_LEVEL // 2), now=NOW)

        assert character.level == 1
        assert character.percent == 50

    def test_more_observation_never_lowers_the_level(self):
        levels = [
            derive(seen("a.exe", count=n), now=NOW).level
            for n in (0, 10, 100, EXP_PER_LEVEL, EXP_PER_LEVEL * 3)
        ]
        assert levels == sorted(levels)


# ── 룬 독립 ────────────────────────────────────────────────────

class TestIndependenceFromRunes:
    """완료 기준의 핵심. 룬을 써도 성장이 빨라지지 않는다."""

    def test_job_completions_do_not_add_exp(self):
        without = derive(seen("a.exe", count=10), now=NOW)
        with_jobs = derive(
            seen("a.exe", count=10)
            + [Observation(JOB_FINISHED, NOW) for _ in range(50)],
            now=NOW,
        )

        assert with_jobs.exp == without.exp
        assert with_jobs.level == without.level
        # 세기는 한다 — 나중에 두 집단을 비교하려는 것뿐이다.
        assert with_jobs.jobs == 50
        assert without.jobs == 0

    def test_jobs_do_not_change_traits(self):
        base = seen("a.exe", count=10)
        assert derive(base, now=NOW).traits == derive(
            base + [Observation(JOB_FINISHED, NOW)] * 20, now=NOW
        ).traits


# ── 성격 축 ────────────────────────────────────────────────────

class TestTraits:

    def test_night_activity_raises_the_night_axis(self):
        night = derive(seen("a.exe", hour=23, count=10), now=NOW)
        day = derive(seen("a.exe", hour=10, count=10), now=NOW)

        assert night.traits.night == 1.0
        assert day.traits.night == 0.0
        assert night.traits.dominant in ("night", "focus")

    def test_early_morning_counts_as_night(self):
        assert derive(seen("a.exe", hour=2, count=4), now=NOW).traits.night == 1.0

    def test_one_app_is_focus_many_apps_is_variety(self):
        one = derive(seen("a.exe", count=10), now=NOW)
        many = derive(
            [
                Observation(FOREGROUND, datetime(2026, 7, 30, 14, i), f"app{i}.exe")
                for i in range(10)
            ],
            now=NOW,
        )

        assert one.traits.focus == 1.0
        assert many.traits.focus == 0.0
        assert many.traits.variety == 1.0
        assert one.traits.variety < many.traits.variety

    def test_variety_is_capped_at_one(self):
        many = [
            Observation(FOREGROUND, datetime(2026, 7, 30, 14, i), f"app{i}.exe")
            for i in range(40)
        ]
        assert derive(many, now=NOW).traits.variety == 1.0

    def test_rhythm_counts_distinct_recent_days(self):
        spread = []
        for day in range(16, 30):
            spread += seen("a.exe", day=day, count=1)

        assert derive(spread, now=NOW).traits.rhythm == 1.0

    def test_old_activity_does_not_count_toward_rhythm(self):
        old = seen("a.exe", day=1, count=5)

        assert derive(old, now=NOW).traits.rhythm == 0.0
        # 경험치는 남는다 — 이미 자란 것을 뺏지 않는다.
        assert derive(old, now=NOW).exp == 5

    def test_a_single_sample_does_not_judge_focus(self):
        assert derive(seen("a.exe", count=1), now=NOW).traits.focus == 0.0

    def test_every_trait_stays_within_range(self):
        mixed = seen("a.exe", hour=23, count=5) + seen("b.exe", hour=9, day=29, count=5)
        traits = derive(mixed, now=NOW).traits

        for value in (traits.night, traits.focus, traits.variety, traits.rhythm):
            assert 0.0 <= value <= 1.0

    def test_personality_line_exists_for_every_dominant_axis(self):
        for axis in ("night", "focus", "variety", "rhythm", "unknown"):
            character = Character(level=1, exp=0, traits=Traits(**{axis: 1.0}) if axis != "unknown" else Traits())
            assert character.personality


# ── 세션 ───────────────────────────────────────────────────────

class TestSessions:

    def test_counts_session_start_observations(self):
        items = [Observation(SESSION_START, NOW)] * 3 + seen("a.exe", count=2)

        assert derive(items, now=NOW).sessions == 3

    def test_falls_back_to_gaps_when_starts_are_missing(self):
        """강제 종료나 정전이면 종료 관찰이 남지 않는다. 로그가 잘려 시작이 없는
        경우에도 세션 수를 짐작할 수 있어야 한다."""
        items = seen("a.exe", day=28, count=2) + seen("a.exe", day=30, count=2)

        assert derive(items, now=NOW).sessions == 2

    def test_no_observations_means_no_sessions(self):
        assert derive([], now=NOW).sessions == 0


# ── 완료 기준 ──────────────────────────────────────────────────

class TestGrowsWithZeroRunes:
    """Phase 6 완료 기준: 룬 0개 사용자에게도 성장이 재현된다.

    저장소와 관찰자를 실제로 지나 캐릭터가 자라는지 본다.
    """

    @pytest.fixture(autouse=True)
    def on(self, monkeypatch):
        monkeypatch.setattr(observe, "enabled", lambda: True)
        monkeypatch.setattr(observe, "retention_days", lambda: 90)
        monkeypatch.setattr(observe, "sample_seconds", lambda: 60)

    def test_a_user_with_no_runes_still_levels_up(self, tmp_path):
        from vader.core.loader import discover

        rune_dir = tmp_path / "runes"
        rune_dir.mkdir()
        loaded, failures = discover(rune_dir)
        assert (loaded, failures) == ([], [])       # 룬 0개가 정상 상태다

        store = ObservationStore(tmp_path / "o.jsonl", days=90)
        clock = [datetime(2026, 7, 20, 21, 0, 0)]
        observer = SessionObserver(
            store, now=lambda: clock[0], app_name=lambda: "obsidian.exe"
        )
        observer.start()

        before = derive(store.load(), now=clock[0])
        assert before.exp == 0

        # 열흘간 매일 밤 한 시간씩 관찰된다
        for day in range(10):
            for minute in range(60):
                clock[0] = datetime(2026, 7, 20 + day, 22, minute)
                observer.sample()

        after = derive(store.load(), now=clock[0])

        assert after.exp == 600
        assert after.level > before.level
        assert after.traits.night == 1.0
        assert after.traits.rhythm > 0
        assert after.jobs == 0                      # 룬을 하나도 안 썼다
        assert "밤" in after.personality

    def test_turning_it_off_stops_growth_but_keeps_what_grew(
        self, tmp_path, monkeypatch
    ):
        """끄는 대가가 손실이면 사람은 끄지 못한다."""
        store = ObservationStore(tmp_path / "o.jsonl", days=90)
        clock = [datetime(2026, 7, 30, 14, 0)]
        observer = SessionObserver(
            store, now=lambda: clock[0], app_name=lambda: "code.exe"
        )
        for minute in range(30):
            clock[0] = datetime(2026, 7, 30, 14, minute)
            observer.sample()
        grown = derive(store.load(), now=clock[0])

        monkeypatch.setattr(observe, "enabled", lambda: False)
        for minute in range(30):
            clock[0] = datetime(2026, 7, 30, 15, minute)
            observer.sample()

        after = derive(store.load(), now=clock[0])
        assert after.exp == grown.exp        # 자란 것은 남는다
        assert after.level == grown.level

    def test_reset_takes_growth_back_to_zero(self, tmp_path):
        store = ObservationStore(tmp_path / "o.jsonl", days=90)
        clock = [datetime(2026, 7, 30, 14, 0)]
        observer = SessionObserver(
            store, now=lambda: clock[0], app_name=lambda: "code.exe"
        )
        for minute in range(30):
            clock[0] = datetime(2026, 7, 30, 14, minute)
            observer.sample()
        assert derive(store.load(), now=clock[0]).exp == 30

        store.reset()

        assert derive(store.load(), now=clock[0]).exp == 0


class TestDoesNotJudgeTooEarly:
    """관찰 1건으로 성격을 단정하면 안 된다. 스모크에서 발견한 결함."""

    def test_one_observation_does_not_claim_a_personality(self):
        character = derive(seen("kakaotalk.exe", count=1), now=NOW)

        # 축은 계산된다 — 앱 하나라 variety 가 0.1 로 최댓값이 된다
        assert character.traits.dominant == "variety"
        # 그래도 말하지는 않는다
        assert character.knows_you is False
        assert "아직" in character.personality

    def test_speaks_once_there_is_enough_to_go_on(self):
        from vader.core.character import MIN_OBSERVATIONS_FOR_PERSONALITY

        enough = derive(
            seen("code.exe", hour=23, count=MIN_OBSERVATIONS_FOR_PERSONALITY), now=NOW
        )

        assert enough.knows_you is True
        assert "아직" not in enough.personality

    def test_the_boundary_is_exact(self):
        from vader.core.character import MIN_OBSERVATIONS_FOR_PERSONALITY as FLOOR

        assert derive(seen("a.exe", count=FLOOR - 1), now=NOW).knows_you is False
        assert derive(seen("a.exe", count=FLOOR), now=NOW).knows_you is True
