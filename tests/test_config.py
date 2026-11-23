"""
설정 테스트.

지키려는 것: 코어 설정에 특정 룬 전용 키가 섞이지 않는다.
IchorAI는 이걸 지키지 못해서 settings의 약 60%가 EONM 전용이었고
두 번째 룬을 붙일 자리가 없었다.
"""
import json
from pathlib import Path

import pytest

from vader.core import config, secrets


@pytest.fixture
def settings(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    # 이 설정에는 data_dir 이 없다 — 키 보호 저장소가 마스터의 진짜 홈을 읽지 않게 막는다.
    monkeypatch.setattr(secrets, "_path", lambda device=False: tmp_path / ("device-secrets.json" if device else "secrets.json"))

    def _write(data: dict) -> Path:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        config.invalidate()
        return path

    yield _write
    config.invalidate()


# ── 기본 읽기 ──────────────────────────────────────────────────

class TestGet:

    def test_reads_a_nested_path(self, settings):
        settings({"api": {"providers": {"gemini": {"model": "flash"}}}})
        assert config.get("api.providers.gemini.model") == "flash"

    def test_missing_path_returns_default(self, settings):
        settings({})
        assert config.get("api.nope", "fallback") == "fallback"

    def test_path_through_non_dict_returns_default(self, settings):
        settings({"api": "not-a-dict"})
        assert config.get("api.providers.gemini", "fallback") == "fallback"

    def test_missing_file_raises_with_a_useful_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "absent.json")
        config.invalidate()
        with pytest.raises(FileNotFoundError, match="settings.template.json"):
            config.load()


# ── 룬 네임스페이스 ────────────────────────────────────────────

class TestRuneNamespace:

    def test_rune_config_returns_the_whole_block(self, settings):
        settings({"runes": {"ins_singo": {"output_dir": "D:/out", "delim": ","}}})
        assert config.rune_config("ins_singo") == {"output_dir": "D:/out", "delim": ","}

    def test_unknown_rune_returns_empty_dict(self, settings):
        settings({"runes": {}})
        assert config.rune_config("건설_공무") == {}

    def test_no_runes_block_returns_empty_dict(self, settings):
        settings({})
        assert config.rune_config("eonm_ipgo") == {}

    def test_rune_get_reads_a_single_value(self, settings):
        settings({"runes": {"eonm_ipgo": {"window_title": "입고창"}}})
        assert config.rune_get("eonm_ipgo", "window_title") == "입고창"

    def test_rune_get_default_when_absent(self, settings):
        settings({"runes": {}})
        assert config.rune_get("eonm_ipgo", "window_title", "") == ""

    def test_set_creates_the_block(self, settings):
        path = settings({})
        config.set_rune_config("건설_공무", "window_title", "공무관리시스템")
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["runes"]["건설_공무"]["window_title"] == "공무관리시스템"

    def test_set_does_not_disturb_other_runes(self, settings):
        path = settings({"runes": {"eonm_ipgo": {"master_db": "A.csv"}}})
        config.set_rune_config("ins_singo", "output_dir", "D:/out")
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["runes"]["eonm_ipgo"]["master_db"] == "A.csv"
        assert saved["runes"]["ins_singo"]["output_dir"] == "D:/out"

    def test_set_invalidates_the_cache(self, settings):
        settings({"runes": {"r": {"k": "old"}}})
        assert config.rune_get("r", "k") == "old"
        config.set_rune_config("r", "k", "new")
        assert config.rune_get("r", "k") == "new"

    def test_installed_rune_ids_skips_comment_keys(self, settings):
        settings({"runes": {"_comment": "설명", "ins_singo": {}, "eonm_ipgo": {}}})
        assert config.installed_rune_ids() == ["eonm_ipgo", "ins_singo"]

    def test_installed_rune_ids_is_empty_by_default(self, settings):
        settings({})
        assert config.installed_rune_ids() == []


# ── 코어 설정 ──────────────────────────────────────────────────

