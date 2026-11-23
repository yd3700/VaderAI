"""공개 저장소(데스크톱 앱만)에는 없는 것 — 웹·설계 문서·병원 업무 룬. 그것을 읽는 시험은 거기서 건너뛴다 (2026-11-23)."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def needs(*parts: str):
    missing = [p for p in parts if not (ROOT / p).exists()]
    return pytest.mark.skipif(bool(missing), reason="비공개 저장소에만 있음: " + ", ".join(missing))
