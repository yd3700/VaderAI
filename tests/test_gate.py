"""2단계 2a — 계정 작업공간과 잠금의 경계 (마스터 결정 2026-11-05, codex1 설계·최소 검증 목록)."""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from vader.core import account, catalog, config, gate, secrets
from vader.desktop import host

ORIGIN = "https://vaderai.test"


@pytest.fixture
def vault(monkeypatch):
    """기기 금고를 메모리로 — 계정 금고에는 아무것도 안 들어가야 한다."""
    store = {}
    monkeypatch.setattr(secrets, "available", lambda: True)
    monkeypatch.setattr(secrets, "get", lambda name, device=False: store.get(name) if device else None)
    monkeypatch.setattr(secrets, "put", lambda name, value, device=False: store.__setitem__(name, value) if device
                        else pytest.fail("계정 금고에 쓰면 안 된다"))
    monkeypatch.setattr(secrets, "delete", lambda name, device=False: store.pop(name, None))
    monkeypatch.setattr(account, "site", lambda: ORIGIN)
    return store


def _server(monkeypatch, *, signed_in=True, answer=None, offline=False, owner="acc-a"):
    monkeypatch.setattr(account, "signed_in", lambda: signed_in)
    monkeypatch.setattr(account, "stored_account", lambda origin: owner)   # 저장된 기기 토큰의 계정

    def me():
        if offline:
            raise account.AccountError("서버에 연결하지 못했습니다.")
        return answer
    monkeypatch.setattr(account, "account", me)


# ── 1. 열리기 전엔 업무 데이터가 없다 ──

def test_nothing_business_opens_before_the_workspace(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_WORKSPACE", True)
    for read in (config.load, config.data_dir, config.rune_dir, lambda: config.get("runes")):
        with pytest.raises(config.WorkspaceLocked):
            read()
    assert config.device_get("account.site", "기본") == "기본"     # 기기 설정은 로그인 전에도 읽는다


def test_the_workspace_ignores_path_overrides_and_never_switches(monkeypatch, tmp_path):
    config.use_workspace(tmp_path / "a")
    assert config.data_dir() == tmp_path / "a" and config.rune_dir() == tmp_path / "a" / "runes"
    assert config.SETTINGS_PATH == tmp_path / "a" / "settings.json"
    assert os.environ[config.WORKSPACE_ENV] == str(tmp_path / "a")      # 자식 프로세스가 이어받는다
    with pytest.raises(RuntimeError, match="다시 켜야"):
        config.use_workspace(tmp_path / "b")


def test_two_accounts_get_two_folders_and_a_profile_must_match(tmp_path):
    a, b = gate.root(ORIGIN, "acc-a"), gate.root(ORIGIN, "acc-b")
    assert a != b and a.parent == config.HOME / "accounts" and "acc-a" not in a.name
    assert gate.root("https://other.test", "acc-a") != a                # 같은 id 라도 웹 주소가 다르면 다른 계정
    folder = gate.open_profile(ORIGIN, "acc-a")
    (folder / "profile.json").write_text(json.dumps({"origin": ORIGIN, "account_id": "acc-b"}), encoding="utf-8")
    with pytest.raises(gate.GateError):
        gate.open_profile(ORIGIN, "acc-a")


# ── 2. PIN ──

def test_pin_shape_rules():
    for bad in ("12345", "1234567", "12a456", "111111", "123456", "987654", None):
        with pytest.raises(gate.GateError):
            gate._check_shape(bad)
    gate._check_shape("482915")


def test_wrong_pins_make_you_wait_and_restarting_does_not_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "_SCRYPT", {**gate._SCRYPT, "n": 2 ** 4})   # 시험은 빠르게 — 파일에 적힌 값으로 다시 잰다
    gate.set_pin(tmp_path, "482915")
    gate.check_pin(tmp_path, "482915")
    for _ in range(5):
        with pytest.raises(gate.GateError, match="맞지 않"):
            gate.check_pin(tmp_path, "000001")
    assert 20 < gate.wait_seconds(tmp_path) <= 30                       # 파일에 남았다 — 앱을 다시 켜도 그대로
    with pytest.raises(gate.GateError, match="초 뒤"):
        gate.check_pin(tmp_path, "482915")                              # 맞는 PIN 도 기다린 뒤에


def test_a_damaged_lock_file_sends_you_to_login_not_to_a_new_pin(tmp_path):
    (tmp_path / "lock.json").write_text("{}", encoding="utf-8")
    with pytest.raises(gate.GateError, match="잊었어요"):
        gate.check_pin(tmp_path, "482915")