class TestCoreSettings:

    def test_api_delay_is_core_not_rune(self, settings):
        """IchorAI는 이 값을 rpa.workflow에 두어 OCR이 룬 설정에 의존했다."""
        settings({"api": {"request_delay_seconds": 2}})
        assert config.api_request_delay_seconds() == 2.0

    def test_api_delay_zero_is_honored(self, settings):
        """0은 falsy지만 유효하다 — 테스트에서 대기를 없앨 때 쓴다."""
        settings({"api": {"request_delay_seconds": 0}})
        assert config.api_request_delay_seconds() == 0.0

    def test_api_delay_default(self, settings):
        settings({})
        assert config.api_request_delay_seconds() == 5.0

    def test_api_key_rejects_the_placeholder(self, settings):
        settings({"api": {"providers": {"gemini": {"api_key": "여기에_키를"}}}})
        with pytest.raises(ValueError, match="키가 없습니다"):
            config.api_key("gemini")

    def test_api_key_rejects_empty(self, settings):
        settings({"api": {"providers": {"gemini": {"api_key": ""}}}})
        with pytest.raises(ValueError):
            config.api_key("gemini")

    def test_rune_dir_lives_outside_the_source_tree(self, settings):
        """설치 룬을 소스 트리에 쓰면 패키징이 불가능해진다 (IchorAI installer.py)."""
        settings({})
        assert config.rune_dir().is_absolute()
        assert Path(config.__file__).parent not in config.rune_dir().parents

    def test_dirs_are_overridable(self, settings):
        settings({"processing": {"data_dir": "D:/vader", "rune_dir": "D:/vader/r"}})
        assert config.data_dir() == Path("D:/vader")
        assert config.rune_dir() == Path("D:/vader/r")


# ── 템플릿 구조 회귀 방지 ──────────────────────────────────────

_CORE_BLOCKS = ("api", "place", "processing")
_RUNE_ONLY_KEYS = (
    "window_title", "selectors", "workflow",
    "master_db", "learned_db",
    "alias_confirm_count", "alias_fuzzy_threshold",
    "manual_minutes_per_file", "wiki_size_warn_mb",
    "scan_dir", "done_dir", "error_dir",
)


def _template() -> dict:
    return json.loads(config.TEMPLATE_PATH.read_text(encoding="utf-8"))


class TestTemplateStructure:

    def test_has_a_runes_block(self):
        assert "runes" in _template()

    def test_ships_with_zero_runes(self):
        """룬 0개가 신규 설치의 정상 상태다."""
        runes = _template()["runes"]
        assert [k for k in runes if not k.startswith("_")] == []

    def test_no_rune_specific_top_level_blocks(self):
        """rpa / paths / hospital 은 EONM 전용이었다. 최상위에 두지 않는다."""
        t = _template()
        for block in ("rpa", "paths", "hospital"):
            assert block not in t, f"'{block}'은 룬 설정입니다"

    def test_core_blocks_have_no_rune_only_keys(self):
        t = _template()
        offenders = [
            f"{block}.{key}"
            for block in _CORE_BLOCKS
            for key in t.get(block, {})
            if key in _RUNE_ONLY_KEYS
        ]
        assert offenders == [], f"코어 블록에 룬 전용 키가 있습니다: {offenders}"

    def test_template_loads_with_the_real_config_module(self, tmp_path, monkeypatch):
        """템플릿을 그대로 복사하면 코어가 부팅되어야 한다."""
        target = tmp_path / "settings.json"
        target.write_text(config.TEMPLATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.setattr(config, "SETTINGS_PATH", target)
        config.invalidate()

        assert config.ocr_provider() == "gemini"
        assert config.installed_rune_ids() == []
        assert config.api_request_delay_seconds() == 5.0
        config.invalidate()


def test_a_switched_off_rune_is_a_screen_setting_not_a_rune_setting(settings):
    """내 룬의 체크 — 끄고 다시 켜면 처음과 같다. 룬 설정(`runes.<id>`)은 건드리지 않는다."""
    settings({"api": {}, "processing": {}, "runes": {"eonm_ipgo": {"window_title": "X"}}})

    config.set_rune_enabled("eonm_ipgo", False)
    assert config.get("desktop.hidden_runes") == ["eonm_ipgo"]
    assert config.rune_config("eonm_ipgo") == {"window_title": "X"}

    config.set_rune_enabled("eonm_ipgo", True)
    assert config.get("desktop.hidden_runes") == []
