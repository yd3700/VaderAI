"""룬 제작소 — Claude Code 가 MCP 로 부르는 검사·시험·포장, 그리고 zip 설치 (마스터 결정 2026-10-13)."""
import json
import zipfile
from pathlib import Path

import pytest

from vader.core import config, seal, workshop
from vader.core.installer import InstallError, install_package
from vader.core.loader import discover

RUNE = '''
from pathlib import Path
from vader.sdk import Input, RuneContext, RuneManifest, RuneResult

class Doubler:
    manifest = RuneManifest(id="doubler", name="두 배", version="0.1.0")

    def run(self, ctx: RuneContext, inputs: list[Input]) -> RuneResult:
        rows = Path(inputs[0].path).read_text(encoding="utf-8").split()
        out = ctx.workdir / "결과.csv"
        out.write_text("\\n".join(str(int(r) * {factor}) for r in rows), encoding="utf-8")
        return RuneResult(ok=True, artifacts=[out])
'''


def make(factor=2, expected="2\n4\n6", top=""):
    home = workshop.project("doubler")
    package = home / "doubler"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(top + "from .rune import Doubler\nrune = Doubler()\n", encoding="utf-8")
    (package / "rune.py").write_text(RUNE.replace("{factor}", str(factor)), encoding="utf-8")
    case = home / "cases" / "주1"
    (case / "input").mkdir(parents=True, exist_ok=True)
    (case / "expected").mkdir(exist_ok=True)
    (case / "input" / "수.txt").write_text("1\n2\n3", encoding="utf-8")
    (case / "expected" / "결과.csv").write_text(expected, encoding="utf-8")
    return home


def allowed(home):
    """사람이 화면에서 요약을 보고 이 버전의 시험을 허용했다."""
    workshop.allow_test(home.name, seal.fingerprint(home / home.name))
    return home


def test_a_test_waits_until_a_person_allows_that_exact_code(tmp_path):
    """허용 전에는 룬 코드가 한 줄도 돌지 않는다. 고치면 다시 허용받는다 (2026-10-14 마스터 결정)."""
    config.set_dev_mode(True)
    marker = tmp_path / "ran"
    make(top=f"open(r'{marker}', 'w').close()\n")
    report = workshop.run_test("doubler")
    assert report["waiting_for_person"] and not report["ok"] and not marker.exists()

    asked, = workshop.test_requests()
    assert asked["id"] == "doubler" and any("파일 열기" in w for w in asked["watch"])

    workshop.allow_test("doubler", asked["fingerprint"])
    assert workshop.test_requests() == [] and workshop.run_test("doubler")["ok"] and marker.exists()

    make(factor=3)
    assert workshop.run_test("doubler")["waiting_for_person"]
    with pytest.raises(workshop.WorkshopError, match="바뀌었습니다"):
        workshop.allow_test("doubler", asked["fingerprint"])


def test_static_check_never_runs_the_rune_code(tmp_path):
    """검사는 import 하지 않는다 — 최상위 코드가 돌면 '검사' 가 곧 실행이다 (codex1)."""
    marker = tmp_path / "ran"
    make(top=f"open(r'{marker}', 'w').close()\n")
    report = workshop.validate_static("doubler")
    assert report["ok"] and report["fingerprint"] and not marker.exists()


def test_static_check_names_an_undeclared_capability():
    home = make()
    (home / "doubler" / "net.py").write_text("import urllib.request\n", encoding="utf-8")
    report = workshop.validate_static("doubler")
    assert not report["ok"] and any("선언하지 않은 권한" in p for p in report["problems"])


def test_an_id_cannot_walk_out_of_the_workshop():
    with pytest.raises(workshop.WorkshopError):
        workshop.project("..\\runes")


def test_testing_needs_dev_mode_turned_on_by_a_person():
    make()
    with pytest.raises(workshop.WorkshopError, match="개발 모드"):
        workshop.run_test("doubler")


def test_a_case_passes_and_a_wrong_rule_shows_the_difference():
    config.set_dev_mode(True)
    allowed(make())
    assert workshop.run_test("doubler")["ok"]

    allowed(make(factor=3))
    case = workshop.run_test("doubler")["cases"][0]
    diff = case["compared"][0]
    assert not case["ok"] and diff["missing"] == [["2"], ["4"]] and diff["extra"] == [["3"], ["9"]]


def test_pack_refuses_source_changed_after_the_test():
    config.set_dev_mode(True)
    home = allowed(make())
    workshop.run_test("doubler")
    (home / "doubler" / "rune.py").write_text(RUNE.replace("{factor}", "5"), encoding="utf-8")
    with pytest.raises(workshop.WorkshopError, match="소스가 바뀌었습니다"):
        workshop.pack("doubler")


def test_packed_rune_installs_and_a_tampered_one_does_not(tmp_path):
    config.set_dev_mode(True)
    allowed(make())
    workshop.run_test("doubler")
    packed = workshop.pack("doubler")
    archive = Path(packed["archive"])
    assert "cases" not in " ".join(zipfile.ZipFile(archive).namelist())   # 업무 자료는 안 들어간다

    installed = install_package(archive, tmp_path / "runes")
    assert installed.manifest.id == "doubler"
    assert [r.manifest.id for r in discover(tmp_path / "runes")[0]] == ["doubler"]

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(tampered, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            dst.writestr(item, data.replace(b"* 2", b"* 9") if item.filename.endswith("rune.py") else data)
    with pytest.raises(InstallError, match="바뀐 파일"):
        install_package(tampered, tmp_path / "other")


def test_a_package_cannot_write_outside_its_folder(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("manifest.json", json.dumps({"id": "doubler", "files": {"../x.py": "0"}}))
        z.writestr("doubler/../x.py", "")
    with pytest.raises(InstallError):
        install_package(evil, tmp_path / "runes")


def test_projects_show_where_each_rune_is_in_the_workshop():
    """제작소 목록 — 시험 전 → 요청 대기 → 통과 → 포장 → 고친 뒤 시험 전."""
    config.set_dev_mode(True)
    home = make()
    state = lambda: {k: v for k, v in workshop.projects()[0].items() if k in ("test", "packed", "waiting")}
    assert state() == {"test": "none", "packed": False, "waiting": False}
    workshop.run_test("doubler")
    assert state()["waiting"]
    allowed(home)
    workshop.run_test("doubler")
    assert state() == {"test": "pass", "packed": False, "waiting": False}
    workshop.pack("doubler")
    assert state()["packed"]
    make(factor=3)
    assert state() == {"test": "stale", "packed": False, "waiting": False}


def test_table_compare_counts_repeated_rows(tmp_path):
    """같은 행이 한 번 더 들어가거나 빠져도 '같음' 이 아니다 (codex1 eonm 시험 길)."""
    want, got = tmp_path / "want.csv", tmp_path / "got.csv"
    want.write_text("a,b\n1,2\n", encoding="utf-8")
    got.write_text("a,b\n1,2\n1,2\n", encoding="utf-8")
    result = workshop._diff(want, got)
    assert not result["same"] and result["extra"] == [["1", "2"]]
    assert workshop._diff(got, want)["missing"] == [["1", "2"]]
