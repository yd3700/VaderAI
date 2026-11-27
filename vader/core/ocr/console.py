"""로그인된 **Claude Code·Codex** 로 문서를 읽는 제공자 (2026-10-29 마스터 결정 — Gemini API 한도 대체, 선택 기능).

API 키 없이 그 PC 에 설치·로그인된 프로그램을 **화면 없이 한 번** 부른다 (`claude -p`, `codex exec`).
룬 코드는 그대로다 — 다른 제공자와 같은 자리에 끼우고, 결과는 게이트웨이가 같은 스키마로 검사한다.

- PDF 는 쪽 그림(PNG)으로 바꿔 `PAGES_PER_CALL` 쪽씩 묶어 보낸다. 묶음마다 "몇~몇 쪽" 을 알려 쪽 번호를 맞춘다.
- 지시문은 **표준 입력**으로 넘긴다 — Windows 의 `claude.cmd` 에 한글·따옴표를 인자로 넘기면 깨질 수 있다.
- 읽기만 (codex1 검토 반영): Claude Code 는 `--tools Read --restricted` (명령 도구 제거·파일 도구는 임시 폴더 안만·
  사용자 설정/훅 무시) + `--strict-mcp-config` (MCP 없음). Codex 는 read-only 샌드박스·승인 없음·사용자 설정과
  규칙 무시. 둘 다 세션을 디스크에 남기지 않는다. 그림은 임시 폴더에만 두고 바로 지운다.
- 오류에 CLI 출력 원문을 담지 않는다 — 환자 이름이 섞일 수 있다. 로그인·사용 한도·그 밖으로만 나눈다.
- 대화 (2026-11-27 마스터 결정, codex2 console-ai-chat E 1단계): Claude Code·Codex 는 **글만 주고받는** 무상태 대화도 한다.
  매번 이력 전체를 표준 입력 한 덩어리로 — CLI 세션을 남기지도 이어 쓰지도 않는다. 도구는 **하나도** 주지 않는다
  (Claude `--tools ""`, Codex 셸·웹 검색 끔. 2026-11-27 실측: 파일 읽기·셸 요청에 둘 다 '도구 없음').
  `chat()` 에 tools 인자가 없다 — 그래서 코어의 도구 루프(설정 바꾸기 등)를 타지 않는다. Antigravity 는 대화도 닫는다.
- 같은 프로그램은 한 번에 하나만 돈다 (프로그램별 차례). 기다리는 대화가 있으면 문서 읽기는 다음 묶음 전에 양보한다.
  차례를 받은 뒤, 프로그램을 띄우기 **직전에** 화면 잠김·호출한 쪽의 확인(`checking`)을 다시 본다 — 기다리는 사이 잠갔거나
  허용을 껐으면 보내지 않는다 (codex1 next3 ②·①).
- API 키·주소 환경 변수는 넘기지 않는다 — 로그인 구독이 아닌 다른 인증·과금 길로 새지 않게 (codex1 next3 ②).

**약관:** 구독 로그인은 본인 사용 용도다. 다른 사람에게 배포해 그쪽 구독으로 돌리게 할지는 두 회사 약관을 확인한 뒤에.
"""
from __future__ import annotations

import json
import os
import signal
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from vader.core import config
from vader.core.ocr.base import OcrProviderError, OcrProviderUnavailable

PAGES_PER_CALL = 5
CALL_TIMEOUT_SECONDS = 600
CHAT_TIMEOUT_SECONDS = 180        # 대화는 짧게 — 문서 읽기의 600초를 그대로 쓰지 않는다
CHAT_WAIT_SECONDS = 60            # 같은 프로그램이 문서를 읽는 중이면 이만큼만 기다린다
CHAT_TOTAL_MAX = 60000           # 이력 예산 (글자). 넘치면 **오래된 대화 턴을 통째로** 뺀다 — 자료(JSON) 메시지를 중간에서 자르지 않는다
#: 구독 대신 API 로 돌게 만드는 환경 변수 — 자식에게 넘기지 않는다
_API_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY")


class _Turn:
    """프로그램 하나의 차례 — 한 번에 하나. 대화가 기다리면 문서 읽기는 새 묶음을 잡지 않고 양보한다."""

    def __init__(self):
        self._cond, self._busy, self._chats = threading.Condition(), False, 0

    def take(self, chat: bool, timeout: float | None = None) -> bool:
        with self._cond:
            self._chats += chat
            try:
                ok = self._cond.wait_for(lambda: not self._busy and (chat or not self._chats), timeout)
                if ok:
                    self._busy = True
                return ok
            finally:
                self._chats -= chat
                self._cond.notify_all()

    def give(self) -> None:
        with self._cond:
            self._busy = False
            self._cond.notify_all()


