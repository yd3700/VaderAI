"""로그인된 Claude Code·Codex 로 읽는 제공자 — CLI 는 가짜로 바꿔 네트워크 없이 본다 (2026-10-29)."""
import json
from pathlib import Path

import pytest

from vader.core.ocr import console
from vader.core.ocr.base import OcrProviderError, OcrProviderUnavailable
from vader.core.ocr.gateway import OcrGateway, UsageLedger

PATIENT = "홍길동가짜환자"


class Fake:
    """subprocess.Popen 대역 — 부른 인자·표준 입력을 남기고 정해 둔 답을 돌려준다.
    답이 None 이면 Codex 가 출력 파일을 쓰지 않은 것."""

    def __init__(self, answers, code=0, stderr=""):
        self.answers, self.code, self.stderr, self.calls = list(answers), code, stderr, []

    def __call__(self, args, cwd, **kw):
        fake = self

        class Proc:
            pid, returncode = 1, fake.code

            def communicate(self, stdin, timeout):
                fake.calls.append({"args": args, "stdin": stdin})
                answer = fake.answers.pop(0)
                if "-o" in args:                            # codex 는 마지막 메시지를 파일로 남긴다
                    if answer is not None:
                        Path(args[args.index("-o") + 1]).write_text(answer, encoding="utf-8")
                    answer = ""
                return answer, fake.stderr
        return Proc()


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    monkeypatch.setattr(console.shutil, "which", lambda name: f"C:/bin/{name}.cmd")


def pdf(tmp_path, pages):
    pdfium = pytest.importorskip("pypdfium2")
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(200, 200)
    path = tmp_path / "명세서.pdf"
    doc.save(str(path))
    return path


def image(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(b"png")
    return path


def claude_answer(rows):
    return json.dumps({"is_error": False, "result": "```json\n" + json.dumps(rows, ensure_ascii=False) + "\n```"})


def test_a_long_pdf_is_read_in_batches_with_true_page_numbers(tmp_path, monkeypatch):
    fake = Fake([claude_answer([{"page": 1}]), claude_answer([{"page": 6}]), claude_answer([{"page": 11}])])
    monkeypatch.setattr(console.subprocess, "Popen", fake)
    rows = console.ConsoleProvider("claude").extract(pdf(tmp_path, 12), "행을 읽어라", list)
    assert [r["page"] for r in rows] == [1, 6, 11] and len(fake.calls) == 3
    assert "6~10쪽" in fake.calls[1]["stdin"] and "전체 12쪽" in fake.calls[1]["stdin"]
    assert "page_011.png, page_012.png 을" in fake.calls[2]["stdin"] and "page_010.png" not in fake.calls[2]["stdin"]
    args = fake.calls[0]["args"]
    assert args[args.index("--tools") + 1] == "Read" and "--restricted" in args and "--strict-mcp-config" in args
    assert "--no-session-persistence" in args
    assert "행을 읽어라" not in " ".join(args), "지시문은 인자가 아니라 표준 입력으로"


def test_a_batch_that_renumbers_its_pages_fails_the_whole_document(tmp_path, monkeypatch):
    """6~10쪽 묶음이 1쪽이라 답하면 앞 묶음과 섞인다 — 부분 결과 없이 전체 실패."""
    monkeypatch.setattr(console.subprocess, "Popen", Fake([claude_answer([{"page": 1}]), claude_answer([{"page": 1}])]))
    with pytest.raises(OcrProviderError, match="범위 밖"):
        console.ConsoleProvider("claude").extract(pdf(tmp_path, 7), "읽어라", list)


def test_codex_is_read_only_and_never_reuses_the_previous_answer(tmp_path, monkeypatch):
    fake = Fake([json.dumps([{"page": 1}]), None])
    monkeypatch.setattr(console.subprocess, "Popen", fake)
    with pytest.raises(OcrProviderError, match="답을 남기지"):
        console.ConsoleProvider("codex").extract(pdf(tmp_path, 7), "읽어라", list)
    args = fake.calls[0]["args"]
    assert args[args.index("-s") + 1] == "read-only" and "-i" in args
    assert {"--ephemeral", "--ignore-user-config", "--ignore-rules"} <= set(args) and 'approval_policy="never"' in args
    assert fake.calls[0]["args"][fake.calls[0]["args"].index("-o") + 1] != args_out(fake.calls[1])


def args_out(call):
    return call["args"][call["args"].index("-o") + 1]


def test_errors_are_classified_without_the_cli_output(tmp_path, monkeypatch):
    """CLI 출력에 환자 이름이 있어도 오류 문장에 담지 않는다. 한도·로그인은 '사용 불가'."""
    limit = json.dumps({"is_error": True, "api_error_status": 429, "api_error": "usage_limit_reached",
                        "result": f"You've hit your 5-hour limit {PATIENT}"})
    cases = [(Fake([limit], code=1), OcrProviderUnavailable, "사용 한도"),
             (Fake([""], code=1, stderr=f"Not logged in. Please run /login {PATIENT}"), OcrProviderUnavailable, "로그인"),
             (Fake([""], code=2, stderr=f"author {PATIENT} 이상한 오류"), OcrProviderError, "코드 2")]
    for fake, kind, words in cases:
        monkeypatch.setattr(console.subprocess, "Popen", fake)
        with pytest.raises(kind, match=words) as caught:
            console.ConsoleProvider("claude").extract(image(tmp_path), "읽어라", list)
        assert PATIENT not in str(caught.value)
    monkeypatch.setattr(console.subprocess, "Popen", Fake([json.dumps({"is_error": False, "result": "모르겠어요"})]))
    with pytest.raises(OcrProviderError, match="JSON"):
        console.ConsoleProvider("claude").extract(image(tmp_path), "읽어라", list)


def test_the_gateway_does_not_retry_a_usage_limit(tmp_path, monkeypatch):
    limit = json.dumps({"is_error": True, "api_error_status": 429, "api_error": "usage_limit_reached"})
    fake = Fake([limit, limit, limit], code=1)
    monkeypatch.setattr(console.subprocess, "Popen", fake)
    gateway = OcrGateway(console.ConsoleProvider("claude"), cache_dir=tmp_path / "cache", ledger=UsageLedger(tmp_path / "usage"),
                         sleep=lambda s: None)
    with pytest.raises(OcrProviderUnavailable):
        gateway.extract(image(tmp_path), "읽어라", list)
    assert len(fake.calls) == 1


def test_it_reads_and_judges_but_does_not_chat():
    provider = console.ConsoleProvider("claude")
    assert provider.supports("extract") and provider.supports("infer") and not provider.supports("chat")
    with pytest.raises(OcrProviderUnavailable):
        provider.chat([{"role": "user", "content": "안녕"}])
