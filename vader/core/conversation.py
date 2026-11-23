"""대화 하나를 **기계 하나가** 갖는다. Phase 15.

완료 기준이 이렇다 — "한 기기에서 만든 펫을 다른 화면에서 만나 **대화가
이어진다**". 폰이 자기 대화를 따로 갖는 순간 그건 다른 펫이다.

그래서 화면이 아니라 여기가 대화를 들고 있다.

    데스크톱 창 ─┐
                 ├─→ conversation.session() ─→ ConversationSession 하나
    폰 브라우저 ─┘

`ConversationSession` 자체는 그대로 둔다. 한 대화를 표현하는 것과 그 대화가
이 기계에 하나뿐이라는 것은 다른 이야기다 — 섞으면 테스트가 전역 상태를
안고 돌아야 한다.

## 한 번에 한 턴

폰과 데스크톱이 동시에 말을 걸 수 있다. 락 없이 두면 이력이 섞이고 한쪽
말이 사라진다. **턴 전체를 잠근다** — 도구 호출까지 한 턴이다.

승인 창은 이 락 안에서 뜬다. 그래서 **폰이 설정을 바꾸려면 데스크톱 앞의
사람이 승인해야 한다.** 원격이 조용히 바꾸는 길은 없다.

## 무엇이 디스크에 남나

    메시지    남는다. 앱을 닫아도 대화가 이어진다
    조회 결과  안 남는다. 수명이 있는 값이고, 되살리면 "그때 값" 이 다시
              지금 값인 척한다. 그 문제로 이미 한 번 데었다

**관찰은 여기 없다.** 대화는 사용자가 스스로 말한 것이라 기계를 떠나도 되고
(`data-map.md`), 관찰은 아니다. 한 파일에 섞으면 매번 따져야 한다.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from vader.core import config, pet as pet_module
from vader.core.chat import ConversationSession, Message

__all__ = ["session", "say", "history", "reassign", "forget", "path"]

#: 이력이 길어도 파일은 이 줄 수까지만 간다. `chat.MAX_TURNS` 가 프롬프트에
#: 실을 양이라면 이쪽은 **다시 열었을 때 보일 양**이라 조금 넉넉하다.
MAX_STORED = 100

_LOCK = threading.RLock()
_CURRENT: ConversationSession | None = None
#: 대화 파일 이름. **데스크톱은 따로 쓴다** (codex1 P1-1, 2026-11-05) — 클래식 대화의 옛 답에는
#: 일지·녹화 도구가 읽어 온 업무 내용이 인용돼 있을 수 있고, 이력은 매 턴 통째로 AI 에 다시 나간다.
_NAME = "conversation.json"


def session(*, approve=None, log=None, **fixed) -> ConversationSession:
    """이 기계의 대화. 처음 부르면 디스크에서 되살린다.

    `approve` 와 `fixed`(화면 사실·도구 여부) 는 **처음 만들 때만** 붙는다. 데스크톱 창이 먼저 열려 있으면
    승인 창은 계속 그 창이 띄운다 — 폰이 자기 승인 통로를 가져오면 그게
    원격이 스스로 허락하는 길이다.
    """
    global _CURRENT
    with _LOCK:
        if _CURRENT is None:
            _CURRENT = ConversationSession(
                pet=pet_module.load(),
                approve=approve,
                messages=_load(),
                **({"log": log} if log else {}),
                **fixed,
            )
        return _CURRENT


def say(text: str, *, approve=None, log=None) -> str:
    """한 턴. **턴 전체가 잠긴다** — 도구 호출까지 한 턴이다."""
    with _LOCK:
        talk = session(approve=approve, log=log)
        answer = talk.say(text)
        _save(talk.messages)
        return answer


def history() -> list[dict]:
    """화면에 그릴 복사본. 진행 중인 턴이 끝날 때까지 기다린다 — 화면은 바쁠 때 부르지 않는다."""
    with _LOCK:
        return [{"role": m.role, "content": m.content} for m in session().messages]


def reassign() -> None:
    """기본 AI 가 바뀌었다 — 다음 턴에 다시 고른다. 이력은 그대로.

    잠그지 않는다: 진행 중인 턴은 이미 고른 것을 쥐고 있고, 대입 하나는 끊기지 않는다.
    """
    if _CURRENT is not None:
        _CURRENT.assignment = None


def forget() -> None:
    with _LOCK:
        session().forget()
        _save([])


def reset() -> None:
    """다음 `session()` 이 디스크에서 다시 읽게 한다. 테스트와 펫 교체용."""
    global _CURRENT
    with _LOCK:
        _CURRENT = None


def use(name: str) -> None:
    """이 프로세스가 쓸 대화 파일. 첫 `session()` 전에 부른다."""
    global _NAME
    with _LOCK:
        if _CURRENT is not None:
            raise RuntimeError("대화가 이미 열렸습니다 — 파일은 처음에만 정합니다")
        _NAME = name


def path() -> Path:
    return config.data_dir() / _NAME


def _load() -> list[Message]:
    """**읽기 실패는 빈 대화다.** 지난 대화를 못 읽는 것이 새 대화까지
    막으면 안 된다."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    out = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict):      # [null]·[1] 도 합법 JSON 이다 — 그 줄만 버린다 (codex1 P2-5)
            continue
        role, content = row.get("role"), row.get("content")
        if role in ("user", "assistant") and isinstance(content, str):
            out.append(Message(role, content))
    return out[-MAX_STORED:]


def _save(messages: list[Message]) -> None:
    try:
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        # 펫·기기 목록과 같은 방식. 쓰다 죽어도 반쯤 쓰인 파일이 안 남는다.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent,
            prefix=".conversation.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(
                [{"role": m.role, "content": m.content} for m in messages[-MAX_STORED:]],
                stream,
                ensure_ascii=False,
                indent=2,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError:
        # 저장 실패가 방금 받은 답을 없애지는 않는다. 원장·일지와 같은 규칙이다.
        pass
