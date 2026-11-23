"""룬을 만드는 LLM 프로그램(Claude Code·Codex)에 VaderAI 를 연결하고 연다 (마스터 결정 2026-10-14).

연결은 각 프로그램의 `mcp add` 를 대신 부르는 것뿐이다. 경로는 박아 두지 않고 **지금 돌고 있는
VaderAI** 에게서 얻는다 — 그래야 어느 PC 에서든 깔린 자리 그대로 등록된다.
로그인은 사람이 한다. 여기서 실행하는 명령은 아래 두 프로그램과 winget 의 고정된 모양뿐이다.

**설치도 클릭 한 번** (마스터 결정 2026-10-23): Windows 앱 설치 관리자(winget)로, 두 회사가 직접 올린
패키지만 깐다. Claude Code 는 Windows 에서 Git 이 있어야 돌아서 Git 이 없으면 함께 깐다.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import vader
from vader.core import config, workshop

NAME = "vaderai"
CLIENTS = {"claude": "Claude Code", "codex": "Codex", "agy": "Antigravity"}
PACKAGES = {"claude": ["Git.Git", "Anthropic.ClaudeCode"], "codex": ["OpenAI.Codex"], "agy": ["Google.AntigravityCLI"]}
#: 열면서 보내는 첫 마디. 따옴표·%·^·& 같은 셸 특수문자를 넣지 않는다 (cmd.exe 를 거친다).
FIRST_MESSAGE = "VaderAI 룬을 만들자. rune_guide 를 먼저 읽고, 업무 합의부터 하나씩 선택형으로 물어봐 줘."
EDIT_MESSAGE = ("VaderAI 의 설치된 룬 {id} 를 고치자. rune_guide 를 먼저 읽고(맨 끝 '지금 고치는 설치된 룬' 에 고칠 폴더가 있다), "
                "무엇을 고칠지 선택형으로 물어봐 줘.")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ClientError(ValueError):
    """사람에게 그대로 보여 줄 수 있는 거절."""


def status() -> list[dict]:
    """설치됐는지, VaderAI 가 등록돼 있는지. 프로그램을 띄우지 않고 설정 파일만 읽는다."""
    out = []
    for client, name in CLIENTS.items():
        installed = shutil.which(client) is not None
        out.append({"id": client, "name": name, "installed": installed,
                    "connected": installed and _registered(client)})
    return out


def install(client: str) -> None:
    """winget 으로 깐다. 이미 깔린 것은 건너뛴다. 끝나면 이 VaderAI 가 바로 찾도록 PATH 를 다시 읽는다."""
    if client not in CLIENTS:
        raise ClientError("지원하지 않는 프로그램입니다.")
    winget = shutil.which("winget")
    if not winget:
        raise ClientError("이 PC 에 Windows 앱 설치 관리자(winget)가 없습니다. "
                          "Microsoft Store 에서 '앱 설치 관리자'를 설치한 뒤 다시 누르세요.")
    error = ""
    for package in PACKAGES[client]:
        if package == "Git.Git" and shutil.which("git"):
            continue
        done = _run([winget, "install", "--id", package, "--exact", "--silent", "--disable-interactivity",
                     "--accept-package-agreements", "--accept-source-agreements"], timeout=900)
        if done.returncode:
            error = (done.stdout or done.stderr).strip()[-300:]
    _refresh_path()
    if not shutil.which(client):   # 실패 코드라도 깔려 있으면 된 것이다 (이미 설치됨도 실패 코드로 온다)
        raise ClientError(f"{CLIENTS[client]} 설치에 실패했습니다. {error}".strip())


def connect(client: str) -> None:
    """그 프로그램에 VaderAI 를 (다시) 등록한다. 옛 경로로 등록돼 있었으면 지금 경로로 바뀐다."""
    exe = _exe(client)
    if importlib.util.find_spec("mcp") is None:
        raise ClientError('연결용 부품(mcp)이 없습니다. VaderAI 를 다시 설치하세요: pip install "vaderai[desktop]"')
    env = f"PYTHONPATH={Path(vader.__file__).resolve().parent.parent}"
    # 등록할 때 연 계정의 작업공간에 묶는다 — MCP 서버가 '마지막 계정' 을 짐작하지 않게 (2단계 codex1)
    bound = f"{config.WORKSPACE_ENV}={config.workspace() or ''}"
    server = ["--", *config.module_command("vader.mcp_server")]
    scope = ["--scope", "user"] if client == "claude" else []
    flag = "-e" if client == "claude" else "--env"
    add = [exe, "mcp", "add", NAME, *scope, flag, env, flag, bound, *server]
    if client == "agy":   # agy 는 플래그가 이름 앞, '--' 없이 명령과 인자 (agy mcp add --help)
        add = [exe, "mcp", "add", "-e", env, "-e", bound, NAME, *server[1:]]
    _run([exe, "mcp", "remove", NAME, *scope])            # 없으면 실패한다 — 상관없다
    done = _run(add)
    if done.returncode:
        raise ClientError(f"{CLIENTS[client]} 연결에 실패했습니다: {(done.stderr or done.stdout).strip()[:300]}")


LOGIN_ARGS = {"claude": [], "codex": ["login"], "agy": []}   # 처음 켜면 로그인 안내가 나온다 (codex 는 login 명령)


def login(client: str) -> None:
    """그 프로그램을 새 콘솔 창으로 연다 — 사람이 거기서 로그인한다 (AI 연결의 [로그인], 2026-11-19).
    VaderAI 가 비밀번호·토큰을 보지 않는다. 명령 모양은 고정이고 화면이 조립하지 못한다."""
    exe = _exe(client)
    shell = os.environ.get("ComSpec") or str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe")
    subprocess.Popen([shell, "/d", "/c", "start", f"{CLIENTS[client]} 로그인", exe, *LOGIN_ARGS[client]],
                     cwd=str(Path.home()), creationflags=NO_WINDOW)


def console_command(client: str, rune_id: str | None = None) -> tuple[str, str]:
    """제작소 콘솔이 띄울 (실행 파일, 인자). 두 프로그램의 고정된 모양뿐이다 — 화면이 조립하지 못한다.
    인자에는 프로그램 이름을 넣지 않는다 — pywinpty 가 실행 파일 이름을 앞에 붙인다.

    npm 이 깐 `claude.cmd` 는 cmd.exe 를 거쳐야 돈다. `/s /c "..."` 는 바깥 따옴표 한 쌍만 벗기므로
    공백 있는 경로와 첫 마디의 따옴표가 그대로 남는다. 경로는 PATH 에서 찾은 절대 경로이고, 제작 폴더
    안의 같은 이름 파일은 쓰지 않는다 (codex1 지적).
    """
    exe = Path(_exe(client)).resolve()
    if not _registered(client):
        raise ClientError(f"{CLIENTS[client]} 에 VaderAI 가 연결되어 있지 않습니다.")
    if workshop.root().resolve() in exe.parents:
        raise ClientError(f"{CLIENTS[client]} 실행 파일이 제작 폴더 안에 있습니다. 설치 위치를 확인하세요.")
    # 고치는 룬이면 그 id 를 첫 마디에 — id 는 소문자·숫자·밑줄뿐이라(`workshop.project` 가 막는다) 따옴표를 깨지 못한다.
    first = EDIT_MESSAGE.format(id=rune_id) if rune_id and workshop.project(rune_id) else FIRST_MESSAGE
    if exe.suffix.lower() in (".cmd", ".bat"):
        shell = os.environ.get("ComSpec") or str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe")
        return shell, f'/d /s /c ""{exe}" "{first}""'
    return str(exe), f'"{first}"'   # 프로그램 이름은 ConPTY 가 앞에 붙인다


def _exe(client: str) -> str:
    if client not in CLIENTS:
        raise ClientError("지원하지 않는 프로그램입니다.")
    exe = shutil.which(client)
    if not exe:
        raise ClientError(f"{CLIENTS[client]} 가 이 PC 에 설치되어 있지 않습니다.")
    return exe


def _registered(client: str) -> bool:
    try:
        if client == "claude":
            data = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
            return NAME in data.get("mcpServers", {})
        if client == "agy":
            return NAME in _run([shutil.which("agy") or "agy", "mcp", "list"], timeout=15).stdout
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return NAME in tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8")).get("mcp_servers", {})
    except (OSError, ValueError):
        return False


def _refresh_path() -> None:
    """설치 관리자가 바꾼 PATH 는 이미 떠 있는 프로그램에 안 보인다 — 레지스트리에서 다시 읽어 앞에 붙인다."""
    import winreg
    keys = ((winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment"))
    parts = []
    for root, key in keys:
        try:
            with winreg.OpenKey(root, key) as handle:
                parts.append(os.path.expandvars(winreg.QueryValueEx(handle, "Path")[0]))
        except OSError:
            pass
    os.environ["PATH"] = os.pathsep.join([*parts, os.environ.get("PATH", "")])


def _run(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