_TURNS = {"claude": _Turn(), "codex": _Turn(), "agy": _Turn()}
_guard = threading.local()


def set_alive(check) -> None:
    """이 스레드의 다음 대화가 '아직 열려 있나' 를 물을 함수. host 가 요청마다 넣는다 (messenger.set_alive 와 같은 모양)."""
    _guard.alive = check


class checking:
    """`with console.checking(fn):` — 이 안에서 부른 대화는 띄우기 직전에 fn() 이 참일 때만 보낸다. 거짓이면 OcrProviderUnavailable."""

    def __init__(self, fn):
        self.fn = fn

    def __enter__(self):
        _guard.checks = [*getattr(_guard, "checks", []), self.fn]

    def __exit__(self, *exc):
        _guard.checks = getattr(_guard, "checks", [])[:-1]


def _still_ok() -> None:
    alive = getattr(_guard, "alive", None)
    if alive is not None and not alive():
        raise OcrProviderUnavailable("잠겨 있어요.")
    if not all(fn() for fn in getattr(_guard, "checks", [])):
        raise OcrProviderUnavailable("보내기 전에 설정이 바뀌어 보내지 않았어요. 다시 물어봐 주세요.")


class _taken:
    def __init__(self, client: str, chat: bool):
        self.turn, self.chat = _TURNS[client], chat

    def __enter__(self):
        if not self.turn.take(self.chat, CHAT_WAIT_SECONDS if self.chat else None):
            raise OcrProviderUnavailable("이 프로그램이 문서를 읽는 중이에요. 잠시 뒤에 다시 물어봐 주세요.")

    def __exit__(self, *exc):
        self.turn.give()
RENDER_SCALE = 2
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: 기본 모델. 무거운 기본 모델은 구독 사용량을 빨리 쓴다 — 설정 `api.providers.<이름>.model` 로 바꾼다.
DEFAULT_MODELS = {"claude_console": "sonnet", "codex_console": "", "agy_console": ""}


