"""화면 자동화 엔진 중개 — 실행 전 엔진 대조, 열린 명령만, 세대(잠금) 앞뒤 확인, 받는 중 크기 상한,
봉투 모순은 '모름', 거절은 엔진이 '안 보냈다' 할 때만, 오류 문장(화면 글자)은 밖으로 안 냄, 상태 폴더 정리."""
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from vader.core import screen

SECRET = "홍길동가짜 900101-1234567"
FAKE = r"""
import json, sys, time
mode, cmd = sys.argv[1], sys.argv[2]
if mode == "flood":
    sys.stdout.write("x" * (64 * 1024 * 1024)); sys.stdout.flush(); sys.exit(0)
if mode == "slow":
    time.sleep(60)
if mode == "stdin":
    open(sys.argv[5], "w", encoding="utf-8").write(sys.stdin.buffer.read().decode("utf-8"))
env = json.loads(sys.argv[3])
sys.stdout.write(json.dumps(env)); sys.exit(int(sys.argv[4]))
"""


@pytest.fixture(autouse=True)
def no_ghost(monkeypatch):
    """가상 커서는 tests/test_ghost.py 에서 — 여기서는 띄우지 않는다(프로세스가 테스트 사이에 남지 않게)."""
    monkeypatch.setattr(screen, "_ghost", lambda *a: None)


@pytest.fixture
def fake(monkeypatch, isolated_settings):
    """엔진 대신 파이썬 — 봉투·종료 코드·쏟기·멈춤을 정한다. 세대는 '열림 g1'."""
    state = {"mode": "env", "env": None, "code": 0, "gen": ["g1"]}
    monkeypatch.setattr(screen, "engine", lambda: Path(sys.executable))
    monkeypatch.setattr(screen.gate, "session_gen", lambda: state["gen"][0] if len(state["gen"]) == 1 else state["gen"].pop(0))
    real = subprocess.Popen

    def popen(argv, **kw):
        state.setdefault("argv", []).append(argv[1:])
        answer = state.get("envs", {}).get(argv[1], state["env"])          # 명령마다 다른 답 (3b: 읽고 바로 조작)
        return real([sys.executable, "-c", FAKE, state["mode"], argv[1], json.dumps(answer), str(state["code"]),
                     state.get("sink", "")], **kw)
    monkeypatch.setattr(screen.subprocess, "Popen", popen)
    screen._issued.clear()
    screen._clear_pending()
    yield state
    screen._issued.clear()
    screen._clear_pending()
    screen._dirty = False


def env(cmd, ok=True, data=None, error=None, version=screen.ENVELOPE):
    return {"version": version, "ok": ok, "command": cmd, "data": data, "error": error}


def snap(tree=None, sid="s1", wid="w-1"):
    return {"snapshot_id": sid, "complete": True, "window": {"id": wid}, "tree": tree or {}}


def test_ok_answer_and_only_open_commands(fake):
    fake["env"] = env("snapshot", data=snap())
    assert screen.run(["snapshot"]) == snap()
    for bad in (["toggle", "@s:e1"], ["launch", "notepad"], [], ["snapshot", 1]):
        with pytest.raises(screen.ScreenError) as got:
            screen.run(bad)
        assert got.value.kind == screen.REFUSED


@pytest.mark.parametrize("answer,code", [
    (env("status", data={"version": screen.ENGINE_VERSION}), 0),          # 다른 명령의 답
    (env("snapshot", data=snap(), error={"code": "INTERNAL"}), 0),         # ok 인데 error
    (env("snapshot", data=snap()), 3),                                     # ok 인데 종료 코드 실패
    (env("snapshot", ok=False, error={"code": "STALE_REF"}), 0),           # 실패인데 종료 코드 0
    (env("snapshot", data=snap(), version="9.9"), 0),
    (env("snapshot", data={"tree": {}}), 0),                               # 스냅샷 모양이 아님
    (env("snapshot", ok=False, error={"code": ["x"], "disposition": {}}), 1),   # 코드가 문자열이 아님
], ids=["command", "ok-with-error", "ok-nonzero", "error-zero", "version", "shape", "code-type"])
def test_contradicting_envelopes_are_unknown(fake, answer, code):
    fake["env"], fake["code"] = answer, code
    with pytest.raises(screen.ScreenError) as got:
        screen.run(["snapshot"])
    assert got.value.kind == screen.UNKNOWN


def test_refused_only_when_not_delivered_and_engine_text_never_leaks(fake):
    for delivery, kind in (("not_delivered", screen.REFUSED), ("delivery_uncertain", screen.UNKNOWN), (None, screen.UNKNOWN)):
        for code in ("STALE_REF", SECRET):
            fake["env"] = env("snapshot", ok=False, error={"code": code, "message": SECRET, "disposition": {"delivery": delivery}})
            fake["code"] = 1
            with pytest.raises(screen.ScreenError) as got:
                screen.run(["snapshot"])
            assert got.value.kind == kind and SECRET not in str(got.value)


def test_output_flood_is_cut_while_reading(fake):
    fake["mode"] = "flood"
    with pytest.raises(screen.ScreenError) as got:
        screen.run(["snapshot"])
    assert got.value.kind == screen.UNKNOWN and not screen._procs


def test_timeout_is_unknown(fake, monkeypatch):
    fake["mode"] = "slow"
    monkeypatch.setattr(screen, "TIMEOUT", 1)
    with pytest.raises(screen.ScreenError) as got:
        screen.run(["snapshot"])
    assert got.value.kind == screen.UNKNOWN


def test_locked_before_refused_and_lock_between_drops_the_result_and_the_files(fake):
    fake["env"] = env("snapshot", data=snap())
    fake["gen"] = [None]
    with pytest.raises(screen.ScreenError) as got:
        screen.run(["snapshot"])
    assert got.value.kind == screen.REFUSED
    for after in (None, "g2"):                         # 잠김, 잠갔다 다시 엶(ABA)
        home = screen.new_home()
        fake["gen"] = ["g1", after]
        home.mkdir(parents=True)
        (home / "refs.json").write_text(SECRET, encoding="utf-8")   # 그 사이 엔진이 쓴 화면 정보
        with pytest.raises(screen.ScreenError) as got:
            screen.run(["snapshot"], home)
        assert got.value.kind == screen.UNKNOWN and not home.exists()


def test_a_sweep_between_launch_and_registration_still_stops_the_engine(fake, monkeypatch):
    """엔진을 띄운 직후·등록 전에 잠금 정리가 지나가도 그 엔진은 남지 않는다 (codex1 화면 r2 P1-1)."""
    fake["mode"] = "slow"
    started = []
    real = screen.subprocess.Popen

    def popen(argv, **kw):
        proc = real(argv, **kw)
        started.append(proc)
        screen.sweep()                                  # 등록 전에 정리
        return proc
    monkeypatch.setattr(screen.subprocess, "Popen", popen)
    home = screen.new_home()
    with pytest.raises(screen.ScreenError) as got:
        screen.run(["snapshot"], home)
    assert got.value.kind == screen.UNKNOWN and started[0].poll() is not None and not screen._procs


def test_sweep_kills_running_engines_and_leftovers_block_every_process(fake, monkeypatch):
    fake["mode"] = "slow"
    t = threading.Thread(target=lambda: pytest.raises(screen.ScreenError, screen.run, ["snapshot"]))
    t.start()
    for _ in range(100):
        if screen._procs:
            break
        threading.Event().wait(.05)
    assert screen._procs and screen.sweep()
    t.join(10)
    assert not t.is_alive() and not screen._procs
    monkeypatch.setattr(screen, "_remove", lambda p: False)   # 지우기 실패 — 표식이 남아 어느 프로세스든 거절
    assert screen.sweep() is False and screen._dirty_mark().exists()
    fake["mode"], fake["env"] = "env", env("snapshot", data=snap())
    with pytest.raises(screen.ScreenError):
        screen.run(["snapshot"])
    monkeypatch.setattr(screen, "_remove", lambda p: True)
    assert screen.sweep() and not screen._dirty_mark().exists()


def test_engine_is_checked_before_it_runs(tmp_path, monkeypatch):
    exe = tmp_path / "tools" / "agent-desktop" / "agent-desktop.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"real")
    monkeypatch.setattr(screen, "__file__", str(tmp_path / "vader" / "core" / "screen.py"))
    monkeypatch.setattr(screen, "_pin", lambda: hashlib.sha256(b"real").hexdigest())
    assert screen.engine() == exe
    for content in (b"tampered", b"real-other-version"):
        exe.write_bytes(content)
        with pytest.raises(screen.ScreenError):
            screen.engine()
    exe.write_bytes(b"real")
    monkeypatch.setattr(screen, "_pin", lambda: None)            # 묶음에 해시가 없으면 실행하지 않는다
    with pytest.raises(screen.ScreenError):
        screen.engine()


def test_state_lives_under_the_workspace_and_is_swept(isolated_settings):
    home = screen.new_home()
    assert home.parent == screen._root() and screen._root().parent == screen.config.data_dir()
    assert not home.exists()                    # 엔진이 제 권한으로 만든다 — 미리 만들면 엔진이 거절
    home.mkdir(parents=True)
    (home / "refs.json").write_text(SECRET, encoding="utf-8")
    assert screen.sweep() and not screen._root().exists()


def test_the_pinned_engine_runs_and_leaves_nothing(isolated_settings):
    try:
        screen.engine()
    except screen.ScreenError:
        pytest.skip("엔진 없음 — packaging/agent_desktop.ps1")
    assert screen.run(["version"])["version"] == screen.ENGINE_VERSION
    assert Path(screen.run(["status"])["state_root"]).parent == screen._root()
    assert not screen._root().exists() or not any(screen._root().iterdir())   # 남길 필요 없는 호출은 바로 지운다
    with pytest.raises(screen.ScreenError):
        screen.run(["snapshot", "--window-id", "w-1"])     # 진단 말고는 잠금 해제가 있어야


# ── 1단계: 읽기 ──

PLANTED = ["홍길동가짜", "900101-1234567", "가나메디칼 진료메모 비밀", "다라상사가짜", "VaderAI 시험 화면"]


W1 = {"id": "w-1", "pid": 9, "process": "p9", "exe": "C:/x/app.exe"}


@pytest.fixture
def looking(fake, monkeypatch):
    """대상 창 하나(W1)를 고른 것으로, 읽은 뒤 다시 본 창 목록도 W1."""
    state = {"after": [W1]}
    monkeypatch.setattr(screen, "_target", lambda key, deadline=None: dict(W1))
    monkeypatch.setattr(screen, "_windows", lambda spec, deadline=None: [dict(x) for x in state["after"]])
    fake["state_after"] = state
    return fake


def node(ref, role="button", name="저장", **kw):
    return {"ref_id": ref, "role": role, "name": name, "available_actions": ["Click"], **kw}


def test_look_shows_only_approved_labels_even_on_buttons(looking):
    """역할이 button 이어도 이름이 승인 목록에 없으면 별칭 — '홍길동에게 보내기' 같은 버튼 (codex1 P1-A)."""
    tree = {"role": "window", "name": PLANTED[0], "children": [
        node("@s1:e1", name="홍길동가짜에게 보내기"),
        node("@s1:e2", description=PLANTED[2]),
        node("@s1:e3", role="textfield", name="이름", value=PLANTED[0], native_id={"value": PLANTED[1]}),
    ]}
    looking["env"] = env("snapshot", data={**snap(tree), "window": {"id": "w-1", "title": PLANTED[4]}})
    got = screen.look("시험 화면")
    assert [(n["ref"], n["label"]) for n in got["nodes"]] == [("@s1:e1", "button#1"), ("@s1:e2", "저장"), ("@s1:e3", "이름")]
    assert got["complete"] and not any(p in json.dumps(got, ensure_ascii=False) for p in PLANTED)
    assert list(screen._issued) == ["s1"] and screen._issued["s1"]["process"] == "p9"


@pytest.mark.parametrize("children", [
    [node("@s1:e1"), node("@s1:e1", name="닫기")],
    [node("@s1:e1", role="weird")],
    [node("@s9:e1")],
    [{"role": "button", "children": "x"}],
], ids=["dup-ref", "unknown-role", "foreign-ref", "bad-type"])
def test_contradicting_trees_are_unknown_and_leave_nothing(looking, children):
    looking["env"] = env("snapshot", data=snap({"role": "window", "children": children}))
    with pytest.raises(screen.ScreenError) as got:
        screen.look("시험 화면")
    assert got.value.kind == screen.UNKNOWN and not screen._issued
    assert not screen._root().exists() or not any(screen._root().iterdir())


