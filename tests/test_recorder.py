"""시연 녹화. **말로 안 나오는 것을 손이 알려 준다.**

마스터 제안(2026-08-11). 이온엠 메뉴를 세 번 고쳤고 세 번 다 빗나갔다 —
화면을 못 보면서 셀렉터를 썼기 때문이다.

여기 테스트는 **마우스를 안 쓴다.** 좌표를 `Step` 으로 바꾸는 자리를
갈아끼울 수 있게 만들어 뒀다. 그래야 이 로직을 CI 에서 지킬 수 있다.
"""
import json

import pytest

from vader.core import config, recorder
from vader.core.recorder import Recorder, Step


def _step(**over) -> Step:
    base = dict(
        at="2026-08-11T10:00:00",
        window_title="이영돈 [] 가나병원",
        control_type="MenuItem",
        name="재고관리",
        automation_id="",
        class_name="#32768",
        rel_x=0.3,
        rel_y=0.05,
    )
    base.update(over)
    return Step(**base)


def _feeding(*steps):
    """좌표를 받아 미리 정한 `Step` 을 차례로 돌려주는 탐침."""
    queue = list(steps)

    def probe(x, y, *, skip_pid=0):
        return queue.pop(0) if queue else None

    return probe


# --- 기록 ---------------------------------------------------------------------


def test_a_click_becomes_a_step():
    made = Recorder(probe=_feeding(_step()))

    made.capture(100, 20)

    assert [s.name for s in made.steps] == ["재고관리"]


def test_our_own_window_is_not_recorded():
    """녹화 창의 `중지` 까지 담기면 재생할 때 그것도 누른다."""
    made = Recorder(probe=lambda x, y, *, skip_pid=0: None)

    assert made.capture(5, 5) is None
    assert made.steps == []


def test_the_same_control_twice_in_a_row_is_one_step():
    """한 번의 클릭이 두 줄이 되면 확인 화면이 거짓말을 한다."""
    made = Recorder(probe=_feeding(_step(), _step()))

    made.capture(100, 20)
    made.capture(100, 20)

    assert len(made.steps) == 1


def test_a_different_control_is_a_new_step():
    made = Recorder(probe=_feeding(_step(), _step(name="임의입고/반납관리", rel_y=0.4)))

    made.capture(100, 20)
    made.capture(100, 200)

    assert [s.name for s in made.steps] == ["재고관리", "임의입고/반납관리"]


def test_a_step_without_a_name_says_it_is_fragile():
    """이름도 `automation_id` 도 없으면 남는 것은 좌표뿐이고, 그건 창 크기가
    바뀌면 깨진다. **확인 화면이 그걸 사용자에게 알려 준다.**"""
    assert _step().findable() is True
    assert _step(name="", automation_id="").findable() is False


def test_the_label_tells_a_person_what_it_was():
    """여기서 사람이 거른다. 못 읽으면 확인이 형식이 된다."""
    label = _step().label()

    assert "재고관리" in label
    assert "MenuItem" in label
    assert "가나병원" in label


# --- 키 입력은 안 담는다 --------------------------------------------------------


def test_only_navigation_keys_are_watched():
    """**비밀번호는 글자다.** 이동 키만 담으면 담길 수가 없다.

    마스터 요청(2026-08-12)으로 키 녹화를 열었지만 선은 그대로다 —
    `↑↓←→ · Tab · Enter · Esc · Alt · F1~F12` 뿐이고 글자·숫자는 없다.
    """
    for code in recorder.NAV_KEYS:
        assert not (0x30 <= code <= 0x5A), (
            f"0x{code:02X} 는 글자·숫자 키다 — 담는 순간 키로거가 된다"
        )
        assert code not in (0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xBF, 0xC0), (
            f"0x{code:02X} 는 기호 키다 — 비밀번호에 들어간다"
        )


def test_the_navigation_keys_cover_what_the_master_asked_for():
    """마스터가 원한 것 — 방향키로 세어서 메뉴를 고르는 것."""
    tokens = set(recorder.NAV_KEYS.values())

    for needed in ("{UP}", "{DOWN}", "{LEFT}", "{RIGHT}", "{ENTER}", "%"):
        assert needed in tokens