class ConsoleProvider:
    def __init__(self, client: str, model: str | None = None) -> None:
        if client not in ("claude", "codex", "agy"):
            raise ValueError(client)
        self._client = client
        self._model = model

    def name(self) -> str:
        return f"{self._client}_console"

    def model(self) -> str:
        return str(self._model or config.get(f"api.providers.{self.name()}.model", DEFAULT_MODELS[self.name()]) or "")

    def adapter_version(self) -> str:
        return "2"   # 격리 플래그·쪽 범위 검사 (codex1 검토)

    def supports(self, role: str) -> bool:
        return role in ("extract", "infer") or (role == "chat" and self._client != "agy")

    def is_available(self) -> bool:
        return shutil.which(self._client) is not None

    def chat(self, messages):
        """글만 주고받는 무상태 대화 — 답은 문자열. tools 인자를 일부러 두지 않는다 (코어 도구 루프 밖)."""
        if not self.supports("chat"):
            raise OcrProviderUnavailable(f"{self.name()} 는 대화를 하지 않습니다 — 문서 읽기·판단만 합니다.")
        text = _transcript(messages)
        with _taken(self._client, chat=True), tempfile.TemporaryDirectory(prefix="vader-console-chat-") as tmp:
            _still_ok()                    # 기다리는 사이 잠겼거나 허용이 꺼졌으면 여기서 멈춘다 — 아직 아무것도 안 보냈다
            return self._ask(text, [], Path(tmp), chat=True)

    # ── 읽기 ──

    def extract(self, path: Path, prompt: str, schema: type) -> object:
        path = Path(path)
        with tempfile.TemporaryDirectory(prefix="vader-console-ocr-") as tmp:
            pages = _pages(path, Path(tmp))
            rows: list = []
            for start in range(0, len(pages), PAGES_PER_CALL):
                batch = pages[start:start + PAGES_PER_CALL]
                where = (f"첨부한 그림 {len(batch)}장은 문서 '{path.name}' 의 {start + 1}~{start + len(batch)}쪽이다 "
                         f"(전체 {len(pages)}쪽). 쪽 번호 필드가 있으면 문서 전체 기준 쪽 번호를 쓴다. "
                         "이 쪽들에 해당하는 결과만 낸다.")
                with _taken(self._client, chat=False):      # 묶음마다 차례를 받는다 — 기다리는 대화가 먼저
                    answer = self._ask(f"{prompt}\n\n{where}\n\n{_format(schema)}", batch, Path(tmp))
                if isinstance(answer, list):
                    _check_pages(answer, start + 1, start + len(batch))
                    rows.extend(answer)
                elif len(pages) == 1:
                    return answer
                else:
                    raise OcrProviderError(f"{self.name()} 가 배열이 아닌 답을 냈습니다 ({start + 1}쪽부터)")
            return rows

    def complete(self, prompt: str, schema: type) -> object:
        with _taken(self._client, chat=False), tempfile.TemporaryDirectory(prefix="vader-console-ask-") as tmp:
            return self._ask(f"{prompt}\n\n{_format(schema)}", [], Path(tmp))

    # ── 부르기 ──

    def _ask(self, text: str, images: list[Path], work: Path, chat: bool = False) -> object:
        """chat 이면 도구 없이 글 하나를 돌려준다. 아니면 답에서 JSON 을 꺼낸다."""
        exe = shutil.which(self._client)
        if not exe:
            raise OcrProviderUnavailable(f"{self._client} 가 이 PC 에 설치되어 있지 않습니다.")
        model = self.model()
        if self._client == "claude":
            names = ", ".join(p.name for p in images)
            if images:
                text = f"이 폴더의 그림 파일 {names} 을 Read 로 차례대로 읽고 답하라.\n\n{text}"
            tools = ["--tools", ""] if chat else ["--tools", "Read", "--allowedTools", "Read"]
            # stream-json — 마지막 result 줄이 예전 봉투와 같고, 같이 오는 rate_limit_event 로 구독 사용량을 안다 (2026-11-27 실측)
            args = [exe, "-p", "--output-format", "stream-json", "--verbose", *tools, "--restricted",
                    "--strict-mcp-config", "--no-session-persistence", "--permission-mode", "dontAsk"] \
                + (["--model", model] if model else [])
            gen = _limits["gen"]        # 부르기 시작한 때의 세대 — 그 사이 잠갔으면 늦게 온 사용량은 버린다 (codex1 next4 ②)
            out = _run(args, text, work, CHAT_TIMEOUT_SECONDS if chat else CALL_TIMEOUT_SECONDS)
            envelope = _envelope(out, gen)
            if envelope is None:
                raise OcrProviderError("Claude Code 응답을 읽지 못했습니다")
            if envelope.get("is_error"):
                _raise_for(envelope.get("api_error_status"), str(envelope.get("api_error", "")), "Claude Code")
            return _text_answer(envelope.get("result")) if chat else _json_answer(str(envelope.get("result", "")))
        if self._client == "agy":
            return _ask_agy(exe, model, text, images, work)
        # 묶음마다 새 출력 파일 — 앞 묶음의 답을 다시 읽지 않게 (codex1 지적)
        handle, name = tempfile.mkstemp(prefix="answer-", suffix=".txt", dir=work)
        os.close(handle)
        last = Path(name)
        last.unlink()
        args = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "-s", "read-only", "-c", 'approval_policy="never"', "-o", str(last)] \
            + (["-c", "features.shell_tool=false", "-c", 'web_search="disabled"'] if chat else []) \
            + (["-m", model] if model else []) + [arg for p in images for arg in ("-i", str(p))]
        _run(args, text, work, CHAT_TIMEOUT_SECONDS if chat else CALL_TIMEOUT_SECONDS)
        try:
            answer = last.read_text(encoding="utf-8")
            return _text_answer(answer) if chat else _json_answer(answer)
        except OSError:
            raise OcrProviderError("Codex 가 답을 남기지 않았습니다") from None


