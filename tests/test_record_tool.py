"""대화로 녹화를 시작하고 멈추고 저장한다.

마스터 요청 (2026-08-11):

> 완성된 룬을 녹화로 수정할 때, 지금처럼 llm에서 이야기하는게 아닌 **실시간
> 대화를 통해서** 이루어지도록 하면 좋겠어.

지금까지의 왕복에는 **가운데에 내가 있었다** — 마스터가 로그를 붙여넣고,
내가 고치고, 재설치하고. 그 자리를 없애는 것이 이 파일이 지키는 것이다.
"""
from tests.private import needs
import pytest

from vader.core import record_tool, recorder
from vader.core.record_tool import RecordTool
from vader.core.recorder import Step


def _step(name="재고관리", **over) -> Step:
    base = dict(
        at="2026-08-11T10:00:00",
        window_title="이영돈 [] 가나병원",
        control_type="MenuItem",
        name=name,
        automation_id="",
        class_name="",
        rel_x=0.3,
        rel_y=0.05,
    )
    base.update(over)
    return Step(**base)


@pytest.fixture
def tool(monkeypatch):
    """마우스를 안 쓰는 녹화기를 물린 도구."""
    made = RecordTool(ask=lambda name, labels: True)

    class Fake:
        def __init__(self):
            self.steps = []
            self.running = False

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    monkeypatch.setattr(recorder, "Recorder", Fake)
    return made


def test_starting_says_it_will_not_record_keys(tool):
    """대화로 시작한다고 키로거가 되어도 되는 것은 아니다.

    사용자가 그 사실을 **듣고** 시연을 시작해야 한다.
    """
    said = tool.invoke("start")

    assert "키 입력은 기록하지 않습니다" in said


def test_stopping_reads_back_what_it_caught(tool):
    """사람이 여기서 거른다. 안 읽어 주면 확인이 형식이 된다."""
    tool.invoke("start")
    tool._recorder.steps = [_step(), _step("임의입고/반납관리")]

    said = tool.invoke("stop")

    assert "2단계" in said
    assert "재고관리" in said
    assert "임의입고/반납관리" in said
    assert "저장하면 다음 실행부터 바로 적용" in said


def test_a_fragile_step_is_pointed_out(tool):
    """좌표로만 찾는 단계는 창 크기가 바뀌는 날 깨진다."""
    tool.invoke("start")
    tool._recorder.steps = [_step(name="", automation_id="")]

    assert "좌표로만" in tool.invoke("stop")


def test_nothing_caught_is_said_plainly(tool):
    tool.invoke("start")

    assert "아무것도 안 잡혔습니다" in tool.invoke("stop")


def test_saving_needs_a_person_to_say_yes(monkeypatch, isolated_settings):
    """녹화는 룬이 다음에 무엇을 누를지 정한다. **설정보다 무겁다.**"""
    made = RecordTool(ask=lambda name, labels: False)

    class Fake:
        steps = [_step()]
        running = False

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(recorder, "Recorder", Fake)
    made.invoke("start")

    said = made.invoke("save", "navigation")

    assert "승인하지 않아" in said
    assert recorder.load("navigation") == []


def test_without_an_approval_channel_nothing_is_saved(monkeypatch, isolated_settings):
    made = RecordTool()

    class Fake:
        steps = [_step()]
        running = False

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(recorder, "Recorder", Fake)
    made.invoke("start")

    assert "승인 통로가 없어" in made.invoke("save")
    assert recorder.load("navigation") == []


def test_an_approved_save_lands_on_disk(tool, isolated_settings):
    tool.invoke("start")
    tool._recorder.steps = [_step(), _step("임의입고/반납관리")]

    said = tool.invoke("save", "navigation")

    assert "저장해 적용" in said
    assert "다음 실행부터" in said
    assert [s.name for s in recorder.load("navigation")] == [
        "재고관리",
        "임의입고/반납관리",
    ]


def test_discarding_leaves_the_old_one_alone(tool, isolated_settings):
    """새로 녹화하다 말았다고 쓰던 것이 없어지면 안 된다."""
    recorder.save("navigation", [_step("옛것")])
    tool.invoke("start")
    tool._recorder.steps = [_step("새것")]

    tool.invoke("discard")

    assert [s.name for s in recorder.load("navigation")] == ["옛것"]


def test_saving_nothing_is_refused(tool, isolated_settings):
    assert "저장할 녹화가 없습니다" in tool.invoke("save")


