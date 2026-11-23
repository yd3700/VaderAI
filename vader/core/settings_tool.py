"""대화로 룬 **설정값**을 바꾼다. Phase 11.7.

마스터 요청 (2026-08-07):

> 이러한 작업 중 수정이 필요하면, **대화 기능으로 수정이 가능하도록**.

오늘 실제로 그랬다 — 마스터가 화면을 캡처해 보내고, 내가 고치고, 다시
설치했다. 그 왕복을 대화 안에서 끝내는 것이 목표다.

## 선을 긋는다

    설정값(시작 쪽 · 기본 도시 · 결과 수)   →  대화로 바꾼다
    프롬프트 · 코드                          →  안 바꾼다 (Phase 12 의 무게)

오늘 고친 것은 프롬프트와 코드였다. 그건 검증 없이 운영이 바뀐다.
**바꿀 수 있는 것은 룬이 `RuneField` 로 선언한 것뿐이다.**

## 조용히 안 바꾼다

쓰기라서 **승인을 받는다.** 펫이 설정을 조용히 바꾸면 "왜 어제와 다르지"
를 아무도 설명 못 한다. 그리고 무엇을 언제 바꿨는지 남긴다 — 되돌릴 수
있어야 한다.

**다음 실행부터 적용된다.** 도는 Job 의 설정을 바꾸면 그 Job 의 앞뒤가
달라진다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vader.core import config

__all__ = ["Change", "SettingsTool", "history", "TOOL_SCHEMA"]

#: 대화로 못 바꾸는 것. 비밀값은 화면에서 받는다 — 대화 기록에 남으면
#: 그 기록이 곧 유출이다.
SECRET_KINDS = frozenset({"secret"})


@dataclass(frozen=True)
class Change:
    at: str
    rune_id: str
    field: str
    before: str
    after: str


TOOL_SCHEMA = {
    "name": "change_setting",
    "description": (
        "설치된 룬의 설정값 하나를 바꾼다. 룬이 선언한 설정만 바꿀 수 있고, "
        "비밀값(API 키 등)은 못 바꾼다. 사용자 승인을 받은 뒤에만 적용된다."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "rune_id": {"type": "string", "description": "설치된 룬의 id"},
            "field": {"type": "string", "description": "설정 이름"},
            "value": {"type": "string", "description": "새 값"},
        },
        "required": ["rune_id", "field", "value"],
        "additionalProperties": False,
    },
}


class SettingsTool:
    """펫이 부르는 설정 변경 도구. **승인을 받아야 실제로 바뀐다.**"""

    def __init__(self, *, ask=None, log=None) -> None:
        #: 사용자에게 묻는 함수. 없으면 **아무것도 안 바꾼다** — 승인 통로가
        #: 없는데 바꾸면 그게 조용한 변경이다.
        self._ask = ask
        self._log = log or (lambda level, message, **fields: None)

    def invoke(self, rune_id: str, field: str, value: str) -> str:
        found = _field_of(rune_id, field)
        if found is None:
            return f"'{rune_id}' 룬에 '{field}' 설정이 없습니다."
        if found.kind in SECRET_KINDS:
            return (
                f"'{found.label or field}' 은 비밀값이라 대화로 못 바꿉니다. "
                "`설정…` 화면에서 넣어 주세요."
            )
        before = str(config.rune_config(rune_id).get(field, found.default or ""))
        if self._ask is None:
            return "승인 통로가 없어 바꾸지 않았습니다."
        if not self._ask(
            "설정을 바꿀까요?",
            f"{rune_id} · {found.label or field}\n\n"
            f"지금: {before or '(없음)'}\n바꿀 값: {value}\n\n"
            "다음 실행부터 적용됩니다.",
        ):
            return "사용자가 승인하지 않아 그대로 두었습니다."

        config.set_rune_configs(rune_id, {field: _typed(found.kind, value)})
        _record(Change(datetime.now().isoformat(timespec="seconds"), rune_id, field, before, value))
        self._log("info", "대화로 설정을 바꿨습니다", rune=rune_id, field=field)
        return (
            f"'{found.label or field}' 을 {before or '(없음)'} → {value} 로 "
            "바꿨습니다. **다음 실행부터 적용됩니다.**"
        )


def _field_of(rune_id: str, field: str):
    from vader.core.loader import discover

    for rune in discover()[0]:
        if rune.manifest.id != rune_id:
            continue
        for declared in rune.manifest.required_fields:
            if declared.name == field:
                return declared
    return None


def _typed(kind: str, value: str):
    if kind == "number":
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return value
    return value


def _path() -> Path:
    return config.data_dir() / "setting_changes.jsonl"


def _record(change: Change) -> None:
    """무엇을 언제 바꿨는지 남긴다. **되돌리려면 알아야 한다.**"""
    try:
        target = _path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(change.__dict__, ensure_ascii=False) + "\n")
    except Exception:
        # 기록 실패가 변경을 되돌리지는 않는다. 원장과 같은 규칙이다.
        pass


def history() -> list[Change]:
    try:
        text = _path().read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    out = []
    for line in text.splitlines():
        try:
            out.append(Change(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            continue
    return out
