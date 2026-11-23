"""
SDK 계약 테스트.

계약은 코드를 쓰기 전에 고정한다. 룬이 의존하기 시작하면 바꾸기 비싸진다.
구현은 나중에 채우되 형태는 지금 확정한다.
"""
import hashlib
from pathlib import Path

import pytest

from vader.sdk import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointKey,
    Failure,
    Input,
    Rune,
    RuneCancelled,
    RuneContext,
    RuneManifest,
    RuneResult,
    UserActionRequest,
    UserActionResponse,
)


# ── 입력은 명시적으로 전달된다 ─────────────────────────────────

class TestInput:

    def test_carries_an_explicit_path(self):
        item = Input(path=Path("invoice.pdf"))
        assert item.path == Path("invoice.pdf")
        assert item.kind == "file"

    def test_is_frozen(self):
        item = Input(path=Path("a.pdf"))
        with pytest.raises(Exception):
            item.path = Path("b.pdf")

    def test_meta_is_per_instance(self):
        a, b = Input(path=Path("a")), Input(path=Path("b"))
        a.meta["page"] = 1
        assert b.meta == {}


# ── 체크포인트 신원 ────────────────────────────────────────────

class TestCheckpointKey:

    def test_identity_is_input_content_not_filename(self, tmp_path):
        """파일명이 달라도 내용이 같으면 같은 체크포인트다."""
        a = tmp_path / "송장.pdf"
        b = tmp_path / "invoice_copy.pdf"
        a.write_bytes(b"same bytes")
        b.write_bytes(b"same bytes")

        key_a = CheckpointKey.from_inputs("eonm_ipgo", [Input(path=a)])
        key_b = CheckpointKey.from_inputs("eonm_ipgo", [Input(path=b)])
        assert key_a == key_b

    def test_changed_content_makes_a_new_checkpoint(self, tmp_path):
        f = tmp_path / "a.pdf"
        f.write_bytes(b"v1")
        first = CheckpointKey.from_inputs("eonm_ipgo", [Input(path=f)])
        f.write_bytes(b"v2")
        second = CheckpointKey.from_inputs("eonm_ipgo", [Input(path=f)])
        assert first != second

    def test_different_rune_same_input_is_a_different_key(self, tmp_path):
        f = tmp_path / "a.pdf"
        f.write_bytes(b"x")
        assert (
            CheckpointKey.from_inputs("eonm_ipgo", [Input(path=f)])
            != CheckpointKey.from_inputs("ins_singo", [Input(path=f)])
        )

    def test_input_order_does_not_change_identity(self, tmp_path):
        a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
        a.write_bytes(b"A")
        b.write_bytes(b"B")
        forward = CheckpointKey.from_inputs("r", [Input(path=a), Input(path=b)])
        reverse = CheckpointKey.from_inputs("r", [Input(path=b), Input(path=a)])
        assert forward == reverse

    def test_identity_is_a_sha256_hex_digest(self, tmp_path):
        f = tmp_path / "a.pdf"
        f.write_bytes(b"payload")
        key = CheckpointKey.from_inputs("r", [Input(path=f)])
        assert key.input_identity == hashlib.sha256(b"payload").hexdigest()

    def test_is_hashable_so_it_can_key_a_dict(self, tmp_path):
        f = tmp_path / "a.pdf"
        f.write_bytes(b"x")
        key = CheckpointKey.from_inputs("r", [Input(path=f)])
        assert {key: "state"}[key] == "state"

    def test_schema_version_is_declared(self):
        """룬 업그레이드로 payload 구조가 바뀌면 이 값을 올린다."""
        assert isinstance(CHECKPOINT_SCHEMA_VERSION, int)
        assert CHECKPOINT_SCHEMA_VERSION >= 1


# ── 취소는 협조적이다 ──────────────────────────────────────────

class TestCancellation:

    def test_is_an_exception_so_it_cannot_be_ignored(self):
        assert issubclass(RuneCancelled, Exception)

    def test_propagates_out_of_a_rune_body(self):
        def body():
            raise RuneCancelled()

        with pytest.raises(RuneCancelled):
            body()


# ── 결과 ───────────────────────────────────────────────────────

class TestRuneResult:

    def test_minimal_success(self):
        result = RuneResult(ok=True)
        assert result.artifacts == []
        assert result.failures == []

    def test_carries_failures(self):
        result = RuneResult(
            ok=False,
            failures=[Failure(kind="code_not_found", message="코드를 찾지 못했습니다")],
        )
        assert result.failures[0].kind == "code_not_found"

    def test_lists_are_per_instance(self):
        a, b = RuneResult(ok=True), RuneResult(ok=True)
        a.artifacts.append(Path("out.csv"))
        assert b.artifacts == []


# ── 매니페스트 ─────────────────────────────────────────────────

