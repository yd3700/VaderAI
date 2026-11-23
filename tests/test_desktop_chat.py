"""데스크톱 대화 서랍 — 도구 없이 화면 사실만으로 답한다 (codex1 2026-11-05).

일지·녹화 설명은 병원 데이터를 AI 로 보낼 수 있고, 설정 바꾸기는 데스크톱의 실행 중 차단·값 검증을
거치지 않는다. 그래서 데스크톱 대화는 도구를 끄고, 화면이 실제로 가진 메뉴만 안내한다.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from vader.core import chat
from vader.core.chat import ConversationSession
from vader.core.ocr import Assignment
from vader.core.ocr.base import ToolCall
from vader.core.pet import Pet
from vader.core.usage import UsageLedger

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "vader/desktop/assets"


class _Provider:
    """도구를 받을 수 있는 척하는 제공자 — 받은 것을 적어 둔다."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def supports(self, role):
        return True

    def name(self):
        return "fake"

    def model(self):
        return "m"

    def chat(self, messages, tools=None):
        self.calls.append((messages, tools))
        return self.answers.pop(0)


@pytest.fixture
def provider(monkeypatch):
    from vader.core import ocr

    made = _Provider("안내입니다.")
    monkeypatch.setattr(ocr, "PROVIDERS", {**ocr.PROVIDERS, "fake": lambda model=None: made})
    monkeypatch.setattr(chat, "_takes_tools", lambda p: True)
    monkeypatch.setattr(chat, "discover", lambda: ([SimpleNamespace(manifest=SimpleNamespace(id="eonm_ipgo"))], []))
    return made


def _session(tmp_path, **kw):
    from vader.desktop.host import CHAT_FACTS
    return ConversationSession(pet=Pet(id="p1", name="바다"), assignment=Assignment("fake", None, False),
                               ledger=UsageLedger(tmp_path / "usage"), screen=CHAT_FACTS, tools=False, **kw)


def test_desktop_chat_offers_no_tools_and_says_only_screen_facts(tmp_path, provider):
    talk = _session(tmp_path)
    assert talk.say("승인은 언제 해요?") == "안내입니다."
    messages, tools = provider.calls[0]
    assert not tools
    system = messages[0]["content"]
    assert "매핑 관리" in system and "eonm_ipgo" in system
    for tool in ("explain_run", "explain_recording", "record_demo", "change_setting", "실행 일지"):
        assert tool not in system, tool


def test_a_tool_call_is_refused_even_if_the_model_makes_one(tmp_path, monkeypatch):
    talk = _session(tmp_path)
    monkeypatch.setattr(chat.journal, "explain", lambda *a: pytest.fail("일지를 읽으면 안 된다"))
    reply = talk._run_tool(ToolCall(id="c1", name="explain_run", arguments={}))
    assert "도구를 쓰지 않습니다" in reply["content"]


def test_the_facts_name_every_menu_and_header_button_on_screen():
    """화면이 바뀌면 사실도 같이 고친다 — 없는 메뉴를 안내하게 두지 않는다."""
    from vader.desktop.host import CHAT_FACTS
    app = (ASSETS / "app.js").read_text(encoding="utf-8")
    pages = re.findall(r"\['\w+','([^']+)'\]", re.search(r"const pages = \[(.*?)\];", app).group(1))
    rail = (ASSETS / "index.html").read_text(encoding="utf-8").split('<aside class="rail">')[1].split("</aside>")[0]
    buttons = re.findall(r'<button id="(?:accountButton|aiButton|chatToggle|msgToggle)"[^>]*>.*?<span class="rail-label">([^<]+)<', rail)
    assert len(pages) == 5 and len(buttons) == 3          # 대화 서랍은 꺼 두었다 (메신저만)
    assert 'id="chatToggle"' not in rail and 'src="chat.js"' not in (ASSETS / "index.html").read_text(encoding="utf-8")
    missing = [name for name in pages + buttons if f"`{name}`" not in CHAT_FACTS]
    assert missing == []


def test_the_host_runs_one_turn_at_a_time(monkeypatch):
    from vader.desktop import host
    api = host.DesktopAPI(MagicMock())   # 처리기 표가 컨트롤러 메서드를 다 집는다
    entered, release = threading.Event(), threading.Event()

    def slow(text):
        entered.set()
        release.wait(5)
        return "답"

    monkeypatch.setattr(host.conversation, "say", slow)
    monkeypatch.setattr(host.conversation, "history", lambda: pytest.fail("턴 중에는 이력을 기다리지 않는다"))
    worker = threading.Thread(target=lambda: api.call("chat_say", {"text": "첫 질문"}))
    worker.start()
    entered.wait(5)
    assert api.call("chat_say", {"text": "또"}) == {"ok": False, "error": "앞 질문의 답을 기다리는 중이에요."}
    assert api.call("chat_history")["data"] == {"busy": True, "messages": []}
    assert not api.call("chat_forget")["ok"]
    release.set()
    worker.join(5)
    assert api.call("chat_say", {"text": "  "})["ok"] is False
    assert api.call("chat_say", {"text": "가" * (host.CHAT_MAX + 1)})["ok"] is False