# Antigravity CLI (agy, 2026-11-19 마스터 결정 — 이 PC 에서 그림 읽기·금지 규칙 확인함, codex1 회신 1 반영).
# 읽기만 — **이 실행에만 쓰는 설정(--settings 파일, 저장 안 됨)** 으로 명령·파일 쓰기·인터넷·브라우저·MCP 를 거부 규칙에 넣는다.
#   거부가 허용보다 앞서고(Deny > Ask > Allow), 화면 없는 실행에서 승인이 필요한 일은 하지 않고 넘어간다. --sandbox 는
#   Windows 11 24H2·2026-08 업데이트 이상에서만 효과가 있어 거기에 기대지 않는다(덤으로만 켠다). agentMode="" 로 auto 를 끈다.
# 기록 — 화면 없는 실행도 ~/.gemini/antigravity-cli 에 대화(brain·conversations·presence)와 요약 한 줄
#   (conversation_summaries.db)을 남긴다. 실시간 출력 첫 줄(init)에서 대화 번호를 받아 **성공·실패·시간 초과 모두** 지운다.
# 글은 -p 인자로만 받는다(agy.exe 는 cmd 를 안 거쳐 한글 그대로). 너무 길면 Windows 명령줄 한도 전에 거절한다.
AGY_HOME = Path.home() / ".gemini" / "antigravity-cli"
AGY_MAX_PROMPT = 24000
AGY_SETTINGS = {"agentMode": "", "toolPermission": "request-review", "enableTerminalSandbox": True, "historySize": 0,
                "permissions": {"deny": ["command(*)", "write_file(*)", "read_url(*)", "execute_url(*)", "mcp(*)"]}}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


#: 문서 읽기를 여는 스위치 — 파일 읽기(read_file)를 작업 폴더로 좁히는 방법을 아직 못 찾아 닫아 둔다 (codex1 회신 2, 2026-11-19).
#: 실측: 금지 규칙으로 명령·쓰기는 막히지만 작업 폴더 밖 파일도 읽힌다. 마스터 결정 전까지 False.
AGY_DOCUMENTS = False