def test_an_expanded_menu_list_is_not_counted_as_a_second_window(monkeypatch):
    """이온엠: 메뉴를 펼치면 제목 없는 WS_POPUP 창(주인 = 메인 창)이 더 잡히지만 비어 있고 항목은 메인 창 트리에
    있다(실PC 2026-11-16). 그 창을 읽어 뿌리 menu·ref 없음·다 봄일 때만 빼고, 나머지(비메뉴 팝업·ref 있는 메뉴·
    모달·제목 있는 대화상자·다른 프로세스 주인·id/주인 모름)는 그대로 센다 (codex1 P1·P2)."""
    exe = r"C:\emr\HisMenu.exe"
    win = lambda id, title, pid=7, proc="p7": {"id": id, "title": title, "pid": pid, "process_instance": proc,
                                               "visible": True, "minimized": False}
    monkeypatch.setattr(screen, "_exe_of", lambda pid: exe)
    monkeypatch.setattr(screen, "_same_file", lambda a, b: a == b)
    owners, trees, listed = {}, {}, []
    monkeypatch.setattr(screen, "_popup_owner", lambda wid: owners.get(wid))

    def run(argv, home=None, deadline=None):
        if argv[0] == "list-windows":
            return listed
        wid = argv[argv.index("--window-id") + 1]
        tree = dict(trees[wid])
        return {"snapshot_id": "s1", "tree": tree, "window": {"id": tree.pop("_window", wid), "title": ""},
                "complete": tree.pop("_complete", True)}
    monkeypatch.setattr(screen, "run", run)
    ids = lambda: [w["id"] for w in screen._windows({"exe": exe})]
    empty_menu = lambda: {"role": "menu"}
    listed[:] = [win("w-16", "EMR"), win("w-32", "")]
    owners["w-32"], trees["w-32"] = 16, empty_menu()
    assert ids() == ["w-16"]
    for tree in ({"role": "window"}, {"role": "menu", "children": [{"role": "menuitem", "ref_id": "@s1:e1"}]},
                 {"role": "menu", "_complete": False},             # 비메뉴 팝업 · 조작할 것이 있는 메뉴 · 덜 봄
                 {"role": "menu", "_window": "w-99"},              # 다른 창의 답
                 {"role": "menu", "children": [None]}, {"role": "menu", "ref_id": ""},      # 틀린 모양 (codex1 회신 2)
                 {"role": "menu", "children": {"x": {"role": "menuitem", "ref_id": "@s1:e1"}}}):
        trees["w-32"] = tree
        assert ids() == ["w-16", "w-32"]
    trees["w-32"] = empty_menu()
    owners["w-32"] = None                                   # 주인 꺼짐(모달) · WS_POPUP 아님 · 모름
    assert ids() == ["w-16", "w-32"]
    owners["w-32"] = 16
    listed[:] = [win("w-16", "EMR"), win("w-32", "저장할까요?")]   # 제목 있는 대화상자 — 주인 물어보지도 않는다
    assert ids() == ["w-16", "w-32"]
    listed[:] = [win("w-16", "EMR", pid=8, proc="p8"), win("w-32", "")]   # 주인이 다른 프로세스
    assert ids() == ["w-16", "w-32"]
    listed[:] = [win("w-16", "EMR"), win("invalid", ""), win("w-48", "다른 EMR", pid=9, proc="p9")]
    owners.pop("w-32")                                      # id 를 못 읽은 창 · 주인 모름 — None 끼리 짝짓지 않는다
    assert ids() == ["w-16", "invalid", "w-48"]
    assert screen._hwnd("w-0x10") == 16 and screen._hwnd("x-1") is None and screen._hwnd("w-") is None
    if sys.platform == "win32":                             # 실제 Win32 — 없는 창·틀린 id 는 주인 없음
        monkeypatch.undo()
        assert screen._popup_owner("w-1") is None and screen._popup_owner("bad") is None


@pytest.mark.parametrize("after", [[], [{**W1, "pid": 10}], [{**W1, "process": "p10"}], [{**W1, "exe": "C:/y.exe"}]],
                         ids=["closed", "other-pid", "restarted", "other-exe"])
def test_the_window_owner_must_not_change_while_reading(looking, after):
    looking["state_after"]["after"] = after
    looking["env"] = env("snapshot", data=snap({"role": "window", "children": [node("@s1:e1")]}))
    with pytest.raises(screen.ScreenError) as got:
        screen.look("시험 화면")
    assert got.value.kind == screen.UNKNOWN and not screen._issued


def test_a_deep_tree_is_reported_incomplete(looking):
    deep = node("@s1:e1")
    for _ in range(70):
        deep = {"role": "group", "children": [deep]}
    looking["env"] = env("snapshot", data=snap({"role": "window", "children": [node("@s1:e2", name="닫기"), deep]}))
    got = screen.look("시험 화면")
    assert got["complete"] is False and [n["label"] for n in got["nodes"]] == ["닫기"]


def test_a_look_that_ends_after_a_lock_is_not_published(looking, monkeypatch):
    looking["env"] = env("snapshot", data=snap({"role": "window", "children": [node("@s1:e1")]}))

    def lock_then(spec, deadline=None):
        screen.sweep()
        return [dict(W1)]
    monkeypatch.setattr(screen, "_windows", lock_then)
    with pytest.raises(screen.ScreenError):
        screen.look("시험 화면")
    assert not screen._issued


def test_cleanup_failures_block_the_next_call_before_any_engine_starts(looking, monkeypatch):
    looking["env"] = env("snapshot", data=snap({"role": "window", "children": [node("@s1:e1", role="weird")]}))
    monkeypatch.setattr(screen, "_remove", lambda p: False)
    monkeypatch.setattr(screen, "_dirty_mark", lambda: Path("Z:/no/such/dir/screen.dirty"))
    with pytest.raises(screen.ScreenError):
        screen.look("시험 화면")
    started = []
    monkeypatch.setattr(screen.subprocess, "Popen", lambda *a, **k: started.append(1))
    with pytest.raises(screen.ScreenError):
        screen.run(["snapshot", "--window-id", "w-1"])
    assert not started and screen.sweep() is False
    monkeypatch.undo()
    screen._dirty = False


def test_look_refuses_apps_not_allowed(fake):
    with pytest.raises(screen.ScreenError):
        screen.look("메모장")


@pytest.fixture
def broker(isolated_settings, monkeypatch):
    if sys.platform != "win32":
        pytest.skip("이름 있는 파이프")
    from vader.core import screen_broker
    state = {"gen": "g1"}
    monkeypatch.setattr(screen, "authority", lambda: state["gen"])     # 앱 메모리의 열림
    monkeypatch.setattr(screen, "apps", lambda: {"시험 화면": {}})
    screen_broker.start()
    yield screen_broker, state
    screen_broker.stop()


def test_the_broker_answers_with_the_key_within_the_same_unlock(broker):
    from multiprocessing.connection import Client
    b, state = broker
    assert b.call({"op": "apps"}) == {"ok": True, "apps": ["시험 화면"]}
    assert b.call({"op": "rm"})["ok"] is False
    info = json.loads(b._file().read_text(encoding="utf-8"))
    c = Client(info["address"], family="AF_PIPE")                      # 키를 모르는 상대 — 답이 없다
    try:
        c.recv_bytes()
        c.send_bytes(b"x" * 32 + json.dumps({"op": "apps", "gen": "g1", "until": time.time() + 30}).encode())
        if c.poll(2):                                                  # 닫힐 뿐 — 받을 답은 없다
            c.recv_bytes()
            pytest.fail("틀린 키에 답했다")
    except (EOFError, OSError):
        pass
    finally:
        c.close()
    state["gen"] = None                                                # 잠김 — 키를 가진 직접 요청도 거절
    assert b.call({"op": "apps"})["ok"] is False
    assert b._handle({"op": "apps", "gen": "g1", "until": time.time() + 30})["ok"] is False
    state["gen"] = "g2"                                                # 다시 엶 — 잠금 전 세대의 요청은 거절
    assert b._handle({"op": "apps", "gen": "g1", "until": time.time() + 30})["ok"] is False
    for bad in (1, float("nan"), float("inf"), True, time.time() + 3600, "x"):          # 지남·비정상·너무 먼 미래
        assert b._handle({"op": "apps", "gen": "g2", "until": bad})["ok"] is False
    assert b._handle({"op": "apps", "gen": "g2", "until": time.time() + 30})["ok"] is True


def test_a_stalled_client_does_not_hold_the_broker(broker):
    from multiprocessing.connection import Client
    b, _ = broker
    info = json.loads(b._file().read_text(encoding="utf-8"))
    stalled = [Client(info["address"], family="AF_PIPE") for _ in range(2)]   # 연결만 하고 아무것도 안 보냄
    try:
        assert b.call({"op": "apps"})["ok"] is True
    finally:
        for c in stalled:
            c.close()


def test_a_reply_after_a_lock_is_withheld(broker, monkeypatch):
    b, state = broker

    called = []

    def look(app, deadline=None):
        called.append(app)
        state["gen"] = "g2"                                            # 처리 중 잠갔다 다시 엶
        return {"app": app, "nodes": []}
    monkeypatch.setattr(screen, "look", look)
    reply = b._handle({"op": "look", "app": "x", "gen": "g1", "until": time.time() + 30})
    assert called == ["x"] and reply == b._closed(screen.UNKNOWN)       # 세대가 바뀌어 닫힘 — 일반 오류가 아니다


def test_engine_start_failures_all_go_through_one_cleanup(fake, monkeypatch):
    """Job 에 못 넣거나 깨우지 못해도 엔진은 끝나고, 폴더는 지워지고, 추적이 남지 않는다 (codex1 r4 P1-2)."""
    fake["env"] = env("snapshot", data=snap())
    originals = {name: getattr(screen, name) for name in ("_in_job", "_resume")}

    def fail(proc):
        raise screen.ScreenError("묶지 못함")
    for broken in originals:
        monkeypatch.setattr(screen, broken, fail)
        with pytest.raises(screen.ScreenError):
            screen.run(["list-windows"])
        assert not screen._procs and not screen._dirty
        monkeypatch.setattr(screen, broken, originals[broken])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UIA")
def test_look_on_the_synthetic_app_leaks_nothing(isolated_settings, monkeypatch, tmp_path):
    try:
        screen.engine()
    except screen.ScreenError:
        pytest.skip("엔진 없음 — packaging/agent_desktop.ps1")
    monkeypatch.setattr(screen.gate, "session_gen", lambda: "g1")
    app = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(Path(__file__).parent / "fixtures" / "screen_app.ps1")], creationflags=screen.NO_WINDOW)
    try:
        got = None
        for _ in range(40):
            try:
                got = screen.look("시험 화면")
                break
            except screen.ScreenError:
                threading.Event().wait(.25)
        assert got and got["complete"]
        labels = {n["label"] for n in got["nodes"]}
        assert {"이름", "주민번호", "확인", "저장"} <= labels
        text = json.dumps(got, ensure_ascii=False)
        assert not any(p in text for p in PLANTED)
        logs = "".join(p.read_text(encoding="utf-8") for p in screen.journal.folder().rglob("*") if p.is_file())
        assert "screen_look" in logs and not any(p in logs for p in PLANTED)
    finally:
        app.kill()
        assert screen.sweep()


def test_mcp_screen_tools_answer_only_within_one_unlock(monkeypatch):
    pytest.importorskip("mcp", reason="pip install .[workshop]")
    from vader import mcp_server
    monkeypatch.setattr(mcp_server.screen_broker, "call", lambda req: {"ok": True, "apps": ["x"]})
    gens = iter(["g1", "g2"])
    monkeypatch.setattr(mcp_server.gate, "session_gen", lambda: next(gens))
    assert mcp_server.screen_apps() == {"ok": False, "refused": mcp_server.CLOSED}
    monkeypatch.setattr(mcp_server.gate, "session_gen", lambda: "g1")
    assert mcp_server.screen_apps() == {"ok": True, "apps": ["x"]}
    monkeypatch.setattr(mcp_server.gate, "session_gen", lambda: None)
    assert mcp_server.screen_look("x")["ok"] is False


def test_every_unlock_gets_a_new_generation_even_if_the_lock_mark_failed(isolated_settings, monkeypatch, tmp_path):
    """잠금 표식 쓰기가 실패해도 다시 풀면 새 세대 — 이전 열림의 요청이 잠금을 놓치지 않는다 (codex1 화면 r2 P1-2)."""
    from unittest.mock import MagicMock
    from vader.desktop import host
    written = []

    def mark(folder, unlocked, gen=""):
        if not unlocked:
            raise OSError("디스크")                        # 잠금 표식을 못 씀
        written.append(gen)
    monkeypatch.setattr(host.gate, "mark", mark)
    api = host.DesktopAPI(controller=MagicMock())
    api._unlock(tmp_path)
    api.lock()
    api._unlock(tmp_path)
    assert len(written) == 2 and all(written) and written[0] != written[1]
    monkeypatch.undo()
    screen.gate.mark(tmp_path, True, "g9")
    screen.gate.mark(tmp_path, False, "g9")
    assert json.loads((screen.config.HOME / "session.json").read_text(encoding="utf-8"))["gen"] == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object")