def test_only_a_recording_name_that_has_a_consumer_can_be_saved(
    tool, isolated_settings
):
    tool.invoke("start")
    tool._recorder.steps = [_step()]

    said = tool.invoke("save", "eonm_guide")

    assert "쓸 수 없는 녹화 이름" in said
    assert "navigation" in said
    assert recorder.load("eonm_guide") == []
    assert tool.current_recorder() is not None


def test_the_schema_does_not_offer_free_form_recording_names():
    name_schema = record_tool.TOOL_SCHEMA["parameters"]["properties"]["name"]

    assert name_schema["enum"] == ["navigation"]


def test_saving_twice_reports_that_the_first_save_already_applied(
    tool, isolated_settings
):
    tool.invoke("start")
    tool._recorder.steps = [_step()]
    tool.invoke("save", "navigation")

    said = tool.invoke("save", "navigation")

    assert "이미 'navigation' 으로 1단계를 저장해 적용했습니다" in said


def test_the_ui_can_receive_the_active_recorder(tool):
    assert tool.current_recorder() is None

    tool.invoke("start")

    assert tool.current_recorder() is tool._recorder


def test_an_unknown_action_says_what_it_knows(tool):
    said = tool.invoke("녹화해줘")

    assert "start" in said and "save" in said


def test_starting_twice_does_not_lose_the_first(tool):
    tool.invoke("start")
    tool._recorder.steps = [_step()]
    tool._recorder.running = True

    tool.invoke("start")

    assert len(tool._recorder.steps) == 1


def test_the_panel_reports_a_saved_take_back_to_its_owner(tool):
    """화면 저장 뒤에도 도구가 같은 것을 또 저장하려 들지 않는다."""
    tool.invoke("start")
    made = tool.current_recorder()
    made.steps = [_step()]
    made.stop()

    assert tool.finish_recorder(made, saved_as="navigation") is True
    assert tool.current_recorder() is None
    assert "이미 'navigation' 으로 1단계를 저장해 적용했습니다" in tool.invoke(
        "save", "navigation"
    )


def test_an_old_panel_cannot_clear_a_new_take(tool):
    """이전 회차의 늦은 저장 콜백은 다음 회차 상태를 건드리지 못한다."""
    tool.invoke("start")
    old = tool.current_recorder()
    old.stop()
    tool.invoke("start")
    current = tool.current_recorder()

    assert current is not old
    assert tool.finish_recorder(old, saved_as="navigation") is False
    assert tool.current_recorder() is current
    assert current.running is True


# --- 대화에 붙어 있나 ------------------------------------------------------------


def test_the_chat_offers_the_recording_tool():
    """도구로 안 붙으면 펫이 부를 방법이 없고, 그러면 대화로 못 고친다."""
    from vader.core import chat

    names = [s["function"]["name"] for s in chat._tool_schemas()]

    assert record_tool.TOOL_SCHEMA["name"] in names


def test_the_session_holds_the_recorder_across_turns(isolated_settings):
    """턴마다 새로 만들면 `시작` 과 `저장` 사이에 잡은 것이 사라진다."""
    from types import SimpleNamespace

    from vader.core import chat, pet as pet_module

    session = chat.ConversationSession(pet=pet_module.Pet(id="p", name="바다"))
    call = SimpleNamespace(id="c1", name="record_demo", arguments={"action": "stop"})

    session._run_tool(call)
    first = session._recorder
    session._run_tool(call)

    assert session._recorder is first


def test_every_tool_asks_through_the_same_channel():
    """도구마다 다른 인자로 승인을 부르면 **하나 붙일 때마다 화면이 깨진다.**

    실제로 녹화 도구를 붙이며 그럴 뻔했다 — 설정 도구는 인자 넷으로 부르고
    화면은 넷을 받는데, 녹화 도구는 둘로 불렀다.
    """
    import inspect

    from vader.core.chat_ui import ChatWindow

    signature = inspect.signature(ChatWindow._approve)
    names = [n for n in signature.parameters if n != "self"]

    assert names == ["title", "body"]


