"""2b — 이 PC 의 기존 공용 데이터를 **로그인한 계정**의 작업공간으로 옮긴다 (마스터 결정 2026-11-05, codex1 설계·검토).

앱은 주인을 판별할 수 없다. 그래서 화면 버튼이 없고, 주인이 이 명령을 직접 실행한다::

    (설치본) vaderai-cli.exe --module vader.core.claim            # 무엇을 옮길지 보기만 (업무 자료는 안 움직인다)
    (설치본) vaderai-cli.exe --module vader.core.claim --apply    # 옮기기

- 먼저 새 앱에서 로그인하고 PIN 까지 정한 뒤 앱을 닫는다. 한 PC 에서 한 번만.
- 앱과 이 명령은 같은 기기 잠금(`gate.hold_instance`)을 잡는다 — 동시에 돌지 않는다. 멈춘 이관의 원장
  (`claim.json`)이 남아 있으면 앱은 그 계정을 열지 않는다 — 다시 실행해 마무리할 때까지 아무도 쓰지 않는다.
- 옮기는 것: 기기 층(로그인 토큰·device.json·오류 진단·광장 회수 기록·계정 폴더)을 뺀 전부. 광장 설치 원장
  (managed·origins)만 catalog 에서 꺼낸다. 옛 기기 연결(devices.json)은 남긴다 — 다시 연결한다.
- 옛 secrets.json 은 키별로 — AI·룬 키만 계정 금고로. 옛 settings.json 은 업무 설정만 계정으로 — 웹 주소
  (`account.site`)·업데이트 주소는 옮기지 않는다: 이관 중에 로그인 서버가 바뀌면 방금 확인한 계정과 어긋난다.
  두 원본은 지우지 않고 `legacy-claimed/` 에 남긴다 (옛 로그인 토큰이 들어 있다 — 확인 뒤 지워도 된다).
- 같은 볼륨 안의 이름 바꾸기다. 계획을 원장에 먼저 적고 한 단계씩 지운다. 중간에 멈추면 같은 명령으로 이어 간다.
  대상에 같은 이름이 있으면 지우지 않고 `.before-claim-<시각>/` 으로 비켜 둔다.
- 파일 안의 절대 경로는 바꾸지 않는다 — 옛 폴더를 가리키던 제작소 연결 등은 앱에서 다시 고른다.
- 설치본 자료만 다룬다. 개발 실행의 저장소 settings.json 은 손대지 않는다.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from vader.core import account, config, gate

#: 기기 층 — 옮기지 않는다.
DEVICE = {"accounts", "crash", "catalog", "device.json", "device-secrets.json", "session.json", "claim.json",
          "devices.json", "app.lock"}
ACCOUNT_CATALOG = ("managed.json", "origins.json")
LEDGER_VERSION = 1


class ClaimError(RuntimeError):
    pass


def ledger_path() -> Path:
    return config.HOME / "claim.json"


def plan(target: Path) -> list[list[str]]:
    """옮길 단계. **바꾸기 전에** 읽을 수 없는 것·다른 폴더를 가리키는 것을 다 걸러 낸다 (codex1 P2-1)."""
    home = config.HOME
    legacy = _read_dict(home / "settings.json", "옛 settings.json")
    _read_dict(home / "secrets.json", "옛 secrets.json")
    _read_dict(target / "secrets.json", "계정 secrets.json")
    for key in ("data_dir", "rune_dir"):
        if (legacy.get("processing") or {}).get(key):
            raise ClaimError(f"옛 설정의 processing.{key} 가 다른 폴더를 가리킵니다. 그 폴더는 손으로 옮겨야 합니다.")
    aside = target / f".before-claim-{time.strftime('%Y%m%d-%H%M%S')}"
    moves = [(home / e.name, target / e.name) for e in sorted(home.iterdir()) if _business(e.name)]
    moves += [(home / "catalog" / n, target / "catalog" / n) for n in ACCOUNT_CATALOG if (home / "catalog" / n).exists()]
    for path in [target, home / "catalog", *(src for src, _ in moves), *(dst for _, dst in moves if dst.exists())]:
        if _linked(path):    # 다른 폴더로 이어진 링크·정션 — 옮기면 바깥을 건드린다. 손으로 정리한다 (codex1)
            raise ClaimError(f"{path.name} 은 다른 폴더로 이어진 링크입니다. 손으로 정리한 뒤 다시 실행하세요.")
    ops = []
    for src, dst in moves:
        if dst.exists():
            ops.append(["move", str(dst), str(aside / dst.relative_to(target))])
        ops.append(["move", str(src), str(dst)])
    if (home / "settings.json").exists():
        if (target / "settings.json").exists():
            ops.append(["move", str(target / "settings.json"), str(aside / "settings.json")])
        ops.append(["settings"])
    if (home / "secrets.json").exists():
        ops.append(["secrets"])
    _check_ops(ops, target)                                     # 처음 계획도 재개와 같은 기준으로
    return ops


def _business(name: str) -> bool:
    return name not in DEVICE | {"secrets.json", "settings.json"} and not name.startswith((".", "legacy-claimed"))


def _linked(path: Path) -> bool:
    """심볼릭 링크·정션(재분석 지점). Path.is_junction 은 3.12 부터라 3.11 에서는 속성을 직접 본다 (CI 3.11 실패 2026-11-10)."""
    try:
        return path.is_symlink() or getattr(os.lstat(path), "st_reparse_tag", 0) == 0xA0000003   # IO_REPARSE_TAG_MOUNT_POINT — OneDrive 자리표시 파일은 아니다
    except OSError:
        return False


def _check_ops(ops: list, target: Path) -> None:
    """원장의 단계는 세 모양뿐 — 옛 업무 항목 → 승인한 계정의 같은 자리, 광장 원장 두 개, 계정 쪽 항목 → 이번
    비켜 두기 폴더의 같은 자리. 기기 파일·다른 계정 폴더는 어느 쪽에도 못 온다 (codex1 P2-2). 경로는 글자로 비교 —
    링크를 따라가지 않는다(링크는 계획 때 거절했다)."""
    home, norm = config.HOME, lambda p: Path(os.path.normpath(p))
    for op in ops:
        if op in (["settings"], ["secrets"]):
            continue
        if not (isinstance(op, list) and len(op) == 3 and op[0] == "move" and all(isinstance(p, str) for p in op[1:])):
            raise ClaimError("옮기기 기록의 모양이 올바르지 않습니다.")
        src, dst = norm(op[1]), norm(op[2])
        legacy = src.parent == home and _business(src.name) and dst == target / src.name
        ledger = src.parent == home / "catalog" and src.name in ACCOUNT_CATALOG and dst == target / "catalog" / src.name
        aside = False
        if target in src.parents and target in dst.parents:
            rel, out = src.relative_to(target).parts, dst.relative_to(target).parts
            aside = (rel[0] not in {"profile.json", "lock.json"} and not rel[0].startswith(".")
                     and out[0].startswith(".before-claim-") and out[1:] == rel)
        if not (legacy or ledger or aside):
            raise ClaimError("옮기기 기록에 허용되지 않은 경로가 있습니다.")


def run(ledger: dict) -> None:
    """원장대로 한 단계씩. 단계를 마칠 때마다 남은 단계로 원장을 바꿔 쓴다."""
    path = ledger_path()
    _save(path, ledger)
    target = Path(ledger["target"])
    while ledger["ops"]:
        _do(target, ledger["ops"][0])
        ledger = {**ledger, "ops": ledger["ops"][1:]}
        _save(path, ledger)
    config.device_set("legacy_claimed", {"target": target.name, "at": time.time()})
    path.unlink()


def _do(target: Path, op: list) -> None:
    """한 단계. 다시 실행해도 같은 결과 — 멈췄다 이어 가도 된다. 이어 가는 동안 앱은 열리지 않는다."""
    kept = config.HOME / "legacy-claimed"
    if op[0] == "move":
        src, dst = Path(op[1]), Path(op[2])
        if not src.exists() and dst.exists():
            return                                              # 지난번에 옮겼다
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)
    elif op[0] == "settings":
        src = config.HOME / "settings.json"
        if not src.exists():
            return
        data = _read_dict(src, "옛 settings.json")
        for section, name in (("account", "site"), ("update", "feed")):   # 기기 설정 — 옮기지 않는다 (codex1 P1-1)
            if isinstance(data.get(section), dict):
                data[section].pop(name, None)
        _save(target / "settings.json", data, indent=2)
        kept.mkdir(exist_ok=True)
        os.replace(src, kept / "settings.json")
    elif op[0] == "secrets":
        src = config.HOME / "secrets.json"
        if not src.exists():
            return
        old = _read_dict(src, "옛 secrets.json")
        new = _read_dict(target / "secrets.json", "계정 secrets.json")
        # 같은 Windows 사용자의 DPAPI 암호문이라 그대로 옮겨도 풀린다. 계정 쪽에 이미 있는 키가 이긴다.
        _save(target / "secrets.json", {**{k: v for k, v in old.items() if not k.startswith("account.")}, **new},
              indent=2)
        kept.mkdir(exist_ok=True)
        os.replace(src, kept / "secrets.json")                  # 옛 토큰이 든 원본 — 지우지 않고 남겨 둔다


def check_ledger(ledger: dict) -> Path:
    """멈췄던 원장이 처음 승인한 그 계정·그 경계 안의 것인가 (codex1 P2-2)."""
    try:
        target = Path(ledger["target"])
        ok = (ledger["version"] == LEDGER_VERSION and target == gate.root(ledger["origin"], ledger["account_id"])
              and isinstance(ledger["ops"], list))
        _check_ops(ledger["ops"], target)
        profile = json.loads((target / "profile.json").read_text(encoding="utf-8"))
        ok = ok and profile == {"origin": ledger["origin"], "account_id": ledger["account_id"]}
    except (OSError, ValueError, KeyError, TypeError, IndexError, ClaimError):
        ok = False
    if not ok:
        raise ClaimError("멈췄던 옮기기 기록이 처음 계획과 맞지 않습니다. 손대지 말고 관리자에게 알려 주세요.")
    return target


def _read_dict(path: Path, label: str) -> dict:
    """없으면 빈 것. 있는데 못 읽거나 모양이 틀리면 거절 — 빈 것으로 보지 않는다."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ClaimError(f"{label} 을 읽지 못했습니다. 아무것도 옮기지 않았습니다.") from None
    if not isinstance(data, dict):
        raise ClaimError(f"{label} 의 모양이 올바르지 않습니다. 아무것도 옮기지 않았습니다.")
    return data


