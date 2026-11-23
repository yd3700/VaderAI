"""제작소 안 콘솔 — ConPTY 세션 하나: 출력 읽기 · 입력 · 크기 · 닫기 (2026-10-14 마스터 결정).

실제 Claude Code·Codex 대신 cmd.exe 로 시험한다 — 콘솔이 하는 일은 어떤 프로그램이든 같다.
"""
import sys
import time

import pytest

if sys.platform != "win32":
    pytest.skip("ConPTY 는 Windows 에만 있다", allow_module_level=True)
pytest.importorskip("winpty")

from vader.core import clients  # noqa: E402
from vader.desktop.console import Console, ConsoleError  # noqa: E402


def read_until(console, session, text, seconds=10):
    seen = ""
    end = time.time() + seconds
    while time.time() < end:
        got = console.read(session)
        seen += got["data"]
        if text in seen or not got["alive"]:
            return seen, got["alive"]
    return seen, True


@pytest.fixture
def console(monkeypatch):
    monkeypatch.setattr(clients, "console_command", lambda client, rune_id=None: ("cmd.exe", "/d /q /k prompt $G"))
    made = Console()
    yield made
    made.stop()


def test_a_session_reads_writes_resizes_and_closes(console):
    session = console.start("claude", 80, 24)["session"]
    console.write(session, "echo 콘솔시험 %CD%\r")
    seen, alive = read_until(console, session, "콘솔시험 C:")
    assert "콘솔시험" in seen and alive
    assert console.resize(session, 120, 40)
    with pytest.raises(ConsoleError, match="이미 열린"):
        console.start("claude", 80, 24)

    console.stop(session)
    assert not console.status()["alive"]
    with pytest.raises(ConsoleError, match="세션이 바뀌었"):
        console.write("old-session", "x")


@pytest.mark.parametrize("cols, rows", [(0, 24), (80, 0), (80.5, 24), ("80", 24), (9999, 24)])
def test_a_hidden_screens_zero_size_is_refused(console, cols, rows):
    with pytest.raises(ConsoleError, match="크기"):
        console.start("claude", cols, rows)


def test_the_screen_hears_when_the_program_exits_by_itself(console):
    """ConPTY 는 프로그램이 끝나도 통로를 열어 둔다 — 그래도 화면은 곧 '끝남' 을 받아야 한다."""
    session = console.start("claude", 80, 24)["session"]
    console.write(session, "exit\r")
    end = time.time() + 10
    while time.time() < end and console.read(session)["alive"]:
        pass
    assert not console.read(session)["alive"] and time.time() < end


def test_a_flood_waits_for_the_screen_instead_of_piling_up(monkeypatch):
    """화면이 안 가져가면 버퍼 상한에서 멈추고, 가져가면 빠짐없이 이어진다."""
    from vader.desktop import console as module

    lines = 60000
    monkeypatch.setattr(module, "MAX_BUFFER", 64 * 1024)
    monkeypatch.setattr(clients, "console_command", lambda client, rune_id=None: (
        sys.executable, f'-c "for i in range({lines}): print(\'L\' + str(i))"'))
    made = Console()
    session = made.start("claude", 200, 50)["session"]
    try:
        time.sleep(2)   # 화면이 멈춰 있다
        assert made._size <= module.MAX_BUFFER + 65536
        seen = ""
        end = time.time() + 30
        while time.time() < end:
            got = made.read(session)
            seen += got["data"]
            if f"L{lines - 1}" in seen or not got["alive"]:
                break
        seen += made.read(session)["data"]
        assert f"L{lines - 1}" in seen and "L30000" in seen
    finally:
        made.stop()


def test_a_pasted_screenshot_lands_outside_the_rune_as_a_png_path(console, monkeypatch, tmp_path):
    """붙여넣은 스크린샷은 룬 폴더 밖 콘솔 전용 폴더에 PNG 로 — 경로만 콘솔에 넣는다 (2026-10-27)."""
    import base64
    from pathlib import Path

    from vader.core import config

    monkeypatch.setattr(config, "data_dir", lambda: tmp_path)
    session = console.start("claude", 80, 24)["session"]
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
    path = Path(console.save_image(session, base64.b64encode(png).decode()))
    assert path.read_bytes() == png and path.parent == tmp_path / "console-paste" / session
    with pytest.raises(ConsoleError):
        console.save_image(session, base64.b64encode(b"GIF89a").decode())   # PNG 만
    with pytest.raises(ConsoleError):
        console.save_image("다른세션", base64.b64encode(png).decode())
