"""룬 화면 문구의 영어 — 룬 패키지 안 `i18n_en.py` (2026-11-24 영문판 5단계 후속, codex1 협의).

번역은 그 버전의 룬과 함께 다닌다(버전별 사전). 코어는 이 파일을 **실행하지 않고** 값만 읽는다(ast.literal_eval) —
서명·승인된 룬 코드와 같은 폴더에 있어도 import 하지 않으므로 화면 문구 말고는 아무것도 바꾸지 못한다.

    EN = {
        "text": {"원문": "English", "{0}: 읽지 못했어요": "{0}: couldn't read it"},   # 룬이 쓴 문구, 값이 끼면 {n} 틀
        "choices": {"질문 원문(틀 가능)": {"선택지": "Choice", "열: {0}": "Column: {0}"}},   # 그 질문의 고정 선택지만
        "values": {"요약 키": {"값": "Value"}},                                        # 그 요약 키의 고정 값만
    }

모양이 틀리거나 너무 크면 없는 것으로 본다(화면은 원문 + '원문 언어' 표시).
"""
from __future__ import annotations

import ast
from pathlib import Path

FILE = "i18n_en.py"
LIMIT_BYTES = 256 * 1024


def load(folder: Path) -> dict | None:
    path = Path(folder) / FILE
    try:
        if not path.is_file() or path.stat().st_size > LIMIT_BYTES:
            return None
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == ["EN"]:
            try:
                return _checked(ast.literal_eval(node.value))
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                return None
    return None


def _flat(d) -> bool:
    return isinstance(d, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in d.items())


def _checked(data) -> dict | None:
    if not isinstance(data, dict) or not set(data) <= {"text", "choices", "values"}:
        return None
    text, choices, values = data.get("text", {}), data.get("choices", {}), data.get("values", {})
    if not _flat(text):
        return None
    for scope in (choices, values):
        if not isinstance(scope, dict) or not all(isinstance(k, str) and _flat(v) for k, v in scope.items()):
            return None
    return {"text": text, "choices": choices, "values": values}
