"""관찰 경계 테스트.

이 파일의 목적은 기능 확인이 아니라 **약속을 지키는 것**이다.
`wiki/architecture/observation-boundary.md` 가 "무엇을 관찰하지 않는가" 를 적었고
여기가 그것을 강제한다. 문서상 약속은 지켜지지 않는다.
"""
from __future__ import annotations

import ast
import socket
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from vader.core import observe
from vader.core.observe import (
    FOREGROUND,
    JOB_FINISHED,
    SESSION_END,
    SESSION_START,
    Observation,
    ObservationStore,
    SessionObserver,
)

ROOT = Path(__file__).resolve().parent.parent
AT = datetime(2026, 7, 30, 14, 3, 0)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("관찰은 어디로도 보내지 않는다")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setattr(observe, "enabled", lambda: True)
    monkeypatch.setattr(observe, "retention_days", lambda: 90)
    monkeypatch.setattr(observe, "sample_seconds", lambda: 60)


def store_at(tmp_path) -> ObservationStore:
    return ObservationStore(tmp_path / "observations.jsonl", days=90)


# ── 경계 ───────────────────────────────────────────────────────

class TestBoundary:
    """관찰하지 않기로 한 것을 관찰하지 않는다."""

    #: 시연 녹화만 예외다 (마스터 제안 2026-08-11, `observation-boundary.md`).
    #: **사용자가 `녹화 시작` 을 누른 동안만** 왼쪽 버튼 상태를 본다.
    #: 키 입력은 여전히 안 본다 — `tests/test_recorder.py` 가 지킨다.
    RECORDER = "vader/core/recorder.py"
    #: 콘솔에서 **사람이 붙여넣기를 누른 때만** 클립보드를 읽는다 (2026-10-27 마스터 요청, 시험 PC 피드백).
    #: 클립보드만 허용 — 키·마우스 훅은 이 파일에서도 금지다.
    CLIPBOARD = "vader/desktop/clipboard.py"
    #: 표 읽기: **자기가 그 표 창에 보낸 복사 뒤, 그 앱이 쓴** 클립보드만 읽고 바로 비운다 (2026-11-17 마스터 결정, codex1 회신 5 PASS).
    TABLE = "vader/core/screen.py"

    def test_no_input_hook_anywhere_in_core(self):
        """전역 키보드·마우스 훅을 설치하지 않는다 (사용자 결정, 2026-07-30).

        훅은 원리상 내용을 볼 수 있는 자리다. 안 쓴다는 약속을 문법으로 지킨다.

        녹화기 하나만 뺀다. **그 예외도 여기 이름을 적어야 통과한다** —
        새 파일이 조용히 입력을 보기 시작하면 이 테스트가 잡는다.
        """
        forbidden = (
            "SetWindowsHookEx",
            "GetAsyncKeyState",
            "GetKeyState",
            "keyboard",
            "pynput",
            "pyHook",
            "GetClipboardData",
        )
        offenders = []
        for path in sorted((ROOT / "vader").rglob("*.py")):
            name = path.relative_to(ROOT).as_posix()
            if name == self.RECORDER:
                continue
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text and not (name in (self.CLIPBOARD, self.TABLE) and token == "GetClipboardData"):
                    offenders.append(f"{name}: {token}")
        assert offenders == [], "입력 훅·클립보드 접근이 들어왔습니다:\n  " + "\n  ".join(offenders)

    def test_observer_never_reads_window_titles(self):
        """창 제목에는 임상 메모와 거래처 문서명이 들어 있다.

        `observation-boundary.md` 에 이 기계의 실물 예시가 있다. 앱 다양성은 앱
        신원을 세는 것이고 제목은 문서마다 바뀌어 같은 앱을 여러 앱으로 센다.
        """
        source = (ROOT / "vader" / "core" / "observe.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "list_window_titles" not in called
        assert "GetWindowTextW" not in source

    def test_stored_value_is_an_executable_name(self, tmp_path):
        store = store_at(tmp_path)
        observer = SessionObserver(store, now=lambda: AT, app_name=lambda: "chrome.exe")
        observer.sample()

        line = store.path.read_text(encoding="utf-8").strip()
        assert '"value": "chrome.exe"' in line
        assert "chrome.exe" == store.load()[0].value


# ── 저장소 ─────────────────────────────────────────────────────

class TestStore:

    def test_round_trips_an_observation(self, tmp_path):
        store = store_at(tmp_path)
        store.record(Observation(FOREGROUND, AT, "code.exe"))

        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0] == Observation(FOREGROUND, AT, "code.exe")

    def test_appends_rather_than_replaces(self, tmp_path):
        store = store_at(tmp_path)
        for name in ("a.exe", "b.exe", "c.exe"):
            store.record(Observation(FOREGROUND, AT, name))

        assert [item.value for item in store.load()] == ["a.exe", "b.exe", "c.exe"]

    def test_a_corrupt_line_does_not_break_loading(self, tmp_path):
        store = store_at(tmp_path)
        store.record(Observation(FOREGROUND, AT, "a.exe"))
        with store.path.open("a", encoding="utf-8") as stream:
            stream.write("{ 깨진 줄\n")
        store.record(Observation(FOREGROUND, AT, "b.exe"))

        # 관찰 로그가 앱을 못 띄우게 하면 안 된다.
        assert [item.value for item in store.load()] == ["a.exe", "b.exe"]

    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert store_at(tmp_path).load() == []

    def test_prune_drops_lines_past_retention(self, tmp_path):
        store = ObservationStore(tmp_path / "o.jsonl", days=7)
        old = AT - timedelta(days=30)
        store.record(Observation(FOREGROUND, old, "old.exe"))
        store.record(Observation(FOREGROUND, AT, "new.exe"))

        assert store.prune(now=AT) == 1
        assert [item.value for item in store.load()] == ["new.exe"]

    def test_prune_on_an_empty_store_is_harmless(self, tmp_path):
        assert store_at(tmp_path).prune(now=AT) == 0

    def test_reset_removes_everything(self, tmp_path):
        store = store_at(tmp_path)
        store.record(Observation(FOREGROUND, AT, "a.exe"))
        store.reset()

        assert store.load() == []
        assert not store.path.exists()