def test_engines_die_with_the_app(tmp_path):
    """앱 프로세스가 강제로 죽어도 Job 에 넣은 엔진은 OS 가 끝낸다 (codex1 r4 P1-2)."""
    code = (
        "import subprocess, sys, time\n"
        "from vader.core import screen\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=screen.SUSPENDED)\n"
        "screen._in_job(p); screen._resume(p)\n"
        "print(p.pid, flush=True); time.sleep(60)\n")
    app = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, cwd=str(Path(__file__).parents[1]))
    child = int(app.stdout.readline())
    app.kill()
    app.wait(10)
    import ctypes
    k32 = ctypes.windll.kernel32
    for _ in range(50):
        h = k32.OpenProcess(0x00100000, False, child)          # SYNCHRONIZE
        if not h or k32.WaitForSingleObject(h, 0) == 0:
            break
        k32.CloseHandle(h)
        threading.Event().wait(.1)
    else:
        pytest.fail("엔진이 앱보다 오래 산다")


def test_a_client_that_never_reads_its_reply_loses_its_slot(broker, monkeypatch):
    """답을 안 읽어 서버 송신이 막혀도 연결 수명이 지나면 슬롯이 돌아온다 (codex1 r5 P2-1)."""
    from multiprocessing.connection import Client
    b, _ = broker
    monkeypatch.setattr(b, "CONN_LIFE", 1.5)
    monkeypatch.setattr(screen, "apps", lambda: {f"앱{i:05d}" + "x" * 200: {} for i in range(2000)})   # 파이프 버퍼보다 큰 답
    info = json.loads(b._file().read_text(encoding="utf-8"))
    key = bytes.fromhex(info["key"])
    hogs = []
    for _ in range(b.MAX_CONN):
        c = Client(info["address"], family="AF_PIPE")
        nonce = c.recv_bytes()
        body = json.dumps({"op": "apps", "gen": "g1", "until": time.time() + 30}).encode()
        c.send_bytes(b._mac(key, nonce, body) + body)                 # 보내고 답은 안 읽는다
        hogs.append(c)
    try:
        for _ in range(60):
            if not b._conns:
                break
            threading.Event().wait(.1)
        assert not b._conns
        assert b.call({"op": "apps"})["ok"] is True
    finally:
        for c in hogs:
            c.close()


def test_no_engine_starts_after_the_deadline(fake, monkeypatch):
    fake["env"] = env("list-windows", data=[])
    started = []
    real = screen.subprocess.Popen
    monkeypatch.setattr(screen.subprocess, "Popen", lambda *a, **k: started.append(1) or real(*a, **k))
    with pytest.raises(screen.ScreenError):
        screen.run(["list-windows"], None, time.monotonic() - 1)
    assert not started


def test_a_look_whose_deadline_runs_out_is_not_published(looking, monkeypatch):
    looking["env"] = env("snapshot", data=snap({"role": "window", "children": [node("@s1:e1")]}))
    clock = {"t": 0.0}
    monkeypatch.setattr(screen.time, "monotonic", lambda: clock["t"])

    def slow(spec, deadline=None):
        clock["t"] = 100.0                      # 읽은 뒤 기한이 다 갔다
        return [dict(W1)]
    monkeypatch.setattr(screen, "_windows", slow)
    with pytest.raises(screen.ScreenError):
        screen.look("시험 화면", 50.0)
    assert not screen._issued


def test_expiry_during_preparation_never_starts_the_engine(fake, monkeypatch):
    """엔진 검사·준비 중에 기한이 지나면 띄우지 않고, Job 에 넣는 사이 지나면 깨우지 않는다 (codex1 r6 P2-2)."""
    fake["env"], fake["mode"] = env("list-windows", data=[]), "slow"
    clock = {"t": 0.0}
    monkeypatch.setattr(screen.time, "monotonic", lambda: clock["t"])
    real_engine, started, resumed = screen.engine, [], []
    monkeypatch.setattr(screen, "engine", lambda: (clock.update(t=10.0), real_engine())[1])
    real_popen = screen.subprocess.Popen
    monkeypatch.setattr(screen.subprocess, "Popen", lambda *a, **k: started.append(1) or real_popen(*a, **k))
    with pytest.raises(screen.ScreenError):
        screen.run(["list-windows"], None, 5.0)
    assert not started
    clock["t"] = 0.0
    monkeypatch.setattr(screen, "engine", real_engine)
    real_job = screen._in_job
    monkeypatch.setattr(screen, "_in_job", lambda proc: (clock.update(t=10.0), real_job(proc))[1])
    monkeypatch.setattr(screen, "_resume", lambda proc: resumed.append(1))
    with pytest.raises(screen.ScreenError):
        screen.run(["list-windows"], None, 5.0)
    assert started and not resumed and not screen._procs and not screen._dirty


def test_a_timed_out_engine_is_killed_before_its_output_is_collected(fake):
    """기한이 지나면 엔진부터 끝낸다 — 출력 회수(최대 10초)를 기다리는 동안 엔진이 더 돌지 않는다 (r6 P2-1)."""
    fake["mode"] = "slow"                                   # 파이프를 연 채 아무것도 안 쓰고 기다리는 엔진
    began = time.monotonic()
    with pytest.raises(screen.ScreenError) as got:
        screen.run(["list-windows"], None, time.monotonic() + 1)
    assert got.value.kind == screen.UNKNOWN and time.monotonic() - began < 4 and not screen._procs


# ── 2단계: 사람 승인 단일 조작 ──

def acted(cmd, delivery="delivered_verified"):
    return env(cmd, data={"action": cmd, "disposition": {"delivery": delivery, "retry": "unsafe"},
                          "surfaces": [{"title": PLANTED[4]}]})


@pytest.fixture
def seen(looking, monkeypatch):
    """마지막 관찰 s1: 저장 버튼 e1·이름 칸 e2·확인 체크 e3. 실행권은 늘 잡힌다."""
    from contextlib import nullcontext
    tree = {"role": "window", "children": [
        node("@s1:e1"), node("@s1:e2", role="textfield", name="이름", available_actions=["SetValue"]),
        node("@s1:e3", role="checkbox", name="확인", available_actions=["Click", "Toggle"])]}
    looking["env"] = env("snapshot", data=snap(tree))
    screen.look("시험 화면")
    looking["argv"] = []
    monkeypatch.setattr(screen, "hold", nullcontext)
    return looking


def approve(state, pid, yes=True):
    card = screen.pending()
    return screen.decide(pid, card["digest"] if card else "", yes)


def test_only_published_refs_and_fitting_actions_can_be_proposed(seen):
    bad = [("시험 화면", "@s1:e1", "toggle", None), ("시험 화면", "@s1:e1", "type", None),
           ("시험 화면", "@s1:e9", "click", None), ("시험 화면", "@s2:e1", "click", None),
           ("다른 앱", "@s1:e1", "click", None), ("시험 화면", "@s1:e1", "set_value", "x"),
           ("시험 화면", "@s1:e2", "click", None), ("시험 화면", "@s1:e3", "set_value", "x"),
           ("시험 화면", "@s1:e2", "set_value", "x" * 1001), ("시험 화면", "@s1:e2", "set_value", "a\x00b"),
           ("시험 화면", "@s1:e2", "set_value", None), ("시험 화면", "@s1:e1", "click", "x"),
           ("시험 화면", "s1:e1", "click", None)]
    for args in bad:
        with pytest.raises(screen.ScreenError):
            screen.propose(*args)
    assert screen.pending() is None and seen["argv"] == []
    seen["env"] = env("snapshot", data={**snap({"role": "window", "children": [node("@s1:e1")]}), "complete": False})
    screen.look("시험 화면")
    with pytest.raises(screen.ScreenError):          # 일부만 본 관찰로는 조작 못 한다
        screen.propose("시험 화면", "@s1:e1", "click")


def test_a_proposal_runs_only_once_after_a_human_approves(seen):
    pid = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
    assert seen["argv"] == [] and screen.outcome(pid)["result"] == "waiting"
    card = screen.pending()
    assert card["label"] == "저장" and card["action"] == "누르기" and card["value"] is None
    seen["env"] = acted("click")
    got = screen.decide(pid, card["digest"], True)
    assert got["result"] == screen.DONE and seen["argv"] == [["click", "@s1:e1", "--timeout-ms", "15000"]]
    assert PLANTED[4] not in json.dumps(got, ensure_ascii=False)          # 엔진 답의 창 제목은 안 나간다
    assert screen.outcome(pid)["result"] == screen.DONE
    with pytest.raises(screen.ScreenError):                               # 한 번만
        screen.decide(pid, card["digest"], True)
    assert not screen._issued and seen["argv"] == [["click", "@s1:e1", "--timeout-ms", "15000"]]   # 관찰도 버렸다 — 다음은 새로 읽기
    with pytest.raises(screen.ScreenError):
        screen.propose("시험 화면", "@s1:e1", "click")


def _menu_screen(looking, monkeypatch):
    """원무(e1) 아래 수가별발생조회(e2) · 원무 아래 말단 '삭제'(e5) · 따로 있는 '숨김'(e3)·이름 칸(e4)·저장 버튼(e6)."""
    from contextlib import nullcontext
    base = dict(screen.DEFAULT_APPS["시험 화면"])
    monkeypatch.setattr(screen, "apps", lambda: {"시험 화면": {**base, "labels": [*base["labels"], "원무", "수가별발생조회", "삭제"]}})
    menu = lambda ref, name, kids=(): {**node(ref, role="menuitem", name=name), "children": list(kids)}
    tree = {"role": "window", "children": [
        menu("@s1:e1", "원무", [menu("@s1:e2", "수가별발생조회"), menu("@s1:e5", "삭제")]), menu("@s1:e3", "숨김"),
        node("@s1:e4", role="textfield", name="이름", available_actions=["SetValue"]), node("@s1:e6", name="저장")]}
    looking["env"] = env("snapshot", data=snap(tree))
    screen.look("시험 화면")
    monkeypatch.setattr(screen, "hold", nullcontext)


def test_a_menu_path_opens_its_menus_and_clicks_the_item_after_one_approval(looking, monkeypatch):
    """이온엠: 펼쳐 둔 WinForms 메뉴는 승인 클릭에 포커스가 넘어가 닫힌다(실PC 2026-11-16). 그래서 via 로 경로째
    제안하고, 승인 한 번 뒤 상위 메뉴부터 차례로 누른다. 경로의 모든 칸은 사람이 정한 이름 · 메뉴 항목 · 누르기만이고,
    관찰한 트리에서 앞 항목이 다음 항목의 조상이어야 한다 (codex1 메뉴 경로 P1·D)."""
    _menu_screen(looking, monkeypatch)
    for ref, action, value, via in (("@s1:e2", "click", None, ["@s1:e3"]),          # 이름 없는 칸 · 계층 아님
                                    ("@s1:e2", "click", None, ["@s1:e4"]),          # 메뉴 항목이 아님
                                    ("@s1:e4", "set_value", "x", ["@s1:e1"]),       # 누르기만
                                    ("@s1:e2", "click", None, ["@s2:e1"]),          # 다른 관찰
                                    ("@s1:e2", "click", None, ["@s1:e2"]),          # 자기 자신
                                    ("@s1:e2", "click", None, ["@s1:e1"] * 4),      # 너무 깊음
                                    ("@s1:e2", "click", None, "@s1:e1"),            # 목록 아님
                                    ("@s1:e2", "click", None, [["@s1:e1"]]),        # 원소가 글자가 아님
                                    ("@s1:e6", "click", None, ["@s1:e5"]),          # 말단 메뉴 → 버튼 (업무 동작 묶음)
                                    ("@s1:e2", "click", None, ["@s1:e5"]),          # 형제 — 앞이 조상이 아님
                                    ("@s1:e1", "click", None, ["@s1:e2"])):         # 거꾸로
        with pytest.raises(screen.ScreenError):
            screen.propose("시험 화면", ref, action, value, via)
    via = ["@s1:e1"]
    pid = screen.propose("시험 화면", "@s1:e2", "click", None, via)["pending"]
    via.append("@s1:e3")                                                  # 승인할 순서는 복사본
    assert screen.pending()["label"] == "원무 › 수가별발생조회"
    looking["argv"], looking["env"] = [], acted("click")
    assert approve(looking, pid)["result"] == screen.DONE
    assert [a[:2] for a in looking["argv"]] == [["click", "@s1:e1"], ["click", "@s1:e2"]]


@pytest.mark.parametrize("answers, kind, calls, deep", [
    (["not_delivered"], screen.REFUSED, 1, False),                               # 첫 메뉴 미전달 — 아래는 안 누름
    ([screen.REFUSED], screen.REFUSED, 1, False),                                # 첫 메뉴 거절
    ([screen.UNKNOWN], screen.UNKNOWN, 1, False),                                # 첫 메뉴 모름
    (["delivered_unverified", screen.REFUSED], screen.UNKNOWN, 2, False),        # 앞 메뉴 보낸 뒤 끝 항목 거절
    (["delivered_unverified", "not_delivered"], screen.UNKNOWN, 2, False),       # 앞 메뉴 보낸 뒤 끝 항목 미전달
    (["delivered_unverified", "delivered_verified"], screen.DONE, 2, False),
    (["delivered_unverified", "not_delivered"], screen.UNKNOWN, 2, True),    # 두 단계 — 중간 메뉴 미전달, 끝은 안 누름
    (["delivered_unverified", screen.UNKNOWN], screen.UNKNOWN, 2, True),     # 중간 메뉴 모름
    (["delivered_unverified"] * 2 + ["delivered_verified"], screen.DONE, 3, True)])
