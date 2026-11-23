"""실행 일지. **룬이 왜 그렇게 판단했는지**를 한 줄씩 남긴다.

마스터 요청 (2026-08-07):

> 이 행은 왜 빠졌어?

지금까지 그 답은 화면의 로그 창에만 있었다. `JobView.logs` 는 메모리에
500줄 상한이고 앱을 닫으면 사라진다. **어제 실행을 오늘 설명할 수 없었다.**

그래서 원장(`usage.py`)과 같은 모양으로 디스크에 남긴다 — 하루에 파일 하나,
한 줄에 판단 하나. JSONL 인 이유도 같다: 추가만 하면 되고 중간에 죽어도
앞줄이 멀쩡하다.

## 남기는 것과 나가는 것이 다르다

    디스크    룬이 남긴 그대로. 품명도 들어 있다 (사람이 읽는다)
    LLM       숫자와 개수만. 문서에서 뽑은 문자열은 안 나간다

`data-map.md` 의 **결과** 줄이 이미 정한 선이다 — "성공·실패·건수는 나가도
된다. **산출물 내용은 아니다** — 병원 문서가 여기 있다". 품명은 산출물
내용이다. 그래서 `explain()` 이 문자열 필드값을 지운다.

**보존은 무기한이다.** 실행 한 번에 수십 줄이고 문서 원문은 안 들어간다.
지우면 "그때 왜 그랬나" 를 영영 설명 못 한다.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from vader.core import config

__all__ = ["Entry", "record", "read", "explain", "runs", "search_runs"]

#: 한 번에 펫에게 주는 줄 수. 긴 배치는 수백 줄이 되는데 전부 실으면
#: 프롬프트가 일지로 가득 찬다. **최근 것이 궁금한 것이다.**
MAX_LINES = 40

#: 원장과 같은 이유로 모듈에 둔다. 룬은 동시에 돌 수 있고 워커 스레드마다
#: 자기 컨텍스트를 갖는다 — 락이 인스턴스에 있으면 서로 다른 락을 잡는다.
_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class Entry:
    at: str
    job_id: str
    rune_id: str
    level: str
    message: str
    #: 룬이 `ctx.log(..., page=3, stated=458000)` 로 붙인 것.
    fields: dict = field(default_factory=dict)


def record(entry: Entry) -> bool:
    """한 줄 남긴다. **실패해도 던지지 않는다.**

    일지를 못 쓰는 것이 도는 Job 을 멈추면 안 된다. 원장과 같은 규칙이다.
    """
    try:
        path = _path(date.fromisoformat(entry.at[:10]))
        line = json.dumps(asdict(entry), ensure_ascii=False, default=str)
        with _WRITE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                # 죽는 순간에 반쯤 쓰인 앞줄이 이 줄까지 삼키지 않게 한다.
                if stream.tell() and not _ends_with_newline(path):
                    stream.write("\n")
                stream.write(line + "\n")
        return True
    except Exception:
        return False


def read(
    *, rune_id: str = "", day: date | None = None, levels: tuple[str, ...] = ()
) -> list[Entry]:
    """그날의 줄. **읽기 실패는 빈 목록이다.**

    설명을 못 하는 것과 대화 자체가 죽는 것 중 전자가 낫다.
    """
    try:
        text = _path(day or date.today()).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = Entry(**json.loads(line))
        except (json.JSONDecodeError, TypeError):
            continue
        if rune_id and entry.rune_id != rune_id:
            continue
        if levels and entry.level not in levels:
            continue
        out.append(entry)
    return out


def explain(rune_id: str = "", day: date | None = None) -> str:
    """펫에게 줄 요약. **문서에서 뽑은 문자열은 빼고 나간다.**

    지우는 것은 필드의 *값*이지 메시지가 아니다. 메시지는 룬이 코드에 적어
    둔 고정 문구다 — "명세서 합계와 뽑아낸 금액이 다릅니다" 는 데이터가
    아니라 판단 이름이다.

    # ponytail: 예외 메시지는 룬이 만든 문자열이라 이 규칙 밖이다. 실제로
    # 새는 것을 보면 그때 예외도 걸러야 한다. 지금 전부 지우면 "왜 실패
    # 했나" 에 답할 수 없다.
    """
    entries = read(rune_id=rune_id, day=day)
    if not entries:
        return "남아 있는 실행 기록이 없습니다."
    lines = []
    for entry in entries[-MAX_LINES:]:
        safe = _numbers_only(entry.fields)
        detail = " · ".join(f"{key}={value}" for key, value in safe.items())
        lines.append(
            f"{entry.at[11:16]} [{entry.level}] {entry.message}"
            + (f" ({detail})" if detail else "")
        )
    return "\n".join(lines)


# ── 실행 기록 별 (2026-11-10 우리은하 1단계) ──
# 메인 그래프의 실행 기록 팔은 runner._finish 가 남긴 결말 줄에서만 만든다 — 실제 실행 하나 = 별 하나.
# 화면에 가는 것은 룬·시각·결과뿐이다. 실패 사유(예외 문장)는 문서 내용이 섞일 수 있어 보내지 않는다.
_END = re.compile(r"^실행이 (\w+) 로 끝났습니다")


def runs(days: int = 7, limit: int = 40, today: date | None = None) -> list[dict]:
    """최근 며칠의 끝난 실행, 새것부터. # ponytail: 하루 파일을 통째로 읽는다 — 수천 건이면 색인으로."""
    today = today or date.today()
    out: list[dict] = []
    for back in range(days):
        for entry in reversed(read(day=today - timedelta(days=back))):
            m = _END.match(entry.message)
            if not m:
                continue
            state = m.group(1).lower()
            out.append({"job_id": entry.job_id, "rune_id": entry.rune_id, "at": entry.at,
                        "state": state, "failed": state == "failed"})
            if len(out) >= limit:
                return out
    return out


