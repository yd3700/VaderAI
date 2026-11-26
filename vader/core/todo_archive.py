"""할 일 완료 기록(아카이브)·통계·엑셀 저장·md 가져오기 (2026-11-26 마스터 요청, codex1 설계 회신).

완료 기록은 옮기거나 지우지 않는다 — 같은 `todo.sqlite3` 에서 끝낸 날(done_at)로 다시 찾는다.
가져온 기록은 날짜를 믿지 않는다(마스터: '편의상 쓴 날짜라 무시') — 끝낸 날 없이 '가져온 기록' 으로 따로 보이고 통계에 넣지 않는다.
가져오기는 미리보기 → 사람이 고른 줄만 한 거래로 저장. 같은 파일·같은 줄은 두 번 들어가지 않는다. AI·공유·알림은 일어나지 않는다.
"""
from __future__ import annotations

import hashlib
import re
import secrets as _token
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from vader.core import todo

PAGE = 50
SCAN_MAX = 20000                # 검색은 제목을 풀어 메모리에서 — 넘으면 기간을 좁히라고 한다 (codex1)
# ponytail: 2만 건까지 제목을 한 번에 푼다(수십 ms). 가져온 기록이 그보다 많아지면 나눠 찾기.
IMPORT_BYTES = 2 * 1024 * 1024
IMPORT_ITEMS = 2000
_pending: dict[str, tuple[Path, str]] = {}      # 미리보기 표 → (파일, 지문). 화면이 경로를 보내지 않게


def _day(text: str, what: str) -> date:
    try:
        return date.fromisoformat(str(text))
    except ValueError:
        raise todo.TodoError(f"{what} 날짜를 알아보지 못했습니다.") from None


def _range(start: str, end: str) -> tuple[date, date]:
    a, b = _day(start, "시작"), _day(end, "끝")
    if a > b:
        raise todo.TodoError("시작 날짜가 끝 날짜보다 늦어요.")
    return a, b


def _rows(con, start: date, end: date, imported: bool):
    if imported:
        return con.execute("SELECT * FROM cards WHERE state='done' AND done_at IS NULL AND imported=1 "
                           "ORDER BY created_at DESC, id").fetchall()
    return con.execute("SELECT * FROM cards WHERE state='done' AND done_at>=? AND done_at<? ORDER BY done_at DESC, id",
                       (start.isoformat(), (end + timedelta(days=1)).isoformat())).fetchall()


def _days(row) -> int | None:
    try:
        return (datetime.fromisoformat(row["done_at"]).date() - datetime.fromisoformat(row["created_at"]).date()).days
    except (TypeError, ValueError):
        return None


def _stats(rows, start: date, end: date) -> dict:
    """끝낸 날을 아는 카드만. 주는 월요일부터."""
    weeks: dict[str, int] = {}
    months: dict[str, int] = {}
    first = start - timedelta(days=start.weekday())
    at = first
    while at <= end:                                   # 빈 주도 0 으로 — 그래프에 빈칸이 보이게
        weeks[at.isoformat()] = 0
        at += timedelta(days=7)
    spans = []
    for r in rows:
        d = datetime.fromisoformat(r["done_at"]).date()
        week = (d - timedelta(days=d.weekday())).isoformat()
        weeks[week] = weeks.get(week, 0) + 1
        months[d.isoformat()[:7]] = months.get(d.isoformat()[:7], 0) + 1
        if (n := _days(r)) is not None and n >= 0:
            spans.append(n)
    return {"done": len(rows), "snoozed": sum(r["snoozed"] for r in rows),
            "avg_days": round(sum(spans) / len(spans), 1) if spans else None,
            "weeks": [{"week": k, "n": v} for k, v in sorted(weeks.items())],
            "months": [{"month": k, "n": v} for k, v in sorted(months.items())]}


def _found(con, a: date, b: date, imported: bool, query: str):
    """(조건에 맞는 행, 통계용 전체 행, 검색 상한을 넘었나) — 화면 목록과 엑셀이 같은 결과를 쓴다."""
    rows = _rows(con, a, b, bool(imported))
    query = str(query or "").strip().lower()
    if not query:
        return rows, rows, False
    if len(rows) > SCAN_MAX and not imported:   # 가져온 기록은 기간으로 좁힐 수 없다 — 모두 풀어 찾는다 (회신 2 P2-4)
        return [], rows, True
    return [r for r in rows if query in todo._unseal(r["title"]).lower()], rows, False


