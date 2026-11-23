"""펫의 정체성. **사용자가 소유하고 다시 만나는 것.**

Phase 6 의 `Character` 와 다르다. 그쪽은 관찰에서 파생된 숫자
(`level`, `Traits`)이고 사용자가 만든 것이 아니다. codex 판단대로,

> "생성" 은 숫자가 파생됐다는 뜻이 아니라 **사용자가 소유하고 다시 만날 수
> 있는 대화 주체가 생겼다는 뜻**이다.

그래서 여기 사는 것은 셋뿐이다 — **ID · 이름 · 말투.** 외형은 초기 필수가
아니고, **레벨과 관찰 성향은 생성의 전제가 아니다.** 먼저 이름과 성격을
가진 비서를 만들고, 관찰을 허용한 경우에만 이후 성향이 변한다.

데이터 지도상 이것은 **기계를 떠나도 되는** 쪽이다 — 사용자가 만든 것이고
행동에서 파생되지 않는다. 그래서 관찰과 다른 파일에 산다. 한 파일에 섞으면
"이건 따라가도 되나" 를 매번 따져야 한다.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vader.core import config

__all__ = ["Pet", "TONES", "load", "save"]

#: 말투. 프리셋으로 두는 이유는 자유 입력을 그대로 시스템 프롬프트에 넣으면
#: 사용자가 자기 펫에게 무엇이든 시키는 통로가 되기 때문이다.
TONES = {
    "다정한": "따뜻하고 다정하게, 존댓말로 말합니다.",
    "간결한": "군더더기 없이 짧게 답합니다. 인사와 사족을 생략합니다.",
    "차분한": "차분하고 정중하게, 서두르지 않고 설명합니다.",
    "발랄한": "밝고 가볍게, 그러나 사실은 정확하게 말합니다.",
}

DEFAULT_TONE = "다정한"


@dataclass
class Pet:
    #: 지속되는 정체성. 이름을 바꿔도 같은 펫이다.
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    tone: str = DEFAULT_TONE

    @property
    def created(self) -> bool:
        """이름이 있으면 만들어진 것이다. 이름이 관계의 최소 단위다."""
        return bool(self.name.strip())

    def persona(self) -> str:
        """시스템 프롬프트의 앞부분.

        **관찰에서 나온 것은 한 글자도 안 들어간다.** 레벨도, 성향도,
        성격 문장도. 그것들은 기계를 떠나면 안 되는 쪽이다.
        """
        return (
            f"당신의 이름은 '{self.name}' 입니다. "
            f"{TONES.get(self.tone, TONES[DEFAULT_TONE])} "
            "당신은 VaderAI 라는 데스크톱 자동화 도구 안에 사는 비서입니다. "
            "사용법과 설치된 자동화(룬)를 설명해 줍니다. "
            "**당신은 자동화를 직접 실행할 수 없습니다.** 실행은 사용자가 "
            "화면에서 직접 합니다. 실행해 달라는 부탁을 받으면 어디서 어떻게 "
            "하는지 알려 주되, 했다고 말하지 마세요."
        )


def path() -> Path:
    return config.data_dir() / "pet.json"


def load() -> Pet:
    """없으면 이름 없는 펫. **없다고 만들지 않는다** — 이름은 사용자가 짓는다."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return Pet()
    if not isinstance(data, dict):
        return Pet()
    return Pet(
        id=str(data.get("id") or uuid.uuid4().hex),
        name=str(data.get("name") or ""),
        tone=str(data.get("tone") or DEFAULT_TONE),
    )


def save(pet: Pet) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # 체크포인트·캐시와 같은 방식. 쓰다 죽어도 반쯤 쓰인 파일이 안 남는다.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent,
        prefix=".pet.", suffix=".tmp", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        json.dump(asdict(pet), stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