def test_no_hooking_library_sneaks_in():
    """전역 훅은 원리상 **내용**을 볼 수 있는 자리다. 우리는 상태만 본다."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "vader/core/recorder.py"
    ).read_text(encoding="utf-8")

    for banned in ("SetWindowsHookEx", "GetKeyboardState", "pynput", "pyHook", "ToUnicode"):
        assert banned not in source, f"{banned} 이 들어왔다 — 내용을 볼 수 있게 된다"


def test_a_recorded_step_has_no_free_text_field():
    """`Step` 에 자유 글자 칸이 생기면 다음 사람이 거기에 타이핑을 담는다.

    `keys` 는 예외지만 **`NAV_KEYS` 의 표기만** 들어온다.
    """
    for banned in ("text", "typed", "value", "chars"):
        assert banned not in Step.__annotations__


# --- 저장과 읽기 ----------------------------------------------------------------


def test_a_saved_recording_comes_back(isolated_settings):
    recorder.save("navigate", [_step(), _step(name="임의입고/반납관리")])

    got = recorder.load("navigate")

    assert [s.name for s in got] == ["재고관리", "임의입고/반납관리"]


def test_a_missing_recording_is_empty(isolated_settings):
    """녹화를 못 읽는 것이 실행을 막지 않는다."""
    assert recorder.load("없는것") == []


def test_a_broken_recording_is_empty(isolated_settings):
    path = recorder.path_for("깨진것")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{망가진", encoding="utf-8")

    assert recorder.load("깨진것") == []


def test_an_unknown_field_does_not_break_the_load(isolated_settings):
    """형식이 자라도 옛 녹화가 읽혀야 한다."""
    path = recorder.path_for("미래")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "format": "vader-recording/9",
            "steps": [{"at": "x", "name": "재고관리", "새필드": 1}],
        }),
        encoding="utf-8",
    )

    assert [s.name for s in recorder.load("미래")] == ["재고관리"]


def test_saved_recordings_are_listed(isolated_settings):
    recorder.save("가", [_step()])
    recorder.save("나", [_step()])

    assert recorder.recordings() == ["가", "나"]


# --- 재생 ---------------------------------------------------------------------


class _Window:
    """`child_window(...)` 로 찾히는 창 대역."""

    def __init__(self, *, finds=(), rect=(0, 0, 1000, 500)):
        self.finds = set(finds)
        self.clicked = []
        self._rect = rect
        self._asked = None

    def child_window(self, **kwargs):
        key = tuple(sorted(kwargs.items()))
        if key not in self.finds:
            raise LookupError(kwargs)
        self._asked = kwargs
        return self

    def click_input(self, coords=None):
        self.clicked.append(coords or self._asked)

    def rectangle(self):
        class R:
            left, top, right, bottom = self._rect

        return R()


def test_replay_prefers_the_automation_id():
    """`automation_id` 가 가장 안 흔들린다. 이름은 번역·버전으로 바뀐다."""
    window = _Window(finds=[(("auto_id", "btnStock"),)])

    assert recorder.replay(_step(automation_id="btnStock"), window) is True
    assert window.clicked == [{"auto_id": "btnStock"}]


def test_replay_falls_back_to_name_and_type():
    window = _Window(finds=[(("control_type", "MenuItem"), ("title", "재고관리"))])

    assert recorder.replay(_step(), window) is True
    assert window.clicked == [{"title": "재고관리", "control_type": "MenuItem"}]


def test_replay_falls_back_to_the_name_alone():
    """UIA 가 종류를 다르게 보고하는 기계가 있다."""
    window = _Window(finds=[(("title", "재고관리"),)])

    assert recorder.replay(_step(), window) is True


def test_coordinates_are_the_last_resort_and_they_warn():
    """좌표까지 내려갔다면 그건 **다음에 깨질 신호다.** 조용히 넘어가면 안 된다."""
    window = _Window(finds=[])
    said = []

    assert recorder.replay(
        _step(name="", automation_id=""),
        window,
        log=lambda level, message, **f: said.append((level, message)),
    ) is True
    assert window.clicked == [(300, 25)]
    assert any(level == "warning" and "깨집니다" in message for level, message in said)


def test_a_step_with_nothing_to_go_on_fails_loudly():
    window = _Window(finds=[])

    assert recorder.replay(
        _step(name="", automation_id="", rel_x=0, rel_y=0), window
    ) is False


# --- 펫이 읽어 준다 (마스터의 4번) ---------------------------------------------


def test_the_pet_reads_a_recording_as_sentences(isolated_settings):
    """녹화는 JSON 이다. **사용자가 그걸 열어 보게 하면 안 된다** —
    Phase 12 가 "사람이 읽는 작업 설명" 이라고 부른 자리다."""
    from vader.core import chat

    recorder.save("navigate", [_step(), _step(name="임의입고/반납관리")])

    said = chat._describe_recording("navigate")

    assert "2단계" in said
    assert "재고관리" in said
    assert "임의입고/반납관리" in said
    assert "{" not in said, "JSON 을 그대로 보여주면 안 된다"


def test_the_pet_warns_about_steps_that_will_break(isolated_settings):
    """좌표로만 찾는 단계는 창 크기가 바뀌는 날 깨진다. 미리 알아야 다시 녹화한다."""
    from vader.core import chat

    recorder.save("navigate", [_step(name="", automation_id="")])

    assert "좌표로만" in chat._describe_recording("navigate")


def test_the_pet_lists_what_is_there_when_asked_for_nothing(isolated_settings):
    from vader.core import chat

    recorder.save("가", [_step()])

    assert "가" in chat._describe_recording("")


def test_the_pet_says_plainly_when_a_recording_is_missing(isolated_settings):
    from vader.core import chat

    assert "없습니다" in chat._describe_recording("없는것")


# --- 탐침 자체 ----------------------------------------------------------------


def test_our_own_process_is_skipped_at_the_probe(monkeypatch):
    """녹화 창의 `중지` 버튼이 담기면 재생할 때 그것도 누른다.

    주입한 탐침으로만 검사하면 **진짜 탐침의 이 가지는 안 걸린다.**
    """
    pytest.importorskip("pywinauto")  # 진짜 탐침은 pywinauto 를 먼저 불러온다
    monkeypatch.setattr(recorder.ctypes.windll.user32, "WindowFromPoint", lambda p: 7)
    monkeypatch.setattr(recorder, "_pid_of", lambda hwnd: 4242)

    assert recorder.probe_at(1, 1, skip_pid=4242) is None


def test_coordinates_are_stored_as_a_ratio(monkeypatch):
    """픽셀로 담으면 창을 옮기거나 크기를 바꾼 날 엉뚱한 곳을 누른다."""
    class Rect:
        left, top, right, bottom = 100, 50, 1100, 550

    def get_rect(hwnd, ref):
        ref._obj.left, ref._obj.top = Rect.left, Rect.top
        ref._obj.right, ref._obj.bottom = Rect.right, Rect.bottom
        return True

    monkeypatch.setattr(recorder.ctypes.windll.user32, "GetWindowRect", get_rect)

    got = recorder._relative(1, 600, 300)

    assert got == {"rel_x": 0.5, "rel_y": 0.5}


def test_a_window_we_cannot_measure_gives_up_on_coordinates(monkeypatch):
    """잴 수 없는 창에 0.0 을 넣으면 재생이 창 왼쪽 위를 누른다."""
    monkeypatch.setattr(
        recorder.ctypes.windll.user32, "GetWindowRect", lambda hwnd, ref: False
    )

    assert recorder._relative(1, 600, 300) == {"rel_x": 0.0, "rel_y": 0.0}


# --- 코어의 기능이다 (마스터 결정 2026-08-11) -----------------------------------


def test_replay_is_a_context_capability_not_a_rune_chore():
    """마스터 결정 — 녹화 재생은 **코어의 기능**이다.

    > 녹화를 룬으로 굳히는 게 아닌 core 의 기능으로 남겨둬야해. 그래야 룬을
    > 만들 때 수월하게 만들 수 있잖아.

    룬이 녹화 파일을 직접 읽으면 룬마다 저장 위치·형식·창 찾기를 다시 짜야
    하고, 그 순간 녹화는 룬 하나의 사정이 된다.
    """
    from vader.sdk import RuneContext

    assert hasattr(RuneContext, "replay")


def test_no_rune_reads_the_recording_files_itself():
    """룬이 저장 위치를 알면 **코어가 형식을 못 바꾼다.**"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "rune-packages"
    for source in root.rglob("*.py"):
        if "__pycache__" in source.parts:
            continue
        text = source.read_text(encoding="utf-8")
        assert "recordings" not in text, f"{source.name} 이 녹화 저장 위치를 안다"
        assert "vader.core.recorder" not in text, f"{source.name} 이 코어를 import 한다"


def test_a_missing_recording_is_false_not_an_explosion(isolated_settings, tmp_path):
    """녹화가 없다는 것은 고장이 아니라 "아직 안 가르쳤다" 이다.

    던지면 룬이 다른 길로 갈 수 없다.
    """
    import threading
    from queue import Queue

    from vader.core.checkpoint import CheckpointStore
    from vader.core.context import JobContext
    from vader.core.job import Job
    from vader.sdk import CheckpointKey

    ctx = JobContext(
        job=Job(rune_id="r", inputs=[], workdir=tmp_path, id="j"),
        events=Queue(),
        cancel=threading.Event(),
        params={},
        checkpoints=CheckpointStore(tmp_path / "cp"),
        checkpoint_key=CheckpointKey(rune_id="r", input_identity="i"),
    )
    said = []
    ctx.log = lambda level, message, **fields: said.append((level, message))

    assert ctx.replay("없는녹화") is False
    # **"녹화가 없다" 와 "창을 못 찾았다" 는 사용자가 할 일이 다르다.**
    # 앞은 녹화하면 되고, 뒤는 창을 띄우면 된다.
    assert any("녹화가 없습니다" in message for _level, message in said)


def test_the_recording_knows_its_own_window(isolated_settings, tmp_path, monkeypatch):
    """룬이 창을 찾아 넘겨 줄 필요가 없다 — 녹화할 때 제목을 같이 적었다."""
    import threading
    from queue import Queue

    from vader.core import context as context_module
    from vader.core.checkpoint import CheckpointStore
    from vader.core.context import JobContext
    from vader.core.job import Job
    from vader.sdk import CheckpointKey

    recorder.save("navigate", [_step(), _step(name="임의입고/반납관리")])
    asked = []
    played = []
    monkeypatch.setattr(
        context_module, "_window_titled", lambda title: asked.append(title) or "창"
    )
    monkeypatch.setattr(
        recorder, "replay", lambda step, window, log=None: played.append(step.name) or True
    )

    ctx = JobContext(
        job=Job(rune_id="r", inputs=[], workdir=tmp_path, id="j"),
        events=Queue(),
        cancel=threading.Event(),
        params={},
        checkpoints=CheckpointStore(tmp_path / "cp"),
        checkpoint_key=CheckpointKey(rune_id="r", input_identity="i"),
    )

    assert ctx.replay("navigate") is True
    # 단계마다 다시 찾는다 — 둘 다 창 제목이 있으므로 두 번 묻는다.
    assert asked == ["이영돈 [] 가나병원", "이영돈 [] 가나병원"]
    assert played == ["재고관리", "임의입고/반납관리"]


