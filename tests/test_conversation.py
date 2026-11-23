"""대화를 화면이 아니라 기계가 갖는다. Phase 15.

"한 기기에서 만든 펫을 다른 화면에서 만나 **대화가 이어진다**" 가 완료
기준이다. 폰이 자기 대화를 따로 가지면 그건 다른 펫이다.
"""
import threading

import pytest

from vader.core import conversation, ocr, pet as pet_module
from vader.core.chat import Message
from vader.core.ocr import Assignment
from vader.core.ocr.base import FakeOcrProvider


@pytest.fixture(autouse=True)
def fresh(isolated_settings, monkeypatch):
    conversation.reset()
    pet_module.save(pet_module.Pet(id="p1", name="바다"))
    yield
    conversation.reset()


@pytest.fixture
def answering(monkeypatch):
    def install(*answers):
        provider = FakeOcrProvider(list(answers))
        monkeypatch.setattr(
            ocr, "PROVIDERS", {**ocr.PROVIDERS, "fake": lambda model=None: provider}
        )
        monkeypatch.setattr(
            conversation, "session", _sessioner(monkeypatch, provider)
        )
        return provider

    return install


def _sessioner(monkeypatch, provider):
    real = conversation.session

    def wrapped(**kwargs):
        talk = real(**kwargs)
        talk.assignment = Assignment("fake", None, False)
        return talk

    return wrapped


def test_two_screens_share_one_conversation(answering):
    """폰이 자기 대화를 따로 가지면 그건 다른 펫이다."""
    answering("하나요", "둘이요")

    conversation.say("데스크톱에서 한 말")
    conversation.say("폰에서 한 말")

    said = [m.content for m in conversation.session().messages]
    assert said == ["데스크톱에서 한 말", "하나요", "폰에서 한 말", "둘이요"]


def test_the_conversation_survives_a_restart(answering):
    """앱을 닫으면 사라지는 대화는 "이어진다" 가 아니다."""
    answering("네")
    conversation.say("어제 한 말")

    conversation.reset()  # 앱을 닫았다 켰다

    assert [m.content for m in conversation.session().messages] == ["어제 한 말", "네"]


def test_lookups_are_not_restored(answering):
    """수명이 있는 값을 되살리면 "그때 값" 이 다시 지금 값인 척한다.

    그 문제로 이미 한 번 데었다 — 펫이 진짜 데이터를 "임의로 만든 답변"
    이라고 자백한 사건의 반대편이다.
    """
    from vader.core.tools import Fact

    answering("네")
    conversation.say("안녕")
    conversation.session().facts.append(
        Fact(text="어제 날씨 3도", source="어디", at="어제", topic="weather")
    )

    conversation.reset()

    assert conversation.session().facts == []


def test_forgetting_clears_the_disk_too(answering):
    """화면에서 지웠는데 파일에 남아 있으면 지운 것이 아니다."""
    answering("네")
    conversation.say("지울 말")

    conversation.forget()
    conversation.reset()

    assert conversation.session().messages == []


def test_a_broken_file_is_an_empty_conversation(isolated_settings):
    """지난 대화를 못 읽는 것이 새 대화까지 막으면 안 된다."""
    conversation.path().parent.mkdir(parents=True, exist_ok=True)
    conversation.path().write_text("{쓰레기", encoding="utf-8")

    assert conversation.session().messages == []


def test_a_junk_row_is_dropped_not_fatal(isolated_settings):
    conversation.path().parent.mkdir(parents=True, exist_ok=True)
    conversation.path().write_text(
        '[{"role": "user", "content": "진짜"}, {"role": "권한상승", "content": "가짜"},'
        ' {"role": "assistant", "content": 3}]',
        encoding="utf-8",
    )

    assert [(m.role, m.content) for m in conversation.session().messages] == [
        ("user", "진짜")
    ]


def test_only_the_recent_turns_are_stored(isolated_settings):
    conversation._save([Message("user", f"{i}번") for i in range(conversation.MAX_STORED + 10)])

    stored = conversation._load()

    assert len(stored) == conversation.MAX_STORED
    assert stored[0].content == "10번"


def test_two_speakers_at_once_do_not_lose_a_turn(answering):
    """폰과 데스크톱이 동시에 말을 걸 수 있다. 락이 없으면 한쪽이 사라진다."""
    answering(*[f"{i}번 답" for i in range(6)])
    errors = []

    def talk(name):
        try:
            for i in range(3):
                conversation.say(f"{name}-{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=talk, args=(n,)) for n in ("폰", "데스크톱")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    said = [m.content for m in conversation.session().messages if m.role == "user"]
    assert sorted(said) == sorted([f"{n}-{i}" for n in ("폰", "데스크톱") for i in range(3)])


def test_a_save_failure_does_not_lose_the_answer(answering, monkeypatch):
    """저장 실패가 방금 받은 답을 없애지는 않는다."""
    answering("이건 답이다")
    monkeypatch.setattr(
        conversation, "path", lambda: (_ for _ in ()).throw(OSError)
    )

    assert conversation.say("안녕") == "이건 답이다"


def test_a_turn_is_not_cut_in_half_by_the_other_screen(isolated_settings, monkeypatch):
    """락이 없으면 순서가 `사용자·사용자·펫·펫` 로 접힌다.

    내용은 안 사라진다 — 그래서 개수만 세면 통과한다. 망가지는 것은
    **차례**고, 다음 턴에 그 이력이 통째로 모델에 나간다.
    """
    import time

    from vader.core import ocr
    from vader.core.ocr.base import FakeOcrProvider

    class SlowProvider(FakeOcrProvider):
        def chat(self, messages, tools=None):
            time.sleep(0.15)  # 다른 화면이 끼어들 만큼
            return "답"

    monkeypatch.setattr(
        ocr, "PROVIDERS", {**ocr.PROVIDERS, "fake": lambda model=None: SlowProvider([])}
    )
    monkeypatch.setattr(conversation, "session", _sessioner(monkeypatch, None))

    threads = [
        threading.Thread(target=conversation.say, args=(f"{name} 말",))
        for name in ("폰", "데스크톱")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    roles = [m.role for m in conversation.session().messages]
    assert roles == ["user", "assistant", "user", "assistant"], f"차례가 접혔다: {roles}"
