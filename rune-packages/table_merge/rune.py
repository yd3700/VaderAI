"""표 합치기·중복 정리 — VaderAI 기본 룬 (2026-11-19 마스터·codex1 합의: 기본 룬은 오프라인 표 룬 하나).

여러 엑셀·CSV 를 칸 이름으로 맞춰 하나로 합치고, 같은 건을 정리한다.
- 기준 칸(예: 전화)이 같고 나머지도 같으면 → 중복으로 뺀다. 한쪽에만 있는 값은 남는 행에 채운다.
- 기준 칸이 같은데 다른 칸 값이 다르면 → 사람에게 묻는다 (답은 체크포인트에 남아, 멈췄다 다시 돌리면 이어서 묻는다).
- 기준 칸이 모두 비어 있으면 → 뺀다.
결과는 새 파일 두 개(통합본·제외목록). 원본은 건드리지 않는다. 인터넷·화면을 쓰지 않는다.
"""
from __future__ import annotations

from datetime import date, datetime
from re import split, sub

from vader.sdk import Failure, RuneField, RuneManifest, RuneResult, UserActionRequest

ALL = "모든 칸"
KEEP, REPLACE, BOTH, KEEP_REST = "앞의 것 유지", "뒤의 것으로 바꾸기", "둘 다 남기기", "남은 것도 모두 앞의 것 유지"


def text(value) -> str:
    """셀 값을 비교·표시용 글자로. 엑셀 숫자 3.0 은 3, 날짜는 2026-11-19."""
    if value is None:
        return ""
    if isinstance(value, datetime) and not (value.hour or value.minute or value.second):
        value = value.date()
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


#: 이름에 이 말이 든 칸만 전화번호로 본다 — 다른 번호(사번·계좌)의 구분 기호는 지우지 않는다 (codex1 2026-11-19)
PHONE = ("전화", "휴대", "핸드폰", "연락처", "phone", "tel", "mobile")


def norm(value, phone=False) -> str:
    """같은 건인지 볼 때의 값 — 띄어쓰기·대소문자를 무시한다. 전화 칸이면 숫자만 (010-0000-1001 = 01000001001)."""
    s = sub(r"\s+", " ", text(value)).strip().casefold()
    if phone:
        digits = sub(r"[\s\-().+]", "", s)
        if digits.isdigit():
            return digits
    return s


def column(name) -> str:
    return sub(r"\s+", "", text(name)).casefold()