def test_the_settings_tool_uses_that_channel(isolated_settings, monkeypatch):
    from types import SimpleNamespace

    from vader.core import settings_tool

    monkeypatch.setattr(
        "vader.core.loader.discover",
        lambda *a, **k: (
            [SimpleNamespace(manifest=SimpleNamespace(
                id="weather",
                required_fields=[SimpleNamespace(
                    name="city", kind="text", label="도시", default=""
                )],
            ))],
            [],
        ),
    )
    seen = []

    settings_tool.SettingsTool(
        ask=lambda title, body: seen.append((title, body)) or True
    ).invoke("weather", "city", "부산")

    assert seen and "설정" in seen[0][0]


# --- 대화창이 목록을 그린다 (마스터 실측 2026-08-11) ---------------------------


def test_starting_from_chat_opens_the_panel(isolated_settings, monkeypatch):
    """대화로만 하려니 **모델이 잡은 것을 요약해 버렸다.**

    마스터 실측:

        마스터: 녹화 끝
        펫:     이대로 저장하시겠어요? (예 / 아니오)

    도구는 단계를 줄줄이 돌려줬는데 화면에는 저 한 줄만 나왔다. 사람이
    확인해야 할 자리에서 확인할 것이 사라졌다.
    """
    from types import SimpleNamespace

    from vader.core import chat, pet as pet_module, recorder as recorder_module

    class Fake:
        def __init__(self):
            self.steps = []
            self.running = False

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    monkeypatch.setattr(recorder_module, "Recorder", Fake)
    opened = []
    session = chat.ConversationSession(
        pet=pet_module.Pet(id="p", name="바다"),
        show_recorder=lambda made: opened.append(made),
    )
    call = SimpleNamespace(id="c1", name="record_demo", arguments={"action": "start"})

    said = session._run_tool(call)["content"]

    assert len(opened) == 1, "시작하면 화면이 떠야 한다"
    assert opened[0] is session._recorder.current_recorder()
    assert "녹화 창을 열었습니다" in said


def test_the_panel_takes_over_the_running_recorder(isolated_settings, monkeypatch):
    """새로 만들면 사용자가 **이미 보여 준 것이 사라진다.**"""
    from vader.core import recorder as recorder_module

    made = RecordTool(ask=lambda title, body: True)

    class Fake:
        def __init__(self):
            self.steps = []
            self.running = False

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    monkeypatch.setattr(recorder_module, "Recorder", Fake)
    made.invoke("start")

    assert made.current_recorder() is not None
    assert made.current_recorder().running is True


def test_a_name_the_rune_never_reads_is_refused(isolated_settings, monkeypatch):
    """마스터가 `eonm_guide` 로 저장했는데 룬은 `navigation` 만 재생한다.

    **조용히 아무 일도 안 일어났다.** 저장했다고 믿게 두면 안 된다.
    """
    from vader.core import recorder as recorder_module

    made = RecordTool(ask=lambda title, body: True)

    class Fake:
        steps = [_step()]
        running = False

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(recorder_module, "Recorder", Fake)
    made.invoke("start")

    said = made.invoke("save", "eonm_guide")

    assert "쓸 수 없는" in said
    assert "navigation" in said
    assert recorder.load("eonm_guide") == []


def test_saving_twice_says_it_is_already_applied(isolated_settings, monkeypatch):
    """"저장할 녹화가 없습니다" 를 사용자는 **실패로 읽는다.**"""
    from vader.core import recorder as recorder_module

    made = RecordTool(ask=lambda title, body: True)

    class Fake:
        steps = [_step()]
        running = False

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(recorder_module, "Recorder", Fake)
    made.invoke("start")
    made.invoke("save", "navigation")

    assert "이미" in made.invoke("save", "navigation")


@needs("rune-packages/eonm_ipgo")
def test_every_purpose_is_a_name_some_rune_actually_replays():
    """목록에 없는 이름을 저장하면 아무 데도 안 쓰인다.

    반대로 **목록에 있는데 아무도 안 읽는 이름**도 같은 거짓말이다.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "rune-packages"
    replayed = " ".join(
        source.read_text(encoding="utf-8")
        for source in root.rglob("*.py")
        if "__pycache__" not in source.parts
    )
    for name, purpose in recorder.PURPOSES:
        # 닫는 괄호까지 보지 않는다 — 인자가 붙는다(`stop=`). 여기서 보는
        # 것은 "이 이름을 재생하는 룬이 있나" 지 호출 모양이 아니다.
        assert f'replay("{name}"' in replayed, f"{name} 을 읽는 룬이 없다"
        assert purpose, f"{name} 이 무엇에 쓰이는지 안 적혀 있다"
