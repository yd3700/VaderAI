"""표 룬 프로필 — 허용 목록 문법과 표 입출력 중개 (2026-10-14 codex1 합의 2번).

작은 표 룬 하나가 제작소 검사 → 시험 → 포장 → 설치 → 실행까지 가고, 금지한 접근은 검사와
설치에서 걸리는지 본다.
"""
from pathlib import Path

import pytest

from vader.core import config, profile, seal, tables, workshop
from vader.core.checkpoint import CheckpointStore
from vader.core.installer import InstallError, install, install_package
from vader.core.loader import discover
from vader.core.runner import JobRunner
from vader.sdk import Input

#: 부서별 금액 합계 — LLM 이 표 룬으로 만들 법한 가장 작은 일.
TOTALS = '''
from vader.sdk import RuneManifest, RuneResult


class DeptTotals:
    manifest = RuneManifest(id="dept_totals", name="부서별 합계", version="1.0.0", profile="table")

    def run(self, ctx, inputs):
        totals = {}
        for source in inputs:
            header, *rows = ctx.read_table(source)
            dept, amount = header.index("부서"), header.index("금액")
            for row in rows:
                totals[row[dept]] = totals.get(row[dept], 0) + int(row[amount])
        out = ctx.write_table("부서별합계.csv", [["부서", "합계"], *sorted([k, v] for k, v in totals.items())])
        return RuneResult(ok=True, summary={"부서": len(totals)}, artifacts=[out])


rune = DeptTotals()
'''


def make_project(body=TOTALS):
    home = workshop.project("dept_totals")
    (home / "dept_totals").mkdir(parents=True, exist_ok=True)
    (home / "dept_totals" / "__init__.py").write_text(body, encoding="utf-8")
    case = home / "cases" / "10월"
    (case / "input").mkdir(parents=True, exist_ok=True)
    (case / "expected").mkdir(exist_ok=True)
    (case / "input" / "청구.csv").write_text("부서,금액\n내과,100\n외과,50\n내과,25\n", encoding="utf-8")
    (case / "expected" / "부서별합계.csv").write_text("부서,합계\n내과,125\n외과,50\n", encoding="utf-8")
    return home


def test_a_small_table_rune_goes_from_workshop_to_a_real_run(tmp_path):
    config.set_dev_mode(True)
    home = make_project()
    assert workshop.validate_static("dept_totals")["profile"] == "table"
    workshop.allow_test("dept_totals", seal.fingerprint(home / "dept_totals"))
    assert workshop.run_test("dept_totals")["ok"]
    archive = Path(workshop.pack("dept_totals")["archive"])

    rune_dir = tmp_path / "runes"
    assert install_package(archive, rune_dir).manifest.profile == "table"
    rune = discover(rune_dir)[0][0].rune
    runner = JobRunner(checkpoints=CheckpointStore(tmp_path / "cp"), data_dir=tmp_path / "jobs")
    job = runner.submit(rune, [Input(path=home / "cases" / "10월" / "input" / "청구.csv")], params={}, dry_run=True)
    assert runner.join(job.id, timeout=10)
    assert job.result.ok and job.result.summary == {"부서": 2}
    assert Path(job.result.artifacts[0]).parent == job.workdir


@pytest.mark.parametrize("line, what", [
    ("import os", "import"),
    ("from pathlib import Path", "pathlib.Path"),
    ("from json import load", "json.load"),
    ("open('x.txt', 'w')", "open"),
    ("getattr(ctx, 'workdir')", "getattr"),
    ("x = ctx.workdir", ".workdir"),
    ("x = inputs[0].path", ".path"),
    ("x = ctx.write_table('a.csv', []).replace('a', 'b')", None),   # 문자열 replace 는 괜찮다
    ("x = '{0.__class__}'.format(ctx)", ".format"),
    ("x = ctx.__class__", ".__class__"),
    ("x = __builtins__", "__builtins__"),
    ("ctx.http_get('https://example.com')", ".http_get"),
])
def test_forbidden_access_is_named(tmp_path, line, what):
    package = tmp_path / "dept_totals"
    package.mkdir()
    body = TOTALS.replace("        totals = {}\n", f"        {line}\n        totals = {{}}\n")
    (package / "__init__.py").write_text(body, encoding="utf-8")
    problems = profile.check(package)
    if what is None:
        assert problems == []
    else:
        assert problems and what in problems[0], problems


def test_an_unknown_decorator_and_a_native_file_are_refused(tmp_path):
    package = tmp_path / "dept_totals"
    package.mkdir()
    (package / "__init__.py").write_text(TOTALS.replace("    def run(", "    @trace\n    def run("), encoding="utf-8")
    (package / "speedup.pyd").write_bytes(b"\0")
    problems = profile.check(package)
    assert any("데코레이터" in p for p in problems) and any("speedup.pyd" in p for p in problems)


def test_the_installer_checks_a_table_claim_itself(tmp_path):
    """제작소를 거치지 않은 폴더가 '표 룬' 이라고 우겨도 설치기가 같은 검사를 한다."""
    package = tmp_path / "src" / "dept_totals"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(TOTALS.replace("        totals = {}\n", "        open('x', 'w')\n        totals = {}\n"),
                                         encoding="utf-8")
    with pytest.raises(InstallError, match="표 룬 규칙"):
        install(package, tmp_path / "runes")
    assert not (tmp_path / "runes" / "dept_totals").exists()


