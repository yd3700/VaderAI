"""목록 배포용 깨끗한 사본과 고정 응답 시험 (2026-10-29 신뢰 계약 v2 11·12절). 가짜 자료만 쓴다."""
import json

import pytest

from vader.core import config, release, seal, workshop

RUNE = '''
from vader.sdk import Input, RuneContext, RuneManifest, RuneResult

class Reader:
    manifest = RuneManifest(id="reader", name="읽기", version="0.1.0")

    def run(self, ctx: RuneContext, inputs: list[Input]) -> RuneResult:
        rows = ctx.ocr(inputs[0].path, "행을 읽어라", list)
        sheet = ctx.http_get("https://example.test/sheet.csv").text
        out = ctx.workdir / "결과.csv"
        out.write_text("\\n".join(f"{r['item']},{r['total']}" for r in rows) + "\\n" + sheet, encoding="utf-8")
        return RuneResult(ok=True, artifacts=[out])
'''
RECORDING = {"format": "vader-recording/1", "steps": [
    {"at": "2026-08-19T11:14:44", "window_title": "홍길동 [입고]   Version=1.0 - 가나병원 Version = 20260211 오전 11:15:31",
     "keys": "{UP 2}", "name": "", "automation_id": "", "control_type": ""}]}


def installed_rune(extra_files=None):
    """설치된 룬 폴더 — 업무 자료가 섞여 있다."""
    folder = config.rune_dir() / "reader"
    (folder / "recordings").mkdir(parents=True)
    (folder / "__init__.py").write_text("from .rune import Reader\nrune = Reader()\n", encoding="utf-8")
    (folder / "rune.py").write_text(RUNE, encoding="utf-8")
    (folder / "recordings" / "nav.json").write_text(json.dumps(RECORDING, ensure_ascii=False), encoding="utf-8")
    (folder / "vaderai-test" / "done").mkdir(parents=True)
    (folder / "vaderai-test" / "done" / "명세서.pdf").write_bytes(b"%PDF-fake")
    (folder / "vaderai-test" / "master.csv").write_text("코드,이름\n", encoding="utf-8")
    (folder / "history.db").write_bytes(b"db")
    for name, text in (extra_files or {}).items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def test_the_release_copy_takes_only_the_allowed_files_and_says_what_it_left(tmp_path):
    source = installed_rune()
    report = release.make(source, tmp_path / "copy")
    assert sorted(report["copied"]) == ["__init__.py", "recordings/nav.json", "rune.py"]
    left = dict(map(tuple, report["left_out"]))
    assert left["vaderai-test/done/명세서.pdf"] == "업무 자료 폴더" and left["history.db"] == "허용 목록 밖"
    step = json.loads((tmp_path / "copy" / "recordings" / "nav.json").read_text(encoding="utf-8"))["steps"][0]
    assert "홍길동" not in step["window_title"] and "오전" not in step["window_title"] and step["at"] == ""
    assert release.check(tmp_path / "copy") == []


