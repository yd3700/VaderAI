"""
설정 로더.

    코어 설정            룬 설정
    ────────────         ─────────────────
    api.*                runes.{rune_id}.*
    place.*
    processing.*

코어는 `runes` 블록 안을 해석하지 않는다. 해당 룬에만 전달한다.
새 룬을 붙일 때 코어 설정은 건드리지 않는다.

IchorAI는 EONM 전용 값(rpa.*, paths.*, processing.alias_*)이 코어 설정에
섞여 있었고, 두 번째 룬을 붙일 자리가 없었다. 여기서는 처음부터 분리한다.
하위 호환 폴백이 없으므로 IchorAI 판보다 단순하다.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from copy import deepcopy
from pathlib import Path
from collections.abc import Iterable
from typing import Any

#: 설치본(PyInstaller)으로 도는가. 설치 폴더는 업데이트 때 통째로 바뀌므로 설정은 그 밖에 둔다.
FROZEN = bool(getattr(sys, "frozen", False))
_ROOT = Path(sys._MEIPASS) if FROZEN else Path(__file__).resolve().parent.parent.parent
SETTINGS_PATH = (Path.home() / ".vaderai" / "settings.json") if FROZEN else _ROOT / "settings.json"
TEMPLATE_PATH = _ROOT / "settings.template.json"
#: 설치본에 실린 룬 (`packaging/vaderai.spec`). 개발 실행에는 없다.
BUNDLED_RUNES = _ROOT / "runes"

#: 설치본에서 `-m` 으로 띄울 수 있는 모듈 — 자식 프로세스(MCP 서버·제작소 시험)와 기존 데이터 이관 명령(2b)만.
CHILD_MODULES = ("vader.mcp_server", "vader.core.workshop", "vader.core.claim", "vader.desktop.ghost")


def module_command(name: str) -> list[str]:
    """`python -m <name>` 에 해당하는 명령. 설치본에는 python 이 없어 옆의 콘솔 실행 파일이 대신 띄운다."""
    if FROZEN:
        return [str(Path(sys.executable).with_name("vaderai-cli.exe")), "--module", name]
    exe = Path(sys.executable)
    console = exe.with_name("python.exe")   # MCP 는 표준 입출력으로 대화한다 — 창 없는 pythonw 대신
    return [str(console if exe.name.lower() == "pythonw.exe" and console.exists() else exe), "-m", name]


#: 기기 층 — 로그인 전에 읽어도 되는 것만 (웹 주소·업데이트·로그인 토큰 금고·잠금 설정·오류 진단·광장 회수 기록).
#: 계정 층은 `HOME/accounts/<키>/` — 로그인하고 PIN 을 푼 뒤에만 연다 (2단계, 마스터 결정 2026-11-05, codex1 설계).
HOME = Path.home() / ".vaderai"
#: 앱·자식 프로세스가 켠다. 켜져 있으면 계정 작업공간이 열리기 전엔 업무 설정·데이터를 못 읽는다 — 공용 폴더로 새지 않게.
REQUIRE_WORKSPACE = False
_workspace: Path | None = None
WORKSPACE_ENV = "VADERAI_WORKSPACE"


class WorkspaceLocked(RuntimeError):
    """로그인·잠금 해제 전이라 업무 데이터를 열 수 없다."""


def use_workspace(root: Path) -> None:
    """이 프로세스의 계정 작업공간. **한 번만** — 바꾸려면 앱을 다시 켠다. 자식 프로세스는 환경변수로 이어받는다."""
    global _workspace, SETTINGS_PATH
    root = Path(root)
    with _lock:
        if _workspace is not None and _workspace != root:
            raise RuntimeError("이미 다른 계정의 작업공간이 열려 있습니다 — 앱을 다시 켜야 합니다.")
        root.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH = root / "settings.json"
        if not SETTINGS_PATH.exists() and TEMPLATE_PATH.exists():
            SETTINGS_PATH.write_text(TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        _workspace = root
        os.environ[WORKSPACE_ENV] = str(root)
        invalidate()


def workspace() -> Path | None:
    return _workspace


def _need_workspace() -> None:
    if REQUIRE_WORKSPACE and _workspace is None:
        raise WorkspaceLocked("로그인하고 잠금을 푼 뒤에 열 수 있습니다.")


def device_get(key_path: str, default: Any = None) -> Any:
    """기기 설정(`HOME/device.json`). 로그인 전에도 읽는다 — 업무 설정은 여기 두지 않는다."""
    try:
        data: Any = json.loads((HOME / "device.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    for key in key_path.split("."):
        if not isinstance(data, dict) or data.get(key) is None:
            return default
        data = data[key]
    return data


def device_set(key_path: str, value: Any) -> None:
    """기기 설정 한 값. None 이면 지운다. 임시 파일에 다 쓴 뒤 바꿔 끼운다."""
    path = HOME / "device.json"
    with _lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        *parents, last = key_path.split(".")
        node = data
        for key in parents:
            node = node.setdefault(key, {})
        if value is None:
            node.pop(last, None)
        else:
            node[last] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".device.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)


_cache: dict | None = None

#: 캐시를 읽고 채우고 비우는 것을 직렬화한다. 재진입 락인 이유는
#: `set_rune_config` 가 `load()` 와 `_write()`(-> `invalidate()`)를 연쇄로
#: 부르기 때문이다.
#
# 왜 필요한가: 락이 없으면 스레드 A 가 파일을 읽고 멈춘 사이 스레드 B 가 새
# 값을 쓰고 `invalidate()` 하면, 깨어난 A 가 **옛 내용을 캐시에 되박아**
# 무효화를 없앤다. 그 뒤 모든 읽기가 옛 값을 본다. 두 룬 동시 실행 검증에서
# 결정적으로 재현됐다.
_lock = threading.RLock()


def load() -> dict:
    """설정 전체. **캐시를 참조로 돌려준다** — 호출자가 바꾸면 전역이 바뀐다.

    쓰기 경로(`set_rune_config`)가 이 참조를 제자리에서 고치고 저장하므로 사본을
    주지 않는다. 룬에게 넘어가는 경계에서는 `rune_config()` 가 사본을 준다.
    """
    global _cache
    with _lock:
        if _cache is None:
            _need_workspace()
            if FROZEN and not SETTINGS_PATH.exists() and TEMPLATE_PATH.exists():
                SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)   # 첫 실행 — 틀에서 만든다
                SETTINGS_PATH.write_text(TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            if not SETTINGS_PATH.exists():
                raise FileNotFoundError(
                    f"settings.json이 없습니다: {SETTINGS_PATH}\n"
                    f"{TEMPLATE_PATH.name}을 복사해서 만드세요."
                )
            _cache = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return _cache


def invalidate() -> None:
    """다음 load()에서 파일을 다시 읽게 한다. 테스트와 저장 후에 쓴다."""
    global _cache
    with _lock:
        _cache = None


def get(key_path: str, default: Any = None) -> Any:
    """점 구분 경로로 값을 읽는다. 예: get("api.providers.gemini.model")"""
    data: Any = load()
    for key in key_path.split("."):
        if not isinstance(data, dict):
            return default
        data = data.get(key)
        if data is None:
            return default
    return data


def _write(data: dict) -> None:
    """임시 파일에 다 쓴 뒤 바꿔 끼운다 — 쓰다 멈춰도 settings.json 이 반쪽이 되지 않는다.
    바로 전 내용은 `settings.json.bak` 에 남는다 (codex1 지적: 그냥 덮어쓰고 있었다)."""
    import os
    import shutil
    from uuid import uuid4

    with _lock:
        temporary = SETTINGS_PATH.with_name(f".{SETTINGS_PATH.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(data, ensure_ascii=False, indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            if SETTINGS_PATH.exists():
                shutil.copy2(SETTINGS_PATH, SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".bak"))
            os.replace(temporary, SETTINGS_PATH)
        finally:
            temporary.unlink(missing_ok=True)
        invalidate()


# ── 룬 설정 ─────────────────────────────────────────────────────

def rune_config(rune_id: str) -> dict:
    """룬 설정 블록 전체. 그대로 RuneContext.params가 된다.

    **깊은 사본을 준다.** `get()` 은 캐시 안의 딕셔너리를 참조로 돌려주므로,
    사본 없이 넘기면 룬이 `ctx.params` 를 건드리는 것이 전역 설정을 바꾸고 다른
    룬의 params 까지 오염시킨다. 두 룬 동시 실행 검증에서 확인했다.
    """
    with _lock:
        params = deepcopy(get(f"runes.{rune_id}", {}) or {})
    params.update(_rune_secrets(rune_id))
    return params


def rune_secret_name(rune_id: str, key: str) -> str:
    return f"runes.{rune_id}.{key}"


def _rune_secrets(rune_id: str) -> dict[str, str]:
    """룬의 비밀 설정(`kind="secret"`) — settings.json 이 아니라 Windows 보호 저장에 있다 (2026-10-21)."""
    from vader.core import secrets

    if not secrets.available():
        return {}
    prefix = rune_secret_name(rune_id, "")
    try:
        names = secrets.names()
    except Exception:
        return {}   # 보호 저장 파일이 깨졌다 — 설정 화면이 비어 보이고 다시 넣게 된다
    found = {}
    for name in names:
        if name.startswith(prefix):
            try:
                found[name[len(prefix):]] = secrets.get(name)
            except Exception:
                pass   # 다른 PC·다른 사용자의 암호문 — 못 푼다. 비어 있는 것으로 보고 다시 넣게 한다
    return found


def rune_get(rune_id: str, key: str, default: Any = None) -> Any:
    return get(f"runes.{rune_id}.{key}", default)


def set_rune_config(rune_id: str, key: str, value: Any) -> None:
    data = load()
    data.setdefault("runes", {}).setdefault(rune_id, {})[key] = value
    _write(data)


def set_rune_configs(rune_id: str, values: dict[str, Any], secret: Iterable[str] = ()) -> None:
    """룬 설정 여러 개를 한 번에 쓴다.

    `set_rune_config`를 반복하면 키마다 파일을 다시 쓴다. 중간에 실패하면
    반쯤 설정된 룬이 남고, 그건 룬이 돌다가 엉뚱한 곳에서 죽는 상태다.

    `secret` 에 든 키(매니페스트의 `kind="secret"`)는 Windows 보호 저장에 쓰고 settings.json 에는 남기지 않는다.
    """
    from vader.core import secrets

    values = dict(values)
    hidden = {key: values.pop(key) for key in secret if key in values} if secrets.available() else {}
    for key, value in hidden.items():
        secrets.put(rune_secret_name(rune_id, key), str(value))
    with _lock:
        data = load()
        block = data.setdefault("runes", {}).setdefault(rune_id, {})
        block.update(values)
        for key in hidden:
            block.pop(key, None)
        _write(data)


def migrate_rune_secrets(rune_id: str, keys: Iterable[str]) -> list[str]:
    """settings.json 에 평문으로 남은 룬 비밀 설정을 보호 저장으로 옮긴다. 옮긴 키들.

    `migrate_keys` 와 같다 — 암호화 → 다시 풀어 확인한 뒤에만 평문을 지운다. 실패한 키는 평문 그대로 둔다.
    """
    from vader.core import secrets

    if not secrets.available():
        return []
    moved = []
    with _lock:
        data = load()
        block = (data.get("runes") or {}).get(rune_id)
        if not isinstance(block, dict):
            return []
        for key in keys:
            plain = block.get(key)
            if not isinstance(plain, str) or not plain:
                continue
            try:
                secrets.put(rune_secret_name(rune_id, key), plain)   # 다시 풀어 같은지 확인까지 한다
            except Exception:
                continue
            del block[key]
            moved.append(key)
        if moved:
            _write(data)
            _write(data)   # 두 번 쓴다 — 첫 번째는 평문이 든 직전 내용을 settings.json.bak 에 남긴다
    return moved


def installed_rune_ids() -> list[str]:
    """설정에 블록이 있는 룬 목록. 밑줄로 시작하는 키는 주석이므로 뺀다.

    **설치 여부의 원본이 아니다.** 이름이 그렇게 읽혀서 실제로 한 번 틀렸다 —
    펫이 이걸로 "설치된 룬" 을 말하다가, 제거했는데 설정이 남은 룬을 있다고
    할 뻔했다. 제거는 설정과 체크포인트를 **일부러 남긴다**(다시 설치하면
    그대로 쓰라고), 그래서 설정과 디스크는 늘 어긋날 수 있다.

    무엇이 설치됐는지는 `loader.discover()` 에게 물어라.
    """
    return sorted(
        rune_id
        for rune_id in (get("runes", {}) or {})
        if not rune_id.startswith("_")
    )


# ── 코어 설정 ───────────────────────────────────────────────────

def ocr_provider() -> str:
    return get("api.ocr_provider", "gemini")


def provider_for(role: str) -> str:
    """그 역할이 탈 제공자.

    배정(`api.roles.{role}.provider`)이 없으면 전역값을 상속한다. **배정을
    안 한 설정은 지금과 똑같이 돈다** — 역할을 나누는 것은 절약을 원하는
    사람의 선택이지 모두가 치러야 할 설정 비용이 아니다.
    """
    return get(f"api.roles.{role}.provider") or ocr_provider()


def model_for(role: str) -> str | None:
    """그 역할만 다른 모델로 돌린다. 없으면 제공자의 기본값.

    **같은 제공자 안에서 모델만 바꾸는 것이 가장 흔한 절약 형태다** —
    문서 추출은 큰 모델로, 이미지가 필요 없는 텍스트 판단은 싼 모델로.
    """
    return get(f"api.roles.{role}.model")


def api_key(provider: str | None = None) -> str:
    """키는 Windows 보호 저장(`secrets`)에서 읽는다. 옮기기 전의 평문이 settings 에 남아 있으면 그것."""
    from vader.core import secrets

    provider = provider or ocr_provider()
    secret = secret_name(connection_of(provider))
    key = _plain_key(provider) or (secrets.get(secret) if secrets.available() else None)
    if not key:
        raise ValueError(f"{provider} API 키가 없습니다. VaderAI 의 'AI 선택' 에서 키를 넣으세요.")
    return key


def secret_name(provider: str) -> str:
    return f"api.providers.{provider}.api_key"


def connection_of(provider: str) -> str:
    """키를 쥔 연결. 같은 연결에 더한 모델은 `connection` 으로 그 키를 가리킨다 — 키를 카드마다 복사하지 않는다.
    없으면 자기 자신(옛 설정은 카드 하나 = 연결 하나)."""
    return get(f"api.providers.{provider}.connection") or provider


def _plain_key(provider: str) -> str:
    key = get(f"api.providers.{provider}.api_key", "") or ""
    return "" if key.startswith("여기에") else key


def migrate_keys() -> list[str]:
    """settings.json 의 평문 키를 보호 저장으로 옮긴다. 옮긴 제공자 이름들.

    하나씩 암호화 → 다시 풀어 확인한 뒤에만 평문을 지운다. 실패하면 그 키는 평문 그대로 남는다 —
    설정을 망가뜨리느니 옮기지 않는다. 평문 키가 없는 다른 항목(예: 검색 키)은 건드리지 않는다.
    """
    from vader.core import secrets

    if not secrets.available():
        return []
    moved = []
    with _lock:
        data = load()
        for name, block in (data.get("api", {}).get("providers") or {}).items():
            plain = block.get("api_key") if isinstance(block, dict) else None
            if not plain or not isinstance(plain, str) or plain.startswith("여기에"):
                continue
            try:
                secrets.put(secret_name(name), plain)
            except Exception:
                continue
            del block["api_key"]
            moved.append(name)
        if moved:
            _write(data)
            # 바로 전 내용을 남기는 백업에도 평문이 있다 — 옮긴 뒤 내용으로 덮어 평문을 없앤다.
            import shutil
            shutil.copy2(SETTINGS_PATH, SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".bak"))
    return moved


def api_request_delay_seconds() -> float:
    """API 호출 사이 대기 시간. 트래픽 분산용 코어 설정이지 룬 설정이 아니다."""
    return float(get("api.request_delay_seconds", 5))


def set_dev_mode(on: bool) -> None:
    """룬 제작소의 개발 모드. 사람이 화면에서 켠다 — 켜져 있을 때만 MCP 시험이 룬 코드를 돌린다."""
    data = load()
    data.setdefault("workshop", {})["dev_mode"] = bool(on)
    _write(data)


def set_rune_enabled(rune_id: str, on: bool) -> None:
    """내 룬의 체크 — 끈 룬은 코어 화면에 안 나온다. 설치·승인·설정은 그대로다.

    룬 설정(`runes.<id>`)이 아니라 화면 설정이다. 룬이 자기 활성 여부를 알 일이 없다.
    """
    data = load()
    hidden = set(data.setdefault("desktop", {}).get("hidden_runes", []))
    (hidden.discard if on else hidden.add)(rune_id)
    data["desktop"]["hidden_runes"] = sorted(hidden)
    _write(data)


def data_dir() -> Path:
    """Job 작업 폴더와 체크포인트가 사는 곳. 소스 트리 밖. 계정 작업공간이 열렸으면 그것 — 다른 경로 지정은 무시한다 (codex1)."""
    _need_workspace()
    if _workspace is not None:
        return _workspace
    configured = get("processing.data_dir")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".vaderai"


def rune_dir() -> Path:
    """설치된 룬이 사는 곳. 소스 트리 밖이라 패키징을 막지 않는다."""
    _need_workspace()
    if _workspace is not None:
        return _workspace / "runes"
    configured = get("processing.rune_dir")
    if configured:
        return Path(configured).expanduser()
    return data_dir() / "runes"


def cache_days() -> int:
    return int(get("processing.cache_days", 30))


def api_retry_max() -> int:
    return int(get("processing.api_retry_max", 3))