def test_a_menu_path_result_reflects_what_was_already_sent(looking, monkeypatch, answers, kind, calls, deep):
    """위 메뉴를 이미 보냈으면 끝 항목이 실패·미전달이어도 '안 보냈다' 가 아니라 모름 (codex1 메뉴 경로 P2)."""
    _menu_screen(looking, monkeypatch)
    if deep:                                    # 원무 › 하위 › 항목
        base = dict(screen.DEFAULT_APPS["시험 화면"])
        monkeypatch.setattr(screen, "apps", lambda: {"시험 화면": {**base, "labels": [*base["labels"], "원무", "하위", "항목"]}})
        menu = lambda ref, name, kids=(): {**node(ref, role="menuitem", name=name), "children": list(kids)}
        looking["env"] = env("snapshot", data=snap({"role": "window", "children": [
            menu("@s1:e1", "원무", [menu("@s1:e2", "하위", [menu("@s1:e3", "항목")])])]}))
        screen.look("시험 화면")
    pid = screen.propose("시험 화면", "@s1:e3" if deep else "@s1:e2", "click", None,
                         ["@s1:e1", "@s1:e2"] if deep else ["@s1:e1"])["pending"]
    sent = []

    def run(argv, home=None, deadline=None, value=None, go=None, menu_path=False):
        answer = answers[len(sent)]
        sent.append(argv[:2])
        if answer in (screen.REFUSED, screen.UNKNOWN):
            raise screen.ScreenError("엔진 오류", answer)
        return {"action": "click", "disposition": {"delivery": answer}}
    monkeypatch.setattr(screen, "run", run)
    assert approve(looking, pid)["result"] == kind and len(sent) == calls


def test_a_value_goes_only_through_stdin(seen, tmp_path):
    secret = "가나메디칼 값 " + PLANTED[1]
    pid = screen.propose("시험 화면", "@s1:e2", "set_value", secret)["pending"]
    assert screen.pending()["value"] == secret                            # 사람은 그대로 본다
    seen["mode"], seen["sink"], seen["env"] = "stdin", str(tmp_path / "in.txt"), acted("set-value")
    assert approve(seen, pid)["result"] == screen.DONE
    assert seen["argv"] == [["set-value", "@s1:e2", "--timeout-ms", "15000", "--stdin"]] and (tmp_path / "in.txt").read_text(encoding="utf-8") == secret
    logs = "".join(f.read_text(encoding="utf-8") for f in screen.journal.folder().rglob("*") if f.is_file())
    assert "screen_act" in logs and secret not in logs and PLANTED[1] not in logs


def test_reject_expiry_changed_value_or_lock_never_sends(seen, monkeypatch):
    pid = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
    assert approve(seen, pid, yes=False)["result"] == screen.REFUSED and not screen._issued
    for spoil in ("expire", "digest", "gen", "relook"):
        seen["env"] = env("snapshot", data=snap({"role": "window", "children": [node("@s1:e1")]}))
        screen.look("시험 화면")
        pid = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
        digest = screen.pending()["digest"]
        seen["argv"] = []
        if spoil == "expire":
            screen._pending["until"] = time.monotonic() - 1
            assert screen.pending() is None
            with pytest.raises(screen.ScreenError):          # 이미 끝난 제안 — 승인할 것이 없다
                screen.decide(pid, digest, True)
            assert seen["argv"] == []
            continue
        elif spoil == "digest":
            digest = "0" * 64
        elif spoil == "gen":
            seen["gen"] = ["g2"]
        else:
            screen.look("시험 화면")                     # 새 관찰이 옛 ref 를 무효로
            seen["argv"] = []
        assert screen.decide(pid, digest, True)["result"] == screen.REFUSED, spoil
        assert seen["argv"] == [], spoil
        seen["gen"] = ["g1"]


def test_no_execution_right_or_a_changed_window_means_not_sent(seen, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def busy():
        raise ValueError("다른 작업이나 녹화가 진행 중이라 보내지 않았어요.")
        yield
    monkeypatch.setattr(screen, "hold", busy)
    pid = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
    assert approve(seen, pid)["result"] == screen.REFUSED and seen["argv"] == []
    from contextlib import nullcontext
    monkeypatch.setattr(screen, "hold", nullcontext)
    seen["env"] = env("snapshot", data=snap({"role": "window", "children": [node("@s1:e1")]}))
    screen.look("시험 화면")
    pid = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
    seen["argv"], seen["state_after"]["after"] = [], [{**W1, "process": "p10"}]   # 같은 창 id, 다른 프로세스
    assert approve(seen, pid)["result"] == screen.REFUSED and seen["argv"] == []


@pytest.mark.parametrize("delivery,code,kind", [("not_delivered", 0, screen.REFUSED), ("delivered_unverified", 0, screen.DONE),
                                                ("sent_maybe", 0, screen.UNKNOWN)])
def test_engine_delivery_decides_the_outcome(seen, delivery, code, kind):
    pid = screen.propose("시험 화면", "@s1:e3", "check")["pending"]
    seen["env"], seen["code"] = acted("check", delivery), code
    assert approve(seen, pid)["result"] == kind
    assert seen["argv"] == [["check", "@s1:e3", "--timeout-ms", "15000"]]                     # 다시 시도하지 않는다


def test_results_are_seen_only_within_the_same_unlock(seen):
    pid = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
    seen["gen"] = ["g2"]
    assert screen.pending() is None
    with pytest.raises(screen.ScreenError):
        screen.outcome(pid)
    seen["gen"] = ["g1"]
    screen.sweep()                                                   # 잠금은 승인 대기도 버린다
    assert screen.pending() is None


def test_broker_routes_proposals_and_results_but_never_approval(monkeypatch):
    from vader.core import screen_broker as b
    monkeypatch.setattr(screen, "current_gen", lambda: "g1")
    got = []
    monkeypatch.setattr(screen, "propose", lambda app, ref, action, value, via: got.append(via) or {"pending": "p1"})
    monkeypatch.setattr(screen, "outcome", lambda pid: {"result": "waiting"})
    req = {"gen": "g1", "until": time.time() + 30}
    assert b._handle({**req, "op": "act", "app": "x", "ref": "@s1:e1", "action": "click"}) == {"ok": True, "pending": "p1"}
    assert b._handle({**req, "op": "act", "app": "x", "ref": "@s1:e2", "action": "click", "via": ["@s1:e1"]})["ok"]
    assert got == [None, ["@s1:e1"]]                                      # 메뉴 경로가 그대로 간다
    assert b._handle({**req, "op": "result", "id": "p1"}) == {"ok": True, "result": "waiting"}
    for op in ("decide", "approve", "pending"):
        assert b._handle({**req, "op": op, "id": "p1"})["ok"] is False


def test_the_controller_shares_one_execution_right(tmp_path):
    from vader.desktop.controller import DesktopController
    c = DesktopController(None, loaded=[])
    with c.screen_hold():
        with pytest.raises(ValueError):
            with c.screen_hold():
                pass
        with pytest.raises(ValueError):
            c.submit("x", [])
    c.recording = {"x": 1}
    with pytest.raises(ValueError):
        with c.screen_hold():
            pass
    c.recording = None
    with c.screen_hold():
        pass
    assert c.screen_acting is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UIA")
def test_approved_actions_on_the_synthetic_app(isolated_settings, monkeypatch):
    from contextlib import nullcontext
    try:
        screen.engine()
    except screen.ScreenError:
        pytest.skip("엔진 없음 — packaging/agent_desktop.ps1")
    monkeypatch.setattr(screen.gate, "session_gen", lambda: "g1")
    monkeypatch.setattr(screen, "hold", nullcontext)
    app = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(Path(__file__).parent / "fixtures" / "screen_app.ps1")], creationflags=screen.NO_WINDOW)
    try:
        def fresh():
            for _ in range(40):
                try:
                    return {n["label"]: n["ref"] for n in screen.look("시험 화면")["nodes"]}
                except screen.ScreenError:
                    threading.Event().wait(.25)
        for label, action, value, want in (("확인", "check", None, screen.DONE), ("확인", "check", None, screen.REFUSED),
                                           ("이름", "set_value", "다라상사가짜", screen.DONE), ("저장", "click", None, screen.DONE)):
            pid = screen.propose("시험 화면", fresh()[label], action, value)["pending"]
            got = approve(None, pid)
            assert got["result"] == want, (label, action, got)
        logs = "".join(p.read_text(encoding="utf-8") for p in screen.journal.folder().rglob("*") if p.is_file())
        assert "screen_act" in logs and not any(p in logs for p in PLANTED) and "다라상사가짜" not in logs
    finally:
        app.kill()
        assert screen.sweep()


def test_an_unanswered_proposal_ends_as_refused_after_60_seconds(seen):
    """카드만 숨지 않는다 — 결과도 끝난 상태(refused), 값·관찰도 버린다 (codex1 2단계 r1 P2-1)."""
    pid = screen.propose("시험 화면", "@s1:e2", "set_value", "다라상사가짜")["pending"]
    screen._pending["until"] = time.monotonic() - 1
    for _ in range(3):
        assert screen.outcome(pid)["result"] == screen.REFUSED
    assert screen._pending is None and not screen._issued and seen["argv"] == []


def test_while_sending_the_result_is_running_not_refused(seen, monkeypatch):
    """승인해 보내는 동안 결과를 물으면 running — 끝나면 같은 id 가 done (r1 P2-2)."""
    pid = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
    digest = screen.pending()["digest"]
    entered, release, seen_mid = threading.Event(), threading.Event(), []
    real = screen._act

    def slow(p, deadline):
        entered.set()
        release.wait(5)
        return real(p, deadline)
    monkeypatch.setattr(screen, "_act", slow)
    seen["env"] = acted("click")
    t = threading.Thread(target=lambda: seen_mid.append(screen.decide(pid, digest, True)))
    t.start()
    assert entered.wait(5)
    assert screen.outcome(pid)["result"] == "running"
    release.set()
    t.join(10)
    assert seen_mid[0]["result"] == screen.DONE and screen.outcome(pid)["result"] == screen.DONE


def test_a_decision_that_could_not_start_leaves_the_card_waiting(seen, monkeypatch):
    """관찰이 오래 걸려 승인을 못 받았으면 제안은 그대로 — 사람이 다시 누를 수 있다 (r1 P2-3)."""
    monkeypatch.setattr(screen, "RUN_AFTER", 0.2)
    pid = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
    digest = screen.pending()["digest"]
    with screen._look_lock:                                   # 다른 관찰이 붙잡고 있다
        got = []
        t = threading.Thread(target=lambda: got.append(pytest.raises(screen.ScreenError, screen.decide, pid, digest, True)))
        t.start()
        t.join(5)
    assert screen.pending()["id"] == pid and screen.outcome(pid)["result"] == "waiting" and seen["argv"] == []
    monkeypatch.setattr(screen, "RUN_AFTER", 15)
    seen["env"] = acted("click")
    assert screen.decide(pid, digest, True)["result"] == screen.DONE


def test_cleaning_an_old_proposal_never_touches_a_newer_observation(seen):
    """같은 snapshot id 의 새 관찰은 옛 제안의 만료·거절 정리에 남는다 (codex1 2단계 r2 NEW-P2-2)."""
    old = screen.propose("시험 화면", "@s1:e1", "click")
    stale = screen._pending
    screen.look("시험 화면")                                        # 같은 s1, 다른 관찰 객체
    newer = screen._issued["s1"]
    assert newer is not stale["seen"]
    screen._pending = stale                                         # 옛 제안이 아직 대기 중인 경우
    stale["until"] = time.monotonic() - 1
    assert screen.outcome(old["pending"])["result"] == screen.REFUSED
    assert screen._issued["s1"] is newer and screen.propose("시험 화면", "@s1:e1", "click")["pending"]


def test_expiry_detaches_the_observation_in_the_same_step(seen, monkeypatch):
    """만료가 제안을 꺼낸 뒤 관찰을 지우기 전에, 같은 관찰로 새 제안이 들어오지 못한다 (r2 NEW-P2-1)."""
    pid = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
    screen._pending["until"] = time.monotonic() - 1
    hit, go, got = threading.Event(), threading.Event(), []
    real = screen._drop

    def slow(seen_):
        hit.set()
        go.wait(5)
        real(seen_)
    monkeypatch.setattr(screen, "_drop", slow)
    t = threading.Thread(target=screen.pending)
    t.start()
    assert hit.wait(5)
    with pytest.raises(screen.ScreenError):                         # 이미 떼어졌다
        screen.propose("시험 화면", "@s1:e1", "click")
    go.set()
    t.join(5)
    assert screen.outcome(pid)["result"] == screen.REFUSED


