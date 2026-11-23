"""새 PC 에서 이온엠 룬 설정이 비어 있으면 첫 설정으로 채운다 — 이미 있는 값은 안 건드린다 (2026-10-21)."""
from tests.private import needs
import importlib
from pathlib import Path

import pytest

from vader.core import config
from vader.core.checkpoint import CheckpointStore
from vader.core.loader import LoadedRune
from vader.core.runner import JobRunner
from vader.desktop.controller import DesktopController


@needs("rune-packages/eonm_ipgo")
def test_a_fresh_pc_gets_working_settings_without_touching_existing_ones(tmp_path, monkeypatch):
    pytest.importorskip("pywinauto", reason="eonm_ipgo 는 pywinauto 선택 의존성이 필요하다")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent / 'rune-packages'))
    eonm = importlib.import_module("eonm_ipgo.rune")

    settings = tmp_path / 'settings.json'
    settings.write_text('{"runes": {"eonm_ipgo": {"target_start": 7}}}', encoding='utf-8')
    monkeypatch.setattr(config, 'SETTINGS_PATH', settings)
    config.invalidate()
    monkeypatch.setattr(config, 'data_dir', lambda: tmp_path)
    monkeypatch.setattr(eonm, '_find_eonm_exe', lambda: 'C:/EONM/HisMenuGM.exe')
    try:
        rune = eonm.EonmIpgo()
        DesktopController(JobRunner(CheckpointStore(tmp_path / 'cp'), data_dir=tmp_path),
                          loaded=[LoadedRune(rune.manifest, rune)])
        got = config.rune_config('eonm_ipgo')
        home = tmp_path / 'rune-data' / 'eonm_ipgo'
        assert got['window_title'] == '입고/반납등록'
        assert got['exe_path'] == 'C:/EONM/HisMenuGM.exe'
        assert got['target_start'] == 7
        assert (home / 'done').is_dir() and (home / 'error').is_dir()
        assert got['done_dir'] == str(home / 'done')
        assert (home / 'master.csv').read_text(encoding='utf-8-sig').count('\n') == 1   # 머리줄만
    finally:
        config.invalidate()
