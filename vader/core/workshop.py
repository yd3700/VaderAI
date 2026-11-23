"""룬 제작소 — 바깥 LLM(Claude Code)이 룬을 만들 때 쓰는 안내·검사·시험·포장.

마스터 결정 (2026-10-13): 룬은 사용자가 평소 쓰는 LLM 에서 만든다. VaderAI 는 MCP 로
도구 네 개만 준다 — `guide` · `validate_static` · `test` · `pack`. **승인·설치·실전·화면
재생은 주지 않는다** (codex1 합의, `wiki/handoff/rune-mcp-workflow-codex.md`).
MCP 서버(`vader/mcp_server.py`)와 시험 모두 이 모듈 하나를 부른다 — 검증 규칙이 둘이 되지 않게.

제작 공간은 GitHub 저장소처럼 생겼다 (마켓 모티브: Obsidian 플러그인 + GitHub)::

    ~/.vaderai/workshop/<id>/        ← 저장소 하나
        <id>/                        ← 룬 패키지 (이것만 포장된다)
        cases/<사례>/input/          ← 시험 입력
        cases/<사례>/expected/       ← 손으로 만든 정답 (같은 이름의 결과와 비교)
        cases/<사례>/params.json     ← (선택) 룬 설정
        cases/<사례>/answers.json    ← (선택) 사람에게 묻는 질문의 답, 순서대로
        cases/<사례>/fixtures.json   ← (선택) 바깥 호출 대신 돌려줄 고정 응답
                                        {"ocr": {"입력파일명": 결과}, "infer": [결과, …], "http_get": {"주소": {"status": 200, "text": "…"}}}
        dist/                        ← 포장 결과: manifest.json + <id>-<버전>.zip
        project.json                 ← (고치는 룬) 어디서 가져왔나 · 연결한 원본 폴더

**설치된 룬 고치기** (마스터 결정 2026-10-15, codex1 합의 `wiki/handoff/rune-edit-record-codex.md`):
내 룬의 [수정] 은 설치본을 `<id>/` 로 복사해 온다. 원본(git)이 따로 있는 PC 는 사람이 그 폴더를 **한 번 연결**한다 —
자동으로 찾지 않는다. 고칠 곳은 언제나 하나(`package_dir()`)이고 검사·시험·포장·콘솔이 모두 그것을 쓴다.
설치본은 직접 고치지 않는다. 녹화는 룬 안 `recordings/<이름>.json` 이라 코드와 함께 설치·승인된다.

**같은 사용자 권한이다 — 보안 경계가 아니다.** 시험은 별도 프로세스·입력 사본·시간 제한으로
사고 범위를 줄일 뿐이다. 그래서 개발 모드를 사람이 VaderAI 화면에서 켰을 때만, 그리고 사람이
코드 요약을 보고 **그 지문을 허용했을 때만** 돈다 (2026-10-14 마스터 결정 — 가상 PC 가 생기면 시험을 그쪽으로).
"""
from __future__ import annotations

import ast
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from vader.core import config, profile, seal, tables
from vader.core.installer import _manifest_keywords, _manifest_string
from vader.core.permissions import summary, undeclared

ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
TEST_TIMEOUT_SECONDS = 120
REPORT = ".last_test.json"
#: 시험 허용을 기다리는 요청. 사람이 VaderAI 화면에서 요약을 보고 허용한다 (2026-10-14 마스터 결정).
TEST_REQUEST = ".test_request.json"
#: 결과와 정답의 차이를 이만큼만 보여 준다. 나머지는 개수만.
DIFF_LIMIT = 20
#: 포장에 넣지 않는 것. 사례(업무 자료일 수 있다)·캐시·포장 결과는 저장소에만 남는다.
PACK_SKIP = {"__pycache__", ".pytest_cache"}
PACK_SUFFIXES = {".py", ".md", ".json", ".txt", ".csv", ".png", ".svg"}

GUIDE = Path(__file__).with_name("workshop_guide.md")
META = "project.json"
#: 룬 안에 녹화를 두기 전, 전역 `recordings/` 에 있던 녹화와 그 주인. 고칠 때 한 번 가져온다.
LEGACY_RECORDINGS = {"navigation": "eonm_ipgo"}


class WorkshopError(ValueError):
    """사람(과 LLM)에게 그대로 보여 줄 수 있는 거절."""