class TestRuneManifest:

    def test_minimal_manifest(self):
        m = RuneManifest(id="ins_singo", name="보험 신고 CSV")
        assert m.version == "1.0.0"
        assert m.target_apps == []

    def test_permissions_field_is_reserved_but_unenforced(self):
        """필드만 예약한다. 외부 룬을 받기 시작할 때 강제를 켠다."""
        m = RuneManifest(id="r", name="n", permissions=["ui.automation"])
        assert m.permissions == ["ui.automation"]

    def test_required_fields_drive_setup_progress(self):
        """비어 있으면 마법사가 'N / M' 대신 'N개 지목됨'을 표시한다."""
        assert RuneManifest(id="r", name="n").required_fields == []


# ── 프로토콜 준수 ──────────────────────────────────────────────

class _FakeContext:
    """계약을 만족하는 최소 구현. 실제 코어 구현은 Phase 1에서 만든다."""

    def __init__(self, workdir: Path):
        self._workdir = workdir
        self.logs: list[tuple[str, str]] = []
        self.progress: list[tuple[int, int | None, str]] = []
        self.asked: list[UserActionRequest] = []
        self._checkpoint: dict | None = None
        self.cancelled = False

    @property
    def workdir(self) -> Path:
        return self._workdir

    @property
    def params(self) -> dict:
        return {}

    @property
    def dry_run(self) -> bool:
        return False

    @property
    def datadir(self) -> Path:
        # 가짜에서는 workdir 과 같은 곳을 쓴다. 실제 코어는 룬 단위로 나눈다.
        return self._workdir

    def http_get(self, url, *, headers=None):
        return None

    def http_post(self, url, *, data=None, json=None, headers=None):
        return None

    def replay(self, name):
        return False

    def infer(self, prompt, schema):
        return None

    def read_table(self, source, sheet=None):
        return []

    def write_table(self, name, rows):
        return str(self._workdir / name)

    def log(self, level, message, **fields):
        self.logs.append((level, message))

    def report_progress(self, current, total, message=""):
        self.progress.append((current, total, message))

    def request_user_action(self, request):
        self.asked.append(request)
        return UserActionResponse(action="ok")

    def load_checkpoint(self):
        return self._checkpoint

    def save_checkpoint(self, data):
        self._checkpoint = data

    def clear_checkpoint(self):
        self._checkpoint = None

    def check_cancelled(self):
        if self.cancelled:
            raise RuneCancelled()

    def ocr(self, path, prompt, schema):
        return []


class _ProbeRune:
    """모든 ctx 메서드를 호출하는 합성 룬.

    ins_singo에 인위적 기능을 넣지 않고도 실행 인프라를 검증한다.
    배포에는 포함하지 않는다.
    """

    manifest = RuneManifest(id="_probe", name="계약 검증용")

    def run(self, ctx, inputs):
        ctx.log("info", "probe start")
        ctx.report_progress(0, 2, "1단계")
        answer = ctx.request_user_action(
            UserActionRequest(kind="confirm", prompt="계속할까요?")
        )
        ctx.save_checkpoint({"step": 1, "answer": answer.action})
        assert ctx.load_checkpoint()["step"] == 1
        ctx.report_progress(1, 2, "2단계")
        ctx.check_cancelled()
        ctx.clear_checkpoint()
        return RuneResult(ok=True, summary={"answer": answer.action})


class TestProtocolConformance:

    def test_fake_context_satisfies_the_protocol(self, tmp_path):
        assert isinstance(_FakeContext(tmp_path), RuneContext)

    def test_probe_rune_satisfies_the_protocol(self):
        assert isinstance(_ProbeRune(), Rune)

    def test_probe_exercises_every_context_method(self, tmp_path):
        ctx = _FakeContext(tmp_path)
        result = _ProbeRune().run(ctx, [])

        assert result.ok
        assert ctx.logs == [("info", "probe start")]
        assert ctx.progress == [(0, 2, "1단계"), (1, 2, "2단계")]
        assert ctx.asked[0].kind == "confirm"
        assert ctx.load_checkpoint() is None      # clear_checkpoint까지 돌았다

    def test_cancel_during_run_propagates(self, tmp_path):
        ctx = _FakeContext(tmp_path)
        ctx.cancelled = True
        with pytest.raises(RuneCancelled):
            _ProbeRune().run(ctx, [])

    def test_checkpoint_survives_a_cancelled_run(self, tmp_path):
        """취소돼도 체크포인트는 남아야 이어하기가 가능하다."""
        ctx = _FakeContext(tmp_path)
        ctx.cancelled = True
        with pytest.raises(RuneCancelled):
            _ProbeRune().run(ctx, [])
        assert ctx.load_checkpoint() == {"step": 1, "answer": "ok"}
