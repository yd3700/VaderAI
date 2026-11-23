"""룬을 만들 프로그램(Claude Code·Codex) 연결 — 경로를 박지 않고 지금 VaderAI 에게서 얻는다 (2026-10-14)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from vader.core import clients


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """Claude Code 만 깔리고 연결된 PC. Codex 는 깔렸지만 연결 안 됨."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"vaderai": {}}}), encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('[mcp_servers.stitch]\nurl = "x"\n', encoding="utf-8")
    monkeypatch.setattr(clients.shutil, "which", lambda name: f"C:/bin/{name}.cmd")
    monkeypatch.setattr(clients.importlib.util, "find_spec", lambda name: object())   # CI 에는 mcp 가 없다
    calls = []
    monkeypatch.setattr(clients, "_run", lambda args, timeout=60: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    monkeypatch.setattr(clients.subprocess, "Popen", lambda args, **kw: calls.append((args, kw)))
    return calls


def test_status_reads_each_programs_own_settings(pc, monkeypatch):
    assert [(c["id"], c["installed"], c["connected"]) for c in clients.status()] == [
        ("claude", True, True), ("codex", True, False), ("agy", True, False)]
    monkeypatch.setattr(clients.shutil, "which", lambda name: None)
    assert not any(c["installed"] or c["connected"] for c in clients.status())


def test_connect_registers_this_vaderai_not_a_fixed_path(pc):
    clients.connect("codex")
    remove, add = pc
    assert remove[1:] == ["mcp", "remove", "vaderai"]
    here = str(Path(clients.vader.__file__).resolve().parent.parent)
    assert add[1:6] == ["mcp", "add", "vaderai", "--env", f"PYTHONPATH={here}"]
    assert add[6:8] == ["--env", f"VADERAI_WORKSPACE={clients.config.workspace() or ''}"]   # 연 계정에 묶는다
    assert add[8:] == ["--", *clients.config.module_command("vader.mcp_server")]
    assert Path(add[9]).parent == Path(sys.executable).parent


def test_connect_refuses_without_the_mcp_part(pc, monkeypatch):
    monkeypatch.setattr(clients.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(clients.ClientError, match="연결용 부품"):
        clients.connect("claude")
    assert pc == []


def test_the_console_command_is_fixed_and_survives_spaces_in_the_path(pc, monkeypatch):
    """npm 의 .cmd 는 cmd.exe /s /c "..." 로 — 바깥 따옴표 한 쌍만 벗겨져 경로 공백·첫 마디가 그대로 간다."""
    monkeypatch.setattr(clients.shutil, "which", lambda name: f"C:/Program Files/npm/{name}.cmd")
    app, cmdline = clients.console_command("claude")
    assert app.lower().endswith("cmd.exe")
    exe = Path("C:/Program Files/npm/claude.cmd").resolve()
    assert cmdline == f'/d /s /c ""{exe}" "{clients.FIRST_MESSAGE}""'
    with pytest.raises(clients.ClientError, match="연결되어 있지 않습니다"):
        clients.console_command("codex")
    with pytest.raises(clients.ClientError):
        clients.console_command("calc")


def test_a_same_named_file_in_the_workshop_is_not_run(pc, monkeypatch):
    monkeypatch.setattr(clients.shutil, "which", lambda name: str(clients.workshop.root() / f"{name}.cmd"))
    with pytest.raises(clients.ClientError, match="제작 폴더"):
        clients.console_command("claude")


def test_an_exe_gets_only_its_arguments(pc, monkeypatch):
    """프로그램 이름은 ConPTY 가 앞에 붙인다 — 여기서 또 넣으면 첫 인자가 실행 파일 경로가 된다."""
    monkeypatch.setattr(clients.shutil, "which", lambda name: f"C:/tools/{name}.exe")
    app, args = clients.console_command("claude")
    assert app.endswith("claude.exe") and args == f'"{clients.FIRST_MESSAGE}"'


def test_install_uses_only_the_official_packages_and_skips_git_when_present(monkeypatch):
    """제작소에서 클릭 한 번으로 설치 (2026-10-23 마스터 결정). 이미 깔린 것이 실패 코드로 와도 깔려 있으면 성공이다."""
    found = {"winget": "C:/winget.exe", "git": "C:/git.exe"}
    calls = []
    def run(args, timeout=60):
        calls.append(args[3])
        found["claude"] = "C:/claude.exe"
        return subprocess.CompletedProcess(args, 1, "이미 설치됨", "")
    monkeypatch.setattr(clients.shutil, "which", found.get)
    monkeypatch.setattr(clients, "_run", run)
    monkeypatch.setattr(clients, "_refresh_path", lambda: None)

    clients.install("claude")
    assert calls == ["Anthropic.ClaudeCode"]

    with pytest.raises(clients.ClientError, match="Codex 설치에 실패"):
        clients.install("codex")
    del found["winget"]
    with pytest.raises(clients.ClientError, match="winget"):
        clients.install("codex")


def test_antigravity_is_registered_with_flags_before_the_name(pc):
    """agy mcp add [flags] <name> <command> [args] — '--' 없이 (2026-11-19 agy 1.5.11 도움말)."""
    clients.connect("agy")
    add = pc[-1]
    assert add[1:4] == ["mcp", "add", "-e"] and add[5] == "-e" and add[7] == "vaderai"
    assert add[8:] == clients.config.module_command("vader.mcp_server")


def test_antigravity_conversation_cleanup_only_touches_its_own_folder(tmp_path, monkeypatch):
    from vader.core.ocr import console
    monkeypatch.setattr(console, "AGY_HOME", tmp_path)
    cid = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    (tmp_path / "brain" / cid).mkdir(parents=True)
    (tmp_path / "conversations").mkdir()
    (tmp_path / "conversations" / f"{cid}.db").write_text("x")
    keep = tmp_path / "conversations" / "other.db"
    keep.write_text("x")
    console._forget_agy("../../etc")          # 번호 모양이 아니면 아무것도 안 지운다
    console._forget_agy(cid)
    assert not (tmp_path / "brain" / cid).exists() and not (tmp_path / "conversations" / f"{cid}.db").exists() and keep.exists()


def test_antigravity_runs_with_session_deny_rules_and_skips_linked_history(tmp_path, monkeypatch):
    """이 실행에만 쓰는 설정으로 명령·쓰기·인터넷·MCP 거부, auto 끔. 기록 폴더가 링크면 아무것도 안 지운다 (codex1 회신 1)."""
    import subprocess as sp
    from vader.core.ocr import console
    deny = console.AGY_SETTINGS["permissions"]["deny"]
    assert {"command(*)", "write_file(*)", "read_url(*)", "execute_url(*)", "mcp(*)"} <= set(deny) and console.AGY_SETTINGS["agentMode"] == ""
    outside = tmp_path / "outside"
    (outside / "brain" / ("1" * 8 + "-1111-1111-1111-" + "1" * 12)).mkdir(parents=True)
    home = tmp_path / "home"
    if sp.run(["cmd", "/c", "mklink", "/J", str(home), str(outside)], capture_output=True).returncode:
        pytest.skip("junction 을 만들 수 없다")
    monkeypatch.setattr(console, "AGY_HOME", home)
    console._forget_agy("1" * 8 + "-1111-1111-1111-" + "1" * 12)
    assert (outside / "brain" / ("1" * 8 + "-1111-1111-1111-" + "1" * 12)).exists()