def root() -> Path:
    return config.data_dir() / "workshop"


def project(rune_id: str) -> Path:
    """제작 공간. 아이디가 경로를 벗어나지 못하게 모양부터 막는다."""
    if not ID_PATTERN.fullmatch(rune_id or ""):
        raise WorkshopError("룬 id 는 영문 소문자로 시작하는 소문자·숫자·밑줄 2~41자입니다 (예: ledger_check)")
    return root() / rune_id


def package_dir(rune_id: str) -> Path:
    """고칠 룬 폴더. 사람이 연결한 원본이 있으면 그것, 없으면 제작 공간 안 사본."""
    source = (_json(project(rune_id) / META) or {}).get("source")
    return Path(source) if source else project(rune_id) / rune_id


def edit(rune_id: str) -> None:
    """설치된 룬을 고치러 제작소로. 이미 고치는 중이면 그대로 둔다 — 덮어쓰지 않는다."""
    home = project(rune_id)
    if (home / META).exists() or (home / rune_id / "__init__.py").is_file():
        return
    installed = config.rune_dir() / rune_id
    if not (installed / "__init__.py").is_file():
        raise WorkshopError("설치된 룬이 아닙니다.")
    shutil.copytree(installed, home / rune_id, ignore=shutil.ignore_patterns("__pycache__"))
    _write_meta(rune_id, {"source": None, "from_fingerprint": seal.fingerprint(installed),
                          "from_version": _manifest_string(installed, "version"),
                          "at": datetime.now().isoformat(timespec="seconds")})


def release_copy(rune_id: str) -> dict:
    """설치된 룬에서 **목록 배포용 깨끗한 사본**으로 제작 공간을 만든다 (`release.make`, 계약 11절).

    이 공간은 연결한 원본을 쓰지 않는다 — 사본만 시험·포장한다. 뺀 파일 목록을 돌려준다.
    """
    from vader.core import release

    home = project(rune_id)
    if (home / META).exists() or (home / rune_id).exists():
        raise WorkshopError("이미 제작 중인 룬입니다. 배포 사본은 빈 제작 공간에서 만듭니다.")
    installed = config.rune_dir() / rune_id
    if not (installed / "__init__.py").is_file():
        raise WorkshopError("설치된 룬이 아닙니다.")
    # 표시를 **먼저** 쓴다 — 사본만 남고 표시가 없으면 일반 공간으로 포장될 수 있다 (codex1 P2-5)
    _write_meta(rune_id, {"source": None, "release_copy": True, "state": "making"})
    try:
        report = release.make(installed, home / rune_id)
        _write_meta(rune_id, {"source": None, "release_copy": True, "state": "ready",
                              "from_fingerprint": seal.fingerprint(installed),
                              "from_version": _manifest_string(installed, "version"),
                              "left_out": report["left_out"], "review": report["review"],
                              "at": datetime.now().isoformat(timespec="seconds")})
    except Exception as exc:
        shutil.rmtree(home, ignore_errors=True)
        if isinstance(exc, release.ReleaseError):
            raise WorkshopError(str(exc)) from None
        raise
    return report


def link_source(rune_id: str, folder: Path) -> None:
    """원본(예: git 저장소의 룬 폴더)을 고칠 곳으로 연결한다. 사람이 고른 폴더만 — 찾아다니지 않는다."""
    meta = _json(project(rune_id) / META) or {}
    if meta.get("release_copy"):
        raise WorkshopError("배포 사본 공간에는 원본 폴더를 연결하지 않습니다 — 사본만 시험·포장합니다.")
    folder = Path(folder).resolve()
    if not (folder / "__init__.py").is_file() or _manifest_string(folder, "id") != rune_id:
        raise WorkshopError(f"'{rune_id}' 룬 폴더가 아닙니다. 매니페스트 id 가 {rune_id} 인 폴더를 고르세요.")
    if config.rune_dir().resolve() in folder.parents:
        raise WorkshopError("설치된 룬 폴더는 직접 고치지 않습니다. 원본 폴더를 고르세요.")
    _write_meta(rune_id, {**meta, "source": str(folder)})


