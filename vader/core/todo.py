"""할 일 카드 (2026-11-10 마스터 요청 — 하루하루 할 일을 카드로, 미루면 나중에 다시 알려준다).

저장: 계정 작업공간 `todo.sqlite3`, 이 PC 에만. 카드 제목은 계정 금고(DPAPI)의 무작위 키로 암호화한다 (메신저와 같은 방식).
날짜·상태·알릴 시각은 찾고 알리는 데 써야 해서 평문이다 — 제목(업무 내용·환자명이 들어갈 수 있다)만 잠근다.
AI 로는 사용자가 '말로 정리' 를 눌렀을 때만 보낸다 (2026-11-11 마스터 결정, `todo_ai`) — 그 밖의 경로로는 보내지 않는다.
미루기는 '언제 다시 알릴지' 가 있어야 끝난다. 알림은 앱이 켜 있는 동안(트레이 포함) 15초마다 보고,
꺼져 있던 동안 지난 알림은 다음에 켤 때 한꺼번에 알린다 (놓친 것 따라잡기).
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import uuid
from datetime import date, datetime

from vader.core import config, secrets

STORE_KEY = "todo.store_key"
SCHEMA = """CREATE TABLE IF NOT EXISTS cards(
  id TEXT PRIMARY KEY, title BLOB NOT NULL, due TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'open',
  snoozed INTEGER NOT NULL DEFAULT 0, remind_at TEXT, reminded INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, done_at TEXT, rev INTEGER NOT NULL DEFAULT 0, pos INTEGER);