# ── 3. 계정 확인·오프라인 7일 ──

def test_server_confirmation_is_remembered_for_offline_use(vault, monkeypatch):
    _server(monkeypatch, answer={"id": "acc-a", "display_name": "가나메디칼"})
    assert gate.who()["account"]["id"] == "acc-a"
    _server(monkeypatch, offline=True)
    assert gate.who() == {"state": "ok", "origin": ORIGIN, "account": {"id": "acc-a", "display_name": "가나메디칼"}}


def test_offline_entry_uses_only_the_record_of_the_account_signed_in_now(vault, monkeypatch):
    """A 로 확인한 기록이 남아 있어도 B 로 로그인한 뒤 서버에 못 닿으면 A 로 들어가지 않는다 (codex1 회신 9). 같은 계정은 그대로."""
    _server(monkeypatch, answer={"id": "acc-a", "display_name": "가나메디칼"})
    gate.who()
    _server(monkeypatch, offline=True, owner="acc-b")
    assert gate.who() == {"state": "offline"}
    _server(monkeypatch, offline=True, owner="acc-a")                       # 같은 계정으로 다시 로그인 — 오프라인 7일 그대로
    assert gate.who()["state"] == "ok"


@pytest.mark.parametrize("age", [8 * 86400, -3600])                    # 7일 넘음 · 시계가 뒤로 감
def test_offline_entry_needs_a_recent_honest_record(vault, monkeypatch, age):
    _server(monkeypatch, answer={"id": "acc-a", "display_name": ""})
    gate.who()
    name = next(iter(vault))
    vault[name] = json.dumps({**json.loads(vault[name]), "at": time.time() - age})
    _server(monkeypatch, offline=True)
    assert gate.who() == {"state": "offline"}


def test_a_server_refusal_drops_offline_entry_too(vault, monkeypatch):
    _server(monkeypatch, answer={"id": "acc-a", "display_name": ""})
    gate.who()
    _server(monkeypatch, answer=None)                                    # 401 — 계정이 끊겼다
    assert gate.who() == {"state": "login"} and not vault
    _server(monkeypatch, offline=True)
    assert gate.who() == {"state": "offline"}                           # 인터넷이 끊겨도 다시 들어가지 못한다


# ── 4. 화면 API 는 잠긴 동안 문만 ──

@pytest.fixture
def door(vault, monkeypatch):
    monkeypatch.setattr(gate, "_SCRYPT", {**gate._SCRYPT, "n": 2 ** 4})
    _server(monkeypatch, answer={"id": "acc-a", "display_name": "가나메디칼"})
    config.device_set("network.notice", config.NOTICE_VERSION)   # 첫 실행 안내는 따로 본다
    api = host.DesktopAPI()
    api._window = MagicMock()

    def start(folder):
        config.use_workspace(folder)
        api._controller = MagicMock()
    monkeypatch.setattr(api, "_start", start)
    return api


def test_first_run_notice_comes_before_any_account_check_and_can_turn_auto_off(door, monkeypatch):
    """첫 실행: 계정 확인(통신) 전에 안내. 끄고 시작하면 업데이트 확인도 광장 자동 받기도 안 한다 (SignPath 조건)."""
    from vader.core import update
    config.device_set("network.notice", None)
    who = host.gate.who
    monkeypatch.setattr(host.gate, "who", lambda: pytest.fail("안내 전에 계정을 확인하면 안 된다"))
    started = []
    monkeypatch.setattr(update, "start", lambda: started.append(config.auto_online()))
    assert door.call("gate_status")["data"] == {"state": "notice"}
    assert config.auto_online() is False                         # 안내 전엔 자동 확인 없음
    monkeypatch.setattr(host.gate, "who", who)
    assert door.call("gate_notice", {"auto": False})["data"]["state"] == "pin_setup"
    assert started == [False] and config.auto_online() is False and config.notice_seen()