# ── 끄기 ───────────────────────────────────────────────────────

class TestDisabled:

    def test_records_nothing_and_creates_no_file(self, tmp_path, monkeypatch):
        """끄면 파일조차 만들지 않는다. 빈 파일이 남으면 껐다고 믿기 어렵다."""
        monkeypatch.setattr(observe, "enabled", lambda: False)
        store = store_at(tmp_path)

        assert store.record(Observation(FOREGROUND, AT, "chrome.exe")) is False
        assert not store.path.exists()

    def test_observer_start_and_sample_are_silent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(observe, "enabled", lambda: False)
        store = store_at(tmp_path)
        observer = SessionObserver(store, now=lambda: AT, app_name=lambda: "chrome.exe")

        observer.start()
        observer.sample()
        observer.job_finished()
        observer.stop()

        assert not store.path.exists()


# ── 표본 주기 ──────────────────────────────────────────────────

class TestSampling:

    def test_does_not_sample_twice_inside_the_interval(self, tmp_path):
        clock = [AT]
        store = store_at(tmp_path)
        observer = SessionObserver(
            store, now=lambda: clock[0], app_name=lambda: "chrome.exe"
        )

        assert observer.sample() is True
        clock[0] = AT + timedelta(seconds=30)
        assert observer.sample() is False
        clock[0] = AT + timedelta(seconds=61)
        assert observer.sample() is True
        assert len(store.load()) == 2

    def test_an_empty_app_name_is_not_recorded(self, tmp_path):
        store = store_at(tmp_path)
        observer = SessionObserver(store, now=lambda: AT, app_name=lambda: "")

        assert observer.sample() is False
        assert store.load() == []

    def test_session_start_prunes_old_lines(self, tmp_path):
        store = ObservationStore(tmp_path / "o.jsonl", days=7)
        store.record(Observation(FOREGROUND, AT - timedelta(days=30), "old.exe"))
        observer = SessionObserver(store, now=lambda: AT, app_name=lambda: "a.exe")

        observer.start()

        kinds = [item.kind for item in store.load()]
        assert kinds == [SESSION_START]

    def test_session_end_is_recorded(self, tmp_path):
        store = store_at(tmp_path)
        SessionObserver(store, now=lambda: AT, app_name=lambda: "a.exe").stop()

        assert store.load()[0].kind == SESSION_END

    def test_job_finished_is_counted(self, tmp_path):
        store = store_at(tmp_path)
        SessionObserver(store, now=lambda: AT, app_name=lambda: "a.exe").job_finished()

        assert store.load()[0].kind == JOB_FINISHED


# ── 실제 창 조회 ───────────────────────────────────────────────

class TestForegroundAppName:

    def test_returns_a_lowercase_executable_or_empty(self):
        """실제 OS 를 부른다. 값이 없어도 되지만 제목이 나오면 안 된다."""
        name = observe.foreground_app_name()

        assert isinstance(name, str)
        if name:
            assert name == name.lower()
            assert name.endswith(".exe")
            assert " - " not in name  # 창 제목 형태가 새어나오지 않는다