def _ask_agy(exe: str, model: str, text: str, images: list[Path], work: Path) -> object:
    if not AGY_DOCUMENTS:
        raise OcrProviderUnavailable("Antigravity 문서 읽기는 아직 닫혀 있어요 — 다른 파일까지 읽을 수 있어 안전 확인 중이에요.")
    if images:
        text = f"이 폴더의 그림 파일 {', '.join(p.name for p in images)} 을 차례대로 읽고 답하라. 파일을 고치거나 명령을 실행하지 마라.\n\n{text}"
    if len(text) > AGY_MAX_PROMPT:
        raise OcrProviderError("Antigravity 에 보낼 지시문이 너무 깁니다")
    settings = work / "agy-settings.json"
    settings.write_text(json.dumps(AGY_SETTINGS), encoding="utf-8")
    args = [exe, "--output-format", "stream-json", "--print-timeout", f"{CALL_TIMEOUT_SECONDS}s", "--mode", "plan",
            "--sandbox", "--settings", str(settings)] + (["--model", model] if model else []) + ["-p", text]
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", cwd=work, creationflags=NO_WINDOW,
                                env={**os.environ, "NO_COLOR": "1"})
    except OSError:
        raise OcrProviderUnavailable("프로그램을 실행하지 못했습니다") from None
    out, err, timed_out = "", "", False
    try:
        out, err = proc.communicate(timeout=CALL_TIMEOUT_SECONDS + 30)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError):
            pass
    finally:
        for cid in set(_UUID.findall(out or "")):   # 대화 번호는 init 줄부터 모든 줄에 있다 — 실패해도 지운다
            _forget_agy(cid)
    if timed_out:
        raise OcrProviderError(f"{CALL_TIMEOUT_SECONDS}초 안에 답하지 않았습니다")
    result = None
    for line in (out or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event") == "result":
            result = event.get("result") or {}
    if result is None or result.get("status") != "SUCCESS":
        _raise_for(None, f"{(result or {}).get('error', '')}\n{err}", "Antigravity", proc.returncode or None)
    return _json_answer(str(result.get("response", "")))


def _real_dir(path: Path) -> bool:
    """링크·재분석 지점(junction)이 아닌 진짜 폴더 — 따라가지 않고 본다."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    import stat as _stat
    return _stat.S_ISDIR(st.st_mode) and not _stat.S_ISLNK(st.st_mode) and not getattr(st, "st_file_attributes", 0) & 0x400


def _forget_agy(conversation: str) -> None:
    """그 대화의 기록과 요약 한 줄을 지운다. 번호 모양이 아니거나, 기록 폴더(또는 그 위)가 링크면 아무것도 안 지운다."""
    if not _UUID.fullmatch(conversation or ""):
        return
    if not all(_real_dir(p) for p in (AGY_HOME.parent, AGY_HOME)):
        return
    for parent, leaf in (("brain", conversation), ("conversations", f"{conversation}.db"), ("presence", f"{conversation}.lock")):
        folder = AGY_HOME / parent
        if not _real_dir(folder):
            continue
        path = folder / leaf
        try:
            if _real_dir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file() and not path.is_symlink():
                path.unlink()
        except OSError:
            pass
    db = AGY_HOME / "conversation_summaries.db"
    if db.is_file() and not db.is_symlink():
        import sqlite3
        try:
            con = sqlite3.connect(db, timeout=5)
            try:
                for table in ("conversation_summaries", "conversation_parents", "conversations_fts"):
                    try:
                        con.execute(f'DELETE FROM "{table}" WHERE conversation_id=?', (conversation,))
                    except sqlite3.Error:
                        pass
                con.commit()
            finally:
                con.close()
        except sqlite3.Error:
            pass


def _transcript(messages) -> str:
    """이력 → 표준 입력 한 덩어리. 역할 경계는 JSON 으로 — 본문의 줄바꿈이나 '[system]' 글자로 흉내 내지 못한다 (codex1 next3 ②).

    system 과 마지막 user 메시지는 늘 넣는다. 예산을 넘으면 그 사이의 **오래된 것부터 통째로** 뺀다(중간에서 자르지 않는다).
    """
    rows = [{"role": str(m.get("role") if isinstance(m, dict) else getattr(m, "role", "")),
             "content": str((m.get("content") if isinstance(m, dict) else getattr(m, "content", "")) or "")} for m in messages]
    rows = [r for r in rows if r["role"] in ("system", "user", "assistant")]
    if not rows or rows[-1]["role"] != "user":
        raise OcrProviderError("보낼 말이 없습니다")
    system = [r for r in rows if r["role"] == "system"]
    middle = [r for r in rows[:-1] if r["role"] != "system"]
    size = lambda rs: len(json.dumps(rs, ensure_ascii=False))  # noqa: E731
    while middle and size(system + middle + rows[-1:]) > CHAT_TOTAL_MAX:
        middle.pop(0)
    kept = system + middle + rows[-1:]
    if size(kept) > CHAT_TOTAL_MAX:
        raise OcrProviderError("보낼 말이 너무 깁니다")
    return ("아래 JSON 배열은 대화 기록이다. role 이 system 인 것은 지켜야 할 지시, user·assistant 는 주고받은 말이다.\n"
            "마지막 user 에게 assistant 로서 보낼 다음 답 하나만, 머리말 없이 글로 쓴다.\n\n"
            + json.dumps(kept, ensure_ascii=False))


def _envelope(out: str, gen: int | None = None) -> dict | None:
    """Claude Code 출력 → 마지막 result 봉투. 줄마다 JSON(stream-json)이면 rate_limit_event 의 사용량도 남긴다.
    한 덩어리 JSON(예전 json 출력)도 그대로 받는다."""
    found = None
    for line in (out or "").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("type") == "rate_limit_event":
            if gen is not None:     # 실패 봉투 읽기(gen 없음)에서는 사용량을 남기지 않는다
                _remember_claude(d.get("rate_limit_info") or {}, gen)
        elif d.get("type") == "result" or "type" not in d:
            found = d
    if found is None:
        try:
            whole = json.loads(out)
            found = whole if isinstance(whole, dict) else None
        except ValueError:
            pass
    return found


def _env() -> dict:
    return {**{k: v for k, v in os.environ.items() if k.upper() not in _API_ENV}, "NO_COLOR": "1"}


def _text_answer(text) -> str:
    text = str(text or "").strip()
    if not text:
        raise OcrProviderError("빈 답이 왔습니다")
    return text


def _run(args: list[str], stdin: str, cwd: Path, timeout: int = CALL_TIMEOUT_SECONDS) -> str:
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", cwd=cwd, creationflags=NO_WINDOW, env=_env())
    except OSError:
        raise OcrProviderUnavailable("프로그램을 실행하지 못했습니다") from None
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise OcrProviderError(f"{timeout}초 안에 답하지 않았습니다") from None
    if proc.returncode:
        status = None
        envelope = _envelope(out)   # Claude Code 는 실패해도 봉투를 낸다
        if envelope is not None:
            status, kind = envelope.get("api_error_status"), str(envelope.get("api_error", ""))
        else:
            kind = f"{out}\n{err}"
        _raise_for(status, kind, Path(args[0]).stem, proc.returncode)
    return out


#: 확정된 문장만 — 업무 출력의 낱말("author" 등)로 오탐하지 않게 (codex1 지적)
LIMIT = re.compile(r"usage_limit_reached|hit your .{0,20}limit|rate_limit_exceeded|usage limit", re.I)
LOGIN = re.compile(r"not logged in|please (run )?.{0,10}log ?in|authentication_error|invalid api key|/login", re.I)


def _raise_for(status, kind: str, who: str, code: int | None = None):
    """원문은 담지 않고 분류만. 사용 한도·로그인은 다시 불러도 같다 — 게이트웨이가 재시도하지 않는다."""
    if status == 429 or LIMIT.search(kind):
        raise OcrProviderUnavailable(f"{who} 구독 사용 한도에 걸렸습니다. 한도가 풀린 뒤 다시 하세요.")
    if status == 401 or LOGIN.search(kind):
        raise OcrProviderUnavailable(f"{who} 로그인이 필요합니다. 그 프로그램에서 다시 로그인하세요.")
    raise OcrProviderError(f"{who} 가 실패했습니다" + (f" (코드 {code})" if code is not None else ""))


def _kill_tree(proc: subprocess.Popen) -> None:
    """.cmd → node 로 뜬 손자까지 끝낸다. 그래야 임시 폴더가 지워진다 (codex1 지적)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW,
                       stdin=subprocess.DEVNULL, timeout=10)
    else:
        proc.send_signal(signal.SIGKILL)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass                       # 정리가 멈춰도 본 호출의 시간 제한을 지킨다 (codex1 6단계 P3-1)