def test_no_new_proposal_while_one_is_being_sent(seen):
    pid = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
    screen._running, screen._pending = screen._pending, None
    with pytest.raises(screen.ScreenError):
        screen.propose("시험 화면", "@s1:e1", "click")
    assert screen.outcome(pid)["result"] == "running"
    screen.sweep()
    assert screen._running is None


def test_a_slow_expiry_cleanup_never_overwrites_a_later_result(seen, monkeypatch):
    """만료 A 의 폴더 정리가 늦어도, 그 사이 끝난 B 의 결과(done)는 남는다 (codex1 2단계 r3 NEW-P2-1)."""
    a = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
    screen._pending["until"] = time.monotonic() - 1
    hit, go = threading.Event(), threading.Event()
    real = screen._drop

    def slow(seen_):
        if seen_ is not None and not hit.is_set():
            hit.set()
            go.wait(5)
        real(seen_)
    monkeypatch.setattr(screen, "_drop", slow)
    t = threading.Thread(target=lambda: screen.pending())              # 카드 폴링이 A 를 만료 정리
    t.start()
    assert hit.wait(5)
    screen.look("시험 화면")
    b = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
    seen["env"] = acted("click")
    assert approve(seen, b)["result"] == screen.DONE
    go.set()
    t.join(5)
    assert screen.outcome(b)["result"] == screen.DONE


# ── 3a 셀렉터 진단 ──

def rec(steps, fmt="vader-recording/1"):
    return json.dumps({"format": fmt, "steps": steps}, ensure_ascii=False)


@pytest.fixture
def rune_dir(tmp_path, isolated_settings):
    """제작소 프로젝트 'rune' — 사람이 연결한 원본 폴더(meta source)로."""
    from vader.core import workshop
    home = tmp_path / "rune"
    (home / "recordings").mkdir(parents=True)
    workshop._write_meta("rune", {"source": str(home)})
    return home


def aid(value):
    return {"native_id": {"kind": "automation_id", "value": value}}


def test_profile_rejects_click_batches_and_ambiguous_selectors(isolated_settings, monkeypatch):
    base = dict(screen.DEFAULT_APPS["시험 화면"])
    assert set(screen.profile("시험 화면")) == {"이름 칸", "확인 체크", "저장 버튼"}
    for bad in ({"x": {"role": "button", "label": "저장", "batch": ["click"]}},
                {"x": {"role": "button", "label": "저장", "aid": "a"}},
                {"x": {"role": "button"}}, {"x": {"role": "button", "label": "승인 안 된 글자"}},
                {"x": {"role": "nope", "aid": "a"}}, {"x": {"role": "button", "aid": ""}}, ["x"]):
        monkeypatch.setattr(screen, "apps", lambda b=bad: {"시험 화면": {**base, "targets": b}})
        with pytest.raises(screen.ScreenError):
            screen.profile("시험 화면")


def test_a_profile_target_names_the_one_field_it_matches(looking, monkeypatch):
    """이온엠: 엔진 패치가 입력칸에 kind parent_automation_id 로 '부모/자기' id 를 준다(txtSugaCd/m_pTextBox). 프로필
    대상과 kind·값이 딱 하나 맞는 '역할#번호' 칸만 카드에 대상 이름으로 보이고, 두 대상이 같은 칸이면 어느 이름도
    안 붙는다. 같은 글자의 plain automation_id 칸은 다른 칸이다 (실PC 2026-11-16, codex1 회신 3)."""
    base = dict(screen.DEFAULT_APPS["시험 화면"])
    scoped = lambda a: {"role": "textfield", "kind": "parent_automation_id", "aid": a, "batch": []}
    targets = {"처방코드 칸": scoped("txtSugaCd/m_pTextBox"), "청구 칸": scoped("txtClaimCd/m_pTextBox"),
               "청구 칸 둘": scoped("txtClaimCd/m_pTextBox"), "위장 칸": scoped("a/b")}
    monkeypatch.setattr(screen, "apps", lambda: {"시험 화면": {**base, "targets": targets}})
    box = lambda ref, kind, a: node(ref, role="textfield", name="", available_actions=["SetValue"],
                                    native_id={"kind": kind, "value": a})
    tree = {"role": "window", "children": [box("@s1:e1", "parent_automation_id", "txtSugaCd/m_pTextBox"),
                                           box("@s1:e2", "parent_automation_id", "txtClaimCd/m_pTextBox"),
                                           box("@s1:e3", "automation_id", "m_pTextBox"),
                                           box("@s1:e4", "automation_id", "a/b")]}
    looking["env"] = env("snapshot", data=snap(tree))
    got = screen.look("시험 화면")
    assert [n["label"] for n in got["nodes"]] == ["처방코드 칸", "textfield#2", "textfield#3", "textfield#4"]
    screen.propose("시험 화면", "@s1:e1", "set_value", "TEST000")
    assert screen.pending()["label"] == "처방코드 칸"
    assert screen._pick({"control_type": "Edit", "automation_id": "a/b"}, screen.profile("시험 화면")) is None
    with pytest.raises(screen.ScreenError):
        monkeypatch.setattr(screen, "apps", lambda: {"시험 화면": {**base, "targets": {
            "x": {"role": "textfield", "kind": "runtime_id", "aid": "1", "batch": []}}}})
        screen.profile("시험 화면")


def test_diagnose_compares_the_screen_only_through_the_profile(looking, rune_dir):
    tree = {"role": "window", "children": [
        node("@s1:e1", role="textfield", name="이름", available_actions=["SetValue"], **aid("nameBox")),
        node("@s1:e2", role="checkbox", name="확인", available_actions=["Toggle"]),
        node("@s1:e3", role="checkbox", name="확인", available_actions=["Toggle"]),
        node("@s1:e4", role="textfield", name=PLANTED[0], **aid(PLANTED[1])),
    ]}
    looking["env"] = env("snapshot", data=snap(tree))
    (rune_dir / "recordings" / "nav.json").write_text(rec([
        {"at": "t", "control_type": "Edit", "automation_id": "nameBox", "name": PLANTED[0]},   # 프로필 aid → 이름 칸
        {"at": "t", "control_type": "CheckBox", "name": "확인"},                                 # 화면에 둘
        {"at": "t", "control_type": "Edit", "automation_id": PLANTED[1]},                        # 추측 — 프로필에 없음
        {"at": "t", "control_type": "Button", "name": "저장"},                                   # 아직 화면에 없음
        {"at": "t", "keys": "{TAB}"}]), encoding="utf-8")
    got = screen.diagnose("시험 화면", "rune", "nav")
    assert [(x["kind"], x["target"], x["matches"]) for x in got["steps"]] == [
        ("click", "이름 칸", 1), ("click", "확인 체크", "many"), ("click", None, None),
        ("click", "저장 버튼", 0), ("key", None, None)]
    assert not any(p in json.dumps(got, ensure_ascii=False) for p in PLANTED)
    assert not screen._issued                                        # 진단 관찰은 게시하지 않는다


def test_diagnose_on_a_partial_view_counts_nothing(looking, rune_dir):
    looking["env"] = env("snapshot", data={**snap({"role": "window", "children": [node("@s1:e1")]}), "complete": False})
    (rune_dir / "recordings" / "nav.json").write_text(rec([{"at": "t", "control_type": "Button", "name": "저장"}]),
                                                      encoding="utf-8")
    got = screen.diagnose("시험 화면", "rune", "nav")
    assert got["complete"] is False and got["steps"][0]["matches"] is None


def test_recording_files_are_read_strictly(looking, rune_dir):
    looking["env"] = env("snapshot", data=snap())
    r = rune_dir / "recordings"
    cases = {"fmt": rec([], fmt="other"), "steps": json.dumps({"format": "vader-recording/1", "steps": {}}),
             "field": rec([{"at": "t", "nope": 1}]), "type": rec([{"at": 1}]), "long": rec([{"at": "x" * 501}]),
             "num": rec([{"at": "t", "rel_x": True}]), "many": rec([{"at": "t"}] * 501), "json": "{"}
    for name, body in cases.items():
        (r / f"{name}.json").write_text(body, encoding="utf-8")
        with pytest.raises(screen.ScreenError):
            screen.diagnose("시험 화면", "rune", name)
    (r / "big.json").write_text(rec([]) + " " * screen.MAX_RECORDING, encoding="utf-8")
    for name in ("big", "../x", "Nav", "missing"):
        with pytest.raises(screen.ScreenError):
            screen.diagnose("시험 화면", "rune", name)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 정션")
def test_a_recordings_junction_out_of_the_rune_is_refused(looking, rune_dir, tmp_path):
    import _winapi
    looking["env"] = env("snapshot", data=snap())
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "nav.json").write_text(rec([]), encoding="utf-8")
    (rune_dir / "recordings").rmdir()
    _winapi.CreateJunction(str(outside), str(rune_dir / "recordings"))
    with pytest.raises(screen.ScreenError):
        screen.diagnose("시험 화면", "rune", "nav")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UIA")
def test_diagnose_on_the_synthetic_app(isolated_settings, monkeypatch, rune_dir):
    try:
        screen.engine()
    except screen.ScreenError:
        pytest.skip("엔진 없음 — packaging/agent_desktop.ps1")
    monkeypatch.setattr(screen.gate, "session_gen", lambda: "g1")
    (rune_dir / "recordings" / "nav.json").write_text(rec([
        {"at": "t", "control_type": "Edit", "automation_id": "nameBox", "name": "이름"},
        {"at": "t", "control_type": "CheckBox", "name": "확인"},
        {"at": "t", "control_type": "Button", "name": "저장"}]), encoding="utf-8")
    app = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(Path(__file__).parent / "fixtures" / "screen_app.ps1")], creationflags=screen.NO_WINDOW)
    try:
        got = None
        for _ in range(40):
            try:
                got = screen.diagnose("시험 화면", "rune", "nav")
                break
            except screen.ScreenError:
                threading.Event().wait(.25)
        assert got and got["complete"]
        assert [(x["target"], x["matches"]) for x in got["steps"]] == [("이름 칸", 1), ("확인 체크", 1), ("저장 버튼", 1)]
        assert not any(p in json.dumps(got, ensure_ascii=False) for p in PLANTED)
    finally:
        app.kill()
        assert screen.sweep()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 정션")
def test_a_junction_at_the_package_or_project_root_is_refused(looking, isolated_settings, tmp_path):
    """제작소 사본의 룬 폴더·프로젝트 폴더가 정션이면 밖을 읽지 않는다. 사람이 연결한 원본은 된다 (3a r1 P1-1)."""
    import _winapi
    from vader.core import workshop
    looking["env"] = env("snapshot", data=snap({"role": "window"}))
    outside = tmp_path / "outside"
    (outside / "recordings").mkdir(parents=True)
    (outside / "recordings" / "nav.json").write_text(rec([]), encoding="utf-8")
    workshop.root().mkdir(parents=True, exist_ok=True)
    (workshop.root() / "ra").mkdir()
    _winapi.CreateJunction(str(outside), str(workshop.root() / "ra" / "ra"))       # 패키지 폴더가 정션
    _winapi.CreateJunction(str(tmp_path), str(workshop.root() / "rb"))             # 프로젝트 폴더가 정션
    (tmp_path / "rb").mkdir()
    (tmp_path / "rb" / "recordings").mkdir()
    (tmp_path / "rb" / "recordings" / "nav.json").write_text(rec([]), encoding="utf-8")
    for rid in ("ra", "rb"):
        with pytest.raises(screen.ScreenError):
            screen.diagnose("시험 화면", rid, "nav")
    (workshop.root() / "rc").mkdir()
    workshop._write_meta("rc", {"source": str(outside)})                             # 사람이 연결한 원본
    assert screen.diagnose("시험 화면", "rc", "nav")["steps"] == []
    (workshop.root() / "rd" / "rd" / "recordings").mkdir(parents=True)                 # 보통 사본
    (workshop.root() / "rd" / "rd" / "recordings" / "nav.json").write_text(rec([]), encoding="utf-8")
    assert screen.diagnose("시험 화면", "rd", "nav")["steps"] == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 정션")
def test_link_info_outside_the_workshop_is_not_trusted(looking, isolated_settings, tmp_path):
    """정션 밖 project.json 의 source, 링크된 project.json 은 연결 원본으로 인정하지 않는다 (3a r2 P1-1)."""
    import _winapi
    from vader.core import workshop
    looking["env"] = env("snapshot", data=snap({"role": "window"}))
    src = tmp_path / "src"
    (src / "recordings").mkdir(parents=True)
    (src / "recordings" / "nav.json").write_text(rec([]), encoding="utf-8")
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "project.json").write_text(json.dumps({"source": str(src)}), encoding="utf-8")
    workshop.root().mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(ext), str(workshop.root() / "re"))                   # 프로젝트가 정션 → 밖 메타
    with pytest.raises(screen.ScreenError):
        screen.diagnose("시험 화면", "re", "nav")
    metas = tmp_path / "metas"
    metas.mkdir()
    (metas / "project.json").write_text(json.dumps({"source": str(src)}), encoding="utf-8")
    (workshop.root() / "rf").mkdir()
    _winapi.CreateJunction(str(metas), str(workshop.root() / "rf" / "m"))
    try:
        (workshop.root() / "rf" / "project.json").symlink_to(metas / "project.json")  # 메타 파일 자체가 링크
    except OSError:
        pytest.skip("이 PC 는 파일 심볼릭 링크를 못 만든다 (개발자 모드 꺼짐)")
    with pytest.raises(screen.ScreenError):
        screen.diagnose("시험 화면", "rf", "nav")
    (workshop.root() / "rg").mkdir()
    workshop._write_meta("rg", {"source": str(src)})                                 # 정상 연결은 된다
    assert screen.diagnose("시험 화면", "rg", "nav")["steps"] == []


