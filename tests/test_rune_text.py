"""룬 패키지 안 화면 문구 영어(i18n_en.py) — 실행하지 않고 값만 읽는다 (영문판 5단계 후속)."""
import re
from pathlib import Path

from vader.core import rune_text

PACKAGES = Path(__file__).resolve().parent.parent / "rune-packages"
holes = lambda s: sorted(re.findall(r"\{\d+\}", s))   # noqa: E731


def test_every_bundled_translation_loads_and_keeps_placeholders():
    found = [p.parent for p in PACKAGES.glob("*/i18n_en.py")]
    assert found, "번역이 실린 룬이 하나는 있어야 한다"
    for folder in found:
        data = rune_text.load(folder)
        assert data is not None, folder.name
        bad = [k for k, v in data["text"].items() if holes(k) != holes(v)]
        for scope in (*data["choices"].values(), *data["values"].values()):
            bad += [k for k, v in scope.items() if holes(k) != holes(v)]
        assert bad == [], (folder.name, bad)


def test_it_reads_values_without_running_code(tmp_path):
    marker = tmp_path / "ran"
    (tmp_path / "i18n_en.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('x')\nEN = {{'text': {{'가': 'A'}}}}\n", encoding="utf-8")
    assert rune_text.load(tmp_path) == {"text": {"가": "A"}, "choices": {}, "values": {}}
    assert not marker.exists()                              # import 하지 않는다


def test_bad_shapes_count_as_no_translation(tmp_path):
    for body in ["EN = {'text': {'가': 1}}", "EN = {'other': {}}", "EN = {'choices': {'q': {'a': ['b']}}}",
                 "EN = open('x').read()", "EN = [", "X = {'text': {}}"]:
        (tmp_path / "i18n_en.py").write_text(body, encoding="utf-8")
        assert rune_text.load(tmp_path) is None, body
    (tmp_path / "i18n_en.py").write_text("EN = {'text': {'가': '" + "A" * 300_000 + "'}}", encoding="utf-8")
    assert rune_text.load(tmp_path) is None                 # 너무 크면 없는 것으로
    assert rune_text.load(tmp_path / "nowhere") is None


def test_every_rune_line_shown_on_screen_has_english_in_its_package():
    """룬이 화면에 내보내는 문구마다 그 패키지 i18n_en.py 에 영어가 있다 — 원문을 고치고 번역을 잊으면 걸린다."""
    import ast
    import importlib.util
    import sys

    tools = Path(__file__).resolve().parent.parent / "tools"
    sys.path.insert(0, str(tools))
    spec = importlib.util.spec_from_file_location("rune_i18n_extract", tools / "rune_i18n_extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    for folder in (p.parent for p in PACKAGES.glob("*/i18n_en.py")):
        found = set().union(*(tool.strings(ast.parse(p.read_text(encoding="utf-8"))) for p in folder.rglob("*.py")
                              if p.name != "i18n_en.py" and "__pycache__" not in p.parts))
        assert sorted(found - set(rune_text.load(folder)["text"])) == [], folder.name


def test_install_preview_carries_the_candidate_package_translation(tmp_path):
    """설치 미리보기는 **후보 패키지**의 번역을 싣는다 — 최초 설치·번역이 바뀐 업데이트에서도 그 버전의 영문 이름 (codex1 후속 P1)."""
    import shutil

    from vader.core import installer

    candidate = tmp_path / "table_merge"
    shutil.copytree(PACKAGES / "table_merge", candidate, ignore=shutil.ignore_patterns("__pycache__"))
    shown = installer.preview(candidate)
    assert shown["i18n_en"]["text"]["표 합치기·중복 정리"] == "Table merge & dedupe"
    (candidate / "i18n_en.py").write_text("EN = {'text': {'표 합치기·중복 정리': 'Table merge v2'}}", encoding="utf-8")
    assert installer.preview(candidate)["i18n_en"]["text"]["표 합치기·중복 정리"] == "Table merge v2"