def test_a_recording_with_typed_text_stops_the_copy(tmp_path):
    source = installed_rune()
    bad = {**RECORDING, "steps": [{**RECORDING["steps"][0], "keys": "password123{ENTER}"}]}
    (source / "recordings" / "nav.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(release.ReleaseError, match="이동 키"):
        release.make(source, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()


def test_check_names_business_data_left_in_a_folder(tmp_path):
    (tmp_path / "scans").mkdir()
    (tmp_path / "scans" / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "list.xlsx").write_bytes(b"x")
    problems = release.check(tmp_path)
    assert any("scans/a.py" in p for p in problems) and any("list.xlsx" in p for p in problems)


def case_with_fixtures(home, fixtures):
    case = home / "cases" / "가짜1"
    (case / "input").mkdir(parents=True)
    (case / "expected").mkdir()
    (case / "input" / "명세서.png").write_bytes(b"fake")
    (case / "expected" / "결과.csv").write_text("거즈,1000\n시트", encoding="utf-8")
    (case / "fixtures.json").write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")


FIXTURES = {"ocr": {"명세서.png": [{"item": "거즈", "total": 1000}]},
            "http_get": {"https://example.test/sheet.csv": {"status": 200, "text": "시트"}}}


def test_a_release_copy_is_tested_with_fixed_answers_and_packed():
    config.set_dev_mode(True)
    installed_rune()
    workshop.release_copy("reader")
    home = workshop.project("reader")
    case_with_fixtures(home, FIXTURES)
    workshop.allow_test("reader", seal.fingerprint(home / "reader"))
    report = workshop.run_test("reader")
    assert report["ok"], report
    packed = workshop.pack("reader")
    assert set(packed["manifest"]["files"]) == {"__init__.py", "rune.py", "recordings/nav.json"}


def test_a_call_without_a_fixed_answer_fails_and_an_unused_answer_is_reported():
    config.set_dev_mode(True)
    installed_rune()
    workshop.release_copy("reader")
    home = workshop.project("reader")
    case_with_fixtures(home, {"ocr": FIXTURES["ocr"], "infer": [{"x": 1}]})      # 시트 응답이 없다
    workshop.allow_test("reader", seal.fingerprint(home / "reader"))
    case = workshop.run_test("reader")["cases"][0]
    assert not case["ok"]
    assert any("http_get" in u for u in case["unverified"]) and any("infer #1" in u for u in case["unverified"])


def test_business_data_slipped_into_the_tested_copy_is_not_packed():
    config.set_dev_mode(True)
    installed_rune()
    workshop.release_copy("reader")
    home = workshop.project("reader")
    (home / "reader" / "환자.csv").write_text("이름\n", encoding="utf-8")     # 시험 전에 사본에 들어갔다
    case_with_fixtures(home, FIXTURES)
    workshop.allow_test("reader", seal.fingerprint(home / "reader"))
    assert workshop.run_test("reader")["ok"]
    with pytest.raises(workshop.WorkshopError, match="배포 사본"):
        workshop.pack("reader")


def test_a_release_copy_space_never_links_the_original_folder():
    installed_rune()
    workshop.release_copy("reader")
    with pytest.raises(workshop.WorkshopError, match="배포 사본"):
        workshop.link_source("reader", config.rune_dir() / "reader")


# ── codex1 2단계 검토 반영 (2026-10-29) ──

def test_a_recording_with_unknown_fields_stops_the_copy(tmp_path):
    for bad in ({**RECORDING, "note": "x"}, {**RECORDING, "steps": [{**RECORDING["steps"][0], "patient": "x"}]}):
        source = installed_rune() if not (config.rune_dir() / "reader").exists() else config.rune_dir() / "reader"
        (source / "recordings" / "nav.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(release.ReleaseError, match="모르는 항목"):
            release.make(source, tmp_path / "copy")


def test_screen_strings_left_in_a_recording_are_returned_for_review(tmp_path):
    source = installed_rune()
    step = {**RECORDING["steps"][0], "name": "가상환자 홍길동"}
    (source / "recordings" / "nav.json").write_text(json.dumps({**RECORDING, "steps": [step]}, ensure_ascii=False),
                                                    encoding="utf-8")
    review = release.make(source, tmp_path / "copy")["review"]
    assert "요소 이름: 가상환자 홍길동" in review and any(r.startswith("창 제목:") for r in review)


def test_a_raw_recording_added_after_the_copy_is_not_packed():
    config.set_dev_mode(True)
    installed_rune()
    workshop.release_copy("reader")
    home = workshop.project("reader")
    (home / "reader" / "recordings" / "raw.json").write_text(json.dumps(RECORDING, ensure_ascii=False), encoding="utf-8")
    case_with_fixtures(home, FIXTURES)
    workshop.allow_test("reader", seal.fingerprint(home / "reader"))
    assert workshop.run_test("reader")["ok"]
    with pytest.raises(workshop.WorkshopError, match="정리하지 않은 녹화"):
        workshop.pack("reader")


CATCHER = RUNE.replace('sheet = ctx.http_get("https://example.test/sheet.csv").text',
                       'try:\n            sheet = ctx.http_get("https://example.test/sheet.csv").text\n'
                       '        except Exception:\n            sheet = "시트"')


def test_a_rune_that_swallows_a_missing_answer_still_fails_the_case():
    config.set_dev_mode(True)
    installed_rune()
    (config.rune_dir() / "reader" / "rune.py").write_text(CATCHER, encoding="utf-8")
    workshop.release_copy("reader")
    home = workshop.project("reader")
    case_with_fixtures(home, {"ocr": FIXTURES["ocr"]})          # 시트 응답 없음 — 룬은 예외를 잡고 '시트' 로 채운다
    workshop.allow_test("reader", seal.fingerprint(home / "reader"))
    case = workshop.run_test("reader")["cases"][0]
    assert not case["ok"] and "고정 응답 없는 바깥 호출" in case.get("error", "")


def test_fixed_answers_behave_like_the_real_context(tmp_path):
    from vader.core.http import HttpError
    from vader.sdk import Input

    (tmp_path / "w").mkdir()
    ctx = workshop._CaseContext(tmp_path / "w", {}, [], {
        "ocr": {"a.png": [1]}, "infer": [{"prompt_contains": "합계", "response": 3}],
        "http_get": {"https://x.test/e": {"status": 500}}})
    real = tmp_path / "a.png"
    real.write_bytes(b"x")
    ctx.inputs = [Input(path=real)]
    with pytest.raises(RuntimeError):
        ctx.ocr(tmp_path / "other" / "a.png", "p", list)        # 이름만 같은 다른 경로
    assert ctx.ocr(real, "p", list) == [1]
    with pytest.raises(TypeError):
        ctx.infer()                                            # 실제처럼 prompt·schema 필수
    with pytest.raises(RuntimeError):
        ctx.infer("다른 질문", dict)                            # 기대한 요청이 아니다
    with pytest.raises(HttpError):
        ctx.http_get("https://x.test/e")                       # 오류 응답은 예외
    assert ctx.violations


def test_a_case_without_an_expected_answer_is_not_release_evidence():
    config.set_dev_mode(True)
    installed_rune()
    workshop.release_copy("reader")
    home = workshop.project("reader")
    case_with_fixtures(home, FIXTURES)
    for f in (home / "cases" / "가짜1" / "expected").iterdir():
        f.unlink()
    workshop.allow_test("reader", seal.fingerprint(home / "reader"))
    assert workshop.run_test("reader")["ok"]
    with pytest.raises(workshop.WorkshopError, match="정답"):
        workshop.pack("reader")


def test_a_release_copy_that_fails_halfway_leaves_no_space(monkeypatch):
    installed_rune()

    def boom(*a, **k):
        raise OSError("디스크 오류")
    monkeypatch.setattr(release, "make", boom)
    with pytest.raises(OSError):
        workshop.release_copy("reader")
    assert not workshop.project("reader").exists()


def test_a_linked_file_is_not_copied(tmp_path):
    source = installed_rune()
    outside = tmp_path / "업무.md"
    outside.write_text("비밀", encoding="utf-8")
    try:
        (source / "help.md").symlink_to(outside)
    except OSError:
        pytest.skip("이 PC 는 링크를 만들 권한이 없다")
    with pytest.raises(release.ReleaseError, match="링크"):
        release.make(source, tmp_path / "copy")