def search_runs(query: str, names: dict | None = None, today: date | None = None) -> dict:
    """'최근 실패'·'어제'·룬 이름 같은 말로 실행 기록을 찾는다. 돌려준 job_id 만 화면에서 빛난다.
    찾은 실행(runs)과 찾은 범위(checked: 최근 7일 중 최신 최대 40건, truncated: 40건에서 끊겼나)도 함께 —
    못 찾았을 때 '어디까지 봤는지' 를 정확히 말한다 (codex1 P2-3). 화면은 같은 runs 로 목록을 그린다."""
    today = today or date.today()
    found = runs(today=today, limit=41)
    truncated = len(found) > 40
    found = found[:40]
    words = (query or "").split()
    names = names or {}
    keep = []
    for run in found:
        ok = True
        for w in words:
            if w in ("최근", "기록", "실행", "찾기", "보여줘", "것"):
                continue
            if "실패" in w:
                ok = run["failed"]
            elif w in ("성공", "완료", "끝난"):
                ok = run["state"] == "succeeded"
            elif w in ("취소", "취소한"):
                ok = run["state"] == "cancelled"
            elif w == "오늘":
                ok = run["at"][:10] == today.isoformat()
            elif w == "어제":
                ok = run["at"][:10] == (today - timedelta(days=1)).isoformat()
            else:
                ok = w in run["rune_id"] or w in names.get(run["rune_id"], "")
            if not ok:
                break
        if ok:
            keep.append(run)
    return {"job_ids": [r["job_id"] for r in keep], "runs": keep, "checked": len(found), "days": 7,
            "truncated": truncated, "all": found}


def _numbers_only(fields: dict) -> dict:
    """숫자와 참거짓만 남긴다. **품명·거래처는 여기서 걸린다.**

    문자열을 통째로 지우는 대신 개수를 남긴다 — "품명 2건" 은 무엇이
    빠졌는지 세는 데 쓰이고, 그 자체로는 문서 내용이 아니다.
    """
    out: dict = {}
    for key, value in (fields or {}).items():
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, (list, tuple)):
            out[f"{key}_수"] = len(value)
        # 문자열은 안 나간다. 페이지 키처럼 숫자여야 할 것이 문자열로
        # 오더라도 마찬가지다 — 여기서 예외를 만들면 선이 무너진다.
    return out


def folder() -> Path:
    """일지가 사는 곳. 펫이 "직접 열어 보세요" 라고 말할 때 쓴다.

    품명이 안 나가는 대신 **어디를 보면 되는지는 알려 준다.** 못 본다고만
    하면 사용자는 그 정보가 사라진 줄 안다.
    """
    return config.data_dir() / "journal"


def _path(day: date) -> Path:
    return folder() / f"{day.isoformat()}.jsonl"


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as stream:
        stream.seek(-1, 2)
        return stream.read(1) == b"\n"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
