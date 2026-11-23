"""목록 배포용 **깨끗한 사본** (2026-10-29 신뢰 계약 v2 11절, codex1 검토 2회).

순서가 계약이다: 원본 → **허용 목록으로 사본** → 녹화 정리 → 사본 점검 → 그 사본으로 시험·포장·서명.
시험 뒤에 파일을 빼거나 고치지 않는다 — 그러면 "시험한 것 = 배포한 것" 이 깨진다.

- 원본에 업무 자료(명세서 PDF·엑셀·DB·시험 폴더)가 있어도 **사본에 안 들어가면** 괜찮다. 무엇을 뺐는지는
  돌려줘서 사람이 본다.
- 사본에 금지 항목이 남으면 사본을 지우고 멈춘다. 조용히 고치지 않는다. 링크(symlink)는 받지 않는다.
- 녹화(`recordings/*.json`)는 정해진 필드만, 이동 키만 담는다. `at` 은 비우고 창 제목은 `stable_part` 로
  줄인다 — 녹화 시각처럼 **변하는 부분**을 자를 뿐 **개인정보 제거가 아니다**. 창 제목과 화면 요소 이름
  (`name`)은 재생에 필요해 남고, 업무 문자열이 들어 있을 수 있어 `review` 로 돌려준다 — **사람이 심사한다**.
- `check() == []` 는 "형식 점검 통과" 이지 "내용 심사 완료" 가 아니다.
"""
from __future__ import annotations

import json
import math
import re
import shutil
from pathlib import Path

ALLOWED_SUFFIXES = {".py", ".md", ".png", ".svg"}
#: 업무 자료가 사는 폴더 — 사본에 통째로 넣지 않는다.
DATA_DIRS = {"vaderai-test", "output", "done", "error", "scans", "cases", "dist"}
FORBIDDEN_SUFFIXES = {".pdf", ".xlsx", ".xls", ".db", ".sqlite", ".csv"}
FILE_LIMIT_BYTES = 1024 * 1024
RECORDING_FORMAT = "vader-recording/1"
#: 녹화 한 단계가 가질 수 있는 필드와 타입 (`recorder.Step`). 모르는 필드가 있으면 멈춘다.
STEP_FIELDS = {"at": str, "window_title": str, "control_type": str, "name": str, "automation_id": str,
               "class_name": str, "keys": str, "rel_x": (int, float), "rel_y": (int, float),
               "ctrl_x": (int, float), "ctrl_y": (int, float)}
#: 녹화가 담을 수 있는 키 표기. 글자·숫자 키는 녹화하지 않는다 (`recorder.NAV_KEYS`).
_NAV = re.compile(r"^(?:\{(?:TAB|ENTER|ESC|LEFT|UP|RIGHT|DOWN|PGUP|PGDN|END|HOME|F(?:[1-9]|1[0-2]))(?: \d{1,3})?\}|%)*$")


class ReleaseError(ValueError):
    """사람에게 그대로 보여 줄 수 있는 거절."""


def make(source: Path, dest: Path) -> dict:
    """`source` 에서 허용된 것만 `dest` 로 복사한다.

    `{"copied": [...], "left_out": [[경로, 이유], ...], "review": [녹화에 남은 창 제목·요소 이름 ...]}`.
    """
    source, dest = Path(source), Path(dest)
    if dest.exists():
        raise ReleaseError(f"사본 자리가 이미 있습니다: {dest}")
    copied, left_out, review = [], [], set()
    root = source.resolve()
    try:
        for path in sorted(p for p in source.rglob("*") if p.is_file() or p.is_symlink()):
            rel = path.relative_to(source)
            if "__pycache__" in rel.parts:
                continue
            if any(part.lower() in DATA_DIRS for part in rel.parts[:-1]):
                left_out.append([rel.as_posix(), "업무 자료 폴더"])
                continue
            recording = _is_recording(rel)
            if path.suffix.lower() not in ALLOWED_SUFFIXES and not recording:
                left_out.append([rel.as_posix(), "허용 목록 밖"])
                continue
            if _linked(path, root):
                raise ReleaseError(f"링크 파일은 넣지 않습니다: {rel.as_posix()}")
            if path.stat().st_size > FILE_LIMIT_BYTES:
                raise ReleaseError(f"1MB 넘는 파일은 넣지 않습니다: {rel.as_posix()}")
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if recording:
                review.update(_review_strings(write_clean_recording(path, target)))
            else:
                shutil.copy2(path, target)
            copied.append(rel.as_posix())
        problems = check(dest)
        if problems:
            raise ReleaseError("사본에 넣으면 안 되는 것이 남았습니다: " + "; ".join(problems[:5]))
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return {"copied": copied, "left_out": left_out, "review": sorted(review)}


def check(folder: Path) -> list[str]:
    """배포 사본(또는 풀어 본 업로드)에 금지 항목이 있는지. 없으면 빈 목록. **내용 심사가 아니다.**"""
    folder = Path(folder)
    root = folder.resolve()
    problems = []
    for path in sorted(p for p in folder.rglob("*") if p.is_file() or p.is_symlink()):
        rel = path.relative_to(folder)
        name = rel.as_posix()
        if "__pycache__" in rel.parts:
            continue
        if _linked(path, root):
            problems.append(f"{name}: 링크 파일")
        elif any(part.lower() in DATA_DIRS for part in rel.parts[:-1]):
            problems.append(f"{name}: 업무 자료 폴더")
        elif path.suffix.lower() in FORBIDDEN_SUFFIXES:
            problems.append(f"{name}: 업무 자료 형식")
        elif path.suffix.lower() not in ALLOWED_SUFFIXES and not _is_recording(rel):
            problems.append(f"{name}: 허용 목록 밖")
        elif path.stat().st_size > FILE_LIMIT_BYTES:
            problems.append(f"{name}: 1MB 넘음")
        elif _is_recording(rel):
            try:
                # **이미 정리된 그대로** 여야 한다 — 원본 녹화를 나중에 넣으면 여기서 걸린다 (codex1 P1-2).
                # 포장 때 몰래 다시 정리하지 않는다: 시험한 지문이 깨진다.
                if path.read_bytes() != _dump(_clean(path)):
                    problems.append(f"{name}: 정리하지 않은 녹화 (배포 사본을 다시 만들거나 녹화 가져오기로 넣으세요)")
            except (ReleaseError, ValueError) as exc:
                problems.append(f"{name}: {exc}")
    return problems