def test_one_failed_step_fails_the_whole_replay(isolated_settings, tmp_path, monkeypatch):
    """절반만 눌린 메뉴는 아무것도 안 누른 것보다 나쁘다 — 화면이 어디에
    있는지 아무도 모른다."""
    import threading
    from queue import Queue

    from vader.core import context as context_module
    from vader.core.checkpoint import CheckpointStore
    from vader.core.context import JobContext
    from vader.core.job import Job
    from vader.sdk import CheckpointKey

    recorder.save("navigate", [_step(), _step(name="둘째")])
    monkeypatch.setattr(context_module, "_window_titled", lambda title: "창")
    monkeypatch.setattr(
        recorder, "replay", lambda step, window, log=None: step.name != "둘째"
    )

    ctx = JobContext(
        job=Job(rune_id="r", inputs=[], workdir=tmp_path, id="j"),
        events=Queue(),
        cancel=threading.Event(),
        params={},
        checkpoints=CheckpointStore(tmp_path / "cp"),
        checkpoint_key=CheckpointKey(rune_id="r", input_identity="i"),
    )

    assert ctx.replay("navigate") is False


def test_a_window_that_is_not_up_is_said_plainly(isolated_settings, tmp_path, monkeypatch):
    """녹화는 있는데 창이 없다. **녹화가 없는 것과 다른 말이어야 한다** —
    사용자가 할 일이 다르다."""
    import threading
    from queue import Queue

    from vader.core import context as context_module
    from vader.core.checkpoint import CheckpointStore
    from vader.core.context import JobContext
    from vader.core.job import Job
    from vader.sdk import CheckpointKey

    recorder.save("navigate", [_step()])
    monkeypatch.setattr(context_module, "_window_titled", lambda title: None)
    played = []
    monkeypatch.setattr(
        recorder, "replay", lambda step, window, log=None: played.append(step) or True
    )

    ctx = JobContext(
        job=Job(rune_id="r", inputs=[], workdir=tmp_path, id="j"),
        events=Queue(),
        cancel=threading.Event(),
        params={},
        checkpoints=CheckpointStore(tmp_path / "cp"),
        checkpoint_key=CheckpointKey(rune_id="r", input_identity="i"),
    )
    said = []
    ctx.log = lambda level, message, **fields: said.append((level, message))

    assert ctx.replay("navigate") is False
    assert played == [], "창도 없는데 누르면 안 된다"
    assert any("한 단계도 재생하지 못했습니다" in message for _level, message in said)
    assert not any("녹화가 없습니다" in message for _level, message in said), (
        "녹화는 있다. 없는 것은 창이다 — 사용자가 할 일이 다르다"
    )


# --- 창 제목의 변하는 부분 (2026-08-11) ----------------------------------------


def test_the_boot_time_is_stripped_from_the_window_title():
    """녹화한 제목에는 **기동 시각**이 들어 있다.

    통째로 찾으면 이온엠을 다시 켠 순간 영원히 못 찾는다. 실제로 그래서
    마스터의 녹화가 적용이 안 됐다.
    """
    from vader.core.context import stable_part

    recorded = (
        "이영돈 []   Version=1.0.0.6 - 의료법인) 가나병원 "
        "(10000001) Version = 20260211 오전 11:15:31"
    )

    needle = stable_part(recorded)

    assert "11:15:31" not in needle
    assert "가나병원" in needle


def test_morning_and_afternoon_are_stripped_too():
    """아침에 녹화하고 오후에 돌리면 그 한 낱말이 달라진다.

    시각을 잘라 놓고 이걸 남기면 **반나절짜리 폭탄**이 된다.
    """
    from vader.core.context import stable_part

    morning = stable_part("가나병원 (10000001) 오전 11:15:31")
    afternoon = stable_part("가나병원 (10000001) 오후 3:02:10")

    assert morning == afternoon


def test_a_recording_finds_eonm_built_on_another_day():
    """이온엠 제목의 `Version = 20260211` 은 빌드 날짜라 PC 마다 다르다 (시험 PC 2026-10-28)."""
    from vader.core.context import stable_part

    recorded = stable_part("Version=1.0.0.6 - 의료법인) 가나병원 (10000001) Version = 20260211")
    live = "박철수 []   Version=1.0.0.6 - 의료법인) 가나병원 (10000001) Version = 20260306 오후 4:17:08"

    assert recorded in live
    assert "10000001" in recorded


def test_a_recording_that_moves_between_windows_follows_it(
    isolated_settings, tmp_path, monkeypatch
):
    """메뉴를 누르면 새 창이 열리고 **다음 단계는 거기 있다.**

    첫 창에 전부 넣으려 하면 뒤가 통째로 안 눌린다 — 마스터 녹화가 정확히
    그 모양이었다(본 창 → 드롭다운 → 입고 화면 → 목록 창).
    """
    import threading
    from queue import Queue

    from vader.core import context as context_module
    from vader.core.checkpoint import CheckpointStore
    from vader.core.context import JobContext
    from vader.core.job import Job
    from vader.sdk import CheckpointKey

    recorder.save(
        "navigate",
        [
            _step(name="재고관리", window_title="본 창"),
            _step(name="임의입고/반납관리", window_title=""),
            _step(name="7001.가부서", window_title="입고 화면"),
        ],
    )
    monkeypatch.setattr(
        context_module, "_window_titled", lambda title: f"창:{title}"
    )
    played = []
    monkeypatch.setattr(
        recorder,
        "replay",
        lambda step, window, log=None: played.append((step.name, window)) or True,
    )

    ctx = JobContext(
        job=Job(rune_id="r", inputs=[], workdir=tmp_path, id="j"),
        events=Queue(),
        cancel=threading.Event(),
        params={},
        checkpoints=CheckpointStore(tmp_path / "cp"),
        checkpoint_key=CheckpointKey(rune_id="r", input_identity="i"),
    )

    assert ctx.replay("navigate") is True
    assert played == [
        ("재고관리", "창:본 창"),
        # 창 제목이 없는 단계는 **직전 창에 그대로 넣는다** — 드롭다운은
        # 자기 제목이 없다.
        ("임의입고/반납관리", "창:본 창"),
        ("7001.가부서", "창:입고 화면"),
    ]


# --- 대화창 안에서 고른다 (마스터 2026-08-12) ----------------------------------


def test_the_step_list_is_rendered_by_the_screen():
    """모델에게 맡겼더니 "이대로 저장하시겠어요?" 한 줄로 요약했다.

    사람이 확인해야 할 자리에서 확인할 것이 사라졌다.
    """
    from vader.core.view import recording_lines

    text = recording_lines([_step(), _step(name="임의입고/반납관리")])

    assert "2단계" in text
    assert "재고관리" in text
    assert "임의입고/반납관리" in text


def test_a_fragile_step_is_marked_in_the_list():
    from vader.core.view import recording_lines

    assert "좌표로만" in recording_lines([_step(name="", automation_id="")])


def test_the_choices_are_save_drop_or_discard():
    """마스터 요청 — 팝업 대신 **대화창 안에서 사지선답**으로."""
    from vader.core.view import recording_choices

    got = recording_choices([_step(), _step()])

    assert got[0] == "이대로 저장"
    assert "1번 빼기" in got and "2번 빼기" in got
    assert got[-1] == "버리기"