@pytest.mark.parametrize("name", ["../밖.csv", "하위/결과.csv", "C:결과.csv", "결과.txt", ".숨김.csv", "결과.csv:몰래"])
def test_write_table_only_writes_one_named_table_in_the_job_folder(tmp_path, name):
    with pytest.raises(tables.TableError):
        tables.write_table(tmp_path, name, [["a"]])


def test_read_table_reads_only_this_jobs_inputs(tmp_path):
    from vader.core.context import JobContext
    from vader.core.job import Job

    mine, other = tmp_path / "mine.csv", tmp_path / "other.csv"
    mine.write_text("a,b\n1,2\n", encoding="utf-8")
    other.write_text("x\n", encoding="cp949")
    job = Job(rune_id="dept_totals", inputs=[Input(path=mine)], workdir=tmp_path)
    ctx = JobContext(job=job, events=None, cancel=None, params={}, checkpoints=None, checkpoint_key=None)
    assert ctx.read_table(Input(path=mine)) == [["a", "b"], ["1", "2"]]
    with pytest.raises(tables.TableError, match="이번 작업"):
        ctx.read_table(Input(path=other))


def test_korean_csv_saved_by_excel_and_an_xlsx_round_trip(tmp_path):
    legacy = tmp_path / "엑셀저장.csv"
    legacy.write_bytes("부서,금액\r\n내과,1\r\n".encode("cp949"))
    assert tables.read_table(legacy) == [["부서", "금액"], ["내과", "1"]]

    pytest.importorskip("openpyxl")
    out = tables.write_table(tmp_path, "결과.xlsx", [["부서", "합계"], ["내과", 125]])
    assert tables.read_table(Path(out)) == [["부서", "합계"], ["내과", 125]]



def _ctx(tmp_path, *paths):
    from vader.core.context import JobContext
    from vader.core.job import Job
    job = Job(rune_id="dept_totals", inputs=[Input(path=p) for p in paths], workdir=tmp_path)
    return JobContext(job=job, events=None, cancel=None, params={}, checkpoints=None, checkpoint_key=None)


def test_a_screen_table_file_is_read_as_tsv_and_deleted_once_a_rune_read_it(tmp_path, monkeypatch):
    """화면 표 읽기가 저장한 .tsv — 룬이 읽으면 바로 지우고, 안 읽힌 것은 만든 지 24시간 뒤 지운다 (마스터 2026-11-18)."""
    from vader.core import screen

    monkeypatch.setattr(config, "workspace", lambda: tmp_path)
    folder = tmp_path / "screen_tables"
    folder.mkdir()
    grid, mine = folder / "fpsA-1.tsv", tmp_path / "mine.tsv"
    for f in (grid, mine):
        f.write_text("\t과목\t금액\r\n\tTEST001\t10\r\n", encoding="utf-8")
    ctx = _ctx(tmp_path, grid, mine)
    assert ctx.read_table(Input(path=grid)) == [["", "과목", "금액"], ["", "TEST001", "10"]]
    assert ctx.read_table(Input(path=mine)) and not grid.exists() and mine.exists()   # 사람이 준 파일은 안 지운다
    with pytest.raises(tables.TableError, match="다시 복사"):                         # 한 번 읽으면 끝 — 재실행은 다시 복사
        ctx.read_table(Input(path=grid))

    old, new = folder / "fpsA-2.tsv", folder / "fpsA-3.tsv"
    for f in (old, new):
        f.write_text("x", encoding="utf-8")
    monkeypatch.setattr(screen, "TABLE_KEEP", -60)          # 만든 시각은 바꿀 수 없으니 기준을 옮긴다
    screen.expire_tables()
    assert not old.exists() and not new.exists()
    monkeypatch.setattr(screen, "TABLE_KEEP", 3600)
    new.write_text("x", encoding="utf-8")
    screen.expire_tables()
    assert new.exists()


def test_a_linked_table_folder_is_never_followed_to_delete_outside_files(tmp_path, monkeypatch):
    """screen_tables 를 바깥 폴더로 잇는 junction — 만료도 소비도 바깥 파일을 지우지 않는다 (codex1 회신 1 P1)."""
    import subprocess
    from vader.core import screen

    work, outside = tmp_path / "work", tmp_path / "outside"
    work.mkdir(); outside.mkdir()
    victim = outside / "x.tsv"
    victim.write_text("a\tb\r\n", encoding="utf-8")
    if subprocess.run(["cmd", "/c", "mklink", "/J", str(work / "screen_tables"), str(outside)],
                      capture_output=True).returncode:
        pytest.skip("junction 을 만들 수 없다")
    monkeypatch.setattr(config, "workspace", lambda: work)
    monkeypatch.setattr(screen, "TABLE_KEEP", -60)
    screen.expire_tables()
    linked = work / "screen_tables" / "x.tsv"
    assert victim.exists() and not screen.own_table(linked) and not screen.own_table(victim)
    _ctx(tmp_path, linked).read_table(Input(path=linked))
    assert victim.exists()