CREATE TABLE IF NOT EXISTS notes(
  id TEXT PRIMARY KEY, card TEXT NOT NULL, parent TEXT NOT NULL, title BLOB NOT NULL,
  done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS notes_parent ON notes(card, parent);
CREATE TABLE IF NOT EXISTS bodies(node TEXT PRIMARY KEY, card TEXT NOT NULL, body BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS share_out(id INTEGER PRIMARY KEY AUTOINCREMENT, peer TEXT NOT NULL, payload BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS share_seen(event TEXT PRIMARY KEY);"""
TITLE_MAX = 200
#: 열쇠 확인→만들기→저장을 한 번에. 앱은 PC 에 하나만 뜬다(gate.hold_instance) — 프로세스 안 잠금이면 된다 (codex1 P1-3).
_KEY_LOCK = threading.Lock()


class TodoError(ValueError):
    pass


def _key() -> bytes:
    with _KEY_LOCK:
        stored = secrets.get(STORE_KEY)
        if not stored:
            if (config.data_dir() / "todo.sqlite3").exists():
                raise TodoError("할 일 기록을 여는 열쇠가 없습니다. 이 PC 의 할 일은 열 수 없습니다.")
            stored = base64.b64encode(os.urandom(32)).decode()
            secrets.put(STORE_KEY, stored)
        return base64.b64decode(stored)


def _seal(text: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    nonce = os.urandom(12)
    return nonce + ChaCha20Poly1305(_key()).encrypt(nonce, text.encode(), b"vader-todo-v1")


def _unseal(blob: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    return ChaCha20Poly1305(_key()).decrypt(blob[:12], blob[12:], b"vader-todo-v1").decode()


def _db() -> sqlite3.Connection:
    _key()
    con = sqlite3.connect(config.data_dir() / "todo.sqlite3", isolation_level=None, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(cards)")}
    if "rev" not in have:   # 2026-11-11 이전 파일
        con.execute("ALTER TABLE cards ADD COLUMN rev INTEGER NOT NULL DEFAULT 0")
    if "pos" not in have:   # 2026-11-12 이전 파일
        con.execute("ALTER TABLE cards ADD COLUMN pos INTEGER")
    for col in ("share_id", "owner", "shared_with"):   # 2026-11-18 멤버 공유
        if col not in have:
            con.execute(f"ALTER TABLE cards ADD COLUMN {col} TEXT")
    return con


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _card(row: sqlite3.Row, now: datetime) -> dict:
    return {"id": row["id"], "title": _unseal(row["title"]), "due": row["due"], "state": row["state"],
            "snoozed": row["snoozed"], "remind_at": row["remind_at"], "rev": row["rev"],
            "owner": row["owner"], "shared_with": json.loads(row["shared_with"] or "[]"),
            "ringing": bool(row["remind_at"] and row["state"] == "open" and row["remind_at"] <= now.isoformat())}


def mark(card: dict) -> str:
    """카드의 판 번호 — 바뀔 때마다 오른다. 제안·폰 변경은 만든 때의 판일 때만 적용한다 (되돌려도 옛 판으로 안 돌아간다)."""
    return str(card["rev"])


def _change(con: sqlite3.Connection, card_id: str, rev: str | None, sql: str, args: tuple) -> None:
    """확인과 바꾸기를 한 문장으로 — 사이에 다른 변경이 끼어들 틈이 없다 (codex1 round6 P2-1)."""
    _one(con, card_id)
    if rev is not None and not str(rev).isdigit():
        raise TodoError("잘못된 판 번호입니다.")
    cur = con.execute(f"UPDATE cards SET {sql}, rev=rev+1 WHERE id=? AND (? IS NULL OR rev=?)",
                      (*args, card_id, rev, None if rev is None else int(rev)))
    if not cur.rowcount:
        raise TodoError("그 사이 할 일이 바뀌어 적용하지 않았습니다.")


def _when(text: str, what: str) -> datetime:
    """이 PC 의 현지 시각만 받는다 (시간대가 붙은 값은 비교·정렬이 어긋난다 — codex1 P2-7)."""
    try:
        at = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise TodoError(f"{what} 시각을 알아보지 못했습니다.") from None
    if at.tzinfo:
        raise TodoError(f"{what} 시각은 이 PC 의 현지 시각으로 주세요.")
    return at.replace(microsecond=0)


def add(title: str, due: str = "", remind_at: str = "") -> dict:
    title = (title or "").strip()
    if not title or len(title) > TITLE_MAX:
        raise TodoError(f"할 일은 1~{TITLE_MAX}자로 적어 주세요.")
    now = _now()
    day = date.fromisoformat(due) if due else now.date()
    remind = _when(remind_at, "알릴").isoformat() if remind_at else None
    con = _db()
    cid = uuid.uuid4().hex
    con.execute("INSERT INTO cards(id,title,due,remind_at,created_at) VALUES (?,?,?,?,?)",
                (cid, _seal(title), day.isoformat(), remind, now.isoformat()))
    return _card(con.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone(), now)


def cards(now: datetime | None = None) -> list[dict]:
    """열린 카드 전부 + 오늘 끝낸 카드. 사람이 끌어 정한 순서가 먼저, 그다음(새로 적은 것)은 날짜순·미룬 횟수 많은 것 먼저."""
    now = now or _now()
    rows = _db().execute(
        "SELECT * FROM cards WHERE state='open' OR (state='done' AND substr(done_at,1,10)=?) "
        "ORDER BY state='done', pos IS NULL, pos, due, snoozed DESC, created_at", (now.date().isoformat(),)).fetchall()
    return [_card(r, now) for r in rows]


def move(card_ids: list[str]) -> list[dict]:
    """화면에서 끌어 놓은 순서 그대로 (열린 카드 id 목록). 내용이 아니라 순서라 판 번호는 그대로다."""
    if not isinstance(card_ids, list) or len(card_ids) > 2000 or len(set(card_ids)) != len(card_ids):
        raise TodoError("순서를 알아보지 못했습니다.")
    con = _db()
    with con:   # 한 번에 — 반만 바뀐 순서가 남지 않게
        con.execute("BEGIN")
        con.executemany("UPDATE cards SET pos=? WHERE id=? AND state='open'", [(i, str(c)) for i, c in enumerate(card_ids)])
    return cards()


def _one(con: sqlite3.Connection, cid: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    if not row:
        raise TodoError("없는 할 일입니다.")
    return row


def done(card_id: str, undo: bool = False, rev: str | None = None, con: sqlite3.Connection | None = None) -> list[dict]:
    own = con or _db()
    _change(own, card_id, rev, "state=?, done_at=?", ("open", None) if undo else ("done", _now().isoformat()))
    row = _one(own, card_id)
    if row["share_id"]:   # 공유한 할 일 — 끝냄을 상대에게도 (보낼 통에 넣고, 메신저가 열릴 때 보낸다)
        peers = [row["owner"]] if row["owner"] else json.loads(row["shared_with"] or "[]")
        _queue(own, peers, {"op": "done", "id": row["share_id"], "undo": bool(undo)})
    if con is None:
        flush_shares()
    return cards() if con is None else []


def snooze(card_id: str, until: str, rev: str | None = None, con: sqlite3.Connection | None = None) -> list[dict]:
    """미루기 — 다시 알릴 시각이 있어야 한다. 그날로 옮기고 미룬 횟수를 센다."""
    at = _when(until, "다시 알릴")
    if _one(con or _db(), card_id)["owner"]:
        raise TodoError("공유받은 할 일은 미룰 수 없어요. 보낸 사람에게 말해 주세요.")
    if at <= _now():
        raise TodoError("다시 알릴 시각은 지금보다 뒤여야 합니다.")
    _change(con or _db(), card_id, rev, "due=?, remind_at=?, reminded=0, snoozed=snoozed+1, state='open', done_at=NULL",
            (at.date().isoformat(), at.isoformat()))
    return cards() if con is None else []


def delete(card_id: str) -> list[dict]:
    """화면이 사람에게 확인받은 뒤에만 부른다. 그 카드의 포스트잇도 같이 지운다."""
    con = _db()
    _one(con, card_id)
    with con:
        con.execute("BEGIN")
        con.execute("DELETE FROM cards WHERE id=?", (card_id,))
        con.execute("DELETE FROM notes WHERE card=?", (card_id,))
        con.execute("DELETE FROM bodies WHERE card=?", (card_id,))
    return cards()


# ── 멤버 공유 (마스터 요청 2026-11-18) ──
# 내 카드를 친구에게 보낸다 — 메신저 종단간 암호로(서버는 내용을 못 본다). 보내는 것은 제목·날짜뿐(포스트잇·알림 시각은 안 간다).
# 받은 사람은 보기·끝냄만(미루기 안 됨, 목록에서 빼기는 자기 PC 만). 한쪽이 끝내면 다른 쪽도 끝난다.
# 보낼 것은 share_out 에 먼저 적고 flush_shares 가 보낸다 — 메신저가 안 열려 있거나 실패해도 다음에 다시 (잃지 않는다).
SHARE_MARK = "\u2063vader-todo:"   # 메신저 글 앞머리 — 화면은 이걸 보고 말풍선을 '할 일 공유' 로 그린다
SHARE_MAX = 30
SHARED_PER_SENDER = 200   # 한 사람에게서 받아 둘 수 있는 공유 카드 (codex1 todo-share P2-2)
SHARED_TOTAL = 1000       # 받은 공유 카드 전체


def _queue(con: sqlite3.Connection, peers, payload: dict) -> None:
    # ev: 이 알림 하나의 번호 — 큐에 적힌 채로 다시 보내도 같다. 받는 쪽은 (보낸 사람, ev) 로 한 번만 적용 (codex1 회신 4)
    blob = _seal(json.dumps({**payload, "ev": uuid.uuid4().hex}, ensure_ascii=False))
    con.executemany("INSERT INTO share_out(peer, payload) VALUES (?,?)", [(p, blob) for p in peers if p])


def share(card_id: str, account_ids: list[str]) -> list[dict]:
    """내 열린 카드를 친구(들)에게. 이미 받은 사람은 다시 보내지 않는다."""
    if not isinstance(account_ids, list) or not account_ids or len(account_ids) > SHARE_MAX \
            or not all(isinstance(a, str) and 0 < len(a) <= 64 for a in account_ids):
        raise TodoError("공유할 멤버를 알아보지 못했어요.")
    con = _db()
    row = _one(con, card_id)
    if row["owner"]:
        raise TodoError("공유받은 할 일은 다시 공유할 수 없어요.")
    if row["state"] != "open":
        raise TodoError("끝낸 할 일은 공유하지 않아요.")
    sid = row["share_id"] or uuid.uuid4().hex
    have = json.loads(row["shared_with"] or "[]")
    new = [a for a in dict.fromkeys(account_ids) if a not in have]
    with con:
        con.execute("BEGIN")
        con.execute("UPDATE cards SET share_id=?, shared_with=?, rev=rev+1 WHERE id=?",
                    (sid, json.dumps(have + new), card_id))
        _queue(con, new, {"op": "share", "id": sid, "title": _unseal(row["title"]), "due": row["due"]})
    flush_shares()
    return cards()


def unshare(card_id: str, account_ids: list[str]) -> list[dict]:
    """공유 취소 (마스터 2026-11-19) — 그 멤버들의 PC 에서 받은 카드를 지우라고 알리고, 내 카드에서 뺀다.
    모두 빠지면 공유 전 모양으로(공유 번호도 없앰). 끝냄 등 이미 오간 상태는 내 카드에 그대로 남는다."""
    if not isinstance(account_ids, list) or not account_ids or len(account_ids) > SHARE_MAX \
            or not all(isinstance(a, str) and 0 < len(a) <= 64 for a in account_ids):
        raise TodoError("공유를 취소할 멤버를 알아보지 못했어요.")
    con = _db()
    row = _one(con, card_id)
    if row["owner"] or not row["share_id"]:
        raise TodoError("내가 공유한 할 일만 취소할 수 있어요.")
    have = json.loads(row["shared_with"] or "[]")
    gone = [a for a in dict.fromkeys(account_ids) if a in have]
    left = [a for a in have if a not in gone]
    with con:
        con.execute("BEGIN")
        con.execute("UPDATE cards SET share_id=?, shared_with=?, rev=rev+1 WHERE id=?",
                    (row["share_id"] if left else None, json.dumps(left) if left else None, card_id))
        _queue(con, gone, {"op": "unshare", "id": row["share_id"]})
    flush_shares()
    return cards()


def flush_shares(send=None) -> int:
    """보낼 통을 비운다. 보낸 것만 지운다(실패한 것은 다음에). 보낸 개수."""
    if not (config.data_dir() / "todo.sqlite3").exists():
        return 0
    if send is None:
        from vader.core import messenger
        send = messenger.send
    con, sent, stuck = _db(), 0, set()
    for r in con.execute("SELECT id, peer, payload FROM share_out ORDER BY id LIMIT 50").fetchall():
        if r["peer"] in stuck:   # 같은 사람에게는 순서대로만 — 앞 알림이 실패하면 뒤(예: 취소)는 다음에 (codex1 unshare 회신 1)
            continue
        text = _unseal(r["payload"])
        payload = json.loads(text)
        if not payload.get("ev"):   # ev 없는 옛 행 — 보내기 전에 번호를 붙여 저장해 둔다(다시 보내도 같게, codex1 회신 5)
            text = json.dumps({**payload, "ev": uuid.uuid4().hex}, ensure_ascii=False)
            con.execute("UPDATE share_out SET payload=? WHERE id=?", (_seal(text), r["id"]))
        try:
            send(r["peer"], SHARE_MARK + text)
        except Exception:   # noqa: BLE001 — 메신저 닫힘·친구 아님·네트워크: 남겨 두고 다음에
            stuck.add(r["peer"])
            continue
        con.execute("DELETE FROM share_out WHERE id=?", (r["id"],))
        sent += 1
    return sent


def receive_share(sender: str, text: str, event: str = "") -> bool:
    """메신저가 안전 번호를 확인한 친구에게서 받은 공유 글 하나를 적용한다. 우리 글이 아니면 False.
    share: 그 사람이 보낸 카드로 넣는다(이미 있으면 제목·날짜만 맞춤). done: 그 공유 카드를 끝냄/되돌림 —
    보낸 사람이 그 카드의 주인이거나 내가 그 사람에게 공유한 카드일 때만. 받은 것을 다시 그 사람에게 돌려보내지 않는다.
    event(메신저 메시지 id)가 있으면 한 번만 — 적용·전파 큐·'적용함' 기록이 한 트랜잭션 (codex1 todo-share 회신 3)."""
    if not isinstance(text, str) or not text.startswith(SHARE_MARK):
        return False
    try:
        msg = json.loads(text[len(SHARE_MARK):])
        sid, op = str(msg["id"]), msg["op"]
    except (ValueError, KeyError, TypeError):
        return True
    if not (0 < len(sid) <= 64):
        return True
    ev = msg.get("ev")
    if isinstance(ev, str) and 0 < len(ev) <= 64:
        event = f"{sender}:{ev}"   # 보낸 쪽이 다시 보낸 같은 알림 — 메시지 id 가 달라도 한 번만
    con = _db()
    with con:
        con.execute("BEGIN IMMEDIATE")
        if event:
            if con.execute("SELECT 1 FROM share_seen WHERE event=?", (event,)).fetchone():
                return True
            con.execute("INSERT INTO share_seen(event) VALUES (?)", (event,))
        _apply_share(con, sender, sid, op, msg)
    return True


def _apply_share(con: sqlite3.Connection, sender: str, sid: str, op, msg: dict) -> None:
    if op == "share":
        title, due = str(msg.get("title", "")).strip()[:TITLE_MAX], str(msg.get("due", ""))
        try:
            due = date.fromisoformat(due).isoformat()
        except ValueError:
            due = _now().date().isoformat()
        if not title:
            return
        row = con.execute("SELECT id FROM cards WHERE share_id=? AND owner=?", (sid, sender)).fetchone()
        if row:
            con.execute("UPDATE cards SET title=?, due=?, rev=rev+1 WHERE id=?", (_seal(title), due, row["id"]))
        elif con.execute("SELECT count(*) FROM cards WHERE owner=?", (sender,)).fetchone()[0] >= SHARED_PER_SENDER \
                or con.execute("SELECT count(*) FROM cards WHERE owner IS NOT NULL").fetchone()[0] >= SHARED_TOTAL:
            from vader.core import journal   # 넘친 공유는 넣지 않고 남긴다(제목 없이) — 받은 카드를 정리하면 다음 것부터 들어온다
            journal.record(journal.Entry(at=journal.now(), job_id="", rune_id="todo", level="warning",
                                         message="todo_share_dropped", fields={"reason": "too_many"}))
        else:
            con.execute("INSERT INTO cards(id,title,due,created_at,share_id,owner) VALUES (?,?,?,?,?,?)",
                        (uuid.uuid4().hex, _seal(title), due, _now().isoformat(), sid, sender))
    elif op == "unshare":   # 보낸 사람(주인)이 공유를 거뒀다 — 그 사람이 준 카드만 지운다(포스트잇 포함)
        for row in con.execute("SELECT id FROM cards WHERE share_id=? AND owner=?", (sid, sender)).fetchall():
            con.execute("DELETE FROM cards WHERE id=?", (row["id"],))
            con.execute("DELETE FROM notes WHERE card=?", (row["id"],))
            con.execute("DELETE FROM bodies WHERE card=?", (row["id"],))
    elif op == "done":
        undo = bool(msg.get("undo"))
        for row in con.execute("SELECT * FROM cards WHERE share_id=?", (sid,)).fetchall():
            mine = row["owner"] is None and sender in json.loads(row["shared_with"] or "[]")
            if row["owner"] != sender and not mine:
                continue
            con.execute("UPDATE cards SET state=?, done_at=?, rev=rev+1 WHERE id=?",
                        ("open", None, row["id"]) if undo else ("done", _now().isoformat(), row["id"]))
            if mine:   # 내가 여럿에게 공유했으면 나머지에게도 알린다
                _queue(con, [a for a in json.loads(row["shared_with"] or "[]") if a != sender],
                       {"op": "done", "id": sid, "undo": undo})


# ── 세부 포스트잇 (마스터 요청 2026-11-18) ──
# 카드를 누르면 포스트잇: 적는 칸(body) + 하위 항목. 하위 항목을 누르면 그 항목의 포스트잇으로 들어가고(드릴다운),
# 위 경로를 누르면 나온다(드릴업). 자리(node)는 카드 자신(카드 id) 또는 하위 항목 id. 내용·제목은 카드 제목과 같이 잠근다.
NOTE_DEPTH = 3          # 카드 아래로 들어갈 수 있는 단 수
BODY_MAX = 4000
NOTE_CHILDREN = 50


def _path(con: sqlite3.Connection, card_id: str, node: str) -> list[dict]:
    """카드에서 그 자리까지 [{node, title}]. 그 카드의 자리가 아니면 TodoError."""
    card = _one(con, card_id)
    path, at = [], node
    while at != card_id:
        row = con.execute("SELECT id, parent, title FROM notes WHERE id=? AND card=?", (at, card_id)).fetchone()
        if not row or len(path) > NOTE_DEPTH:
            raise TodoError("없는 포스트잇입니다.")
        path.append({"node": row["id"], "title": _unseal(row["title"])})
        at = row["parent"]
    return [{"node": card_id, "title": _unseal(card["title"])}] + path[::-1]


def note(card_id: str, node: str = "") -> dict:
    """그 자리의 포스트잇 — 경로, 적은 내용, 하위 항목(각자 그 밑 항목 수)."""
    con = _db()
    node = node or card_id
    path = _path(con, card_id, node)
    body = con.execute("SELECT body FROM bodies WHERE node=? AND card=?", (node, card_id)).fetchone()
    kids = con.execute("SELECT n.id, n.title, n.done, (SELECT count(*) FROM notes c WHERE c.parent=n.id) AS n "
                       "FROM notes n WHERE n.card=? AND n.parent=? ORDER BY n.created_at, n.id", (card_id, node)).fetchall()
    return {"card": card_id, "node": node, "path": path, "depth": len(path) - 1,
            "can_open": len(path) - 1 < NOTE_DEPTH, "body": _unseal(body["body"]) if body else "",
            "children": [{"id": k["id"], "title": _unseal(k["title"]), "done": bool(k["done"]), "count": k["n"]} for k in kids]}


def note_body(card_id: str, node: str, body: str) -> dict:
    body = str(body or "")
    if len(body) > BODY_MAX:
        raise TodoError(f"내용은 {BODY_MAX}자까지 적을 수 있어요.")
    con = _db()
    _path(con, card_id, node)
    if body.strip():
        con.execute("INSERT INTO bodies(node, card, body) VALUES (?,?,?) ON CONFLICT(node) DO UPDATE SET body=excluded.body",
                    (node, card_id, _seal(body)))
    else:
        con.execute("DELETE FROM bodies WHERE node=?", (node,))
    return note(card_id, node)


def note_add(card_id: str, node: str, title: str) -> dict:
    title = (title or "").strip()
    if not title or len(title) > TITLE_MAX:
        raise TodoError(f"항목은 1~{TITLE_MAX}자로 적어 주세요.")
    con = _db()
    if len(_path(con, card_id, node)) - 1 >= NOTE_DEPTH:
        raise TodoError(f"포스트잇은 {NOTE_DEPTH}단까지 들어갈 수 있어요.")
    if con.execute("SELECT count(*) FROM notes WHERE card=? AND parent=?", (card_id, node)).fetchone()[0] >= NOTE_CHILDREN:
        raise TodoError(f"한 포스트잇에 항목은 {NOTE_CHILDREN}개까지예요.")
    con.execute("INSERT INTO notes(id, card, parent, title, created_at) VALUES (?,?,?,?,?)",
                (uuid.uuid4().hex, card_id, node, _seal(title), _now().isoformat(timespec="microseconds")))
    return note(card_id, node)


def note_done(card_id: str, item: str, undo: bool = False) -> dict:
    con = _db()
    row = con.execute("SELECT parent FROM notes WHERE id=? AND card=?", (item, card_id)).fetchone()
    if not row:
        raise TodoError("없는 항목입니다.")
    con.execute("UPDATE notes SET done=? WHERE id=?", (0 if undo else 1, item))
    return note(card_id, row["parent"])


def note_delete(card_id: str, item: str) -> dict:
    """항목과 그 밑 전부(내용 포함). 화면이 두 번 눌러 확인받은 뒤에만."""
    con = _db()
    row = con.execute("SELECT parent FROM notes WHERE id=? AND card=?", (item, card_id)).fetchone()
    if not row:
        raise TodoError("없는 항목입니다.")
    gone, frontier = [item], [item]
    while frontier:
        frontier = [r[0] for r in con.execute(
            f"SELECT id FROM notes WHERE card=? AND parent IN ({','.join('?' * len(frontier))})", (card_id, *frontier))]
        gone += frontier
    with con:
        con.execute("BEGIN")
        marks = ",".join("?" * len(gone))
        con.execute(f"DELETE FROM notes WHERE id IN ({marks})", gone)
        con.execute(f"DELETE FROM bodies WHERE node IN ({marks})", gone)
    return note(card_id, row["parent"])


def due(now: datetime | None = None) -> list[list[str]]:
    """알릴 때가 됐는데 아직 안 알린 카드 id. 꺼져 있던 동안 지난 것도 여기서 잡힌다.
    알린 뒤 ack 로 표시한다 — 알림이 실패하면 다음 바퀴에 다시 (codex1 2026-11-10: 먼저 표시하면 유실).
    알림 문장에 제목은 넣지 않는다 (어깨너머·잠금 화면)."""
    if not (config.data_dir() / "todo.sqlite3").exists():
        return []
    now = now or _now()
    rows = _db().execute("SELECT id, remind_at FROM cards WHERE state='open' AND reminded=0 AND remind_at IS NOT NULL AND remind_at<=?",
                         (now.isoformat(),)).fetchall()
    return [[r["id"], r["remind_at"]] for r in rows]


def ack(cards: list[list[str]]) -> None:
    """알림을 띄운 [id, remind_at] 을 '알렸음' 으로 — 그 사이 다시 미뤄 예약이 바뀐 카드는 건드리지 않는다."""
    con = _db()
    con.executemany("UPDATE cards SET reminded=1 WHERE id=? AND remind_at=?", [(i, at) for i, at in cards])
