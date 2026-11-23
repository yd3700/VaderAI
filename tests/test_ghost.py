from vader.desktop import ghost


def test_cursor_picture_is_red_to_black_arrow_with_a_clear_background():
    rows = ghost.picture()
    assert len(rows) == ghost.H and all(len(r) == ghost.W for r in rows)
    assert rows[0][ghost.W - 1] == ghost.KEY                       # 오른쪽 위는 투명
    inner = [c for r in rows for c in r if c not in (ghost.KEY, "#ffffff")]
    red = lambda c: int(c[1:3], 16)
    assert max(map(red, inner)) > 150 and min(map(red, inner)) < 60   # 빨강에서 검정까지



import queue
import threading
import time

import pytest

from vader.core import screen


class FakeChild:
    def __init__(self, block=None):
        self.lines, self.block, self.dead, self.killed = [], block, False, False

        class In:
            def write(_, b):
                if self.block:
                    self.block.wait(5)
                if self.dead:
                    raise OSError("broken pipe")
                self.lines.append(b)

            def flush(_):
                pass
        self.stdin = In()

    def poll(self):
        return 0 if self.dead or self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, t=None):
        return 0


@pytest.fixture
def ghosts(monkeypatch):
    made = []
    state = {"block": None, "made": made, "gen": "g1", "spawn": None, "job_fail": False}

    def popen(*a, **k):
        if state["spawn"]:
            state["spawn"].wait(5)
        made.append(FakeChild(state["block"]))
        return made[-1]

    def job(p):
        if state["job_fail"]:
            raise screen.ScreenError("job")
    monkeypatch.setattr(screen.subprocess, "Popen", popen)
    monkeypatch.setattr(screen, "_in_job", job)
    monkeypatch.setattr(screen, "_resume", lambda p: None)
    monkeypatch.setattr(screen, "current_gen", lambda: state["gen"])
    monkeypatch.setattr(screen, "_ghost_q", None)
    monkeypatch.setattr(screen, "_ghost_proc", None)
    monkeypatch.setattr(screen, "_procs", set())
    return state


def wait(cond, t=3):
    end = time.monotonic() + t
    while not cond() and time.monotonic() < end:
        time.sleep(.01)
    return cond()


def test_the_cursor_child_is_tracked_like_the_engine_and_gets_points(ghosts):
    screen._ghost((10, 20), "g1")
    assert wait(lambda: ghosts["made"] and ghosts["made"][0].lines == [b"10 20\n"])
    assert screen._ghost_proc in screen._procs                     # sweep 이 끝낸다


def test_a_stuck_cursor_never_holds_up_the_action(ghosts):
    ghosts["block"] = threading.Event()
    began = time.monotonic()
    for i in range(5):
        screen._ghost((i, i), "g1")                                  # 자식이 안 읽어도 바로 돌아온다
    assert time.monotonic() - began < .2
    ghosts["block"].set()


def test_a_slow_cursor_start_never_holds_the_shared_lock(ghosts):
    ghosts["spawn"] = threading.Event()
    screen._ghost((1, 1), "g1")
    time.sleep(.1)
    assert screen._lock.acquire(timeout=.2)                          # 조작·중단·sweep 이 기다리지 않는다
    screen._lock.release()
    ghosts["spawn"].set()


def test_an_action_that_outlived_a_lock_draws_nothing(ghosts):
    ghosts["gen"] = None                                             # _act 의 세대 확인 뒤 잠겼다
    screen._ghost((1, 1), "g1")
    time.sleep(.2)
    assert ghosts["made"] == []


def test_a_lock_while_the_cursor_starts_ends_that_child(ghosts):
    ghosts["spawn"] = threading.Event()
    screen._ghost((1, 1), "g1")
    time.sleep(.05)
    ghosts["gen"] = None                                             # 만드는 사이 잠겼다
    ghosts["spawn"].set()
    assert wait(lambda: ghosts["made"] and ghosts["made"][0].killed)
    assert ghosts["made"][0].lines == [] and not screen._procs and screen._ghost_proc is None


def test_a_child_outside_the_job_is_ended_and_never_reused(ghosts):
    ghosts["job_fail"] = True
    screen._ghost((1, 1), "g1")
    assert wait(lambda: ghosts["made"] and ghosts["made"][0].killed)
    ghosts["job_fail"] = False
    screen._ghost((2, 2), "g1")
    assert wait(lambda: len(ghosts["made"]) == 2 and ghosts["made"][1].lines == [b"2 2\n"])
    assert ghosts["made"][0].lines == []


def test_a_broken_cursor_pipe_is_ignored_and_a_new_child_follows(ghosts):
    screen._ghost((1, 1), "g1")
    assert wait(lambda: ghosts["made"])
    ghosts["made"][0].dead = True
    screen._ghost((2, 2), "g1")
    assert wait(lambda: len(ghosts["made"]) == 2 and ghosts["made"][1].lines == [b"2 2\n"])
