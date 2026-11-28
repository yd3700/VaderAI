"""2b — 기존 공용 데이터를 로그인한 계정으로 옮기기 (마스터 결정 2026-11-05, codex1 설계·검토)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from vader.core import account, claim, config, gate

ORIGIN = "https://vaderai.test"


@pytest.fixture
def pc(monkeypatch):
    """옛 공용 데이터가 있는 PC. 새 앱에서 로그인·PIN 까지 했고 앱은 꺼져 있다."""
    home = config.HOME
    (home / "runes" / "eonm").mkdir(parents=True)
    (home / "runes" / "eonm" / "__init__.py").write_text("# 룬", encoding="utf-8")
    (home / "jobs").mkdir()
    (home / "seals.json").write_text("{}", encoding="utf-8")
    (home / "devices.json").write_text("{}", encoding="utf-8")
    (home / "crash").mkdir()
    (home / "crash-raw").mkdir()                                     # 진단 원본(절대 경로가 섞임) — 기기 층에 남아야 다음 시작에 정리된다
    (home / "crash-raw" / "native_raw_x.txt").write_text("raw", encoding="utf-8")
    (home / "catalog").mkdir()
    for name in ("state.json", "blocked.json", "origins.json", "managed.json"):
        (home / "catalog" / name).write_text("{}", encoding="utf-8")
    (home / "settings.json").write_text(json.dumps({"runes": {"eonm": {"x": 1}}, "account": {"site": "https://dev.test"},
                                                    "update": {"feed": "C:/feed"}}), encoding="utf-8")
    (home / "secrets.json").write_text(json.dumps({"api.gemini": "암호문", "account.device_token.ab": "옛 토큰"}),
                                       encoding="utf-8")
    monkeypatch.setattr(account, "signed_in", lambda: True)
    monkeypatch.setattr(gate, "who", lambda: {"state": "ok", "origin": ORIGIN,
                                              "account": {"id": "acc-a", "display_name": "가나메디칼"}})
    folder = gate.open_profile(ORIGIN, "acc-a")
    (folder / "runes" / "eonm").mkdir(parents=True)                 # 첫 로그인 때 깔린 번들 룬
    (folder / "settings.json").write_text("{}", encoding="utf-8")
    (folder / "lock.json").write_text("{}", encoding="utf-8")       # PIN 을 정했다
    yield home, folder
    gate.release_instance()


def finished(home, folder):
    assert (folder / "runes" / "eonm" / "__init__.py").exists() and (folder / "jobs").is_dir()
    assert (folder / "seals.json").exists() and (folder / "catalog" / "origins.json").exists()
    assert (folder / "catalog" / "managed.json").exists()
    for kept in ("crash", "crash-raw/native_raw_x.txt", "devices.json", "catalog/state.json", "catalog/blocked.json"):   # 기기 층·옛 연결은 남는다
        assert (home / kept).exists(), kept
    aside = next(folder.glob(".before-claim-*"))                    # 계정 쪽에 있던 것은 지우지 않고 비켜 둔다
    assert (aside / "runes" / "eonm").is_dir() and (aside / "settings.json").exists()
    assert (folder / "lock.json").exists() and (folder / "profile.json").exists()
    assert json.loads((folder / "secrets.json").read_text(encoding="utf-8")) == {"api.gemini": "암호문"}
    assert json.loads((folder / "settings.json").read_text(encoding="utf-8")) == {"runes": {"eonm": {"x": 1}},
                                                                                  "account": {}, "update": {}}
    assert config.device_get("account.site") is None                # 로그인 서버는 이관 중에 안 바뀐다 (codex1 P1-1)
    assert (home / "legacy-claimed" / "secrets.json").exists() and (home / "legacy-claimed" / "settings.json").exists()
    assert not (home / "claim.json").exists() and config.device_get("legacy_claimed")


def test_looking_moves_no_business_data(pc):
    home, folder = pc
    assert claim.main([]) == 0
    assert (home / "runes").exists() and not (folder / "jobs").exists() and not (home / "claim.json").exists()


def test_apply_moves_business_data_and_leaves_the_device_layer(pc):
    home, folder = pc
    assert claim.main(["--apply"]) == 0
    finished(home, folder)
    assert claim.main(["--apply"]) == 1                             # 한 번만


def _crash_on_rename(monkeypatch, n):
    real, calls = os.rename, []

    def rename(src, dst):
        calls.append(1)
        if len(calls) == n:
            raise OSError("이름 바꾸기 전에 멈춤")
        real(src, dst)
    monkeypatch.setattr(claim.os, "rename", rename)


def _crash_after_rename(monkeypatch, n):
    real, calls = claim._save, []

    def save(path, data, indent=None):
        if path == claim.ledger_path():
            calls.append(1)
            if len(calls) == n:                                     # 이름은 바꿨는데 원장을 못 고쳤다
                raise OSError("원장 쓰기 전에 멈춤")
        real(path, data, indent)
    monkeypatch.setattr(claim, "_save", save)


def _crash_before_keeping_original(monkeypatch, _):
    real = os.replace

    def replace(src, dst):
        if "legacy-claimed" in str(dst) and str(dst).endswith("settings.json"):
            raise OSError("계정 설정은 썼는데 원본을 못 옮김")
        real(src, dst)
    monkeypatch.setattr(claim.os, "replace", replace)


def _crash_before_dropping_ledger(monkeypatch, _):
    real = config.device_set

    def device_set(key, value):
        real(key, value)
        raise OSError("끝 표시는 했는데 원장을 못 지움")
    monkeypatch.setattr(config, "device_set", device_set)


@pytest.mark.parametrize("crash,n", [(_crash_on_rename, 3), (_crash_after_rename, 3), (_crash_after_rename, 6),
                                     (_crash_before_keeping_original, 0), (_crash_before_dropping_ledger, 0)])
def test_a_stop_at_any_step_resumes_to_the_same_result(pc, monkeypatch, crash, n):
    home, folder = pc
    with monkeypatch.context() as m:
        crash(m, n)
        with pytest.raises(OSError):
            claim.main(["--apply"])
    assert (home / "claim.json").exists()                           # 계획이 남았다
    monkeypatch.setattr(gate, "who", lambda: pytest.fail("이어 갈 때는 처음 승인한 계획대로"))
    assert claim.main([]) == 0 and (home / "claim.json").exists()  # 보기는 이어 가지 않는다
    assert claim.main(["--apply"]) == 0
    finished(home, folder)


def test_a_tampered_ledger_is_refused(pc):
    home, folder = pc
    ops = claim.plan(folder)
    ledger = {"version": 1, "origin": ORIGIN, "account_id": "acc-a", "name": "", "target": str(folder),
              "ops": [["move", str(home / "jobs"), "C:/Windows/jobs"], *ops]}
    claim._save(home / "claim.json", ledger)
    assert claim.main(["--apply"]) == 1 and (home / "jobs").exists()
    claim._save(home / "claim.json", {**ledger, "ops": ops, "account_id": "acc-b"})
    assert claim.main(["--apply"]) == 1 and (home / "jobs").exists()
    other = gate.root(ORIGIN, "acc-b")
    for bad in (["move", str(home / "jobs"), str(other / "jobs")],                       # 다른 계정 폴더로
                ["move", str(home / "device-secrets.json"), str(folder / "x.json")],     # 기기 파일을
                ["move", str(folder / "profile.json"), str(folder / ".before-claim-1" / "profile.json")],
                ["move", str(home / "jobs")]):                                          # 모양이 틀림
        claim._save(home / "claim.json", {**ledger, "ops": [bad, *ops]})
        assert claim.main(["--apply"]) == 1 and (home / "jobs").exists(), bad


def test_a_planned_move_validates_the_same_before_and_after_each_step(pc):
    home, folder = pc
    ops = claim.plan(folder)
    claim._check_ops(ops, folder)
    claim._do(folder, ops[0])
    claim._do(folder, ops[1])
    claim._check_ops(ops, folder)                                   # 옮긴 뒤에도 같은 기준으로 통과


@pytest.mark.skipif(os.name != "nt", reason="정션은 Windows")
def test_a_junction_is_refused_before_anything_moves(pc, tmp_path):
    home, folder = pc
    outside = tmp_path / "outside"
    outside.mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(home / "workshop"), str(outside)], check=True, capture_output=True)
    assert claim.main(["--apply"]) == 1 and (home / "runes").exists() and not (home / "claim.json").exists()


def test_unreadable_files_are_refused_before_anything_moves(pc):
    home, folder = pc
    (home / "secrets.json").write_text("[1, 2", encoding="utf-8")
    assert claim.main(["--apply"]) == 1
    assert (home / "runes").exists() and (folder / "settings.json").exists() and not (home / "claim.json").exists()


def test_refuses_without_a_pin_or_with_paths_pointing_elsewhere(pc):
    home, folder = pc
    (folder / "lock.json").unlink()
    assert claim.main(["--apply"]) == 1 and (home / "runes").exists()
    (folder / "lock.json").write_text("{}", encoding="utf-8")
    (home / "settings.json").write_text(json.dumps({"processing": {"data_dir": "D:/vader"}}), encoding="utf-8")
    assert claim.main(["--apply"]) == 1 and (home / "runes").exists()


def test_the_app_and_a_move_never_run_together(pc):
    """다른 프로세스(앱)가 기기 잠금을 잡고 있으면 옮기지 않는다 (codex1 P1-2)."""
    home, _ = pc
    holder = subprocess.Popen([sys.executable, "-c", (
        "import sys, time; from pathlib import Path; from vader.core import config, gate; "
        f"config.HOME = Path({str(home)!r}); assert gate.hold_instance(); print('held', flush=True); time.sleep(30)")],
        stdout=subprocess.PIPE, text=True, cwd=os.getcwd())
    try:
        assert holder.stdout.readline().strip() == "held"
        assert claim.main(["--apply"]) == 1 and (home / "runes").exists()
    finally:
        holder.kill()
        holder.wait()
    time.sleep(0.2)
    assert claim.main(["--apply"]) == 0


def test_the_app_will_not_open_an_account_while_a_move_is_unfinished(pc, monkeypatch):
    from vader.desktop import host
    home, _ = pc
    (home / "claim.json").write_text("{}", encoding="utf-8")
    api = host.DesktopAPI()
    assert api.call("gate_status")["data"] == {"state": "claiming"}
    assert not api.call("gate_unlock", {"pin": "482915"})["ok"]