def write_clean_recording(source: Path, target: Path) -> dict:
    """정리한 녹화를 쓴다. 정리본을 돌려준다."""
    cleaned = _clean(source)
    Path(target).write_bytes(_dump(cleaned))
    return cleaned


def _dump(cleaned: dict) -> bytes:
    """정리본의 **바이트** — 서버(`catalog-archive.ts`)의 `JSON.stringify(data, null, 2)` 와 같게 직접 쓴다.
    줄바꿈 LF, 숫자는 JS 표기(1.0→1, 1e-05→0.00001), 비유한 값·참거짓은 거절 (2026-11-23 eonm_ipgo 첫 제출 거절, codex1 4차)."""
    def value(v):
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ReleaseError(f"녹화 좌표가 숫자가 아닙니다: {v!r}")
        # 정수는 JS 안전 범위 안만 — 밖이면 서버가 다른 수로 읽는다(2**53+1). 큰 정수는 isfinite 에서 넘치기도 한다 (codex1 5차)
        # 실수도 같은 범위 — 2**53 넘는 실수는 모두 정수값이라 `1e16` 이 정수로 쓰였다가 다시 읽을 때 걸린다 (codex1 6차)
        if isinstance(v, float) and not math.isfinite(v) or abs(v) > 2**53 - 1:
            raise ReleaseError(f"녹화 좌표가 다룰 수 없는 수입니다: {str(v)[:20]}")
        return _js_number(v)

    nl = "\n"

    def step(item: dict) -> str:
        if not item:
            return "{}"
        rows = ("," + nl).join(f"      {json.dumps(k, ensure_ascii=False)}: {value(v)}" for k, v in item.items())
        return "{" + nl + rows + nl + "    }"

    steps = "[]" if not cleaned["steps"] else (
        "[" + nl + ("," + nl).join(f"    {step(x)}" for x in cleaned["steps"]) + nl + "  ]")
    text = "{" + nl + '  "format": ' + value(cleaned["format"]) + "," + nl + '  "steps": ' + steps + nl + "}"
    return text.encode("utf-8")


def _js_number(v: int | float) -> str:
    """JS `Number.prototype.toString` — 가장 짧은 자릿수(파이썬 repr 과 같다)를, 10의 지수가 -7~20 이면 소수로, 밖이면 1e-7·1.5e+21 로."""
    from decimal import Decimal

    d = Decimal(repr(float(v)) if isinstance(v, float) else str(v))
    if d == 0:
        return "0"
    exp = d.adjusted()
    if -7 < exp < 21:
        text = format(d, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    sign, digits, _ = d.as_tuple()
    mantissa = str(digits[0]) + ("." + "".join(map(str, digits[1:])).rstrip("0") if len(digits) > 1 else "")
    mantissa = mantissa.rstrip(".")
    return ("-" if sign else "") + f"{mantissa}e{'+' if exp > 0 else '-'}{abs(exp)}"


def _is_recording(rel: Path) -> bool:
    return rel.parts[0] == "recordings" and len(rel.parts) == 2 and rel.suffix == ".json"


def _linked(path: Path, root: Path) -> bool:
    """링크이거나 사본 루트 밖을 가리키면 참 — 허용 확장자 이름으로 바깥 파일을 끌어오지 못하게 (codex1 P2-6)."""
    return path.is_symlink() or not path.resolve().is_relative_to(root)


def _clean(path: Path) -> dict:
    from vader.core.context import stable_part

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"녹화를 읽지 못했습니다: {exc}") from None
    if not isinstance(data, dict) or set(data) != {"format", "steps"} or data["format"] != RECORDING_FORMAT \
            or not isinstance(data["steps"], list):
        raise ReleaseError("녹화 형식이 아니거나 모르는 항목이 있습니다")
    steps = []
    for step in data["steps"]:
        if not isinstance(step, dict) or not set(step) <= set(STEP_FIELDS):
            raise ReleaseError(f"녹화 단계에 모르는 항목이 있습니다: {sorted(set(step) - set(STEP_FIELDS))[:3]}")
        for key, kind in STEP_FIELDS.items():
            if key in step and (not isinstance(step[key], kind) or isinstance(step[key], bool)):
                raise ReleaseError(f"녹화 단계의 {key} 값이 맞지 않습니다")
        if not _NAV.match(step.get("keys", "")):
            raise ReleaseError(f"녹화에 이동 키가 아닌 입력이 있습니다: {step['keys'][:20]}")
        steps.append({**step, "window_title": stable_part(step.get("window_title", "")), "at": ""})
    return {"format": RECORDING_FORMAT, "steps": steps}


def _review_strings(recording: dict) -> list[str]:
    """심사할 문자열 — 창 제목과 화면 요소 이름. 업무 화면의 글자가 들어 있을 수 있다."""
    out = []
    for step in recording["steps"]:
        out += [f"창 제목: {step['window_title']}"] if step.get("window_title") else []
        out += [f"요소 이름: {step['name']}"] if step.get("name") else []
    return out