class TableMerge:
    manifest = RuneManifest(
        id="table_merge", name="표 합치기·중복 정리", version="1.0.0", profile="table",
        required_fields=[RuneField("기준열", label="같은 건으로 볼 칸 (예: 전화 · 여럿이면 쉼표 · 비우면 실행할 때 물어요)",
                                   default="", optional=True)],
    )

    def run(self, ctx, inputs):
        if not inputs:
            return RuneResult(ok=False, failures=[Failure("input", "합칠 엑셀·CSV 파일을 골라 주세요.")])
        tables = []
        for n, item in enumerate(inputs, 1):
            name = item.name or f"파일{n}"
            try:
                rows = ctx.read_table(item)
            except Exception as exc:
                return RuneResult(ok=False, failures=[Failure("read", f"{name}: 읽지 못했어요 ({exc})")])
            if len(rows) < 2:
                return RuneResult(ok=False, failures=[Failure("empty", f"{name}: 첫 줄(칸 이름) 아래에 행이 없어요.")])
            header = [text(h).strip() or f"열{i}" for i, h in enumerate(rows[0], 1)]
            tables.append((name, header, rows[1:]))

        # 칸 이름으로 맞춘다 — 순서가 달라도, 한 파일에만 있는 칸도 합친다
        columns, at = [], {}
        for _, header, _ in tables:
            for h in header:
                if column(h) not in at:
                    at[column(h)] = len(columns)
                    columns.append(h)

        keys = self.key_columns(ctx, tables, columns, at)
        if isinstance(keys, RuneResult):
            return keys
        whole = keys is None
        keys = list(range(len(columns))) if whole else keys
        phone = [any(w in column(c) for w in PHONE) for c in columns]

        kept, dropped, unsure, index = [], [], [], {}
        total = sum(len(rows) for _, _, rows in tables)
        done = filled = 0
        for t, (name, header, rows) in enumerate(tables):
            where = [at[column(h)] for h in header]
            for line, row in enumerate(rows, 2):
                ctx.check_cancelled()
                cells = [None] * len(columns)
                for p, v in zip(where, row):
                    cells[p] = v
                record = {"id": f"{t}:{line}", "from": name, "line": line, "cells": cells}
                key = tuple(norm(cells[i], phone[i]) for i in keys)
                done += 1
                if not whole and not any(key):   # 기준 칸이 모두 비었을 때만 — 하나만 비었으면 그대로 비교
                    dropped.append(("기준 칸이 비어 있음", record))
                elif key in index:
                    first = kept[index[key]]
                    a = [norm(v, phone[i]) for i, v in enumerate(first["cells"])]
                    b = [norm(v, phone[i]) for i, v in enumerate(cells)]
                    differ = [i for i in range(len(columns)) if a[i] and b[i] and a[i] != b[i]]
                    if differ:
                        unsure.append((index[key], record, differ))
                    else:
                        for i in range(len(columns)):   # 한쪽에만 있는 값은 남는 행에 채운다
                            if not a[i] and b[i]:
                                first["cells"][i] = cells[i]
                                filled += 1
                        dropped.append((f"중복 — {first['from']} {first['line']}행과 같음", record))
                else:
                    index[key] = len(kept)
                    kept.append(record)
                if done % 200 == 0:
                    ctx.report_progress(done, total, "행 비교")
        ctx.report_progress(total, total, "행 비교")

        # 애매한 것만 사람에게 — 답은 그때마다 남겨, 멈췄다 다시 돌리면 답한 것은 다시 묻지 않는다
        saved = ctx.load_checkpoint() or {}
        decisions, rest = dict(saved.get("decisions") or {}), saved.get("rest")
        extra = []
        for n, (k, record, differ) in enumerate(unsure, 1):
            ctx.check_cancelled()
            first = kept[k]
            pick = decisions.get(record["id"]) or rest
            if not pick:
                show = lambda r: " · ".join(f"{columns[i]} {text(r['cells'][i]) or '(빈칸)'}" for i in differ)
                answer = ctx.request_user_action(UserActionRequest(
                    kind="choice",
                    prompt=f"같은 건으로 보이는데 {', '.join(columns[i] for i in differ)} 값이 달라요. 어떻게 할까요? ({n}/{len(unsure)})",
                    options=[KEEP, REPLACE, BOTH, KEEP_REST],
                    context={"앞": f"{first['from']} {first['line']}행 — {show(first)}",
                             "뒤": f"{record['from']} {record['line']}행 — {show(record)}"}))
                if answer.action == "abort":
                    return RuneResult(ok=False, failures=[Failure("stopped", "확인을 멈췄어요. 다시 실행하면 답한 것은 건너뛰고 이어서 물어요.")])
                pick = str(answer.value) if answer.action == "ok" else BOTH   # 건너뛰면 둘 다 남긴다 — 빼지 않는다
                if pick == KEEP_REST:
                    pick = rest = KEEP
                decisions[record["id"]] = pick
                ctx.save_checkpoint({"decisions": decisions, "rest": rest})
            if pick == REPLACE:
                dropped.append((f"사람 확인 — {record['from']} {record['line']}행으로 바꿈", first))
                kept[k] = record
            elif pick == BOTH:
                extra.append(record)
            else:
                dropped.append((f"사람 확인 — {first['from']} {first['line']}행을 남김", record))
        kept += extra

        head = [*columns, "출처", "원래 행"]
        artifacts = [ctx.write_table("통합본.xlsx", [head] + [[*r["cells"], r["from"], r["line"]] for r in kept])]
        if dropped:
            artifacts.append(ctx.write_table("제외목록.xlsx", [["제외 이유", *head]]
                                             + [[why, *r["cells"], r["from"], r["line"]] for why, r in dropped]))
        ctx.clear_checkpoint()
        empty = sum(1 for why, _ in dropped if why == "기준 칸이 비어 있음")   # 기준 칸 '모두' 빈 행
        summary = {"합친 파일": len(tables), "읽은 행": total, "통합본 행": len(kept),
                   "중복 제외": len(dropped) - empty, "기준 칸 빈 행 제외": empty,
                   "사람이 확인한 행": len(unsure), "다른 파일 값으로 채운 칸": filled,
                   "기준": ALL if whole else ", ".join(columns[i] for i in keys)}
        ctx.log("info", "표 합치기 완료", **summary)
        return RuneResult(ok=True, artifacts=artifacts, summary=summary)

    @staticmethod
    def key_columns(ctx, tables, columns, at):
        """같은 건으로 볼 칸 번호들. None 이면 모든 칸. 설정이 없으면 모든 파일에 있는 칸 중에서 묻는다."""
        chosen = text(ctx.params.get("기준열")).strip()
        if not chosen:
            common = [c for c in columns if all(column(c) in {column(h) for h in header} for _, header, _ in tables)]
            answer = ctx.request_user_action(UserActionRequest(
                kind="choice", prompt="어느 칸이 같으면 같은 건으로 볼까요? (전화·이메일처럼 건마다 다른 칸이 좋아요)",
                options=[*common, ALL], context={"합칠 파일": ", ".join(n for n, _, _ in tables)}))
            if answer.action == "abort":
                return RuneResult(ok=False, failures=[Failure("stopped", "기준 칸을 고르지 않아 멈췄어요.")])
            chosen = str(answer.value) if answer.action == "ok" else ALL
        if chosen == ALL:
            return None
        names = [c.strip() for c in split(r"[,，]", chosen) if c.strip()]
        missing = [c for c in names if column(c) not in at]
        if missing:
            return RuneResult(ok=False, failures=[Failure("setting", f"'{', '.join(missing)}' 칸이 없어요. 있는 칸: {', '.join(columns)}")])
        return [at[column(c)] for c in names]
