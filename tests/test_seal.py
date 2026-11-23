import json

import pytest

from vader.core import seal as seal_module


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(seal_module.config, "data_dir", lambda: data)
    return data / "seals.json"


def _source(tmp_path):
    source = tmp_path / "sample"
    source.mkdir()
    (source / "__init__.py").write_bytes(b"VALUE = 1\r\n")
    return source


def test_unsealed_rune_is_locked(tmp_path, ledger):
    source = _source(tmp_path)

    assert seal_module.sealed("sample", source) is False
    assert "승인 전입니다" in seal_module.why("sample", source)


def test_seal_records_who_and_why(tmp_path, ledger):
    source = _source(tmp_path)

    assert seal_module.seal("sample", source, "master", "골든 테스트 통과") is None

    assert seal_module.sealed("sample", source) is True
    assert "승인된 룬" in seal_module.why("sample", source)
    record = json.loads(ledger.read_text(encoding="utf-8"))["sample"]
    assert record["by"] == "master"
    assert record["note"] == "골든 테스트 통과"
    assert len(record["digest"]) == 64
    assert record["sealed_at"]


def test_changing_file_bytes_breaks_the_seal(tmp_path, ledger):
    source = _source(tmp_path)
    seal_module.seal("sample", source, "master", "ok")

    (source / "__init__.py").write_bytes(b"VALUE = 2\r\n")

    assert seal_module.sealed("sample", source) is False
    assert "룬이 바뀌었습니다" in seal_module.why("sample", source)


def test_changing_only_line_endings_breaks_the_seal(tmp_path, ledger):
    source = _source(tmp_path)
    seal_module.seal("sample", source, "master", "ok")

    (source / "__init__.py").write_bytes(b"VALUE = 1\n")

    assert seal_module.sealed("sample", source) is False


def test_renaming_a_file_breaks_the_seal(tmp_path, ledger):
    source = _source(tmp_path)
    extra = source / "rules.json"
    extra.write_bytes(b"{}")
    seal_module.seal("sample", source, "master", "ok")

    extra.rename(source / "policy.json")

    assert seal_module.sealed("sample", source) is False


def test_swapping_names_of_equal_content_files_breaks_the_seal(tmp_path, ledger):
    source = _source(tmp_path)
    (source / "a.txt").write_bytes(b"A")
    (source / "b.txt").write_bytes(b"B")
    seal_module.seal("sample", source, "master", "ok")

    (source / "a.txt").write_bytes(b"B")
    (source / "b.txt").write_bytes(b"A")

    assert seal_module.sealed("sample", source) is False


def test_python_cache_artifacts_do_not_break_the_seal(tmp_path, ledger):
    source = _source(tmp_path)
    seal_module.seal("sample", source, "master", "ok")

    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-311.pyc").write_bytes(b"cache")
    (source / "loose.pyc").write_bytes(b"cache")
    (source / "optimized.pyo").write_bytes(b"cache")

    assert seal_module.sealed("sample", source) is True


def test_a_runtime_data_file_inside_source_breaks_the_seal(tmp_path, ledger):
    source = _source(tmp_path)
    seal_module.seal("sample", source, "master", "ok")

    (source / "runtime-state.json").write_text("{}", encoding="utf-8")

    assert seal_module.sealed("sample", source) is False


@pytest.mark.parametrize("contents", ["{broken", "[]", '{"sample": null}'])
def test_an_unreadable_or_invalid_ledger_never_opens_the_lock(
    tmp_path, ledger, contents
):
    source = _source(tmp_path)
    ledger.parent.mkdir(parents=True)
    ledger.write_text(contents, encoding="utf-8")

    assert seal_module.sealed("sample", source) is False
    assert "실사용 실행이 잠겼습니다" in seal_module.why("sample", source) or "승인 전입니다" in seal_module.why("sample", source)


def test_sealing_does_not_overwrite_a_broken_ledger(tmp_path, ledger):
    source = _source(tmp_path)
    ledger.parent.mkdir(parents=True)
    ledger.write_text("{broken", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        seal_module.seal("sample", source, "master", "ok")

    assert ledger.read_text(encoding="utf-8") == "{broken"


def test_missing_source_is_locked(tmp_path, ledger):
    missing = tmp_path / "missing"

    assert seal_module.sealed("sample", missing) is False
    assert "승인 전입니다" in seal_module.why("sample", missing)