def import_recording(rune_id: str, name: str) -> None:
    """옛 전역 녹화를 이 룬 안으로 복사한다. 원본은 지우지 않는다. 이미 있으면 덮지 않는다."""
    from vader.core import recorder

    if LEGACY_RECORDINGS.get(name) != rune_id or not recorder.path_for(name).is_file():
        raise WorkshopError("가져올 녹화가 없습니다.")
    target = package_dir(rune_id) / "recordings" / f"{name}.json"
    if target.exists():
        raise WorkshopError("이 룬에 이미 같은 이름의 녹화가 있습니다.")
    target.parent.mkdir(exist_ok=True)
    if (_json(project(rune_id) / META) or {}).get("release_copy"):
        from vader.core import release

        try:   # 배포 사본 공간에는 정리한 녹화만 (codex1 P1-2)
            release.write_clean_recording(recorder.path_for(name), target)
        except release.ReleaseError as exc:
            raise WorkshopError(str(exc)) from None
        return
    shutil.copy2(recorder.path_for(name), target)


def _write_meta(rune_id: str, meta: dict) -> None:
    home = project(rune_id)
    home.mkdir(parents=True, exist_ok=True)
    (home / META).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def dev_mode() -> bool:
    config.invalidate()  # 화면에서 방금 켰을 수 있다 — MCP 서버는 오래 떠 있다
    return config.get("workshop.dev_mode") is True


def guide() -> str:
    text = GUIDE.read_text(encoding="utf-8").replace("{{WORKSHOP}}", str(root()))
    editing = [p for p in projects() if p["source"] or p["installed"] != "none"]
    if editing:   # 설치된 룬을 고치는 중 — 고칠 폴더는 여기 적힌 곳 하나다
        text += "\n\n## 지금 고치는 설치된 룬\n\n" + "\n".join(
            f"- `{p['id']}` ({p['name']}) — 고칠 폴더: `{p['path']}`. 사례·시험 기록은 `{project(p['id'])}`. "
            f"녹화: {', '.join(p['recordings']) or '없음'}" for p in editing)
    return text


# ── 정적 검사 — 코드를 실행하지 않는다 ─────────────────────────────