# ── 3b 묶음 승인 ──

def plan_tree(checks=1):
    return {"role": "window", "children": [
        node("@s1:e1", role="textfield", name="이름", available_actions=["SetValue"], **aid("nameBox")),
        *[node(f"@s1:e{2 + i}", role="checkbox", name="확인", available_actions=["Click", "Toggle"]) for i in range(checks)],
        node("@s1:e9", name="저장")]}


@pytest.fixture
def planning(looking, monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(screen, "hold", nullcontext)
    looking["envs"] = {"snapshot": env("snapshot", data=snap(plan_tree())), "set-value": acted("set-value"),
                       "check": acted("check"), "uncheck": acted("uncheck"), "click": acted("click")}
    looking["argv"] = []
    return looking


STEPS = [{"target": "이름 칸", "action": "set_value", "value": "다라상사가짜"},
         {"target": "확인 체크", "action": "check"}, {"target": "저장 버튼", "action": "click"}]


def approved(steps=STEPS, minutes=2):
    pid = screen.plan_propose("시험 화면", steps, minutes)["plan"]
    card = screen.plan_card()
    assert card["kind"] == "ask" and screen.plan_decide(pid, card["digest"], True)["result"] == screen.DONE
    return pid


def acts(state):
    return [a[0] for a in state["argv"] if a[0] != "snapshot" and a[0] != "list-windows"]


def test_plan_proposals_are_checked_against_the_profile(planning):
    bad = [([{"target": "없는 대상", "action": "click"}], 2), ([{"target": "저장 버튼", "action": "set_value", "value": "x"}], 2),
           ([{"target": "확인 체크", "action": "set_value", "value": "x"}], 2), ([{"target": "저장 버튼", "action": "click", "value": "x"}], 2),
           ([{"target": "저장 버튼", "action": "toggle"}], 2), ([{"target": "저장 버튼", "action": "click", "ref": "@s1:e9"}], 2),
           ([{"target": "이름 칸", "action": "set_value", "value": "x" * 501}], 2), ([STEPS[1]] * 11, 2), ([], 2),
           ([{"target": "이름 칸", "action": "set_value", "value": "x" * 500}] * 5, 2)]
    bad += [(STEPS, m) for m in (True, 0, -1, 6, float("nan"), float("inf"), "2")]
    for steps, minutes in bad:
        with pytest.raises(screen.ScreenError):
            screen.plan_propose("시험 화면", steps, minutes)
    assert screen.plan_card() is None and acts(planning) == []


def test_a_plan_runs_in_order_and_clicks_wait_for_a_human(planning, tmp_path):
    pid = approved()
    planning["mode"], planning["sink"] = "stdin", str(tmp_path / "in.txt")
    assert screen.step(pid, 0)["result"] == screen.DONE and acts(planning) == ["set-value"]
    assert (tmp_path / "in.txt").read_text(encoding="utf-8") == "다라상사가짜"
    assert screen.step(pid, 0)["result"] == screen.DONE and acts(planning) == ["set-value"]   # 다시 안 보낸다
    with pytest.raises(screen.ScreenError):
        screen.step(pid, 2)                                                                     # 순서 밖
    for bad in (True, -1, "1", 1.0):
        with pytest.raises(screen.ScreenError):
            screen.step(pid, bad)
    planning["mode"] = "env"
    assert screen.step(pid, 1)["result"] == screen.DONE and acts(planning) == ["set-value", "check"]
    got = screen.step(pid, 2)
    assert got["result"] == "confirming" and acts(planning) == ["set-value", "check"]           # click 은 카드
    assert screen.step(pid, 2)["result"] == "confirming"                                        # 카드 하나 그대로
    card = screen.pending()
    assert card["label"] == "저장 버튼" and card["plan"] == "묶음 3/3 단계"
    assert screen.decide(card["id"], card["digest"], True)["result"] == screen.DONE
    status = screen.outcome(pid)
    assert status["result"] == "complete" and [r["result"] for r in status["steps"]] == [screen.DONE] * 3
    assert acts(planning) == ["set-value", "check", "click"] and screen.plan_card() is None


def test_an_unverified_or_failed_step_ends_the_plan(planning):
    pid = approved()
    planning["envs"]["set-value"] = acted("set-value", "delivered_unverified")
    assert screen.step(pid, 0)["result"] == screen.DONE
    status = screen.outcome(pid)
    assert status["result"] == "stopped" and status["steps"][0]["delivery"] == "delivered_unverified"
    assert screen.step(pid, 1)["result"] == "stopped" and acts(planning) == ["set-value"]


def test_a_target_that_is_not_unique_or_an_owner_change_stops_the_plan(planning, monkeypatch):
    pid = approved()
    planning["envs"]["snapshot"] = env("snapshot", data=snap(plan_tree(checks=2)))
    assert screen.step(pid, 0)["result"] == screen.DONE
    assert screen.step(pid, 1)["result"] == "stopped" and acts(planning) == ["set-value"]       # 확인 체크가 둘
    planning["envs"]["snapshot"] = env("snapshot", data=snap(plan_tree()))
    pid = approved()
    other = {**W1, "process": "p10"}
    monkeypatch.setattr(screen, "_target", lambda key, deadline=None: dict(other))
    planning["state_after"]["after"] = [other]
    planning["argv"] = []
    assert screen.step(pid, 0)["result"] == "stopped" and acts(planning) == []


def test_interventions_end_the_plan(planning, monkeypatch):
    for intervene in ("single", "stop", "new_plan", "profile", "expire", "sweep"):
        pid = approved()
        if intervene == "single":
            screen.look("시험 화면")
            screen.propose("시험 화면", "@s1:e9", "click")
        elif intervene == "stop":
            assert screen.stop_plan() is True
        elif intervene == "new_plan":
            screen.plan_propose("시험 화면", STEPS, 2)
        elif intervene == "profile":
            base = dict(screen.DEFAULT_APPS["시험 화면"])
            changed = {**base, "targets": {**base["targets"], "저장 버튼": {"role": "button", "label": "확인", "batch": []}}}
            monkeypatch.setattr(screen, "apps", lambda: {"시험 화면": changed})
        elif intervene == "expire":
            screen._plan["until"] = time.monotonic() - 1
        else:
            screen.sweep()
        planning["argv"] = []
        if intervene == "sweep":
            with pytest.raises(screen.ScreenError):
                screen.step(pid, 0)
        else:
            assert screen.step(pid, 0)["result"] == "stopped", intervene
        assert acts(planning) == [], intervene
        monkeypatch.setattr(screen, "apps", lambda: {"시험 화면": screen.DEFAULT_APPS["시험 화면"]})
        screen._clear_pending()


def test_the_controller_ends_a_plan_when_a_job_or_recording_starts(planning):
    from vader.desktop.controller import DesktopController
    approved()
    with pytest.raises(Exception):
        DesktopController(None, loaded=[]).submit("x", [])
    assert screen._plan["state"] == "stopped"


def test_a_confirm_card_never_outlives_the_plan_and_reject_or_expiry_stops_it(planning):
    pid = approved(steps=[STEPS[2]], minutes=0.5)
    assert screen.step(pid, 0)["result"] == "confirming"
    assert screen._pending["until"] <= screen._plan["until"]
    card = screen.pending()
    assert screen.decide(card["id"], card["digest"], False)["result"] == screen.REFUSED
    assert screen.outcome(pid)["result"] == "stopped" and acts(planning) == []
    pid = approved(steps=[STEPS[2]])
    assert screen.step(pid, 0)["result"] == "confirming"
    screen._pending["until"] = time.monotonic() - 1
    assert screen.pending() is None and screen.outcome(pid)["result"] == "stopped" and acts(planning) == []


def test_a_stopped_plan_refuses_a_late_confirmation(planning):
    pid = approved(steps=[STEPS[2]])
    assert screen.step(pid, 0)["result"] == "confirming"
    card = screen.pending()
    screen.stop_plan()
    with pytest.raises(screen.ScreenError):                          # 카드도 같이 사라졌다
        screen.decide(card["id"], card["digest"], True)
    assert acts(planning) == [] and screen.outcome(pid)["result"] == "stopped"


def test_a_step_being_sent_answers_running(planning, monkeypatch):
    pid = approved()
    entered, release, got = threading.Event(), threading.Event(), []
    real = screen._act

    def slow(p, deadline):
        entered.set()
        release.wait(5)
        return real(p, deadline)
    monkeypatch.setattr(screen, "_act", slow)
    t = threading.Thread(target=lambda: got.append(screen.step(pid, 0)))
    t.start()
    assert entered.wait(5)
    assert screen.step(pid, 0)["result"] == "running" and screen.outcome(pid)["result"] == "executing"
    with pytest.raises(screen.ScreenError):
        screen.propose("시험 화면", "@s1:e9", "click")               # 보내는 중엔 다른 제안 안 받음
    release.set()
    t.join(10)
    assert got[0]["result"] == screen.DONE and acts(planning) == ["set-value"]


def test_the_broker_proposes_and_steps_plans_but_never_approves(monkeypatch):
    from vader.core import screen_broker as b
    monkeypatch.setattr(screen, "current_gen", lambda: "g1")
    monkeypatch.setattr(screen, "plan_propose", lambda app, steps, minutes: {"plan": "p1"})
    monkeypatch.setattr(screen, "step", lambda plan, k, deadline: {"result": "running"})
    req = {"gen": "g1", "until": time.time() + 30}
    assert b._handle({**req, "op": "plan", "app": "x", "steps": [], "minutes": 1}) == {"ok": True, "plan": "p1"}
    assert b._handle({**req, "op": "step", "plan": "p1", "step": 0}) == {"ok": True, "result": "running"}
    for op in ("plan_decide", "plan_card", "stop_plan"):
        assert b._handle({**req, "op": op, "id": "p1"})["ok"] is False


def test_an_oversized_request_is_refused_before_sending(broker):
    b, _ = broker
    got = b.call({"op": "plan", "app": "x", "steps": [{"value": "x" * (b.MAX_REQUEST + 1)}]})
    assert got["ok"] is False and "너무 커요" in got["refused"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UIA")
def test_a_plan_on_the_synthetic_app(isolated_settings, monkeypatch):
    from contextlib import nullcontext
    try:
        screen.engine()
    except screen.ScreenError:
        pytest.skip("엔진 없음 — packaging/agent_desktop.ps1")
    monkeypatch.setattr(screen.gate, "session_gen", lambda: "g1")
    monkeypatch.setattr(screen, "hold", nullcontext)
    app = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(Path(__file__).parent / "fixtures" / "screen_app.ps1")], creationflags=screen.NO_WINDOW)
    try:
        for _ in range(40):
            try:
                screen.look("시험 화면")
                break
            except screen.ScreenError:
                threading.Event().wait(.25)
        pid = approved()
        assert screen.step(pid, 0)["result"] == screen.DONE
        assert screen.step(pid, 1)["result"] == screen.DONE
        assert screen.step(pid, 2)["result"] == "confirming"
        card = screen.pending()
        assert screen.decide(card["id"], card["digest"], True)["result"] == screen.DONE
        assert screen.outcome(pid)["result"] == "complete"
        logs = "".join(p.read_text(encoding="utf-8") for p in screen.journal.folder().rglob("*") if p.is_file())
        assert "screen_plan" in logs and not any(p in logs for p in PLANTED)
    finally:
        app.kill()
        assert screen.sweep()



@pytest.mark.parametrize("intervene", ["new_plan", "single", "stop", "job"])
def test_an_old_approval_being_confirmed_never_revives_after_an_intervention(planning, monkeypatch, intervene):
    """계획 A 승인이 창 확인 중일 때 새 계획·단일 제안·중단·룬 작업이 끼면 A 는 살아나지 않는다 (3b r1 P1-1)."""
    a = screen.plan_propose("시험 화면", STEPS, 2)["plan"]
    digest = screen.plan_card()["digest"]
    entered, release, got = threading.Event(), threading.Event(), []
    real = screen._target

    def slow(key, deadline=None):
        if not entered.is_set():
            entered.set()
            release.wait(5)
        return real(key, deadline)
    monkeypatch.setattr(screen, "_target", slow)
    t = threading.Thread(target=lambda: got.append(screen.plan_decide(a, digest, True)))
    t.start()
    assert entered.wait(5)
    b = None
    if intervene == "new_plan":
        b = screen.plan_propose("시험 화면", STEPS, 2)["plan"]
    elif intervene == "single":
        screen.look("시험 화면")
        screen.propose("시험 화면", "@s1:e9", "click")
    elif intervene == "stop":
        screen.stop_plan()
    else:
        from vader.desktop.controller import DesktopController
        with pytest.raises(Exception):
            DesktopController(None, loaded=[]).submit("x", [])
    release.set()
    t.join(10)
    assert got[0]["result"] == screen.REFUSED and screen._plan is None
    if b:                                                             # 새 계획 카드는 그대로
        assert screen.plan_card()["id"] == b


