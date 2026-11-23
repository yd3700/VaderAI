"""룬 비밀 설정(kind="secret")은 settings.json 이 아니라 Windows 보호 저장에 있다 (2026-10-21 마스터 결정).

지키려는 것: 비밀번호가 settings.json 에 평문으로 남지 않는다 · 룬은 전과 같이 ctx.params 로 받는다 ·
옛 평문은 옮긴 뒤 지운다.
"""
import json
import sys

import pytest

from vader.core import config, secrets

if sys.platform != "win32":
    pytest.skip("DPAPI 는 Windows 에만 있다", allow_module_level=True)


def settings_text(isolated_settings):
    return isolated_settings.read_text(encoding="utf-8")


def test_a_secret_field_is_kept_out_of_settings_but_reaches_the_rune(isolated_settings):
    config.set_rune_configs("eonm_ipgo", {"employee_id": "u1", "password": "s3cret!"}, secret=["password"])
    assert "s3cret!" not in settings_text(isolated_settings)
    assert config.rune_config("eonm_ipgo") == {"employee_id": "u1", "password": "s3cret!"}
    assert "password" not in config.rune_config("ins_singo")       # 다른 룬에는 안 섞인다


def test_an_old_plain_password_moves_once_and_disappears(isolated_settings):
    config.set_rune_configs("eonm_ipgo", {"password": "old-plain", "window_title": "w"})
    assert "old-plain" in settings_text(isolated_settings)

    assert config.migrate_rune_secrets("eonm_ipgo", ["password"]) == ["password"]
    assert "old-plain" not in settings_text(isolated_settings)
    assert "old-plain" not in isolated_settings.with_name("settings.json.bak").read_text(encoding="utf-8")
    assert json.loads(settings_text(isolated_settings))["runes"]["eonm_ipgo"] == {"window_title": "w"}
    assert config.rune_config("eonm_ipgo")["password"] == "old-plain"
    assert config.migrate_rune_secrets("eonm_ipgo", ["password"]) == []    # 두 번째는 할 일이 없다


def test_an_unreadable_secret_looks_empty_instead_of_breaking(isolated_settings, monkeypatch):
    config.set_rune_configs("eonm_ipgo", {"password": "x"}, secret=["password"])
    monkeypatch.setattr(secrets, "get", lambda name: (_ for _ in ()).throw(secrets.SecretError("다른 PC")))
    assert config.rune_config("eonm_ipgo") == {}
