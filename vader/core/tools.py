"""펫에게 주는 **사실**. 지금은 시계 하나뿐이다.

2026-08-11 에 정보 룬(날씨·네이버 검색)을 걷어내면서 이 파일도 같이 줄었다.
마스터가 제품을 대행 하나로 좁혔고, 조회 도구는 그 넷 어디에도 없었다.
**더 나쁜 것은 그것들이 할루시네이션의 표면이었다는 것이다** — 3건을
조회하고 6줄을 답한 일, 진짜 데이터를 "임의로 만든 답변" 이라고 자백한 일이
전부 여기서 나왔다.

시각은 남는다. 인터넷이 필요 없고, 모두에게 필요하고, **지어낼 여지가 없다.**
모델은 오늘이 며칠인지 모른다.

되살릴 때는 `git log -- vader/core/infotool.py` 를 봐라. 계약(선언만 믿지
않고 코어가 소스를 훑는 것)은 그대로 쓸 만하다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

__all__ = ["Fact", "now_line", "facts_for"]


@dataclass(frozen=True)
class Fact:
    """펫에게 주는 사실 하나. **어디서 언제 온 것인지 같이 간다.**"""

    text: str
    source: str
    at: datetime
    #: 같은 주제의 옛것을 밀어내는 열쇠.
    topic: str

    def line(self) -> str:
        return f"{self.text} (출처: {self.source})"


def now_line() -> Fact:
    """지금 시각. **항상 준다** — 공짜이고, 모델은 오늘이 며칠인지 모른다."""
    at = datetime.now()
    return Fact(
        # 한글은 strftime 밖에서 붙인다. Windows 의 strftime 은 서식 문자열을
        # 로캘 코덱으로 인코딩해서, 한국어 로캘이 아닌 기계(CI)에서 터진다.
        text=(
            f"지금 시각: {at.year}년 {at.month:02d}월 {at.day:02d}일 "
            f"{at.hour:02d}시 {at.minute:02d}분 ({'월화수목금토일'[at.weekday()]}요일)"
        ),
        source="이 컴퓨터의 시계",
        at=at,
        topic="now",
    )


def facts_for(question: str, *, log=None) -> list[Fact]:
    """이번 턴에 붙일 사실. **코어가 아는 것은 시각뿐이다.**

    전에는 여기서 설치된 정보 룬에게 물었다. 그 룬들이 사라졌으므로 물을
    데가 없고, **없는 것을 아는 척하지 않는 것이 지금 약속이다.**
    """
    del question, log
    return [now_line()]