@pytest.mark.parametrize("path", ["batch", "confirm"])
def test_a_stop_before_the_engine_wakes_means_nothing_is_sent(planning, monkeypatch, path):
    """보내기 직전(창 재확인 중) 중단하면 엔진 조작 0회 — batch·확인 카드 둘 다 (3b r1 P1-2)."""
    pid = approved(steps=[STEPS[1] if path == "batch" else STEPS[2]])
    entered, release, got = threading.Event(), threading.Event(), []
    real = screen._act

    def held(p, deadline):
        entered.set()
        release.wait(5)
        return real(p, deadline)
    monkeypatch.setattr(screen, "_act", held)
    if path == "batch":
        t = threading.Thread(target=lambda: got.append(screen.step(pid, 0)))
    else:
        assert screen.step(pid, 0)["result"] == "confirming"
        card = screen.pending()
        t = threading.Thread(target=lambda: got.append(screen.decide(card["id"], card["digest"], True)))
    t.start()
    assert entered.wait(5)
    planning["argv"] = []
    assert screen.stop_plan() is True
    release.set()
    t.join(10)
    assert acts(planning) == [] and got[0]["result"] == screen.REFUSED
    assert screen.outcome(pid)["result"] == "stopped"


def test_a_stop_after_sending_keeps_the_result_and_blocks_only_the_next_step(planning, monkeypatch):
    pid = approved()
    real = screen.run

    def then_stop(args, home=None, deadline=None, value=None, go=None, menu_path=False):
        out = real(args, home, deadline, value, go, menu_path)
        if args[0] == "set-value":
            screen.stop_plan()                                       # 보낸 뒤 중단
        return out
    monkeypatch.setattr(screen, "run", then_stop)
    assert screen.step(pid, 0)["result"] == screen.DONE
    status = screen.outcome(pid)
    assert status["result"] == "stopped" and status["steps"][0]["delivery"] == "delivered_verified"
    planning["argv"] = []
    assert screen.step(pid, 1)["result"] == "stopped" and acts(planning) == []


def test_a_single_proposal_that_stops_a_confirming_plan_cleans_its_observation(planning):
    pid = approved(steps=[STEPS[2]])
    assert screen.step(pid, 0)["result"] == "confirming"
    home = screen._pending["seen"]["home"]
    home.mkdir(parents=True, exist_ok=True)
    for ref in ("@s1:e9", "@s1:e8"):                                 # 그 관찰의 ref·없는 ref — 둘 다 거절
        with pytest.raises(screen.ScreenError):
            screen.propose("시험 화면", ref, "click")
    assert not home.exists() and screen._pending is None and not screen._issued
    assert screen.outcome(pid)["result"] == "stopped"


def test_plans_are_only_for_the_built_in_synthetic_app(planning, monkeypatch, rune_dir):
    base = screen.DEFAULT_APPS["시험 화면"]
    for apps in ({"업무 앱": {**base}}, {"시험 화면": {**base, "exe": r"C:\\x\\real.exe"}}):
        monkeypatch.setattr(screen, "apps", lambda a=apps: a)
        key = next(iter(apps))
        with pytest.raises(screen.ScreenError):
            screen.plan_propose(key, STEPS, 2)
        (rune_dir / "recordings" / "nav.json").write_text(rec([]), encoding="utf-8")
        assert screen.diagnose(key, "rune", "nav")["steps"] == []        # 진단은 된다


def test_a_stop_while_the_engine_is_suspended_never_wakes_it(planning, monkeypatch):
    """띄운 뒤 Job 에 넣는 사이 중단되면 깨우지 않는다 — 깨우기 직전 확인 (codex1 3b r2 후속)."""
    pid = approved(steps=[STEPS[1]])
    real_job, woke = screen._in_job, []

    def stop_then_job(proc):
        if screen._running is not None:          # 조작 엔진일 때만(앞의 snapshot·창 목록은 그대로)
            screen.stop_plan()
        real_job(proc)
    monkeypatch.setattr(screen, "_in_job", stop_then_job)
    real_resume = screen._resume
    monkeypatch.setattr(screen, "_resume", lambda proc: (screen._running is not None and woke.append(1), real_resume(proc)))
    planning["argv"] = []
    got = screen.step(pid, 0)
    assert got["result"] == screen.REFUSED and woke == [] and not screen._procs
    assert acts(planning) == ["check"]                # 띄우기는 했지만 깨우지 않았다
    assert screen.outcome(pid)["result"] == "stopped"


def test_refusal_names_failed_checks_but_no_screen_text():
    details = {"actionable": False, "checks": [
        {"check": "visible", "status": "pass"},
        {"check": "receives_events", "status": "fail", "reason": "비밀 글자",
         "occluder": {"role": "menu", "name": "환자이름", "bounds": {"x": 1}}},
        {"check": "Bad Name!", "status": "fail"}, "junk"]}
    raw = json.dumps({"version": screen.ENVELOPE, "command": "click", "ok": False, "data": None,
                      "error": {"code": "ACTION_FAILED", "message": "비밀 글자", "details": details,
                                "disposition": {"delivery": "not_delivered"}}}).encode()
    with pytest.raises(screen.ScreenError) as e:
        screen._envelope("click", 1, raw)
    msg = str(e.value)
    assert "receives_events=fail(가림:menu)" in msg and "비밀" not in msg and "환자" not in msg and "Bad" not in msg
    assert screen._checks(None) == screen._checks({"checks": "x"}) == ""


def test_an_unnamed_menu_item_cannot_be_clicked_even_without_a_path(looking, monkeypatch):
    """엔진이 닫힌 메뉴 속(안 보이는) 메뉴 항목도 누를 수 있게 되므로, 무엇인지 모르는 '역할#번호' 메뉴 항목은 거절."""
    _menu_screen(looking, monkeypatch)
    with pytest.raises(screen.ScreenError, match="사람이 정한 이름"):
        screen.propose("시험 화면", "@s1:e3", "click")                    # '숨김' 은 승인 목록에 없다
    assert screen.propose("시험 화면", "@s1:e2", "click")["pending"]       # 이름 있는 메뉴 항목은 경로 없이도 된다
    assert "닫혀 있어도" in screen.pending()["action"]                     # 카드가 '보이는 곳 누르기' 가 아님을 알린다
    assert screen.pending()["label"] == "원무 › 수가별발생조회"             # 경로 없이 눌러도 카드엔 메뉴 경로 전체
    seen = next(iter(screen._issued.values()))
    assert screen._menu_top(seen, "@s1:e2") == "@s1:e1"                   # 가상 커서는 닫힌 항목 대신 맨 위 메뉴(원무)로
    assert screen._menu_top(seen, "@s1:e1") == "@s1:e1" and screen._menu_top(seen, "@s1:e6") == "@s1:e6"


def test_a_menu_item_under_an_unnamed_menu_is_refused(looking, monkeypatch):
    """같은 이름 메뉴 항목이 여러 메뉴에 있어 카드에 경로 전체가 보여야 한다 — 위 메뉴가 '역할#번호' 면 무엇인지 몰라 거절."""
    from contextlib import nullcontext
    base = dict(screen.DEFAULT_APPS["시험 화면"])
    monkeypatch.setattr(screen, "apps", lambda: {"시험 화면": {**base, "labels": [*base["labels"], "조회"]}})
    menu = lambda ref, name, kids=(): {**node(ref, role="menuitem", name=name), "children": list(kids)}
    looking["env"] = env("snapshot", data=snap({"role": "window", "children": [menu("@s1:e1", "모르는메뉴", [menu("@s1:e2", "조회")])]}))
    screen.look("시험 화면")
    monkeypatch.setattr(screen, "hold", nullcontext)
    with pytest.raises(screen.ScreenError, match="위 메뉴"):
        screen.propose("시험 화면", "@s1:e2", "click")


def test_a_card_shows_the_whole_menu_path_even_when_via_skips_a_menu(looking, monkeypatch):
    """codex1 P2: Root→A→Query 와 Root→B→Query 에 via=[Root] 를 주어도 카드는 'Root › A › Query' — 건너뛴 중간 메뉴도
    보이고, 그 중간 메뉴가 이름 없는 칸이면 거절."""
    from contextlib import nullcontext
    base = dict(screen.DEFAULT_APPS["시험 화면"])
    monkeypatch.setattr(screen, "apps", lambda: {"시험 화면": {**base, "labels": [*base["labels"], "관리자", "가", "조회"]}})
    menu = lambda ref, name, kids=(): {**node(ref, role="menuitem", name=name), "children": list(kids)}
    looking["env"] = env("snapshot", data=snap({"role": "window", "children": [
        menu("@s1:e1", "관리자", [menu("@s1:e2", "가", [menu("@s1:e3", "조회")]), menu("@s1:e4", "모름", [menu("@s1:e5", "조회")])])]}))
    screen.look("시험 화면")
    monkeypatch.setattr(screen, "hold", nullcontext)
    screen.propose("시험 화면", "@s1:e3", "click", None, ["@s1:e1"])
    assert screen.pending()["label"] == "관리자 › 가 › 조회"
    with pytest.raises(screen.ScreenError, match="위 메뉴"):
        screen.propose("시험 화면", "@s1:e5", "click", None, ["@s1:e1"])      # 건너뛴 '모름' 은 이름 없는 칸


def test_menu_path_identity_reaches_the_engine_only_for_apps_that_turn_it_on(looking, monkeypatch):
    """같은 이름 메뉴 항목의 경로 식별은 앱 설정 `"menu_path": true` 일 때만 엔진에 켠다 — 관찰과 조작이 같은 설정."""
    from contextlib import nullcontext
    seen_env, inner = [], screen.subprocess.Popen
    monkeypatch.setattr(screen.subprocess, "Popen",
                        lambda argv, **kw: seen_env.append((argv[1], kw["env"].get("AGENT_DESKTOP_MENU_PATH_IDENTITY"))) or inner(argv, **kw))
    monkeypatch.setattr(screen, "hold", nullcontext)
    base = dict(screen.DEFAULT_APPS["시험 화면"])
    for on, want in ((True, "1"), (False, None), ("1", None)):               # 정확히 true 만
        seen_env.clear()
        monkeypatch.setattr(screen, "apps", lambda on=on: {"시험 화면": {**base, "menu_path": on}})
        looking["env"] = env("snapshot", data=snap({"role": "window", "children": [node("@s1:e1", name="확인")]}))
        looking["envs"] = {"click": env("click", data={"action": "click", "disposition": {"delivery": "delivered_unverified"}})}
        screen.look("시험 화면")
        pid = screen.propose("시험 화면", "@s1:e1", "click")["pending"]
        screen.decide(pid, screen.pending()["digest"], True)
        got = dict(seen_env)
        assert got["snapshot"] == want and got["click"] == want, (on, seen_env)


@pytest.mark.parametrize("start, flip_at", [(True, "snapshot"), (False, "snapshot"), (True, "approve"), (False, "approve")])
def test_an_action_uses_the_menu_path_setting_its_snapshot_was_taken_with(looking, monkeypatch, start, flip_at):
    """codex1 회신 6: 설정이 관찰 도중이나 승인 전에 바뀌어도, 경로 메뉴(via)와 끝 조작은 그 스냅샷에 실제로 쓴 값."""
    from contextlib import nullcontext
    setting, seen_env, inner = {"on": start}, [], screen.subprocess.Popen

    def popen(argv, **kw):
        seen_env.append((argv[1], kw["env"].get("AGENT_DESKTOP_MENU_PATH_IDENTITY")))
        if argv[1] == "snapshot" and flip_at == "snapshot":
            setting["on"] = not start                                # 관찰 도중 설정이 뒤집힌다
        return inner(argv, **kw)
    monkeypatch.setattr(screen.subprocess, "Popen", popen)
    monkeypatch.setattr(screen, "hold", nullcontext)
    base = dict(screen.DEFAULT_APPS["시험 화면"])
    monkeypatch.setattr(screen, "apps", lambda: {"시험 화면": {**base, "labels": [*base["labels"], "원무", "조회"],
                                                          "menu_path": setting["on"]}})
    menu = lambda ref, name, kids=(): {**node(ref, role="menuitem", name=name), "children": list(kids)}
    looking["env"] = env("snapshot", data=snap({"role": "window", "children": [menu("@s1:e1", "원무", [menu("@s1:e2", "조회")])]}))
    looking["envs"] = {"click": env("click", data={"action": "click", "disposition": {"delivery": "delivered_unverified"}})}
    screen.look("시험 화면")
    pid = screen.propose("시험 화면", "@s1:e2", "click", None, ["@s1:e1"])["pending"]
    if flip_at == "approve":
        setting["on"] = not start                                    # 관찰 발급 뒤, 승인 전
    screen.decide(pid, screen.pending()["digest"], True)
    want = "1" if start else None
    assert [v for c, v in seen_env if c == "snapshot"] == [want]
    assert [v for c, v in seen_env if c == "click"] == [want, want]  # via 한 번 + 끝 조작 한 번