def test_a_long_recording_does_not_overflow_the_button_row():
    """다 그리면 버튼 줄이 넘친다."""
    from vader.core.view import MAX_DROP_BUTTONS, recording_choices

    got = recording_choices([_step() for _ in range(20)])

    assert len([g for g in got if g.endswith("번 빼기")]) == MAX_DROP_BUTTONS


def test_nothing_caught_offers_a_retry():
    """빈 목록에 `저장` 을 내밀면 사용자가 그걸 누른다."""
    from vader.core.view import recording_choices

    assert recording_choices([]) == ["다시 녹화", "그만"]


def test_the_chat_window_no_longer_opens_a_popup_to_record():
    """마스터 지적 — 창이 하나 더 뜨면 그때부터 사용자는 **두 곳을 본다.**"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert not (root / "vader/core/record_ui.py").exists()
    text = (root / "vader/core/chat_ui.py").read_text(encoding="utf-8")
    assert "RecordDialog" not in text
    assert "Toplevel" in text, "이름·말투 창은 그대로다 — 녹화만 안에서 한다"


def test_two_windows_with_the_same_title_are_refused(monkeypatch):
    """제목 부분 일치라 동명 창이 있으면 **엉뚱한 세션에 입력한다** (codex).

    이 프로젝트가 이미 두 번 만난 실패다. 최소화된 창은 `_top_windows` 가 이미 뺀다.
    """
    import sys

    from vader.core import context as context_module

    class App:
        def __init__(self, backend=None):
            pass

        def connect(self, handle, timeout=None):
            self.handle = handle
            return self

        def window(self, handle):
            return ("창", handle)

    monkeypatch.setitem(sys.modules, "pywinauto.application", type("M", (), {"Application": App}))
    title = "이영돈 []   Version=1.0.0.6 - 의료법인) 가나병원 (10000001)"

    def seen(*windows):
        monkeypatch.setattr(context_module, "_top_windows", lambda: list(windows))

    seen((1, title), (2, title))
    assert context_module._window_titled("가나병원") is context_module.AMBIGUOUS_WINDOW
    seen()
    assert context_module._window_titled("가나병원") is None
    seen((7, "카카오톡"), (3, title))
    assert context_module._window_titled("가나병원") == ("창", 3)


def test_top_windows_reads_the_real_desktop():
    """Win32 로 직접 본다 — 앱 안 작업 스레드에서 UIA 목록은 이온엠 창을 못 봤다 (2026-11-07)."""
    import threading

    from vader.core.context import _top_windows

    seen = []
    worker = threading.Thread(target=lambda: seen.extend(_top_windows()))
    worker.start()
    worker.join(10)
    assert seen and all(isinstance(h, int) and text for h, text in seen)


def test_a_failed_lookup_says_why_before_falling_to_coordinates():
    """마스터 로그에 "좌표로 눌렀습니다" 만 있고 **이유가 없었다.**

    왜 이름으로 못 찾았는지 모르면 다음에 무엇을 고칠지도 모른다.
    """
    window = _Window(finds=[])
    said = []

    recorder.replay(
        _step(),
        window,
        log=lambda level, message, **fields: said.append((message, fields)),
    )

    _message, fields = next((m, f) for m, f in said if "좌표로" in m)
    assert fields["tried"], "무엇을 어떻게 찾아봤는지 남아야 한다"
    assert any("name+type" in row for row in fields["tried"])


def test_the_open_screen_name_is_stripped_from_the_title():
    """제목의 `[…]` 안에 **지금 열린 화면 이름**이 들어간다.

        녹화 때   이영돈 []   Version=1.0.0.6 - …
        실행 때   이영돈 [고정자산]   Version=1.0.0.6 - …

    그래서 녹화한 창을 영영 못 찾았다 (마스터 로그 2026-08-12).
    """
    from vader.core.context import stable_part

    needle = stable_part(
        "이영돈 []   Version=1.0.0.6 - 의료법인) 가나병원 (10000001) "
        "Version = 20260211 오전 11:15:31"
    )

    assert "이영돈" not in needle or "[" not in needle
    assert needle in (
        "이영돈 [고정자산]   Version=1.0.0.6 - 의료법인) 가나병원 (10000001) "
        "Version = 20260211 오전 9:02:11"
    )


def test_replay_gets_something_that_can_search_its_children(monkeypatch):
    """찾은 창을 **`WindowSpecification`** 으로 준다 — `child_window` 는 그것에만 있다.

    UIAWrapper 를 넘겼더니 재생이 매번 이름 찾기에 실패하고 좌표로 떨어졌다 (마스터 로그 2026-08-12).
    """
    import sys

    from vader.core import context as context_module

    class Spec:
        def child_window(self, **_kwargs):
            return self

    class App:
        def __init__(self, backend=None):
            pass

        def connect(self, **kwargs):
            assert kwargs["handle"] == 42
            return self

        def window(self, **_k):
            return Spec()

    monkeypatch.setattr(context_module, "_top_windows", lambda: [(42, "이영돈 [] 가나병원")])
    monkeypatch.setitem(sys.modules, "pywinauto.application", type("M", (), {"Application": App}))

    got = context_module._window_titled("가나병원")

    assert hasattr(got, "child_window"), "자식을 찾을 수 있는 것이어야 한다"


# --- 키도 녹화한다 (마스터 2026-08-12) -----------------------------------------


def test_a_navigation_key_becomes_a_step(monkeypatch):
    """마스터 요청 — 키 순서를 설정에 빼면 **시연의 어느 지점에 끼는지**를
    표현할 방법이 없다."""
    made = Recorder(probe=_feeding())
    monkeypatch.setattr(recorder, "_title_of", lambda hwnd: "이온엠 본 창")
    monkeypatch.setattr(recorder.ctypes.windll.user32, "GetForegroundWindow", lambda: 1)

    made.capture_key("{DOWN}")

    assert [s.keys for s in made.steps] == ["{DOWN}"]
    assert made.steps[0].window_title == "이온엠 본 창"


def test_the_same_key_pressed_again_is_folded_into_a_count(monkeypatch):
    """`↓` 를 여덟 번 누른 것은 `{DOWN 8}` 한 줄이어야 한다.

    여덟 줄이면 확인 화면을 사람이 못 읽는다.
    """
    made = Recorder(probe=_feeding())
    monkeypatch.setattr(recorder, "_title_of", lambda hwnd: "창")
    monkeypatch.setattr(recorder.ctypes.windll.user32, "GetForegroundWindow", lambda: 1)

    for _ in range(8):
        made.capture_key("{DOWN}")

    assert [s.keys for s in made.steps] == ["{DOWN 8}"]


def test_a_different_key_starts_a_new_step(monkeypatch):
    made = Recorder(probe=_feeding())
    monkeypatch.setattr(recorder, "_title_of", lambda hwnd: "창")
    monkeypatch.setattr(recorder.ctypes.windll.user32, "GetForegroundWindow", lambda: 1)

    made.capture_key("{RIGHT}")
    made.capture_key("{RIGHT}")
    made.capture_key("{DOWN}")
    made.capture_key("{ENTER}")

    assert [s.keys for s in made.steps] == ["{RIGHT 2}", "{DOWN}", "{ENTER}"]


def test_a_key_step_reads_plainly_in_the_list(monkeypatch):
    """사람이 확인 화면에서 읽고 거른다."""
    assert "키 {DOWN 8}" in _step(name="", keys="{DOWN 8}").label()


def test_a_key_step_is_never_fragile():
    """좌표도 UIA 도 안 쓴다. 창이 움직여도 같다."""
    assert _step(name="", automation_id="", keys="{DOWN}").findable() is True


def test_replaying_a_key_step_sends_it_to_the_focused_window(monkeypatch):
    sent = []

    class Window:
        def set_focus(self):
            sent.append("focus")

    import sys

    monkeypatch.setitem(
        sys.modules,
        "pywinauto.keyboard",
        type("M", (), {"send_keys": lambda value, **k: sent.append(value)}),
    )

    assert recorder.replay(_step(name="", keys="{DOWN 8}"), Window()) is True
    assert sent == ["focus", "{DOWN 8}"]


# --- 연달아 녹화하기 (마스터 2026-08-12) ---------------------------------------


class _FakeTop:
    """`after` 를 손으로 돌리는 tkinter 대역. 폴링 사슬을 셀 수 있다."""

    def __init__(self):
        self.pending = []

    def after(self, _ms, func):
        self.pending.append(func)

    def after_idle(self, func):
        self.pending.append(func)

    def winfo_exists(self):
        return True

    def tick(self):
        """지금 걸린 것만 한 번 돌린다. 새로 걸린 것은 다음 차례."""
        due, self.pending = self.pending, []
        for func in due:
            func()
        return len(due)


class _FakeLabel:
    def __init__(self):
        self.text = ""

    def config(self, text=""):
        self.text = text


def _window():
    """`ChatWindow` 의 녹화 부분만 떼어 쓴다. tkinter 없이 돌린다."""
    from vader.core.chat_ui import ChatWindow

    made = ChatWindow.__new__(ChatWindow)
    made.top = _FakeTop()
    made.status = _FakeLabel()
    made._recorder_obj = None
    made._take = 0
    made.session = type("S", (), {"_recorder": None})()
    made.choices_drawn = []
    made._ask_choice = lambda options: made.choices_drawn.append(
        [label for label, _action in options]
    )
    made._clear_choices = lambda: None
    made._append = lambda who, text, tag: None
    return made


class _Rec:
    """실제 흐름처럼 **이미 켜진 채로** 넘어온다 (`_record` 가 `start()` 를 부른다)."""

    def __init__(self):
        self.steps = []
        self.running = True

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


def test_three_recordings_leave_only_one_polling_chain():
    """**연달아 두세 번 하면 적용이 안 됐다** (마스터).

    폴링 사슬이 회차마다 하나씩 늘고, 사슬마다 0.5초에 한 번 버튼을 지우고
    다시 만들었다. 누르는 순간 사라진 버튼은 클릭이 안 먹는다.
    """
    window = _window()

    for _ in range(3):
        window._recording(_Rec())
        window.top.tick()  # 각 회차의 첫 폴링

    # 세 사슬이 살아 있으면 여기서 3이 나온다.
    assert window.top.tick() == 1, "사슬은 늘 하나여야 한다"


def test_the_count_does_not_redraw_the_buttons():
    """개수를 버튼 글자에 쓰면 매 틱마다 버튼을 다시 만들어야 한다."""
    window = _window()
    made = _Rec()
    window._recording(made)
    drawn = len(window.choices_drawn)

    made.steps.append(_step())
    for _ in range(5):
        window.top.tick()

    assert len(window.choices_drawn) == drawn, "폴링이 버튼을 다시 그리면 안 된다"
    assert "1단계" in window.status.text


def test_starting_again_stops_the_previous_recorder():
    """안 멈추면 옛 녹화기의 스레드가 계속 담고, 둘이 서로 다른 것을 저장한다."""
    window = _window()
    first = _Rec()
    window._recording(first)

    window.open_recorder(_Rec())
    window.top.tick()

    assert first.running is False


def test_saving_nothing_says_so_instead_of_going_quiet(isolated_settings):
    """눌렀는데 **아무 일도 안 일어나는 것**이 "적용이 안 된다" 로 보인다."""
    window = _window()
    said = []
    window._append = lambda who, text, tag: said.append(text)
    window._recording(_Rec())

    window._save_recording()

    assert any("저장할 녹화가 없습니다" in text for text in said)


def test_the_panel_hands_ownership_back_after_saving(isolated_settings, monkeypatch):
    """화면은 녹화기를 **빌릴 뿐**이고 회차 상태는 도구가 소유한다 (codex).

    안 알리면 펫이 나중에 같은 것을 또 저장하려 들거나, "저장할 녹화가
    없습니다" 를 실패처럼 말한다.
    """
    from vader.core.record_tool import RecordTool

    window = _window()
    tool = RecordTool(ask=lambda title, body: True)

    class Fake(_Rec):
        pass

    monkeypatch.setattr(recorder, "Recorder", Fake)
    tool.invoke("start")
    made = tool.current_recorder()
    made.steps.append(_step())
    window.session = type("S", (), {"_recorder": tool})()
    window._recording(made)

    window._save_recording()

    assert tool.current_recorder() is None, "도구가 회차를 놓아야 한다"
    assert "이미" in tool.invoke("save", "navigation")


# --- 컨트롤 안에서 어디를 눌렀나 (마스터 2026-08-12) ---------------------------


class _Control:
    def __init__(self, *, rect=(0, 0, 200, 24)):
        self._rect = rect
        self.clicked = []

    def rectangle(self):
        class R:
            left, top, right, bottom = self._rect

        return R()

    def click_input(self, coords=None):
        self.clicked.append(coords)


def test_the_arrow_is_clicked_not_the_text_box():
    """마스터 보고 — `[1002.구매과   |▼]` 에서 **▼ 를 눌러야 목록이 열린다.**

    정체로 찾은 뒤 가운데를 누르면 글자칸이 눌리고 아무 일도 안 난다.
    """
    control = _Control(rect=(0, 0, 200, 24))

    recorder._click_inside(control, _step(ctrl_x=0.95, ctrl_y=0.5))

    assert control.clicked == [(190, 12)], "오른쪽 끝 ▼ 를 눌러야 한다"


def test_an_old_recording_still_clicks_the_middle():
    """옛 녹화에는 이 값이 없다. 전과 같이 동작해야 한다."""
    control = _Control()

    recorder._click_inside(control, _step())

    assert control.clicked == [None], "가운데 클릭은 인자 없이 부른다"


def test_the_click_position_survives_a_resized_control():
    """창이 커져도 **컨트롤 안 비율은 같다.**"""
    wide = _Control(rect=(0, 0, 400, 30))

    recorder._click_inside(wide, _step(ctrl_x=0.95, ctrl_y=0.5))

    assert wide.clicked == [(380, 15)]


def test_where_inside_the_control_is_recorded():
    """녹화가 그걸 안 담으면 재생이 알 도리가 없다."""
    class Info:
        class rectangle:
            left, top, right, bottom = 100, 50, 300, 74

    got = recorder._inside(Info(), 290, 62)

    assert got["ctrl_x"] > 0.9, "오른쪽 끝을 눌렀다"
    assert 0.4 < got["ctrl_y"] < 0.6


def test_an_unmeasurable_control_falls_back_to_the_middle():
    class Info:
        @property
        def rectangle(self):
            raise RuntimeError("못 잰다")

    assert recorder._inside(Info(), 1, 1) == {}


def test_replay_uses_the_recorded_spot_not_the_middle():
    """정체로 **찾는 것**과 **어디를 누르는지**는 다른 문제다.

    `_click_inside` 만 검사하면 재생이 그걸 안 부르는 것을 못 잡는다.
    """
    control = _Control(rect=(0, 0, 200, 24))

    class Window:
        def child_window(self, **_kwargs):
            return control

    assert recorder.replay(_step(automation_id="cmb", ctrl_x=0.95), Window()) is True
    assert control.clicked == [(190, 12)], "가운데가 아니라 녹화된 자리"


def test_probe_carries_the_spot_into_the_step(monkeypatch):
    """`_inside` 를 만들어 놓고 `probe_at` 이 안 쓰면 녹화에 안 담긴다."""
    import sys

    class Info:
        control_type = "Pane"
        name = "1002.가부서"
        automation_id = "cmbIpGoUserDept"
        class_name = "WindowsForms10"

        class rectangle:
            left, top, right, bottom = 0, 0, 200, 24

    monkeypatch.setitem(
        sys.modules,
        "pywinauto.uia_defines",
        type("M", (), {"IUIA": lambda: type("I", (), {"iuia": type("U", (), {
            "ElementFromPoint": staticmethod(lambda p: None)})()})()}),
    )
    monkeypatch.setitem(
        sys.modules,
        "pywinauto.uia_element_info",
        type("M", (), {"UIAElementInfo": lambda _el: Info()}),
    )
    monkeypatch.setattr(recorder.ctypes.windll.user32, "WindowFromPoint", lambda p: 1)
    monkeypatch.setattr(recorder.ctypes.windll.user32, "GetAncestor", lambda h, f: 1)
    monkeypatch.setattr(recorder, "_title_of", lambda hwnd: "창")
    monkeypatch.setattr(recorder, "_relative", lambda hwnd, x, y: {"rel_x": 0.0, "rel_y": 0.0})

    step = recorder.probe_at(190, 12)

    assert step.ctrl_x > 0.9, "오른쪽 끝을 눌렀다는 것이 담겨야 한다"


def test_replay_stops_when_the_caller_says_it_got_what_it_came_for(
    isolated_settings, tmp_path, monkeypatch
):
    """**녹화는 부르는 쪽이 원하는 것보다 길다.**

    마스터의 2026-08-19 녹화는 메뉴로 입고 화면을 연 뒤에도 이어진다 — 부서를
    고르고, 품목창을 누르고, `{ENTER}` 와 `{TAB 6}` 까지. 그 꼬리는 **룬이 자기
    코드로 다시 하는 일**이라, 통째로 재생하면 같은 키가 두 번 들어간다.
    두 번째 `{TAB 6}` 이 출발점을 옮겨 날짜 칸에 설 것이 단가 칸에 섰다.

    이 테스트가 빨개지면 그 버그가 돌아온 것이다.
    """
    import threading
    from queue import Queue

    from vader.core import context as context_module
    from vader.core.checkpoint import CheckpointStore
    from vader.core.context import JobContext
    from vader.core.job import Job
    from vader.sdk import CheckpointKey

    # 마스터 녹화의 모양 그대로: 메뉴 셋 → 그 뒤로 룬이 다시 하는 일 셋.
    recorder.save(
        "navigate",
        [
            _step(name="재고관리"),
            _step(name="", control_type="", keys="{UP 2}"),
            _step(name="", control_type="", keys="{ENTER}"),
            _step(name="", control_type="Pane", automation_id="cmbIpGoUserDept"),
            _step(name="부서항목", control_type="ListItem"),
            _step(name="", control_type="Edit", automation_id="m_pTextBox"),
            _step(name="", control_type="", keys="{ENTER}"),
            _step(name="", control_type="", keys="{TAB 6}"),
        ],
    )
    played = []
    monkeypatch.setattr(context_module, "_window_titled", lambda title: "창")
    monkeypatch.setattr(
        recorder,
        "replay",
        lambda step, window, log=None: played.append(step.keys or step.automation_id or step.name)
        or True,
    )

    ctx = JobContext(
        job=Job(rune_id="r", inputs=[], workdir=tmp_path, id="j"),
        events=Queue(),
        cancel=threading.Event(),
        params={},
        checkpoints=CheckpointStore(tmp_path / "cp"),
        checkpoint_key=CheckpointKey(rune_id="r", input_identity="i"),
    )

    # **경계는 품목 검색창이다.** 부르는 쪽이 자기 셀렉터로 알아본다 —
    # 번호를 세지 않는다. 다시 녹화하면 번호는 바뀌지만 셀렉터는 안 바뀐다.
    assert ctx.replay("navigate", stop=lambda step: step.automation_id == "m_pTextBox") is True

    # 부서 두 단계까지 재생된다. 입고 종류의 부서는 그 뒤에 룬이 다시 고른다 (rune.KIND_DEPT).
    assert played == ["재고관리", "{UP 2}", "{ENTER}", "cmbIpGoUserDept", "부서항목"]
    assert "m_pTextBox" not in played, "품목창부터는 룬이 누른다"
    assert played.count("{TAB 6}") == 0, "탭은 룬이 보낸다. 두 번 보내면 출발점이 밀린다"


def test_replay_without_a_stop_condition_still_plays_everything(
    isolated_settings, tmp_path, monkeypatch
):
    """조건을 안 주면 지금까지 그대로다. 다른 녹화를 조용히 자르면 안 된다."""
    import threading
    from queue import Queue

    from vader.core import context as context_module
    from vader.core.checkpoint import CheckpointStore
    from vader.core.context import JobContext
    from vader.core.job import Job
    from vader.sdk import CheckpointKey

    recorder.save("navigate", [_step(name="하나"), _step(name="둘"), _step(name="셋")])
    played = []
    monkeypatch.setattr(context_module, "_window_titled", lambda title: "창")
    monkeypatch.setattr(
        recorder, "replay", lambda step, window, log=None: played.append(step.name) or True
    )

    ctx = JobContext(
        job=Job(rune_id="r", inputs=[], workdir=tmp_path, id="j"),
        events=Queue(),
        cancel=threading.Event(),
        params={},
        checkpoints=CheckpointStore(tmp_path / "cp"),
        checkpoint_key=CheckpointKey(rune_id="r", input_identity="i"),
    )

    assert ctx.replay("navigate") is True
    assert played == ["하나", "둘", "셋"]


def _ctx_for(tmp_path):
    import threading
    from queue import Queue

    from vader.core.checkpoint import CheckpointStore
    from vader.core.context import JobContext
    from vader.core.job import Job
    from vader.sdk import CheckpointKey

    return JobContext(
        job=Job(rune_id="r", inputs=[], workdir=tmp_path, id="j"),
        events=Queue(),
        cancel=threading.Event(),
        params={},
        checkpoints=CheckpointStore(tmp_path / "cp"),
        checkpoint_key=CheckpointKey(rune_id="r", input_identity="i"),
    )


def test_the_prep_the_rune_already_did_is_skipped_not_fatal(
    isolated_settings, tmp_path, monkeypatch
):
    """**마스터는 로그인 창부터 녹화한다.** 룬은 그걸 이미 했다.

    2026-08-19 녹화가 그 모양이다 — 바탕화면 클릭, `Form1` 의 `{TAB}`,
    `{ENTER}`(로그인), 그다음이 메뉴다. 룬은 `login()` 으로 로그인한 **뒤에**
    재생을 부르므로 `Form1` 은 사라진 뒤고, 예전 규칙이면 첫 단계에서 통째로
    실패했다.

    창을 못 찾으면 키를 **아무 데도 안 보낸다.** 그래서 지나갈 수 있다.
    """
    from vader.core import context as context_module

    recorder.save(
        "navigate",
        [
            _step(window_title="Form1", name="", control_type="", keys="{TAB}"),
            _step(window_title="Form1", name="", control_type="", keys="{ENTER}"),
            _step(name="재고관리"),
            _step(name="", control_type="", keys="{UP 2}"),
        ],
    )
    played = []
    said = []
    # `Form1` 은 이미 닫혔다. 본 창만 있다.
    monkeypatch.setattr(
        context_module,
        "_window_titled",
        lambda title: None if "Form1" in title else "창",
    )
    monkeypatch.setattr(
        recorder,
        "replay",
        lambda step, window, log=None: played.append(step.keys or step.name) or True,
    )

    ctx = _ctx_for(tmp_path)
    ctx.log = lambda level, message, **fields: said.append((level, message, fields))

    assert ctx.replay("navigate") is True
    assert played == ["재고관리", "{UP 2}"], "로그인 부분은 지나가고 메뉴부터 누른다"
    # **조용히 지나가면 안 된다.** 녹화가 통째로 헛돌아도 모르게 된다.
    skipped = [f for _l, m, f in said if "건너뛰었습니다" in m]
    assert skipped and len(skipped[0]["skipped"]) == 2


def test_a_failure_after_we_have_started_is_still_fatal(
    isolated_settings, tmp_path, monkeypatch
):
    """앞쪽을 무르는 것이지 규칙을 버리는 것이 아니다.

    **절반만 눌린 메뉴는 아무것도 안 누른 것보다 나쁘다** — 화면이 어디에
    있는지 아무도 모른다. 한 단계라도 성공한 뒤에는 그대로다.
    """
    from vader.core import context as context_module

    recorder.save(
        "navigate",
        [_step(name="첫째"), _step(name="둘째"), _step(name="셋째")],
    )
    played = []
    monkeypatch.setattr(context_module, "_window_titled", lambda title: "창")
    monkeypatch.setattr(
        recorder,
        "replay",
        lambda step, window, log=None: (played.append(step.name), step.name != "둘째")[1],
    )

    assert _ctx_for(tmp_path).replay("navigate") is False
    assert played == ["첫째", "둘째"], "둘째에서 멈춘다. 셋째는 안 누른다"


def test_a_recording_where_nothing_at_all_can_be_replayed_fails(
    isolated_settings, tmp_path, monkeypatch
):
    """전부 건너뛰고 성공했다고 하면, 화면은 그대로인데 다음 단계가 돈다."""
    from vader.core import context as context_module

    recorder.save("navigate", [_step(window_title="Form1"), _step(window_title="Form1")])
    monkeypatch.setattr(context_module, "_window_titled", lambda title: None)
    monkeypatch.setattr(recorder, "replay", lambda step, window, log=None: True)

    assert _ctx_for(tmp_path).replay("navigate") is False


def test_a_recording_without_the_handover_point_is_refused_outright(
    isolated_settings, tmp_path, monkeypatch
):
    """**모르면 안 누른다** (codex 지적 2026-08-19).

    부르는 쪽이 "여기부터 내 일" 이라고 표시한 자리를 녹화에서 못 찾았다면,
    이 녹화가 어디서 끝나는지 우리가 모른다는 뜻이다. 모르는 채로 끝까지
    누르면 업무 키가 두 번 들어간다 — 탭이 밀린 이유가 그것이었다.
    """
    from vader.core import context as context_module

    recorder.save("navigate", [_step(name="재고관리"), _step(name="", keys="{TAB 6}")])
    played = []
    monkeypatch.setattr(context_module, "_window_titled", lambda title: "창")
    monkeypatch.setattr(
        recorder, "replay", lambda step, window, log=None: played.append(step) or True
    )

    ctx = _ctx_for(tmp_path)
    said = []
    ctx.log = lambda level, message, **fields: said.append((level, message))

    assert ctx.replay("navigate", stop=lambda step: step.automation_id == "m_pTextBox") is False
    assert played == [], "인계 지점을 모르면 한 단계도 안 누른다"
    assert any("다시 녹화" in message for _level, message in said), "할 일을 말해 준다"


def test_a_control_we_could_not_press_is_fatal_even_before_the_first_success(
    isolated_settings, tmp_path, monkeypatch
):
    """**창을 찾았다는 것은 이미 눌러 봤다는 뜻이다** (codex 지적).

    앞쪽 관용은 "창이 아예 없다" 에만 준다. 창은 있는데 컨트롤을 못 눌렀다면
    그건 지나간 준비 동작이 아니라 **지금 실패한 조작**이고, 넘기면 메뉴가
    반쯤 열린 채로 다음 키가 나간다.
    """
    from vader.core import context as context_module

    recorder.save("navigate", [_step(name="재고관리"), _step(name="", keys="{UP 2}")])
    played = []
    monkeypatch.setattr(context_module, "_window_titled", lambda title: "창")
    monkeypatch.setattr(
        recorder,
        "replay",
        lambda step, window, log=None: (played.append(step.name), False)[1],
    )

    assert _ctx_for(tmp_path).replay("navigate") is False
    assert played == ["재고관리"], "첫 단계에서 멈춘다. 다음 키는 안 나간다"


class _Step5:
    """`label()`·`findable()` 만 있으면 목록을 그릴 수 있다."""

    def __init__(self, name):
        self.name = name

    def label(self):
        return self.name

    def findable(self):
        return True


def test_a_step_past_the_fourth_can_be_dropped_by_typing_its_number():
    """**버튼은 넷까지다.** 마스터가 15번을 빼려다 거절당했다 (2026-08-19).

        나:  15번 빼줘
        펫:  그건 제가 알 수 없습니다. 저는 이 컴퓨터의 업무 자동화만…

    녹화를 다듬지 못하면 못 쓸 녹화가 그대로 저장되고, 그러면 재생이 통째로
    죽는다. 검토 중에는 **모델을 안 거친다** — 버튼과 같은 규칙이다.
    """
    window = _window()
    made = _Rec()
    made.steps = [_Step5(f"{n}단계") for n in range(1, 16)]
    window._recorder_obj = made
    said = []
    window._append = lambda who, text, tag: said.append(text)

    window._recorded()
    assert window._reviewing is True
    # 다섯 번째부터는 버튼이 없다. 그 사실을 화면이 말해 준다.
    assert any("번호를 쳐서" in text for text in said)

    window._drop_step(14)

    assert len(made.steps) == 14
    assert all(step.name != "15단계" for step in made.steps)


def test_typing_a_number_out_of_range_is_said_not_swallowed():
    """조용히 무시하면 뺐다고 믿은 채로 저장한다."""
    window = _window()
    made = _Rec()
    made.steps = [_Step5("1단계"), _Step5("2단계")]
    window._recorder_obj = made
    said = []
    window._append = lambda who, text, tag: said.append(text)

    window._drop_step(40)

    assert len(made.steps) == 2, "아무것도 안 뺀다"
    assert any("없습니다" in text for text in said)


class _FakeEntry:
    def __init__(self, text):
        self.text = text

    def get(self):
        return self.text

    def delete(self, *_args):
        self.text = ""


def _typing(text, *, reviewing=True):
    """`_send` 만 떼어 돌린다. 모델로 갔는지 대신 눌렀는지를 본다."""
    from vader.core.chat_ui import ChatWindow

    window = ChatWindow.__new__(ChatWindow)
    window.entry = _FakeEntry(text)
    window._busy = False
    window._reviewing = reviewing
    window._append = lambda who, body, tag: None
    dropped = []
    window._drop_step = dropped.append
    asked = []
    window.pet = type("P", (), {"created": True})()
    window._set_busy = lambda busy: asked.append(busy)
    # 모델 쪽으로 가는 길은 여기서 끊는다. 우리가 보는 것은 **갔는가** 다.
    window._ask = lambda _text: None
    window._send()
    return dropped, asked


def test_the_typed_number_never_reaches_the_model_while_reviewing():
    """**모델을 거치면 SCOPE 가 거절한다** — 실제로 그랬다 (2026-08-19).

        나:  15번 빼줘
        펫:  그건 제가 알 수 없습니다…
    """
    dropped, asked = _typing("15번 빼줘")
    assert dropped == [14], "15번을 뺀다"
    assert asked == [], "모델을 안 부른다"


def test_only_a_bare_number_is_taken_as_a_drop():
    """검토 중이라고 아무 말이나 삼키면 대화가 막힌다."""
    for text in ("15번 빼줘", "15번", "15", "3번 지워줘"):
        dropped, asked = _typing(text)
        assert dropped and not asked, f"{text!r} 는 줄 빼기다"

    for text in ("이온엠이 뭐야", "15번 항목을 저장해줘", "다시 녹화하자"):
        dropped, asked = _typing(text)
        assert dropped == [] and asked == [True], f"{text!r} 는 대화다"


def test_a_number_outside_a_review_is_an_ordinary_message():
    """녹화 검토 중이 아니면 `15` 는 그냥 말이다."""
    dropped, asked = _typing("15번 빼줘", reviewing=False)
    assert dropped == [] and asked == [True]


def _navigation_ctx(tmp_path):
    import threading
    from queue import Queue

    from vader.core.checkpoint import CheckpointStore
    from vader.core.context import JobContext
    from vader.core.job import Job
    from vader.sdk import CheckpointKey

    events = Queue()
    ctx = JobContext(
        job=Job(rune_id="r", inputs=[], workdir=tmp_path, id="j"),
        events=events,
        cancel=threading.Event(),
        params={},
        checkpoints=CheckpointStore(tmp_path / "cp"),
        checkpoint_key=CheckpointKey(rune_id="r", input_identity="i"),
    )
    recorder.save("navigate", [
        _step(name="재고관리"),
        _step(name="부서항목", control_type="ListItem"),
        _step(name="", control_type="Edit", automation_id="m_pTextBox"),
    ])
    return ctx, events


def _logged(events):
    out = []
    while not events.empty():
        event = events.get()
        if hasattr(event, "message"):
            out.append((event.level, event.message))
    return out


def test_two_windows_with_the_recorded_title_stop_the_replay_at_once(
    isolated_settings, tmp_path, monkeypatch
):
    """2026-10-12 — 연습 뒤 이온엠이 둘. 창을 못 고르자 메뉴·부서가 **조용히 건너뛰어졌다.**

    없는 창은 '아직 안 뜬 준비 동작' 으로 넘기지만, 여럿은 넘기면 안 된다. 바로 멈추고 이유를 말한다.
    """
    from vader.core import context as context_module

    ctx, events = _navigation_ctx(tmp_path)
    played = []
    monkeypatch.setattr(context_module, "_window_titled", lambda title: context_module.AMBIGUOUS_WINDOW)
    monkeypatch.setattr(recorder, "replay", lambda step, window, log=None: played.append(step) or True)

    assert ctx.replay("navigate", stop=lambda step: step.automation_id == "m_pTextBox") is False
    assert played == []
    assert any(level == "error" and "여럿 떠 있어" in message for level, message in _logged(events))


def test_reaching_the_handoff_with_nothing_pressed_is_not_success(
    isolated_settings, tmp_path, monkeypatch
):
    """인계 지점에 닿았어도 **하나도 안 눌렀으면 실패다.** 예전엔 성공을 돌려줘 룬이 헛기다렸다."""
    from vader.core import context as context_module

    ctx, events = _navigation_ctx(tmp_path)
    monkeypatch.setattr(context_module, "_window_titled", lambda title: None)   # 창이 안 떠 있다
    monkeypatch.setattr(recorder, "replay", lambda step, window, log=None: True)

    assert ctx.replay("navigate", stop=lambda step: step.automation_id == "m_pTextBox") is False
    assert any(level == "error" and "한 단계도" in message for level, message in _logged(events))


def test_a_window_list_that_cannot_be_read_is_an_error_not_a_missing_window(
    isolated_settings, tmp_path, monkeypatch
):
    """예외를 '창 없음' 으로 삼켜 8단계가 1초 만에 전부 건너뛰어졌다 (2026-11-07). 왜 못 읽었는지 남긴다."""
    from vader.core import context as context_module

    def broken(title):
        raise OSError("COMError -2147220991")

    ctx, events = _navigation_ctx(tmp_path)
    monkeypatch.setattr(context_module, "_window_titled", broken)
    monkeypatch.setattr(recorder, "replay", lambda step, window, log=None: True)

    assert ctx.replay("navigate", stop=lambda step: step.automation_id == "m_pTextBox") is False
    assert any(level == "error" and "녹화한 창에 붙지 못해" in message for level, message in _logged(events))


def test_a_click_step_brings_its_window_forward_before_clicking(monkeypatch):
    """뒤에 있는 창의 첫 클릭은 창을 앞으로 가져오는 데만 쓰인다 — 메뉴가 안 열렸다 (2026-10-12)."""
    happened = []

    class Control:
        def click_input(self, **_k):
            happened.append("click")

        def rectangle(self):
            return type("R", (), {"left": 0, "top": 0, "right": 10, "bottom": 10})()

    class Window:
        def set_focus(self):
            happened.append("focus")

        def child_window(self, **_k):
            return Control()

    monkeypatch.setattr(recorder.time, "sleep", lambda _s: None)
    assert recorder.replay(_step(name="재고관리", control_type="MenuItem"), Window()) is True
    assert happened == ["focus", "click"]


def test_a_dropdown_of_the_program_in_front_is_not_brought_forward(monkeypatch):
    """부서 목록(Form6)은 앞으로 가져오는 순간 닫힌다 — '7001.공급실' 을 못 눌렀다 (2026-10-15)."""
    happened = []

    class Control:
        def click_input(self, **_k):
            happened.append("click")

    class Window:
        handle = 7

        def set_focus(self):
            happened.append("focus")

        def child_window(self, **_k):
            return Control()

    monkeypatch.setattr(recorder.time, "sleep", lambda _s: None)
    monkeypatch.setattr(recorder, "_pid_of", lambda hwnd: 100)    # 앞 창도 목록도 이온엠
    assert recorder.replay(_step(name="7001.공급실", control_type="ListItem"), Window()) is True
    assert happened == ["click"]

    monkeypatch.setattr(recorder, "_pid_of", lambda hwnd: 100 if hwnd == 7 else 200)   # 앞은 VaderAI
    happened.clear()
    assert recorder.replay(_step(name="재고관리", control_type="MenuItem"), Window()) is True
    assert happened == ["focus", "click"]


def test_a_rune_with_its_own_recordings_never_falls_back_to_the_old_one(isolated_settings, tmp_path):
    """룬 안 녹화가 없거나 깨졌는데 옛 전역 녹화를 누르면, 승인하지 않은 동작이 나간다 (2026-10-15)."""
    import json

    recorder.path_for("navigation").parent.mkdir(parents=True, exist_ok=True)
    recorder.path_for("navigation").write_text(json.dumps({"steps": [{"at": "x", "keys": "{TAB}"}]}), encoding="utf-8")
    (config.rune_dir() / "r" / "recordings").mkdir(parents=True)     # 룬은 자기 녹화 폴더를 가졌다 — 비어 있다
    ctx = _ctx_for(tmp_path)
    assert ctx.replay("navigation") is False
    assert any("녹화가 없습니다" in m for _l, m in _logged(ctx._events))
