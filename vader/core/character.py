"""관찰에서 성격과 성장을 뽑는다.

**경험치는 관찰에서만 나온다.** Phase 6 완료 기준이 "룬을 설치하지 않은
사용자에게도 성장 변화가 재현된다" 이므로, Job 완료는 세기만 하고 성장에 쓰지
않는다. 룬을 쓰는 사람이 더 빨리 자라면 그건 자동화 도구의 성장축이지 사용자의
성장축이 아니다.

판단이 여기 다 모여 있고 화면은 `ui.py` 가 그린다 — `view.py` 와 같은 이유다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from vader.core.observe import (
    FOREGROUND,
    JOB_FINISHED,
    SESSION_END,
    SESSION_START,
    Observation,
)

#: 관찰 1건이 주는 경험치. 표본 주기가 기본 60초라 대략 "활동 1분 = 1" 이다.
EXP_PER_OBSERVATION = 1

#: 레벨 하나에 필요한 경험치. 선형이다 — 곡선을 넣을 근거가 아직 없다.
# ponytail: 선형 유지. 실사용에서 레벨이 너무 빨리/느리게 오르면 그때 곡선을 넣는다.
EXP_PER_LEVEL = 240

#: 성격을 말하기 전에 필요한 최소 관찰 수. 표본 주기 60초 기준 약 1시간.
#
# 없으면 관찰 1건으로 "여러 일을 옮겨 다닙니다" 가 나온다 — 앱 하나만 봤는데
# variety 가 0.1 로 최댓값이 되기 때문이다. 사람에 대해 한 시간도 안 보고
# 성격을 단정하면 안 된다. 축 계산 자체는 그대로 두고 **말하는 것만** 막는다.
MIN_OBSERVATIONS_FOR_PERSONALITY = 60

#: 밤 사람으로 보는 시각 (포함~미포함). 22시~새벽 5시.
NIGHT_HOURS = set(range(22, 24)) | set(range(0, 5))

#: 이 시간 이상 벌어지면 같은 세션으로 보지 않는다. 세션 종료 관찰이 유실될 수
#: 있어(강제 종료, 정전) 시작만 두 번 남는 경우를 막는다.
SESSION_GAP = timedelta(hours=6)


@dataclass(frozen=True)
class Traits:
    """0.0~1.0 네 축. 관찰이 없으면 전부 0 이다."""

    #: 밤 시간대 활동 비율
    night: float = 0.0
    #: 한 앱에 머무는 정도. 연속 표본이 같은 앱일 비율
    focus: float = 0.0
    #: 앱 다양성. 서로 다른 앱 수를 10개로 정규화
    variety: float = 0.0
    #: 활동 리듬. 관찰이 있는 날 수를 최근 14일로 정규화
    rhythm: float = 0.0

    @property
    def dominant(self) -> str:
        """가장 높은 축. 전부 0 이면 "unknown"."""
        scores = {
            "night": self.night,
            "focus": self.focus,
            "variety": self.variety,
            "rhythm": self.rhythm,
        }
        best = max(scores, key=lambda key: scores[key])
        return best if scores[best] > 0 else "unknown"


@dataclass(frozen=True)
class Character:
    level: int
    exp: int
    traits: Traits
    #: 관찰된 세션 수. 성장에는 쓰지 않고 화면에만 쓴다.
    sessions: int = 0
    #: Job 완료 수. **성장에 쓰지 않는다** — 룬과 독립인지 비교하려고 센다.
    jobs: int = 0

    @property
    def exp_into_level(self) -> int:
        return self.exp % EXP_PER_LEVEL

    @property
    def percent(self) -> int:
        return round(self.exp_into_level / EXP_PER_LEVEL * 100)

    @property
    def knows_you(self) -> bool:
        """성격을 말할 만큼 봤는가."""
        return self.exp >= MIN_OBSERVATIONS_FOR_PERSONALITY

    @property
    def personality(self) -> str:
        if not self.knows_you:
            return "아직 당신을 배우는 중입니다"
        return {
            "night": "밤에 일하는 사람 곁을 지킵니다",
            "focus": "한 가지에 오래 붙어 있습니다",
            "variety": "여러 일을 옮겨 다닙니다",
            "rhythm": "매일 꾸준히 옵니다",
            "unknown": "아직 당신을 배우는 중입니다",
        }[self.traits.dominant]


def derive(observations: list[Observation], *, now: datetime | None = None) -> Character:
    """관찰 목록에서 캐릭터를 만든다. 순수 함수다 — 저장소를 모른다."""
    foreground = [item for item in observations if item.kind == FOREGROUND]
    jobs = sum(1 for item in observations if item.kind == JOB_FINISHED)
    exp = len(foreground) * EXP_PER_OBSERVATION

    return Character(
        level=exp // EXP_PER_LEVEL + 1,
        exp=exp,
        traits=_traits(foreground, now=now or datetime.now()),
        sessions=_count_sessions(observations),
        jobs=jobs,
    )


def _traits(foreground: list[Observation], *, now: datetime) -> Traits:
    if not foreground:
        return Traits()

    night = sum(1 for item in foreground if item.at.hour in NIGHT_HOURS)
    apps = {item.value for item in foreground if item.value}

    # 연속 표본이 같은 앱이면 머문 것으로 본다. 표본이 하나면 판단하지 않는다.
    pairs = list(zip(foreground, foreground[1:]))
    same = sum(1 for a, b in pairs if a.value == b.value)

    recent = now - timedelta(days=14)
    active_days = {item.at.date() for item in foreground if item.at >= recent}

    return Traits(
        night=round(night / len(foreground), 3),
        focus=round(same / len(pairs), 3) if pairs else 0.0,
        variety=round(min(1.0, len(apps) / 10), 3),
        rhythm=round(min(1.0, len(active_days) / 14), 3),
    )


def _count_sessions(observations: list[Observation]) -> int:
    """세션 시작 관찰을 센다.

    종료 관찰은 유실될 수 있다 — 강제 종료나 정전이면 남지 않는다. 그래서 시작만
    세고, 시작이 없는데 관찰이 이어지는 경우(로그가 잘렸을 때)는 간격으로 나눈다.
    """
    starts = [item for item in observations if item.kind == SESSION_START]
    if starts:
        return len(starts)

    stamps = [item.at for item in observations if item.kind != SESSION_END]
    if not stamps:
        return 0
    sessions = 1
    for before, after in zip(stamps, stamps[1:]):
        if after - before > SESSION_GAP:
            sessions += 1
    return sessions