def _check_pages(rows: list, first: int, last: int) -> None:
    """묶음의 쪽 번호가 그 묶음 범위 안인가 — 모델이 1쪽부터 다시 매기면 다른 쪽과 섞인다 (codex1 지적)."""
    for row in rows:
        page = row.get("page") if isinstance(row, dict) else None
        if isinstance(page, int) and not first <= page <= last:
            raise OcrProviderError(f"{first}~{last}쪽 묶음에서 범위 밖 쪽 번호({page})가 나왔습니다")


def _pages(path: Path, work: Path) -> list[Path]:
    """PDF 는 쪽 그림으로, 그림은 그대로 복사한다 (원본 경로를 프로그램에 주지 않는다)."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return [Path(shutil.copy2(path, work / f"page_001{suffix}"))]
    if suffix != ".pdf":
        raise OcrProviderError(f"읽을 수 없는 파일 형식입니다: {path.suffix}")
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise OcrProviderUnavailable("PDF 를 그림으로 바꾸는 부품(pypdfium2)이 없습니다") from None
    try:
        document = pdfium.PdfDocument(str(path))
    except Exception:  # noqa: BLE001 — 깨진·암호 PDF
        raise OcrProviderError("PDF 를 열지 못했습니다") from None
    try:
        out = []
        for index in range(len(document)):
            target = work / f"page_{index + 1:03d}.png"
            document[index].render(scale=RENDER_SCALE).to_pil().save(target)
            out.append(target)
        return out
    except Exception:  # noqa: BLE001
        raise OcrProviderError("PDF 쪽을 그림으로 바꾸지 못했습니다") from None
    finally:
        document.close()


def _format(schema: type) -> str:
    from vader.core.ocr.claude import _json_schema

    return ("응답은 아래 JSON 스키마를 따르는 JSON **하나만** 출력한다. 설명·마크다운 금지.\n"
            + json.dumps(_json_schema(schema), ensure_ascii=False))


def _json_answer(text: str) -> object:
    """답에서 JSON 을 꺼낸다 — 코드 울타리(```json)를 붙여 보내는 경우가 있다."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
        if start >= 0:
            try:
                return json.loads(text[start:])
            except ValueError:
                pass
    raise OcrProviderError("답에서 JSON 을 찾지 못했습니다")


# ── 사용량 (Codex 만) ──
# 공식 `codex app-server` 의 account/rateLimits/read (codex2 B, 2026-11-27 실측). 토큰·자격 증명 파일은 읽지 않는다 — CLI 가 자기 로그인을 쓴다.
# 창 이름은 windowDurationMins 로만 붙이고, 값이 없으면 없는 대로(0% 로 채우지 않는다). 60초 동안은 지난 값을 다시 쓴다.
# Claude Code 는 밖에서 읽을 공식 수단을 못 찾았다 — 화면에 '—' 와 /usage 안내.
LIMITS_TTL = 60
_limits = {"at": 0.0, "value": None, "gen": 0, "reading": False}
_limits_lock = threading.Lock()      # 짧게만 잡는다 — 프로그램을 부르는 동안에는 안 잡는다 (화면 잠금이 기다리지 않게, codex1 회신 2 #2)


def forget_limits() -> None:
    """화면 잠금·연결 끊기 때 — 지난 값을 버리고, 지금 읽는 중인 답도 버리게 세대를 올린다."""
    with _limits_lock:
        _limits.update(at=0.0, value=None, gen=_limits["gen"] + 1)
        _claude.update(at=0.0, value=None)


# Claude Code — 밖에서 따로 묻는 공식 수단은 없고, 호출할 때마다 stream-json 에 rate_limit_event 가 같이 온다(2026-11-27 실측).
# 그 값을 메모리에만 남겨 '마지막 호출 기준' 으로 보인다. 사용량을 보려고 따로 부르지 않는다.
_claude = {"at": 0.0, "value": None}
_CLAUDE_WINDOWS = {"five_hour": 300, "seven_day": 10080}


def _remember_claude(info: dict, gen: int) -> None:
    import time
    windows = []
    unified = info.get("unifiedWindows") if isinstance(info, dict) else None
    for key, mins in _CLAUDE_WINDOWS.items():
        w = unified.get(key) if isinstance(unified, dict) else None
        if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
            at = w.get("resetsAt")
            windows.append({"window_mins": mins, "used_percent": round(w["utilization"] * 100),
                            "resets_at": at if isinstance(at, (int, float)) else None})
    if windows:
        with _limits_lock:
            if _limits["gen"] == gen:
                _claude.update(at=time.time(), value=windows)


def claude_limits() -> tuple:
    """(창 목록 또는 None, 마지막으로 본 때). 이 앱이 Claude Code 를 부른 적 없으면 (None, 0)."""
    with _limits_lock:
        return _claude["value"], _claude["at"]


def codex_limits() -> list[dict] | None:
    """[{"window_mins", "used_percent", "resets_at"}…] 또는 None(못 읽음). 이 PC 의 codex 가 없으면 None.
    다른 스레드가 읽는 중이면 기다리지 않고 지난 값."""
    import time
    with _limits_lock:
        if _limits["reading"] or time.time() - _limits["at"] < LIMITS_TTL:
            return _limits["value"]
        _limits["reading"], gen = True, _limits["gen"]
    value = None
    try:
        value = _read_codex_limits()
    except Exception:  # noqa: BLE001 — 사용량은 덤이다. 업무를 막지 않는다
        value = None
    finally:
        with _limits_lock:
            _limits["reading"] = False
            if _limits["gen"] == gen:        # 읽는 사이 잠갔으면 이 답은 버린다
                _limits.update(at=time.time(), value=value)
            else:
                value = None
    return value


def _read_codex_limits(timeout: float = 15) -> list[dict] | None:
    exe = shutil.which("codex")
    if not exe:
        return None
    proc = subprocess.Popen([exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW, env=_env(),
                            cwd=tempfile.gettempdir())
    found: list = []

    def talk():
        def send(o):
            proc.stdin.write(json.dumps(o) + "\n")
            proc.stdin.flush()
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "vaderai", "version": "1"}}})
        for line in proc.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == 1:
                if "result" not in m:
                    return
                send({"method": "initialized"})
                send({"id": 2, "method": "account/rateLimits/read"})
            elif m.get("id") == 2:
                found.append((m.get("result") or {}).get("rateLimits") or {})
                return
    reader = threading.Thread(target=talk, daemon=True)
    reader.start()
    reader.join(timeout)
    if proc.poll() is None:
        _kill_tree(proc)
    if not found:
        return None
    out = []
    for key in ("primary", "secondary"):
        w = found[0].get(key)
        if isinstance(w, dict) and isinstance(w.get("usedPercent"), (int, float)):
            mins, at = w.get("windowDurationMins"), w.get("resetsAt")
            out.append({"window_mins": mins if isinstance(mins, int) else None, "used_percent": w["usedPercent"],
                        "resets_at": at if isinstance(at, (int, float)) else None})
    return out or None