def test_business_calls_are_refused_until_the_pin_and_again_after_lock(door):
    assert door.call("catalog") == {"ok": False, "error": "잠겨 있어요.", "locked": True}
    assert door.call("gate_status")["data"]["state"] == "pin_setup"
    assert not door.call("gate_pin_setup", {"pin": "482915", "again": "482916"})["ok"]
    assert door.call("gate_pin_setup", {"pin": "482915", "again": "482915"})["data"] == {"state": "open"}
    assert config.data_dir() == gate.root(ORIGIN, "acc-a")
    assert door.call("catalog")["ok"] and gate.session_open()
    door.lock()
    assert door.call("catalog")["locked"] and not gate.session_open()
    door._window.evaluate_js.assert_called_with("location.replace('gate.html')")
    door._controller.record_discard.assert_called()                     # 잠긴 동안 남의 조작을 담지 않는다
    assert not door.call("gate_unlock", {"pin": "111112"})["ok"]
    assert door.call("gate_status")["data"]["state"] == "pin"
    assert door.call("gate_unlock", {"pin": "482915"})["data"] == {"state": "open"}


def test_another_account_after_opening_needs_a_restart(door, monkeypatch):
    door.call("gate_status")
    door.call("gate_pin_setup", {"pin": "482915", "again": "482915"})
    door.lock()
    _server(monkeypatch, answer={"id": "acc-b", "display_name": "다른 사람"})
    assert door.call("gate_status")["data"] == {"state": "restart", "name": "다른 사람"}
    assert not door.call("gate_unlock", {"pin": "482915"})["ok"]        # 새 계정 확인 전엔 문이 무엇도 열지 않는다


def test_forgot_pin_resets_only_for_the_same_account(door, monkeypatch):
    door.call("gate_status")
    door.call("gate_pin_setup", {"pin": "482915", "again": "482915"})
    door.lock()
    door.call("gate_status")
    monkeypatch.setattr(account, "login", lambda: {"id": "acc-b"})
    _server(monkeypatch, answer={"id": "acc-b", "display_name": ""})
    assert door.call("gate_forgot")["data"]["state"] == "restart"
    assert gate.has_pin(gate.root(ORIGIN, "acc-a"))                    # 다른 계정이 A 의 PIN 을 지우지 못한다
    _server(monkeypatch, answer={"id": "acc-a", "display_name": ""})
    door.call("gate_status")
    monkeypatch.setattr(account, "login", lambda: {"id": "acc-a"})
    assert door.call("gate_forgot")["data"]["state"] == "pin_setup"


def test_signing_in_or_resetting_is_refused_while_open(door, monkeypatch):
    door.call("gate_status")
    door.call("gate_pin_setup", {"pin": "482915", "again": "482915"})
    monkeypatch.setattr(account, "login", lambda: pytest.fail("열린 채로 토큰을 바꾸면 안 된다"))
    assert not door.call("gate_login")["ok"] and not door.call("gate_forgot")["ok"]
    assert gate.has_pin(gate.root(ORIGIN, "acc-a"))


def test_a_pin_screen_left_open_cannot_outlive_a_server_refusal(door, monkeypatch):
    door.call("gate_status")
    door.call("gate_pin_setup", {"pin": "482915", "again": "482915"})
    door.lock()
    door.call("gate_status")                                            # PIN 칸이 떠 있다
    _server(monkeypatch, answer=None)                                   # 그사이 서버가 계정을 끊었다
    assert not door.call("gate_unlock", {"pin": "482915"})["ok"] and not door._open


def test_an_answer_finished_after_locking_is_not_delivered(door):
    door.call("gate_status")
    door.call("gate_pin_setup", {"pin": "482915", "again": "482915"})
    door._controller.catalog.side_effect = lambda: door.lock() or {"runes": ["업무"]}
    assert door.call("catalog") == {"ok": False, "error": "잠겨 있어요.", "locked": True}
    door._controller.catalog.side_effect = lambda: door.lock() or (_ for _ in ()).throw(ValueError("업무 경로"))
    door.call("gate_status")
    assert door.call("gate_unlock", {"pin": "482915"})["data"] == {"state": "open"}
    calls = door._controller.catalog.call_count
    assert door.call("catalog") == {"ok": False, "error": "잠겨 있어요.", "locked": True}   # 오류 문장도 보내지 않는다
    assert door._controller.catalog.call_count == calls + 1


def test_a_cancelled_reset_login_can_be_retried_from_the_same_screen(door, monkeypatch):
    door.call("gate_status")
    door.call("gate_pin_setup", {"pin": "482915", "again": "482915"})
    door.lock()
    door.call("gate_status")

    def cancelled():
        raise account.AccountError("시간 안에 로그인하지 않았습니다.")
    monkeypatch.setattr(account, "login", cancelled)
    assert not door.call("gate_forgot")["ok"]
    assert door.call("gate_status")["data"]["state"] == "pin"           # 화면이 상태를 다시 받으면 그대로 이어진다
    assert door.call("gate_unlock", {"pin": "482915"})["data"] == {"state": "open"}