def test_changing_the_default_ai_reassigns_the_next_turn(monkeypatch):
    from vader.desktop import host
    seen = []
    monkeypatch.setattr(host.conversation, "reassign", lambda: seen.append(True))
    api = host.DesktopAPI(MagicMock())
    assert api.call("ai_default", {"provider": "gemini"})["ok"]
    assert seen == [True]


def test_the_drawer_never_writes_model_text_as_html():
    js = (ASSETS / "chat.js").read_text(encoding="utf-8")
    # innerHTML 은 고정 문구·아이콘에만 — 답(answer)·이력(content)·사용자 말(text)은 글자로만 붙인다
    for line in js.splitlines():
        if "innerHTML" in line:
            assert not re.search(r"innerHTML\s*=[^;]*\b(answer|content|text|part)\b", line), line


def test_the_desktop_keeps_its_own_conversation_file(isolated_settings, monkeypatch):
    """codex1 P1-1 — 클래식 대화의 옛 답(도구가 읽은 업무 내용이 섞였을 수 있다)을 데스크톱이 AI 로 다시 보내지 않는다."""
    from vader.core import conversation
    from vader.core.chat import Message
    monkeypatch.setattr(conversation, "_NAME", conversation._NAME)
    conversation.reset()
    conversation._save([Message("assistant", "가나메디칼 창 제목을 눌렀습니다")])   # 클래식 이력
    conversation.use("desktop-conversation.json")
    try:
        assert conversation.session(tools=False).messages == []
        with pytest.raises(RuntimeError):
            conversation.use("conversation.json")              # 열린 뒤에는 못 바꾼다
    finally:
        conversation.reset()


def test_junk_rows_of_any_type_do_not_stop_the_app(isolated_settings, monkeypatch):
    """codex1 P2-5 — [null]·[1]·["x"] 도 합법 JSON 이다. 이제 대화는 창보다 먼저 열린다."""
    from vader.core import conversation
    conversation.reset()
    conversation.path().parent.mkdir(parents=True, exist_ok=True)
    conversation.path().write_text('[null, 1, "x", {"role": "user", "content": "진짜"}]', encoding="utf-8")
    try:
        assert [m.content for m in conversation.session().messages] == ["진짜"]
    finally:
        conversation.reset()


def test_forget_and_history_take_the_same_turn_lock(monkeypatch):
    """codex1 P2-1 — 검사와 수행 사이에 턴이 끼지 않는다: 지우는 동안 보내기는 바로 거절된다."""
    from vader.desktop import host
    api = host.DesktopAPI(MagicMock())
    inside, release = threading.Event(), threading.Event()

    def slow_forget():
        inside.set()
        release.wait(5)

    monkeypatch.setattr(host.conversation, "forget", slow_forget)
    monkeypatch.setattr(host.conversation, "say", lambda text: pytest.fail("지우는 중에 보내면 안 된다"))
    worker = threading.Thread(target=lambda: api.call("chat_forget"))
    worker.start()
    inside.wait(5)
    assert api.call("chat_say", {"text": "질문"})["ok"] is False
    release.set()
    worker.join(5)


def test_a_closing_or_unprepared_app_refuses_chat(monkeypatch):
    from vader.desktop import host
    api = host.DesktopAPI(MagicMock())
    api._chat_off = "창을 닫는 중이에요."
    monkeypatch.setattr(host.conversation, "say", lambda text: pytest.fail("닫는 중에 보내면 안 된다"))
    assert api.call("chat_say", {"text": "질문"}) == {"ok": False, "error": "창을 닫는 중이에요."}
    assert api.call("chat_history")["ok"] is False


def test_closing_and_taking_a_turn_are_one_step(monkeypatch):
    """codex1 — '꺼짐' 검사와 턴 접수 사이에 창 닫기가 끼지 않는다: 닫기는 접수가 끝날 때까지 기다렸다 선다."""
    from vader.desktop import host
    api = host.DesktopAPI(MagicMock())
    taken, closed = threading.Event(), threading.Event()

    def say(text):
        taken.set()
        closed.wait(5)
        return "답"

    monkeypatch.setattr(host.conversation, "say", say)
    worker = threading.Thread(target=lambda: api.call("chat_say", {"text": "질문"}))
    worker.start()
    taken.wait(5)
    api.chat_off("창을 닫는 중이에요.")          # 접수된 턴은 그대로, 새 턴은 거절
    closed.set()
    worker.join(5)
    assert api.call("chat_say", {"text": "또"})["error"] == "창을 닫는 중이에요."
    assert not api._chat_gate.locked()


def test_the_messenger_drawer_never_writes_received_text_as_html():
    """친구가 보낸 글·아이디는 textContent 로만. innerHTML 은 아이콘과 고정 문구에만 (3단계 3d)."""
    js = (ASSETS / "messenger.js").read_text(encoding="utf-8")
    for line in js.splitlines():
        for target in re.findall(r"innerHTML\s*=([^;]*)", line):
            assert re.fullmatch(r"\s*(f\?''\s*:\s*)?icon\('\w+'\)\s*|\s*'[^'+]*'\s*", target), line