def _save(path: Path, data, indent=None) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    os.replace(temporary, path)


def _describe(ops: list, target: Path, say) -> None:
    for op in ops:
        if op[0] == "move" and Path(op[1]).parent in (config.HOME, config.HOME / "catalog"):
            say(f"  옮김   {Path(op[1]).relative_to(config.HOME)}")
        elif op[0] == "move":
            say(f"  비켜둠 {Path(op[1]).relative_to(target)} (계정 쪽에 이미 있던 것)")
        else:
            say({"settings": "  설정   업무 설정만 계정으로 (웹 주소·업데이트 주소는 옮기지 않음)",
                 "secrets": "  키     AI·룬 키만 계정 금고로 (옛 로그인 토큰은 옮기지 않음)"}[op[0]])


def main(argv: list[str], say=print) -> int:
    apply = "--apply" in argv
    if not gate.hold_instance():                                # 앱이나 다른 옮기기가 돌고 있다 (codex1 P1-2)
        say("VaderAI 앱을 먼저 닫아 주세요. (다른 옮기기가 실행 중일 수도 있어요.)")
        return 1
    try:
        return _main(apply, say)
    except ClaimError as exc:
        say(str(exc))
        return 1
    finally:
        gate.release_instance()


def _main(apply: bool, say) -> int:
    if not config.FROZEN:
        say("개발 실행입니다 — 저장소의 settings.json 은 옮기지 않습니다 (설치본 자료만).")
    if ledger_path().exists():                                  # 지난번에 멈췄다 — 처음 승인한 계획대로 이어 간다
        try:
            ledger = json.loads(ledger_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ledger = {}
        target = check_ledger(ledger)
        say(f"멈췄던 옮기기를 이어 갑니다 — [{ledger.get('name') or '계정'}] ({ledger['account_id']}) 남은 단계:")
        _describe(ledger["ops"], target, say)
        if apply:
            run(ledger)
            say("다 옮겼습니다. 앱을 켜서 PIN 으로 들어가세요.")
        else:
            say("보기만 했습니다. 이어 가려면 --apply 를 붙여 다시 실행하세요.")
        return 0
    if config.device_get("legacy_claimed"):
        say("이 PC 의 기존 데이터는 이미 한 계정으로 옮겼습니다.")
        return 1
    if not account.signed_in():
        say("먼저 VaderAI 앱에서 로그인하고 PIN 을 정한 뒤, 앱을 닫고 다시 실행해 주세요.")
        return 1
    found = gate.who()
    if found["state"] != "ok":
        say("서버에서 계정을 확인하지 못했습니다. 인터넷 연결을 확인하거나 앱에서 다시 로그인해 주세요.")
        return 1
    origin, account_id = found["origin"], found["account"]["id"]
    name = found["account"]["display_name"] or "이름 없는 계정"
    target = gate.root(origin, account_id)
    if not gate.has_pin(target):
        say("먼저 VaderAI 앱에서 이 계정의 PIN 을 정한 뒤, 앱을 닫고 다시 실행해 주세요.")
        return 1
    gate.open_profile(origin, account_id)                       # 이미 있다 — 적힌 계정이 맞는지만 본다
    ops = plan(target)
    say(f"이 PC 의 기존 데이터를 [{name}] 계정으로 옮깁니다. 같은 Windows 를 쓰는 다른 사람의 자료일 수 있으니 확인하세요.")
    _describe(ops, target, say)
    say("  남김   기기 연결(devices.json) — 앱에서 다시 연결하세요")
    if not apply:
        say("보기만 했습니다. 옮기려면 --apply 를 붙여 다시 실행하세요.")
        return 0
    run({"version": LEDGER_VERSION, "origin": origin, "account_id": account_id, "name": name,
         "target": str(target), "ops": ops})
    say("다 옮겼습니다. 앱을 켜서 PIN 으로 들어가세요. Claude Code·Codex 는 제작소에서 다시 연결하세요.")
    say("옛 로그인 토큰이 든 원본은 ~/.vaderai/legacy-claimed/ 에 남겼습니다. 잘 되는 것을 확인한 뒤 지워도 됩니다.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