# ── 표 읽기 (마스터 결정 2026-11-17: 설정이 켠 앱에서 카드 없이 · codex1 회신 1·2: 실제 입력 없이 그 표 창에만 메시지) ──

@pytest.fixture
def tabling(monkeypatch, tmp_path):
    """표 fpsA(HWND 77) 하나가 있는 창. _copy 는 기록만 하고 클립보드 글자를 돌려준다."""
    from contextlib import contextmanager
    state = {"calls": [], "grids": {"fpsA": [77], "fpsB": [78, 79]}, "complete": True, "tables": "fps", "gen": ["g1"],
             "clip": "\t과목\t금액\r\n\tTEST001\t10\r\n\tTEST002\t20\r\n", "hold": True, "fail": None}
    win = {"id": "w-0x10", "pid": 1, "process": "p", "exe": "x.exe"}

    def copy(grid, top, pid, name, go, deadline=None):
        state["calls"].append(("copy", grid, top, pid, name))
        if not go():
            raise screen.ScreenError("잠김")
        if state["fail"]:
            raise screen.ScreenError(state["fail"])
        return state["clip"]

    @contextmanager
    def hold():
        if not state["hold"]:
            raise ValueError("다른 작업이 돌고 있어요.")
        state["calls"].append("hold")
        yield
    monkeypatch.setattr(screen, "apps", lambda: {"앱": {"exe": "x.exe", "tables": state["tables"]}})
    monkeypatch.setattr(screen, "_target", lambda key, deadline=None: win)
    monkeypatch.setattr(screen, "_grids", lambda top, pid, prefix, deadline=None: (state["calls"].append(("grids", top, prefix)),
                                                                    (state["grids"], state["complete"]))[1])
    monkeypatch.setattr(screen, "_copy", copy)
    monkeypatch.setattr(screen, "hold", hold)
    monkeypatch.setattr(screen, "current_gen", lambda: state["gen"][0])
    monkeypatch.setattr(screen.os, "name", "nt")
    monkeypatch.setattr(screen.config, "workspace", lambda: tmp_path)
    monkeypatch.setattr(screen.journal, "record", lambda entry: None)
    return state


def test_table_copy_saves_the_table_as_is_and_returns_only_counts(tabling, tmp_path):
    assert screen.table("앱") == {"app": "앱", "complete": True, "tables": ["fpsA", "fpsB"]}
    assert tabling["calls"] == [("grids", 0x10, "fps")]                       # 목록만 — 아무것도 보내지 않는다
    tabling["calls"].clear()
    got = screen.table("앱", "fpsA")
    assert tabling["calls"] == [("grids", 0x10, "fps"), "hold", ("copy", 77, 0x10, 1, "fpsA")]
    assert (got["lines"], got["columns"]) == (3, 3) and set(got) == {"app", "table", "lines", "columns", "file"}
    assert "TEST" not in json.dumps(got, ensure_ascii=False) and "과목" not in json.dumps(got, ensure_ascii=False)
    saved = Path(got["file"])
    assert saved.parent == tmp_path / "screen_tables" and saved.read_bytes().decode("utf-8") == tabling["clip"]
    assert screen.table("앱", "fpsA")["file"] != got["file"]                 # 같은 초에 다시 읽어도 덮어쓰지 않는다


@pytest.mark.parametrize("change, calls", [
    ({"tables": None}, []),                                                    # 설정이 안 켠 앱
    ({"grids": {"fpsB": [78, 79]}}, [("grids", 0x10, "fps")]),                # 그 표가 없다
    ({"complete": False}, [("grids", 0x10, "fps")]),                          # 다 못 봤다 — 하나라는 근거가 없다
    ({"hold": False}, [("grids", 0x10, "fps")]),                              # 작업·녹화가 실행권을 쥐고 있다
    ({"fail": "그 표에서 복사되지 않았어요."}, [("grids", 0x10, "fps"), "hold", ("copy", 77, 0x10, 1, "fpsA")]),
])
def test_table_copy_refuses_without_saving(tabling, tmp_path, change, calls):
    tabling.update(change)
    with pytest.raises(screen.ScreenError):
        screen.table("앱", "fpsA")
    assert tabling["calls"] == calls and not list(tmp_path.glob("screen_tables/*"))   # 폴더는 미리 만들어도 파일은 없다


def test_table_copy_does_not_send_or_keep_a_file_after_the_app_locked(tabling, tmp_path, monkeypatch):
    for gens in (["g1", "g1", None], ["g1", "g1", "g1", "g1", None]):        # 보내기 직전에 · 파일을 쓴 뒤에 잠김
        seq = iter(gens)
        monkeypatch.setattr(screen, "current_gen", lambda: next(seq, None))
        with pytest.raises(screen.ScreenError):
            screen.table("앱", "fpsA")
        assert not list((tmp_path / "screen_tables").glob("*")) if (tmp_path / "screen_tables").exists() else True


@pytest.mark.parametrize("name", ["panel1", "fpsB", "fps A", 3])
def test_table_copy_only_takes_a_table_the_setting_covers_and_the_window_has_once(tabling, name):
    with pytest.raises(screen.ScreenError):
        screen.table("앱", name)
    assert not any(isinstance(c, tuple) and c[0] == "copy" for c in tabling["calls"])


class _FakeWin32:
    """_copy·_take 가 부르는 Win32 만. post 는 보낸 메시지를 적고, copies 에 따라 클립보드를 바꾼다."""

    def __init__(self, post_ok=(True, True, True), copies=True, owner_pid=1, attach_ok=True, restore_ok=True, open_ok=True):
        self.posts, self.keys_set, self.seq, self.text, self.owner_pid = [], [], 5, "OLD", owner_pid
        self.post_ok, self.copies, self.attach_ok, self.restore_ok = list(post_ok), copies, attach_ok, restore_ok
        self.attached = self.opened = False
        self.open_ok = open_ok
        self.emptied = 0

    def PostMessageW(self, h, msg, wp, lp):
        ok = self.post_ok.pop(0)
        if ok:
            self.posts.append(msg)
            if msg == 0x0100 and self.copies and self.ctrl:
                self.seq, self.text = self.seq + 1, "\tA\r\n\tTEST\r\n"
        return ok

    def GetKeyboardState(self, keys): return True

    def SetKeyboardState(self, keys):
        self.ctrl = keys[0x11] == 0x80
        self.keys_set.append(self.ctrl)
        return self.restore_ok or self.ctrl

    def AttachThreadInput(self, me, them, on):
        self.attached = on and self.attach_ok
        return self.attach_ok
    def GetWindowThreadProcessId(self, h, out): return 9
    def IsHungAppWindow(self, h): return False
    def GetClipboardSequenceNumber(self): return self.seq
    def OpenClipboard(self, h):
        self.opened = self.open_ok
        return self.open_ok
    def CloseClipboard(self): self.opened = False; return True
    def GetClipboardOwner(self): return 1234
    def GetClipboardData(self, fmt): return 1
    def EmptyClipboard(self):
        self.emptied += 1
        self.text, self.seq = "", self.seq + 1                                   # 비워도 순번이 오른다 (회신 4)
        return True
    def GetCurrentThreadId(self): return 8
    def GlobalLock(self, h): return 1
    def GlobalUnlock(self, h): return True


@pytest.fixture
def win32(monkeypatch):
    import ctypes
    holder = {}

    def make(**kw):
        fake = _FakeWin32(**kw)
        fake.ctrl = False
        holder["fake"] = fake
        monkeypatch.setattr(screen, "_u32", lambda: (ctypes, None, fake, fake))
        monkeypatch.setattr(screen, "_pid_of", lambda w32, h: fake.owner_pid if h == 1234 else 1)
        monkeypatch.setattr(screen, "_idle", lambda w32: True)
        monkeypatch.setattr(screen, "_still", lambda *a: True)
        monkeypatch.setattr(ctypes, "wstring_at", lambda ptr: fake.text)
        monkeypatch.setattr(screen.time, "sleep", lambda s: None)
        monkeypatch.setattr(screen, "TABLE_COPY_WAIT", 0.01)
        return fake
    holder["make"] = make
    return holder


def test_copy_sends_corner_click_then_ctrl_insert_and_takes_and_clears_the_apps_clipboard(win32):
    fake = win32["make"]()
    assert screen._copy(77, 0x10, 1, "fpsA", lambda: True) == "\tA\r\n\tTEST\r\n"
    assert fake.posts == [0x0201, 0x0202, 0x0100] and fake.keys_set == [True, False] and not fake.attached
    assert fake.emptied == 1 and not fake.opened


@pytest.mark.parametrize("kw, sent, kind, emptied", [
    ({"post_ok": (False,)}, [], screen.REFUSED, 0),                           # 첫 메시지부터 실패 — 안 보냄, 비울 것 없음
    ({"post_ok": (True, False)}, [0x0201], screen.UNKNOWN, 1),                # 도중 실패 — 선택이 바뀌었을 수 있다
    ({"copies": False}, [0x0201, 0x0202, 0x0100], screen.UNKNOWN, 1),        # 복사가 안 왔다(늦을 수 있다)
    ({"owner_pid": 2}, [0x0201, 0x0202, 0x0100], screen.UNKNOWN, 1),         # 다른 프로세스가 쓴 것은 안 읽고 비운다
    ({"restore_ok": False}, [0x0201, 0x0202, 0x0100], screen.UNKNOWN, 1),    # Ctrl 을 못 꺼도 복사된 표는 비운다 (회신 3)
    ({"restore_ok": False, "post_ok": (False,)}, [], screen.UNKNOWN, 0),     # 안 보냈어도 Ctrl 을 못 끈 것은 알린다 (회신 3)
    ({"open_ok": False}, [0x0201, 0x0202, 0x0100], screen.UNKNOWN, 0),       # 클립보드를 못 열면 남았을 수 있다고
])
def test_copy_failures_say_whether_anything_was_sent_and_always_clean_up(win32, kw, sent, kind, emptied):
    fake = win32["make"](**kw)
    with pytest.raises(screen.ScreenError) as e:
        screen._copy(77, 0x10, 1, "fpsA", lambda: True)
    assert e.value.kind == kind and fake.posts == sent and fake.emptied == emptied
    assert fake.keys_set[-1] is False and not fake.attached and not fake.opened   # Ctrl 끄고, 떼고, 클립보드 닫음
    if kw.get("open_ok") is False:
        assert "남았을 수" in str(e.value)


def test_copy_warns_that_a_late_copy_may_stay_even_after_cleaning_moved_the_sequence(win32):
    win32["make"](copies=False)
    with pytest.raises(screen.ScreenError, match="늦게 오면"):
        screen._copy(77, 0x10, 1, "fpsA", lambda: True)


def test_copy_stops_waiting_when_the_app_locks_or_the_request_expires_after_the_last_message(win32, monkeypatch):
    for go, deadline in ((iter([True] * 5 + [False]), None), (None, time.monotonic() + 0.05)):   # 잠김 · 기한만 (codex1 회신 5)
        fake, began = win32["make"](copies=False), time.monotonic()
        monkeypatch.setattr(screen, "TABLE_COPY_WAIT", 60.0)                     # make 뒤에 — 기다림 상한으로 끝나지 않게
        go = go or iter(lambda: True, None)                                     # 늘 열림 — 기한만이 끝낸다
        with pytest.raises(screen.ScreenError, match="늦게 오면") as e:
            screen._copy(77, 0x10, 1, "fpsA", lambda go=go: next(go, False), deadline)
        assert e.value.kind == screen.UNKNOWN and fake.posts == [0x0201, 0x0202, 0x0100]
        assert time.monotonic() - began < 5 and fake.keys_set[-1] is False and not fake.attached and fake.emptied == 1


def test_copy_stops_sending_when_the_app_locks_midway(win32):
    fake, calls = win32["make"](), iter([True, True, False])               # 붙이기 전·첫 메시지 전만 열림
    with pytest.raises(screen.ScreenError) as e:
        screen._copy(77, 0x10, 1, "fpsA", lambda: next(calls, False))
    assert e.value.kind == screen.UNKNOWN and fake.posts == [0x0201] and fake.emptied == 1 and not fake.attached


def test_copy_sends_nothing_when_the_app_locked_or_attach_fails(win32):
    fake = win32["make"]()
    with pytest.raises(screen.ScreenError) as e:
        screen._copy(77, 0x10, 1, "fpsA", lambda: False)
    assert e.value.kind == screen.REFUSED and fake.posts == [] and not fake.attached
    fake = win32["make"](attach_ok=False)
    with pytest.raises(screen.ScreenError):
        screen._copy(77, 0x10, 1, "fpsA", lambda: True)
    assert fake.posts == [] and fake.keys_set == []
