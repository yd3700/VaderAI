from pathlib import Path

from vader.core import seal
from vader.core.loader import discover


def _write_rune(root: Path, rune_id: str, body: str) -> None:
    package = root / rune_id
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(body, encoding="utf-8")
    seal.trust(rune_id, package)   # 사람이 설치한 것처럼


def _valid_rune(manifest_id: str) -> str:
    return f"""
from vader.sdk import RuneManifest, RuneResult

manifest = RuneManifest(id={manifest_id!r}, name="테스트 룬")

def run(ctx, inputs):
    return RuneResult(ok=True)
"""


def test_discovers_one_valid_rune(tmp_path):
    _write_rune(tmp_path, "sample", _valid_rune("sample"))

    loaded, failures = discover(tmp_path)

    assert failures == []
    assert [item.manifest.id for item in loaded] == ["sample"]
    assert loaded[0].rune.run(None, []).ok


def test_empty_rune_directory_is_normal(tmp_path):
    assert discover(tmp_path) == ([], [])


def test_missing_rune_directory_is_normal(tmp_path):
    assert discover(tmp_path / "missing") == ([], [])


def test_missing_manifest_does_not_block_other_runes(tmp_path):
    _write_rune(tmp_path, "bad", "def run(ctx, inputs):\n    return None\n")
    _write_rune(tmp_path, "good", _valid_rune("good"))

    loaded, failures = discover(tmp_path)

    assert [item.manifest.id for item in loaded] == ["good"]
    assert len(failures) == 1
    assert failures[0].rune_id == "bad"
    assert "manifest" in failures[0].reason


def test_missing_run_is_a_failure(tmp_path):
    _write_rune(
        tmp_path,
        "bad",
        "from vader.sdk import RuneManifest\n"
        "manifest = RuneManifest(id='bad', name='bad')\n",
    )

    loaded, failures = discover(tmp_path)

    assert loaded == []
    assert len(failures) == 1
    assert "run" in failures[0].reason


def test_manifest_id_must_match_directory_name(tmp_path):
    _write_rune(tmp_path, "directory_name", _valid_rune("different_id"))

    loaded, failures = discover(tmp_path)

    assert loaded == []
    assert len(failures) == 1
    assert failures[0].rune_id == "directory_name"
    assert "does not match" in failures[0].reason


def test_import_error_does_not_block_other_runes(tmp_path):
    _write_rune(tmp_path, "broken", "raise RuntimeError('import exploded')\n")
    _write_rune(tmp_path, "healthy", _valid_rune("healthy"))

    loaded, failures = discover(tmp_path)

    assert [item.manifest.id for item in loaded] == ["healthy"]
    assert len(failures) == 1
    assert failures[0].rune_id == "broken"
    assert "import exploded" in failures[0].reason


def test_package_can_use_relative_imports(tmp_path):
    package = tmp_path / "relative"
    package.mkdir()
    (package / "helper.py").write_text("NAME = '상대 import 룬'\n", encoding="utf-8")
    (package / "__init__.py").write_text(
        "from vader.sdk import RuneManifest, RuneResult\n"
        "from .helper import NAME\n"
        "manifest = RuneManifest(id='relative', name=NAME)\n"
        "def run(ctx, inputs):\n"
        "    return RuneResult(ok=True)\n",
        encoding="utf-8",
    )
    seal.trust("relative", package)

    loaded, failures = discover(tmp_path)

    assert failures == []
    assert loaded[0].manifest.name == "상대 import 룬"


# ── 룬의 모양 ───────────────────────────────────────────────────

class TestRuneShape:
    """README 예제(클래스)와 모듈 수준 정의를 둘 다 받는다.

    처음에는 모듈만 받았는데, 그러면 README 대로 클래스를 쓴 룬이
    "missing manifest" 로 로드에 실패했다. 계약의 Rune Protocol 은
    `def run(self, ...)` 라 인스턴스를 전제한다.
    """

    def test_loads_a_class_instance_exposed_as_rune(self, tmp_path):
        _write_rune(
            tmp_path,
            "class_form",
            "from vader.sdk import RuneManifest, RuneResult\n"
            "class MyRune:\n"
            "    manifest = RuneManifest(id='class_form', name='클래스형')\n"
            "    def run(self, ctx, inputs):\n"
            "        return RuneResult(ok=True)\n"
            "rune = MyRune()\n",
        )
        loaded, failures = discover(tmp_path)

        assert failures == []
        assert [item.manifest.id for item in loaded] == ["class_form"]
        assert loaded[0].rune.run(None, []).ok

    def test_rune_attribute_wins_over_module_level_definitions(self, tmp_path):
        """둘 다 있으면 `rune` 이 이긴다. 규약이 갈리면 안 된다."""
        _write_rune(
            tmp_path,
            "both",
            "from vader.sdk import RuneManifest, RuneResult\n"
            "manifest = RuneManifest(id='틀린것', name='모듈')\n"
            "def run(ctx, inputs):\n"
            "    return RuneResult(ok=False)\n"
            "class R:\n"
            "    manifest = RuneManifest(id='both', name='클래스')\n"
            "    def run(self, ctx, inputs):\n"
            "        return RuneResult(ok=True)\n"
            "rune = R()\n",
        )
        loaded, failures = discover(tmp_path)

        assert failures == []
        assert loaded[0].manifest.name == "클래스"


def test_a_folder_changed_after_install_is_not_imported(tmp_path):
    """사람이 확인한 지문이 아니면 목록에 띄우려고 import 하는 것만으로도 코드가 돈다 — 그래서 안 한다."""
    _write_rune(tmp_path, "sample", _valid_rune("sample"))
    (tmp_path / "sample" / "__init__.py").write_text(
        f"open({str(tmp_path / 'ran')!r}, 'w').close()\n" + _valid_rune("sample"), encoding="utf-8")

    loaded, failures = discover(tmp_path)

    assert loaded == [] and [f.rune_id for f in failures] == ["sample"]
    assert "다시 설치" in failures[0].reason
    assert not (tmp_path / "ran").exists()