def test_a_late_heartbeat_cannot_reopen_a_locked_session(door, monkeypatch):
    door.call("gate_status")
    door.call("gate_pin_setup", {"pin": "482915", "again": "482915"})
    monkeypatch.setattr(host, "_idle_seconds", lambda: door.lock() or 0)   # 열림 확인과 맥박 사이에 다른 스레드가 잠근다
    monkeypatch.setattr(host, "_session_locked", lambda: False)
    clock = iter([10_000.0, 10_010.0])
    monkeypatch.setattr(host.time, "time", lambda: next(clock, 10_010.0))
    ticks = iter([None])
    with pytest.raises(StopIteration):
        host._watch(door, sleep=lambda s: next(ticks))
    assert not json.loads((config.HOME / "session.json").read_text(encoding="utf-8"))["unlocked"]


def test_a_failed_session_marker_leaves_the_door_shut(door, monkeypatch):
    door.call("gate_status")
    monkeypatch.setattr(gate, "mark", lambda *a: (_ for _ in ()).throw(OSError("디스크")))
    assert not door.call("gate_pin_setup", {"pin": "482915", "again": "482915"})["ok"]
    assert door.call("catalog")["locked"]


def test_the_watcher_locks_on_idle_and_stops_screen_work_on_windows_lock(monkeypatch):
    locks = []
    api = SimpleNamespace(_open=True, _closing=False, _folder=None, _sgen="g", _controller=MagicMock(), lock=lambda: locks.append(1),
                          _door=__import__("threading").Lock())

    def run(idle, locked, opened=True):
        api._open = opened
        monkeypatch.setattr(host, "_idle_seconds", lambda: idle)
        monkeypatch.setattr(host, "_session_locked", lambda: locked)
        ticks = iter([None])
        with pytest.raises(StopIteration):
            host._watch(api, sleep=lambda s: next(ticks))
    run(5, False)
    assert not locks and not api._controller.stop_screen_work.called
    run(10 * 60, False)
    assert len(locks) == 1 and not api._controller.stop_screen_work.called   # 유휴만으로는 작업을 안 멈춘다
    run(0, True, opened=False)                                         # 앱이 먼저 잠겼어도 Windows 잠금은 화면 작업을 멈춘다
    assert api._controller.stop_screen_work.called and len(locks) == 1


def test_windows_lock_cancels_only_screen_jobs():
    from vader.desktop.controller import DesktopController
    controller = DesktopController(MagicMock(), loaded=[])
    job = SimpleNamespace(id="j1", rune_id="eonm", is_terminal=False)
    controller.jobs, controller.active = {"j1": {"job": job}}, "j1"
    controller.runes = {"eonm": SimpleNamespace(manifest=SimpleNamespace(target_apps=["EONM"]))}
    controller.stop_screen_work()
    controller.runner.cancel.assert_called_once_with("j1")
    controller.runner.cancel.reset_mock()
    controller.runes["eonm"].manifest.target_apps = []
    controller.stop_screen_work()
    controller.runner.cancel.assert_not_called()                        # 파일만 다루는 작업은 계속


# ── 5. 기기 공통 vs 계정별 ──

def test_revocation_records_are_per_device_and_install_ledger_per_account(tmp_path):
    config.use_workspace(tmp_path / "acc")
    assert catalog._file("blocked.json").parent == config.HOME / "catalog"
    assert catalog._file("state.json").parent == config.HOME / "catalog"
    assert catalog._file("origins.json").parent == tmp_path / "acc" / "catalog"
    assert catalog._file("managed.json").parent == tmp_path / "acc" / "catalog"


def test_child_processes_open_only_a_real_account_folder(monkeypatch, tmp_path):
    monkeypatch.setenv(config.WORKSPACE_ENV, str(tmp_path / "anywhere"))
    gate.open_child()
    assert config.workspace() is None and config.REQUIRE_WORKSPACE      # 공용 폴더로 내려가지 않는다
    folder = gate.open_profile(ORIGIN, "acc-a")
    monkeypatch.setenv(config.WORKSPACE_ENV, str(folder))
    gate.open_child()
    assert config.workspace() == folder
    assert not gate.session_open()                                      # 앱이 열어 두지 않았으면 MCP 는 거절
    gate.mark(folder, True)
    assert gate.session_open()