def validate_static(rune_id: str) -> dict:
    """문법·구조·매니페스트·권한 선언. **import 하지 않는다** — 최상위 코드도 돌지 않는다."""
    package = package_dir(rune_id)
    problems: list[str] = []
    if not (package / "__init__.py").is_file():
        problems.append(f"패키지가 없습니다: {package}\\__init__.py")
        return _static_report(rune_id, package, problems)
    for path in sorted(package.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            problems.append(f"문법 오류 {path.relative_to(package)}:{exc.lineno} {exc.msg}")
        except UnicodeError:
            problems.append(f"UTF-8 이 아닙니다: {path.relative_to(package)}")
    if not any(True for _ in _manifest_keywords(package)):
        problems.append("RuneManifest(...) 를 찾지 못했습니다")
    elif _manifest_string(package, "id") != rune_id:
        problems.append(f"매니페스트 id 가 폴더 이름({rune_id})과 다릅니다")
    if not _manifest_string(package, "version"):
        problems.append("매니페스트에 version 이 없습니다")
    declared = _declared(package)
    for capability, reasons in sorted(undeclared(declared, package).items()):
        problems.append(f"선언하지 않은 권한 {capability}: {', '.join(reasons)}")
    if profile.declared(package) == profile.PROFILE:
        problems += profile.check(package)
    return _static_report(rune_id, package, problems)


def _declared(package: Path) -> list[str]:
    for keyword in _manifest_keywords(package):
        if keyword.arg == "permissions" and isinstance(keyword.value, ast.List):
            return [e.value for e in keyword.value.elts if isinstance(e, ast.Constant)]
    return []


def _static_report(rune_id: str, package: Path, problems: list[str]) -> dict:
    table = profile.declared(package) == profile.PROFILE
    return {"rune_id": rune_id, "ok": not problems, "problems": problems,
            "fingerprint": seal.fingerprint(package),
            "profile": "table" if table else "legacy",
            **({} if table else {"profile_note": "표 룬(profile=\"table\")이 아닙니다. 기존 방식이라 이 PC 에서만 쓰고 "
                                                 "동료 공유·공개는 할 수 없습니다. 표 처리라면 표 룬으로 만드세요."}),
            "unverified": "코드를 실행하지 않은 검사입니다. 동작과 실행 안전성은 확인하지 않았습니다."}


# ── 시험 — 사례마다 별도 프로세스 ─────────────────────────────────

def run_test(rune_id: str, timeout: int = TEST_TIMEOUT_SECONDS) -> dict:
    """`cases/` 의 사례를 연습 실행으로 돌려 `expected/` 와 비교한다. 결과는 저장소에 남긴다."""
    if not dev_mode():
        raise WorkshopError("개발 모드가 꺼져 있습니다. VaderAI 의 룬 화면에서 '개발 모드' 를 켠 뒤 다시 시험하세요.")
    static = validate_static(rune_id)
    if not static["ok"]:
        return {**static, "cases": [], "note": "정적 검사를 통과하지 못해 시험하지 않았습니다."}
    home = project(rune_id)
    cases = sorted(p for p in (home / "cases").glob("*") if (p / "input").is_dir())
    if not cases:
        raise WorkshopError(f"시험 사례가 없습니다. {home / 'cases'}\\<사례>\\input 과 expected 를 만드세요.")
    with tempfile.TemporaryDirectory(prefix="vader-test-") as tmp:
        # 사본을 돌린다 — 허용을 확인한 뒤 원본이 바뀌어도 시험되는 것은 허용한 그 지문이다.
        package = Path(shutil.copytree(package_dir(rune_id), Path(tmp) / rune_id,
                                       ignore=shutil.ignore_patterns("__pycache__")))
        if not seal.trusted(_test_key(rune_id), package):
            (home / TEST_REQUEST).write_text(json.dumps(
                {"fingerprint": seal.fingerprint(package), "at": datetime.now().isoformat(timespec="seconds")}),
                encoding="utf-8")
            return {**static, "ok": False, "cases": [], "waiting_for_person": True,
                    "note": "이 버전은 아직 사람이 시험을 허용하지 않았습니다. 사용자에게 'VaderAI 룬 화면의 "
                            "개발 모드에서 시험 요청을 확인하고 허용해 달라' 고 말한 뒤, 허용하면 rune_test 를 다시 "
                            "부르세요. 코드를 고치면 다시 허용받아야 합니다."}
        tested = seal.fingerprint(package)
        # 사례도 사본으로 고정한다 — 도는 중에 fixtures·정답이 바뀌어도 기록은 실제로 쓴 것이다 (codex1 P2-4)
        frozen = Path(shutil.copytree(home / "cases", Path(tmp) / "cases"))
        cases_fingerprint = seal.fingerprint(frozen)
        results = [_run_case(package, frozen / case.name, timeout) for case in cases]
    report = {
        "rune_id": rune_id, "ok": all(r["ok"] for r in results), "at": datetime.now().isoformat(timespec="seconds"),
        "fingerprint": tested, "cases_fingerprint": cases_fingerprint,
        "python": sys.version.split()[0], "cases": results,
        "isolation": "OS 격리 없음 — 같은 사용자 권한의 별도 프로세스, 입력 사본, 시간 제한",
    }
    (home / REPORT).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def projects() -> list[dict]:
    """제작 공간의 룬들과 지금 상태. 화면의 제작소 목록이다. 코드를 실행하지 않는다.

    test: none(시험 전) · pass · fail · stale(시험 뒤 코드가 바뀜). packed: 지금 코드 그대로 포장돼 있나.
    """
    out = []
    base = root()
    waiting = {t["id"] for t in test_requests()}
    for home in sorted(base.iterdir() if base.is_dir() else []):
        if not (home.is_dir() and ID_PATTERN.fullmatch(home.name)):
            continue
        package = package_dir(home.name)
        if not (package / "__init__.py").is_file():
            continue
        current = seal.fingerprint(package)
        meta = _json(home / META) or {}
        installed = seal.fingerprint(config.rune_dir() / home.name)
        report = _json(home / REPORT)
        test = ("none" if not report else "stale" if report.get("fingerprint") != current
                else "pass" if report.get("ok") else "fail")
        out.append({"id": home.name, "name": _manifest_string(package, "name") or home.name,
                    "version": _manifest_string(package, "version"),
                    "profile": "table" if profile.declared(package) == profile.PROFILE else "legacy",
                    "test": test, "tested_at": (report or {}).get("at", ""),
                    "cases": [{"case": c.get("case"), "ok": bool(c.get("ok"))} for c in (report or {}).get("cases", [])],
                    "packed": (_json(home / "dist" / "manifest.json") or {}).get("fingerprint") == current,
                    "waiting": home.name in waiting,
                    "source": meta.get("source") or "", "path": str(package),
                    # same: 고친 것이 없다 · changed: 고친 내용이 아직 설치되지 않았다 · none: 설치돼 있지 않다
                    "installed": "none" if not installed else "same" if installed == current else "changed",
                    # 가져온 뒤 설치본이 따로 바뀌었다 — 이 편집본이 더 오래됐을 수 있다 (codex1 지적)
                    "installed_moved": bool(installed and meta.get("from_fingerprint")
                                            and installed != meta["from_fingerprint"] and installed != current),
                    "recordings": sorted(r.stem for r in (package / "recordings").glob("*.json")),
                    "legacy_recordings": _legacy_for(home.name, package)})
    return out


def _legacy_for(rune_id: str, package: Path) -> list[str]:
    from vader.core import recorder

    return [n for n, owner in LEGACY_RECORDINGS.items() if owner == rune_id
            and recorder.path_for(n).is_file() and not (package / "recordings" / f"{n}.json").exists()]


def _json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _test_key(rune_id: str) -> str:
    """시험 허용 원장의 열쇠. 룬 id 에는 '/' 가 없어 설치 확인과 섞이지 않는다."""
    return f"workshop/{rune_id}"


def test_requests() -> list[dict]:
    """사람이 확인해야 할 시험 요청. 요약은 요청 파일이 아니라 **지금 코드**에서 다시 만든다 — 파일은 LLM 도 쓸 수 있다."""
    out = []
    for request in sorted(root().glob(f"*/{TEST_REQUEST}")):
        rune_id = request.parent.name
        package = package_dir(rune_id)
        try:
            asked = json.loads(request.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        current = seal.fingerprint(package)
        if current and asked.get("fingerprint") == current and not seal.trusted(_test_key(rune_id), package):
            out.append({"id": rune_id, "name": _manifest_string(package, "name") or rune_id,
                        "fingerprint": current, "at": asked.get("at", ""),
                        "profile": "table" if profile.declared(package) == profile.PROFILE else "legacy",
                        **summary(package)})
    return out


def allow_test(rune_id: str, fingerprint: str) -> None:
    """사람이 요약을 보고 '이 버전은 이 PC 에서 시험해도 된다' 고 허용한다. 배포 근거는 아니다."""
    home = project(rune_id)
    if seal.fingerprint(package_dir(rune_id)) != fingerprint:
        raise WorkshopError("요청한 뒤에 코드가 바뀌었습니다. 다시 시험을 요청하게 하세요.")
    seal.trust(_test_key(rune_id), package_dir(rune_id))
    (home / TEST_REQUEST).unlink(missing_ok=True)


def _run_case(package: Path, case: Path, timeout: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="vader-case-") as tmp:
        work = Path(tmp)
        shutil.copytree(case / "input", work / "input")  # 원본 사례는 룬이 못 건드린다
        try:
            done = subprocess.run(
                [*config.module_command("vader.core.workshop"), str(package), str(case), str(work)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                # MCP 서버의 stdin(대화 파이프)을 물려주지 않는다 — 다른 스레드가 읽는 중이면 Windows 에서 자식이 시작하다 멈춘다
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),   # 콘솔 실행 파일이라도 창을 띄우지 않는다
                # 바이트코드를 남기지 않는다 — LLM 이 1초 안에 고치면 옛 .pyc 가 그대로 돌았다.
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        except subprocess.TimeoutExpired:
            return {"case": case.name, "ok": False, "error": f"{timeout}초 안에 끝나지 않았습니다 (중단함)"}
        result_file = work / "result.json"
        if not result_file.exists():
            return {"case": case.name, "ok": False, "error": "룬 프로세스가 결과 없이 끝났습니다",
                    "stderr": done.stderr[-2000:]}
        result = json.loads(result_file.read_text(encoding="utf-8"))
        diffs = _compare(case / "expected", [Path(a) for a in result.pop("artifacts", [])])
        ok = result.get("ok", False) and all(d["same"] for d in diffs)
        return {"case": case.name, **result, "ok": ok, "compared": diffs}


def _compare(expected_dir: Path, artifacts: list[Path]) -> list[dict]:
    """정답 파일마다 같은 이름(앞의 날짜 숫자는 무시)의 결과와 비교한다."""
    def key(path: Path) -> str:
        return re.sub(r"^\d+_", "", path.name)

    produced = {key(a): a for a in artifacts}
    out = []
    for want in sorted(expected_dir.glob("*")) if expected_dir.is_dir() else []:
        got = produced.get(key(want))
        if got is None:
            out.append({"file": want.name, "same": False, "detail": "이 정답에 맞는 결과 파일이 없습니다"})
            continue
        out.append({"file": want.name, **_diff(want, got)})
    return out


def _diff(want: Path, got: Path) -> dict:
    if want.suffix.lower() in {".xlsx", ".csv"}:
        a, b = _rows(want), _rows(got)
        # 행의 존재만이 아니라 횟수까지 — 같은 행이 두 번 들어가거나 빠져도 잡는다 (codex1 eonm 시험 길)
        want_n, got_n = Counter(map(tuple, a)), Counter(map(tuple, b))
        missing = [list(r) for r in (want_n - got_n).elements()]
        extra = [list(r) for r in (got_n - want_n).elements()]
        return {"same": not missing and not extra, "rows": [len(a), len(b)],
                "missing": missing[:DIFF_LIMIT], "extra": extra[:DIFF_LIMIT],
                "detail": f"정답 {len(a)}행 · 결과 {len(b)}행 · 빠짐 {len(missing)} · 더함 {len(extra)}"}
    if want.read_bytes() == got.read_bytes():
        return {"same": True, "detail": "같음"}
    return {"same": False, "detail": "내용이 다릅니다 (표 파일이 아니라 줄 단위 비교는 하지 않았습니다)"}


def _rows(path: Path) -> list[list[str]]:
    """표를 '시트 이름 + 셀 값' 줄 목록으로. 서식은 보지 않는다."""
    def cell(v: Any) -> str:
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return "" if v is None else str(v).strip()

    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            return [[cell(v) for v in row] for row in csv.reader(f) if any(row)]
    from openpyxl import load_workbook
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        return [[sheet.title, *(cell(v) for v in row)]
                for sheet in book.worksheets for row in sheet.iter_rows(values_only=True)
                if any(v is not None and str(v).strip() for v in row)]
    finally:
        book.close()   # 읽기 전용은 파일을 잡고 있다 — 안 닫으면 Windows 가 사례 임시 폴더를 못 지운다


# ── 포장 — 시험한 그대로 ────────────────────────────────────────

def pack(rune_id: str) -> dict:
    """마지막 시험을 통과한 **그 소스**만 포장한다. GitHub 릴리스처럼 manifest.json + zip 두 개."""
    home = project(rune_id)
    package = package_dir(rune_id)
    report_file = home / REPORT
    if not report_file.exists():
        raise WorkshopError("시험 기록이 없습니다. 먼저 시험하세요.")
    report = json.loads(report_file.read_text(encoding="utf-8"))
    if not report.get("ok"):
        raise WorkshopError("마지막 시험이 통과하지 못했습니다. 고친 뒤 다시 시험하세요.")
    if report.get("fingerprint") != seal.fingerprint(package):
        raise WorkshopError("시험 뒤에 소스가 바뀌었습니다. 다시 시험한 뒤 포장하세요.")
    if report.get("cases_fingerprint") != seal.fingerprint(home / "cases"):
        raise WorkshopError("시험 뒤에 사례(입력·정답)가 바뀌었습니다. 다시 시험한 뒤 포장하세요.")
    static = validate_static(rune_id)   # 검사한 것만 포장한다 — 규칙이 바뀌었으면 여기서 걸린다
    if not static["ok"]:
        raise WorkshopError("정적 검사를 통과하지 못했습니다: " + "; ".join(static["problems"][:5]))
    meta = _json(home / META) or {}
    if meta.get("release_copy"):
        from vader.core import release

        if meta.get("state") != "ready":
            raise WorkshopError("배포 사본을 만들다 멈춘 공간입니다. 지우고 다시 만드세요.")
        leaked = release.check(package)    # 시험한 그 사본에 금지 항목이 들어왔으면 포장하지 않는다
        if leaked:
            raise WorkshopError("배포 사본에 넣으면 안 되는 것이 있습니다: " + "; ".join(leaked[:5]))
        # 배포 시험은 사례마다 정답 대조가 하나 이상 있어야 한다 — 비교 없는 통과는 증거가 아니다 (codex1 P1-3)
        if any(not case.get("compared") for case in report["cases"]):
            raise WorkshopError("정답(expected) 대조가 없는 시험 사례가 있습니다. 배포 사본은 정답과 비교해야 합니다.")

    everything = [p for p in package.rglob("*") if p.is_file() and "__pycache__" not in p.relative_to(package).parts]
    files = sorted(p for p in everything if not PACK_SKIP.intersection(p.relative_to(package).parts)
                   and p.suffix.lower() in PACK_SUFFIXES)
    left_out = sorted(p.relative_to(package).as_posix() for p in set(everything) - set(files))
    if left_out:   # 빠지는 파일이 있으면 시험한 지문과 포장 내용이 달라진다
        raise WorkshopError("포장에 넣지 않는 파일이 룬 폴더에 있습니다: " + ", ".join(left_out[:5])
                            + ". 지우거나 사례 폴더(cases)로 옮긴 뒤 다시 시험하세요.")
    version = _manifest_string(package, "version")
    manifest = {
        "id": rune_id, "name": _manifest_string(package, "name"), "version": version,
        "profile": profile.declared(package) or "legacy",
        "permissions": _declared(package), "fingerprint": report["fingerprint"],
        "files": {p.relative_to(package).as_posix(): _sha256(p) for p in files},
        "tested": {"at": report["at"], "cases": [c["case"] for c in report["cases"]],
                   "cases_fingerprint": report["cases_fingerprint"], "isolation": report["isolation"]},
        "packed_at": datetime.now().isoformat(timespec="seconds"),
    }
    dist = home / "dist"
    dist.mkdir(exist_ok=True)
    archive = dist / f"{rune_id}-{version}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for p in files:
            z.write(p, f"{rune_id}/{p.relative_to(package).as_posix()}")
    (dist / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"archive": str(archive), "manifest": manifest,
            "next": "VaderAI 룬 화면의 '룬 설치' 에서 이 zip 을 골라 설치하고, 연습 실행 뒤 사람이 승인합니다."}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── 사례 하나를 도는 쪽 (별도 프로세스) ─────────────────────────────

class _CaseContext:
    """시험용 RuneContext. 연습 실행이고, 바깥으로 나가는 것은 전부 '시험에서 안 함' 이다."""

    def __init__(self, work: Path, params: dict, answers: list, fixtures: dict | None = None):
        self.workdir = work / "out"
        self.datadir = work / "data"
        self.workdir.mkdir()
        self.datadir.mkdir()
        self.params = params
        self.dry_run = True
        #: 제작소 시험 표시 — 화면을 쓰는 룬은 이것을 보고 실제 창 대신 화면 없는 대역을 쓴다 (codex1 eonm 시험 길)
        self.screenless = True
        self.logs: list[dict] = []
        self.unverified: list[str] = []
        self._answers = list(answers)
        # 사례의 `fixtures.json` — 바깥 호출 대신 돌려줄 고정 응답 (2026-10-29 신뢰 계약 12절).
        # 표에 없는 호출은 지금처럼 거부한다. 배포 소스는 바꾸지 않는다.
        fixtures = fixtures or {}
        self._ocr = dict(fixtures.get("ocr") or {})
        self._infer = list(fixtures.get("infer") or [])
        self._http = dict(fixtures.get("http_get") or {})
        self.violations: list[str] = []
        self.inputs: list = []
        self._checkpoint: dict | None = None

    def log(self, level, message, **fields):
        self.logs.append({"level": level, "message": message, **{k: str(v) for k, v in fields.items()}})

    def report_progress(self, current, total, message=""):
        pass

    def request_user_action(self, request):
        from vader.sdk import UserActionResponse
        if not self._answers:
            self.unverified.append(f"질문에 답이 없어 중단: {request.prompt}")
            return UserActionResponse(action="abort")
        return UserActionResponse(action="ok", value=self._answers.pop(0))

    def load_checkpoint(self):
        return self._checkpoint

    def save_checkpoint(self, data):
        self._checkpoint = data

    def clear_checkpoint(self):
        self._checkpoint = None

    def check_cancelled(self):
        pass

    def replay(self, name, stop=None):
        self.unverified.append(f"화면 재생 '{name}' — 시험에서는 누르지 않습니다 (미검증)")
        return False

    def _refuse(self, what):
        self.unverified.append(f"{what} — 시험에서는 쓰지 않습니다 (미검증)")
        # 룬이 이 예외를 잡고 계속해도 사례는 실패다 — 고정 응답 없는 바깥 호출을 했다 (codex1 P1-3)
        self.violations.append(what)
        raise RuntimeError(f"시험에서는 {what} 를 쓰지 않습니다. 사례에 고정 자료를 넣으세요")

    def read_table(self, source, sheet=None):
        if source not in self.inputs:
            raise tables.TableError("이번 작업의 입력만 읽을 수 있습니다")
        return tables.read_table(source.path, sheet)

    def write_table(self, name, rows):
        return tables.write_table(self.workdir, name, rows)

    # 실제 context 와 같은 서명이다 — 인자를 빠뜨리면 시험에서도 TypeError (codex1 P2-1).
    def ocr(self, path, prompt, schema):
        name = Path(path).name
        # 이번 사례의 입력 파일만 — 이름만 같은 다른 경로는 고정 응답을 받지 못한다
        if name not in self._ocr or Path(path).resolve() not in {Path(i.path).resolve() for i in self.inputs}:
            self._refuse(f"ocr({name})")
        return self._ocr.pop(name)

    def infer(self, prompt, schema):
        if not self._infer:
            self._refuse("infer")
        answer = self._infer.pop(0)
        if not isinstance(answer, dict) or "response" not in answer:
            self._refuse('infer (고정 응답은 {"prompt_contains": …, "response": …} 모양이어야 합니다)')
        if answer.get("prompt_contains") and answer["prompt_contains"] not in str(prompt):
            self._refuse(f"infer (기대한 요청이 아닙니다: {answer['prompt_contains'][:30]})")
        return answer["response"]

    def http_get(self, url, *, headers=None):
        from vader.core.http import HttpError, Response

        if url not in self._http:
            self._refuse(f"네트워크(http_get {url[:60]})")
        answer = self._http.pop(url)
        status = int(answer.get("status", 200))
        if status >= 400:    # 실제 gateway 처럼 — 오류 응답은 본문이 아니라 예외다 (codex1 P2-2)
            raise HttpError(f"HTTP {status}")
        return Response(status=status, text=str(answer.get("text", "")), headers=dict(answer.get("headers") or {}))

    def unused_fixtures(self) -> list[str]:
        """쓰지 않은 고정 응답 — 룬이 예상한 호출을 안 했다는 신호다."""
        return ([f"ocr({n})" for n in self._ocr] + [f"infer #{i + 1}" for i in range(len(self._infer))]
                + [f"http_get({u[:60]})" for u in self._http])

    def http_post(self, *a, **k):
        self._refuse("네트워크(http_post)")


def _case_main(package: Path, case: Path, work: Path) -> None:
    from vader.core.loader import _load_package, _validate_module
    from vader.sdk import Input

    def read_json(name, default):
        f = case / name
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else default

    ctx = _CaseContext(work, read_json("params.json", {}), read_json("answers.json", []),
                       read_json("fixtures.json", {}))
    out: dict[str, Any] = {"ok": False}
    try:
        rune = _validate_module(package.name, _load_package(package)).rune
        inputs = [Input(path=p) for p in sorted((work / "input").glob("*")) if p.is_file()]
        ctx.inputs = inputs
        result = rune.run(ctx, inputs)
        out = {"ok": bool(result.ok), "summary": result.summary,
               "failures": [f"{f.kind}: {f.message}" for f in result.failures],
               "artifacts": [str(a) for a in result.artifacts]}
    except Exception as exc:  # 룬의 어떤 실패도 시험 결과로 돌려준다
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    ctx.unverified += [f"고정 응답을 쓰지 않음: {name}" for name in ctx.unused_fixtures()]
    if ctx.violations:
        out.update(ok=False, error="고정 응답 없는 바깥 호출: " + ", ".join(ctx.violations))
    out.update(logs=ctx.logs[-50:], unverified=ctx.unverified)
    (work / "result.json").write_text(json.dumps(out, ensure_ascii=False, default=str), encoding="utf-8")


if __name__ == "__main__":
    from vader.core import gate
    gate.open_child()       # 앱이 넘긴 계정 작업공간만 — 없으면 업무 데이터를 못 연다
    _case_main(*(Path(a) for a in sys.argv[1:4]))