def archive(start: str, end: str, query: str = "", offset: int = 0, imported: bool = False) -> dict:
    """{items, total, more, limited, stats}. imported=True 면 가져온 기록(끝낸 날 없음)."""
    a, b = _range(start, end)
    offset = max(0, int(offset))
    con = todo._db()
    rows, every, limited = _found(con, a, b, bool(imported), query)
    stats = None if imported else _stats(every, a, b)
    now = todo._now()
    page = rows[offset:offset + PAGE]
    items = [{**todo._card(r, now), "done_at": r["done_at"], "created_at": r["created_at"], "days": _days(r)} for r in page]
    return {"items": items, "total": len(rows), "more": offset + PAGE < len(rows), "limited": limited, "stats": stats}


# ── 엑셀 저장 ──

def _notes(con, card: str) -> list[tuple[str, str, str]]:
    """카드의 세부 내용 — (경로, 하위 항목, 적은 내용) 을 항목마다 한 줄. 내용 하나는 4000자라 한 칸에 들어간다."""
    out = []

    def body(node):
        row = con.execute("SELECT body FROM bodies WHERE node=? AND card=?", (node, card)).fetchone()
        return todo._unseal(row["body"]) if row else ""

    def walk(node, path):
        for k in con.execute("SELECT id, title, done FROM notes WHERE card=? AND parent=? ORDER BY created_at, id", (card, node)):
            title = ("✓ " if k["done"] else "") + todo._unseal(k["title"])
            out.append((" > ".join(path), title, body(k["id"])))
            walk(k["id"], path + [todo._unseal(k["title"])])
    if top := body(card):
        out.append(("", "", top))
    walk(card, [])
    return out


def _text_row(sheet, values: list) -> None:
    sheet.append(values)
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str):
            cell.data_type = "s"           # '=' 로 시작해도 수식이 되지 않는다


def export(path: Path, start: str, end: str, imported: bool = False, query: str = "") -> dict:
    """화면에 보이는 조건(기간·검색·가져온 기록) 그대로 이 PC 의 xlsx 로. 둘째 시트에 세부 내용."""
    from openpyxl import Workbook
    a, b = _range(start, end)
    con = todo._db()
    rows, _every, limited = _found(con, a, b, bool(imported), query)
    if limited:
        raise todo.TodoError("기간 안의 완료 기록이 많아 제목으로 찾을 수 없어요. 기간을 좁혀 주세요.")
    book = Workbook()
    sheet = book.active
    sheet.title = "가져온 기록" if imported else "완료 기록"
    notes = book.create_sheet("세부 내용")
    _text_row(sheet, ["번호", "끝낸 날", "할 일", "미룬 횟수", "적은 날", "걸린 날", "세부 항목 수"])
    _text_row(notes, ["번호", "할 일", "위치", "하위 항목", "적은 내용"])
    lines = 0
    for n, r in enumerate(rows, 1):
        title, detail = todo._unseal(r["title"]), _notes(con, r["id"])
        _text_row(sheet, [n, (r["done_at"] or "")[:10], title, r["snoozed"], r["created_at"][:10],
                          "" if _days(r) is None else _days(r), len(detail)])
        for where, item, text in detail:
            _text_row(notes, [n, title, where, item, text])
            lines += 1
    for col, width in zip("ABCDEFG", (6, 12, 48, 10, 12, 8, 12)):
        sheet.column_dimensions[col].width = width
    for col, width in zip("ABCDE", (6, 36, 30, 30, 80)):
        notes.column_dimensions[col].width = width
    path = Path(path)
    book.save(path)
    return {"path": str(path), "rows": len(rows), "notes": lines}


def downloads() -> Path:
    """엑셀을 둘 곳 — 내 PC 의 다운로드 폴더(없으면 홈). 셸마다 저장 창이 달라 고정 위치에 두고 탐색기로 보여 준다."""
    home = Path.home()
    return home / "Downloads" if (home / "Downloads").is_dir() else home


def export_file(start: str, end: str, imported: bool = False, query: str = "") -> dict:
    stem = "가져온 기록" if imported else f"완료 기록 {start}~{end}"
    folder = downloads()
    path = folder / f"{stem}.xlsx"
    n = 1
    while path.exists():                 # 덮어쓰지 않는다
        n += 1
        path = folder / f"{stem} ({n}).xlsx"
    return export(path, start, end, imported, query)


# ── md 가져오기 ──

_ITEM = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(?:\[([ xX])\]\s+)?(.*\S)\s*$")
_HEAD = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_EMPTY = re.compile(r"^\[[ xX]\]$")


