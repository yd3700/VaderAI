"""배포 메타데이터와 CI의 최소 계약."""
from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def project_config() -> dict:
    return tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )


def test_version_number_has_one_source():
    config = project_config()

    assert "version" not in config["project"]
    assert config["project"]["dynamic"] == ["version"]
    assert config["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "vader.__version__"
    }


def test_setuptools_build_contains_only_vader_packages():
    config = project_config()

    assert config["build-system"]["build-backend"] == "setuptools.build_meta"
    package_find = config["tool"]["setuptools"]["packages"]["find"]
    assert package_find["include"] == ["vader", "vader.*"]
    assert {"tests*", "rune-packages*", "tools*", "wiki*"}.issubset(
        package_find["exclude"]
    )


def test_console_script_uses_guarded_main():
    config = project_config()

    assert config["project"]["scripts"] == {
        "vaderai": "vader.__main__:main"
    }


def test_core_required_dependencies_stay_empty():
    assert project_config()["project"]["dependencies"] == []


def test_sdist_manifest_excludes_repo_only_directories():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    for directory in ("tests", "rune-packages", "tools", "wiki"):
        assert f"prune {directory}" in manifest


def test_ci_runs_core_and_full_windows_matrices():
    workflow = (
        ROOT / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert workflow.count("runs-on: windows-latest") == 2
    assert 'python-version: ["3.11"]' in workflow and 'python-version: ["3.12"]' in workflow   # 2026-11-18 조합 2개로
    assert 'python -m pip install ".[eonm,supply]"' in workflow
    assert "python -m pytest -rs" in workflow
    assert "python -m pytest -q" in workflow
    assert workflow.count("python -m build") == 2
