"""설치본 실행 (런칭 0단계, 2026-10-21) — python 이 없는 곳에서 자식 프로세스를 띄우는 길과 설정 위치."""
import sys
from pathlib import Path

import pytest

from vader import __main__ as entry
from vader.core import config


def test_a_child_module_runs_through_python_in_development():
    command = config.module_command("vader.mcp_server")
    assert command[1:] == ["-m", "vader.mcp_server"] and Path(command[0]).name.startswith("python")


def test_the_installed_app_runs_children_through_its_console_exe(monkeypatch):
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\VaderAI\current\VaderAI.exe")
    assert config.module_command("vader.core.workshop") == [
        r"C:\Apps\VaderAI\current\vaderai-cli.exe", "--module", "vader.core.workshop"]


def test_only_the_child_modules_can_be_started_by_name(monkeypatch):
    ran = []
    monkeypatch.setattr("runpy.run_module", lambda name, **kw: ran.append(name))
    entry.main(["--module", "vader.mcp_server"])
    assert ran == ["vader.mcp_server"]
    with pytest.raises(SystemExit):                     # 임의 모듈은 argparse 가 거절한다
        entry.main(["--module", "os"])
    assert ran == ["vader.mcp_server"]