def parse_md(text: str) -> list[dict]:
    """맨 바깥 목록 줄 하나가 한 건. 안쪽 목록은 하위 항목(3단까지), 이어지는 글은 세부 내용, 제목(#)은 분류.
    목록이 없는 제목 아래 글은 그 제목이 한 건. [x] 는 끝냄, [ ] 는 열림, 표시 없음은 None(화면에서 고른다)."""
    items: list[dict] = []
    head, heading, base = "", None, None
    stack: list[tuple[int, dict]] = []                  # (들여쓰기, 하위 항목) — 지금 항목의 안쪽 경로
    for line in text.splitlines():
        if not line.strip():
            continue
        if m := _HEAD.match(line):
            head, stack, base = m.group(1).strip(), [], None
            heading = {"title": head, "state": None, "body": [], "children": [], "section": "", "heading": True}
            items.append(heading)
            continue
        m = _ITEM.match(line.expandtabs(4))
        if m and _EMPTY.match(m.group(3)):
            continue                                    # '- [ ]' 처럼 글이 없는 줄
        if m:
            indent, mark, words = len(m.group(1)), m.group(2), m.group(3)
            state = None if mark is None else ("done" if mark in "xX" else "open")
            if base is None or indent <= base:
                if heading is not None:
                    heading["listed"] = True            # 목록이 있는 제목은 분류일 뿐 — 한 건이 아니다
                base, stack = indent, []
                items.append({"title": words, "state": state, "body": [], "children": [], "section": head})
                continue
            while stack and stack[-1][0] >= indent:
                stack.pop()
            node = {"title": words, "done": state == "done", "children": []}
            (stack[-1][1]["children"] if stack else items[-1]["children"]).append(node)
            stack.append((indent, node))
            continue
        if items:                                       # 이어지는 글 — 지금 건의 세부 내용
            items[-1]["body"].append(line.strip())
    return [{"title": it["title"], "state": it["state"], "body": "\n".join(it["body"]),
             "children": it["children"], "section": it["section"]}
            for it in items if not (it.get("heading") and not it["body"])]   # 글이 있는 제목은 한 건으로 남긴다(사라지지 않게)


def _clip(text: str) -> str:
    return text if len(text) <= todo.TITLE_MAX else text[:todo.TITLE_MAX - 1] + "…"


def _convert(it: dict) -> dict:
    """한 건을 카드로 바꾼 모양 — 미리보기와 저장이 같은 결과를 쓴다. 넣지 못하는 것은 lost 에 센다(사람이 확인 전에 본다)."""
    extra = [it["title"]] if len(it["title"]) > todo.TITLE_MAX else []     # 넘친 제목은 본문 앞에 온전히
    if it["section"]:
        extra.insert(0, f"[분류] {it['section']}")
    body = "\n".join(extra + ([it["body"]] if it["body"] else []))
    lost = {"items": 0, "body": max(0, len(body) - todo.BODY_MAX)}

    def keep(nodes, depth):
        out = []
        for n, node in enumerate(nodes):
            if n >= todo.NOTE_CHILDREN:
                lost["items"] += _count([node])
                continue
            # 200자를 넘는 하위 제목은 그 항목의 적는 칸에 (회신 2 P2-3) — 적는 칸 한도를 넘는 만큼은 잘린다고 미리 센다 (회신 3 P2)
            spill = node["title"] if len(node["title"]) > todo.TITLE_MAX else ""
            lost["body"] += max(0, len(spill) - todo.BODY_MAX)
            out.append({"title": _clip(node["title"]), "done": node["done"], "body": spill[:todo.BODY_MAX],
                        "children": keep(node["children"], depth + 1) if depth < todo.NOTE_DEPTH else []})
            if depth >= todo.NOTE_DEPTH:
                lost["items"] += _count(node["children"])
        return out
    return {"title": _clip(it["title"]), "state": it["state"], "body": body[:todo.BODY_MAX],
            "children": keep(it["children"], 1), "lost": lost}


def _count(nodes) -> int:
    return sum(1 + _count(n["children"]) for n in nodes)


def drop_pending() -> None:
    """미리보기 표를 버린다 — 창을 닫거나 잠글 때."""
    _pending.clear()


def _read(path: Path) -> tuple[str, str]:
    with open(path, "rb") as f:
        data = f.read(IMPORT_BYTES + 1)      # 상한까지만 읽는다
    if len(data) > IMPORT_BYTES:
        raise todo.TodoError("md 파일은 2MB 까지 가져올 수 있어요.")
    try:
        return data.decode("utf-8-sig"), hashlib.sha256(data).hexdigest()
    except UnicodeDecodeError:
        raise todo.TodoError("UTF-8 로 저장한 md 파일만 가져올 수 있어요.") from None


def import_preview(path: Path) -> dict:
    """{token, items:[{i, title, state, children, body, already, duplicate}], counts}. 아무것도 저장하지 않는다."""
    text, digest = _read(path)
    parsed = parse_md(text)
    if not parsed:
        raise todo.TodoError("가져올 항목을 찾지 못했어요. 목록(- 항목) 이나 제목(#) 이 있는 md 인지 확인해 주세요.")
    if len(parsed) > IMPORT_ITEMS:
        raise todo.TodoError(f"한 번에 {IMPORT_ITEMS}건까지 가져올 수 있어요.")
    con = todo._db()
    taken = {r[0] for r in con.execute("SELECT key FROM imports WHERE key LIKE ?", (digest + ":%",))}
    known = {todo._unseal(r["title"]) for r in con.execute("SELECT title FROM cards WHERE imported=1")}
    token = _token.token_urlsafe(16)
    _pending.clear()
    _pending[token] = (Path(path), digest)
    items = []
    for i, it in enumerate(parsed):
        card = _convert(it)
        items.append({"i": i, "title": card["title"], "state": card["state"], "children": _count(card["children"]),
                      "lost_items": card["lost"]["items"], "lost_body": card["lost"]["body"],
                      "body": card["body"][:300], "already": f"{digest}:{i}" in taken, "duplicate": card["title"] in known})
    return {"token": token, "name": Path(path).name, "items": items,
            "counts": {"all": len(items), "already": sum(x["already"] for x in items),
                       "duplicate": sum(x["duplicate"] and not x["already"] for x in items)}}


def import_apply(token: str, picks: list[int], plain: str = "done") -> dict:
    """미리보기에서 고른 줄만 한 거래로. 파일이 그사이 바뀌었으면 저장하지 않는다. plain — 표시 없는 줄을 끝냄/열림 중 무엇으로."""
    got = _pending.get(str(token))
    if not got:
        raise todo.TodoError("미리보기가 끝났어요. 다시 골라 주세요.")
    if plain not in ("done", "open"):
        raise todo.TodoError("가져올 상태를 알아보지 못했습니다.")
    path, digest = got
    text, now_digest = _read(path)
    if now_digest != digest:
        raise todo.TodoError("파일이 그사이 바뀌었어요. 다시 가져오기를 눌러 주세요.")
    parsed = parse_md(text)
    if not isinstance(picks, list) or not all(type(i) is int and 0 <= i < len(parsed) for i in picks) or len(set(picks)) != len(picks):
        raise todo.TodoError("고른 항목을 알아보지 못했습니다.")
    con = todo._db()
    now = todo._now()
    stamp = now.isoformat()
    added = 0
    with con:
        con.execute("BEGIN IMMEDIATE")
        for i in sorted(picks):
            key = f"{digest}:{i}"
            if con.execute("SELECT 1 FROM imports WHERE key=?", (key,)).fetchone():
                continue                                    # 같은 파일 같은 줄 — 두 번 넣지 않는다
            card = _convert(parsed[i])
            title, body = card["title"], card["body"]
            state = card["state"] or plain
            cid = uuid.uuid4().hex
            con.execute("INSERT INTO cards(id,title,due,state,created_at,done_at,imported) VALUES (?,?,?,?,?,NULL,1)",
                        (cid, todo._seal(title), now.date().isoformat(), state, stamp))
            if body:
                con.execute("INSERT INTO bodies(node, card, body) VALUES (?,?,?)", (cid, cid, todo._seal(body)))
            _children(con, cid, cid, card["children"], stamp)
            con.execute("INSERT INTO imports(key, card, at) VALUES (?,?,?)", (key, cid, stamp))
            added += 1
    _pending.pop(str(token), None)
    return {"added": added, "skipped": len(picks) - added}


def _children(con, card: str, parent: str, nodes: list, stamp: str) -> None:
    """_convert 가 이미 3단·50개로 맞춘 하위 항목을 그대로."""
    for n, node in enumerate(nodes):
        nid = uuid.uuid4().hex
        con.execute("INSERT INTO notes(id, card, parent, title, done, created_at) VALUES (?,?,?,?,?,?)",
                    (nid, card, parent, todo._seal(node["title"]), int(node["done"]), f"{stamp}.{n:06d}"))
        if node["body"]:
            con.execute("INSERT INTO bodies(node, card, body) VALUES (?,?,?)", (nid, card, todo._seal(node["body"])))
        _children(con, card, nid, node["children"], stamp)
