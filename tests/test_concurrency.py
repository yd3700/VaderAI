"""두 룬이 같은 프로세스에서 독립적으로 동시에 실행되는지 검증한다.

순서를 ``sleep``으로 추측하지 않는다. 실행이 겹쳐야 하는 지점은 Barrier와
Event로 직접 맞추고, 모든 대기에는 상한을 둔다.
"""
from __future__ import annotations
from tests.private import needs

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from vader.core.checkpoint import CheckpointStore
from vader.core.events import Finished, LogLine, UserActionRequested
from vader.core.job import JobState
from vader.core.ocr import FakeOcrProvider, OcrGateway
from vader.core.runner import JobRunner
from vader.sdk import (
    CheckpointKey,
    Input,
    RuneManifest,
    RuneResult,
    UserActionRequest,
    UserActionResponse,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from eonm_harness import FROZEN_NOW, RecordingDriver  # noqa: E402
except ImportError:   # 공개 저장소 — 이온엠 시험 도구는 비공개
    FROZEN_NOW = RecordingDriver = None


TIMEOUT = 5.0
ROOT = Path(__file__).resolve().parent.parent
RUNE_SOURCES = ROOT / "rune-packages"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("동시 실행 테스트는 네트워크를 쓰면 안 됩니다")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def rune_from(fn, rune_id: str):
    class _Rune:
        manifest = RuneManifest(id=rune_id, name=f"동시성 탐침 {rune_id}")

        def run(self, ctx, inputs):
            return fn(ctx, inputs)

    return _Rune()


def wait_until(predicate, timeout: float = TIMEOUT) -> bool:
    """상태 관측용 bounded wait. 실행 순서를 이 함수로 만들지는 않는다."""

    deadline = time.monotonic() + timeout
    changed = threading.Event()
    while time.monotonic() < deadline:
        if predicate():
            return True
        changed.wait(0.005)
    return bool(predicate())


def test_jobs_overlap_and_isolate_workdir_datadir_params_events_and_checkpoints(
    tmp_path,
):
    """두 Job이 배리어에서 실제로 만나고 여섯 격리 축을 한 번에 관측한다."""

    store = CheckpointStore(tmp_path / "checkpoints")
    runner = JobRunner(checkpoints=store, data_dir=tmp_path / "data")
    barrier = threading.Barrier(2, timeout=TIMEOUT)
    seen: dict[str, dict] = {}

    input_a = tmp_path / "a.pdf"
    input_b = tmp_path / "b.pdf"
    input_a.write_bytes(b"A")
    input_b.write_bytes(b"B")

    def make_run(rune_id: str, other_id: str):
        def run(ctx, inputs):
            marker = ctx.workdir / f"{rune_id}.txt"
            marker.write_text(rune_id, encoding="utf-8")
            ctx.save_checkpoint({"owner": rune_id})
            seen[rune_id] = {
                "workdir": ctx.workdir,
                "datadir": ctx.datadir,
                "params": dict(ctx.params),
                "checkpoint": ctx.load_checkpoint(),
            }
            ctx.log("info", f"{rune_id}-at-barrier")
            barrier.wait()
            seen[rune_id]["foreign_visible"] = (
                ctx.workdir / f"{other_id}.txt"
            ).exists()
            return RuneResult(ok=True, summary={"owner": rune_id})

        return run

    first = runner.submit(
        rune_from(make_run("alpha", "beta"), "alpha"),
        [Input(path=input_a)],
        params={"owner": "alpha"},
    )
    second = runner.submit(
        rune_from(make_run("beta", "alpha"), "beta"),
        [Input(path=input_b)],
        params={"owner": "beta"},
    )

    assert runner.join(first.id, TIMEOUT)
    assert runner.join(second.id, TIMEOUT)
    assert first.state is second.state is JobState.SUCCEEDED

    assert seen["alpha"]["workdir"] != seen["beta"]["workdir"]
    assert not seen["alpha"]["foreign_visible"]
    assert not seen["beta"]["foreign_visible"]
    assert seen["alpha"]["datadir"] != seen["beta"]["datadir"]
    assert seen["alpha"]["params"] == {"owner": "alpha"}
    assert seen["beta"]["params"] == {"owner": "beta"}
    assert seen["alpha"]["checkpoint"] == {"owner": "alpha"}
    assert seen["beta"]["checkpoint"] == {"owner": "beta"}

    events = runner.drain_events()
    assert {event.job_id for event in events} == {first.id, second.id}
    for job in (first, second):
        assert any(
            isinstance(event, LogLine) and event.job_id == job.id
            for event in events
        )
        assert any(
            isinstance(event, Finished) and event.job_id == job.id
            for event in events
        )


def test_config_write_cannot_be_lost_behind_an_inflight_cache_fill(
    tmp_path, monkeypatch
):
    """load 중 쓰기를 직렬화해 새 값을 옛 캐시가 다시 가리지 못하게 한다."""

    from vader.core import config

    settings = tmp_path / "settings.json"
    old = {"runes": {"alpha": {"value": "old"}}}
    new = {"runes": {"alpha": {"value": "new"}}}
    settings.write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", settings)
    monkeypatch.setattr(config, "_cache", None)

    read_started = threading.Event()
    release_read = threading.Event()
    write_started = threading.Event()
    write_done = threading.Event()
    original_read_text = Path.read_text

    def delayed_read(path, *args, **kwargs):
        text = original_read_text(path, *args, **kwargs)
        if path == settings and not read_started.is_set():
            read_started.set()
            assert release_read.wait(TIMEOUT)
        return text

    monkeypatch.setattr(Path, "read_text", delayed_read)
    loaded = []

    reader = threading.Thread(target=lambda: loaded.append(config.load()))

    def write():
        write_started.set()
        try:
            config._write(new)
        finally:
            write_done.set()

    writer = threading.Thread(target=write)
    reader.start()
    assert read_started.wait(TIMEOUT)
    writer.start()
    assert write_started.wait(TIMEOUT)
    assert write_done.wait(0.3) is False, "load 진행 중에 write가 완료됐다"
    release_read.set()
    reader.join(TIMEOUT)
    writer.join(TIMEOUT)

    assert not reader.is_alive() and not writer.is_alive()
    assert loaded == [old]
    assert config.load() == new


def test_rune_config_returns_independent_nested_params(tmp_path, monkeypatch):
    """한 룬의 ctx.params 변경이 캐시나 다음 Job의 params를 오염시키지 않는다."""

    from vader.core import config

    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"runes": {"alpha": {"nested": {"value": 1}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SETTINGS_PATH", settings)
    monkeypatch.setattr(config, "_cache", None)

    first = config.rune_config("alpha")
    second = config.rune_config("alpha")
    first["nested"]["value"] = 999

    assert second == {"nested": {"value": 1}}
    assert config.rune_config("alpha") == {"nested": {"value": 1}}


def test_runner_reads_each_runes_own_config_concurrently(tmp_path, monkeypatch):
    from vader.core import config

    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "runes": {
                    "alpha": {"owner": "alpha", "nested": {"value": 1}},
                    "beta": {"owner": "beta", "nested": {"value": 2}},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SETTINGS_PATH", settings)
    monkeypatch.setattr(config, "_cache", None)

    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    barrier = threading.Barrier(2, timeout=TIMEOUT)
    seen = {}

    def make_run(rune_id):
        def run(ctx, inputs):
            seen[rune_id] = json.loads(json.dumps(ctx.params))
            barrier.wait()
            if rune_id == "alpha":
                ctx.params["nested"]["value"] = 999
            barrier.wait()
            seen[f"{rune_id}-after"] = json.loads(json.dumps(ctx.params))
            return RuneResult(ok=True)

        return run

    first = runner.submit(rune_from(make_run("alpha"), "alpha"), [])
    second = runner.submit(rune_from(make_run("beta"), "beta"), [])
    assert runner.join(first.id, TIMEOUT)
    assert runner.join(second.id, TIMEOUT)
    assert seen["alpha"] == {"owner": "alpha", "nested": {"value": 1}}
    assert seen["beta"] == {"owner": "beta", "nested": {"value": 2}}
    assert seen["alpha-after"]["nested"]["value"] == 999
    assert seen["beta-after"]["nested"]["value"] == 2
    assert config.rune_config("alpha")["nested"]["value"] == 1


def test_same_rune_jobs_share_datadir_but_not_workdir(tmp_path):
    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    barrier = threading.Barrier(2, timeout=TIMEOUT)
    seen = []
    lock = threading.Lock()

    def run(ctx, inputs):
        with lock:
            seen.append((ctx.workdir, ctx.datadir))
        barrier.wait()
        return RuneResult(ok=True)

    rune = rune_from(run, "shared-rune")
    # 입력을 갈라 둔다. 같은 룬에 **같은 입력**은 이제 동시에 못 돈다 —
    # 둘 다 체크포인트를 비어 있는 것으로 읽고 같은 일을 두 번 한다.
    first_input = tmp_path / "first.txt"
    first_input.write_text("첫째", encoding="utf-8")
    second_input = tmp_path / "second.txt"
    second_input.write_text("둘째", encoding="utf-8")
    first = runner.submit(rune, [Input(path=first_input)])
    second = runner.submit(rune, [Input(path=second_input)])

    assert runner.join(first.id, TIMEOUT)
    assert runner.join(second.id, TIMEOUT)
    assert seen[0][0] != seen[1][0]
    assert seen[0][1] == seen[1][1]


def test_cancelling_one_job_does_not_stop_the_other(tmp_path):
    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    both_started = threading.Barrier(2, timeout=TIMEOUT)
    release = threading.Event()
    survivor_continued = threading.Event()

    def cancellable(ctx, inputs):
        both_started.wait()
        assert release.wait(TIMEOUT)
        ctx.check_cancelled()
        return RuneResult(ok=True)

    def survivor(ctx, inputs):
        both_started.wait()
        assert release.wait(TIMEOUT)
        survivor_continued.set()
        return RuneResult(ok=True)

    cancelled = runner.submit(rune_from(cancellable, "cancel-me"), [])
    survived = runner.submit(rune_from(survivor, "survivor"), [])
    assert wait_until(
        lambda: runner.get(cancelled.id).state is JobState.RUNNING
        and runner.get(survived.id).state is JobState.RUNNING
    )

    assert runner.cancel(cancelled.id)
    release.set()

    assert runner.join(cancelled.id, TIMEOUT)
    assert runner.join(survived.id, TIMEOUT)
    assert cancelled.state is JobState.CANCELLED
    assert survived.state is JobState.SUCCEEDED
    assert survivor_continued.is_set()


def test_one_rune_exception_does_not_fail_the_other(tmp_path):
    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    barrier = threading.Barrier(2, timeout=TIMEOUT)

    def boom(ctx, inputs):
        barrier.wait()
        raise RuntimeError("isolated boom")

    def okay(ctx, inputs):
        barrier.wait()
        return RuneResult(ok=True)

    failed = runner.submit(rune_from(boom, "boom"), [])
    succeeded = runner.submit(rune_from(okay, "okay"), [])
    assert runner.join(failed.id, TIMEOUT)
    assert runner.join(succeeded.id, TIMEOUT)

    assert failed.state is JobState.FAILED
    assert failed.failure.kind == "RuntimeError"
    assert succeeded.state is JobState.SUCCEEDED


def test_simultaneous_user_actions_are_correlated_to_their_jobs(tmp_path):
    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    barrier = threading.Barrier(2, timeout=TIMEOUT)

    def ask(ctx, inputs):
        barrier.wait()
        answer = ctx.request_user_action(
            UserActionRequest(
                kind="confirm",
                prompt=f"{ctx.params['owner']} 질문",
            )
        )
        return RuneResult(ok=True, summary={"answer": answer.action})

    first = runner.submit(
        rune_from(ask, "ask-a"),
        [],
        params={"owner": "a"},
    )
    second = runner.submit(
        rune_from(ask, "ask-b"),
        [],
        params={"owner": "b"},
    )
    assert wait_until(
        lambda: first.state is JobState.WAITING_USER
        and second.state is JobState.WAITING_USER
    )

    events = runner.drain_events()
    requests = {
        event.job_id: event
        for event in events
        if isinstance(event, UserActionRequested)
    }
    assert set(requests) == {first.id, second.id}
    assert requests[first.id].correlation_id != requests[second.id].correlation_id

    # correlation_id만 맞아도 안 된다. Job 소유까지 맞아야 전달된다.
    assert not runner.respond(
        first.id,
        requests[second.id].correlation_id,
        UserActionResponse(action="wrong"),
    )
    assert runner.respond(
        first.id,
        requests[first.id].correlation_id,
        UserActionResponse(action="answer-a"),
    )
    assert runner.respond(
        second.id,
        requests[second.id].correlation_id,
        UserActionResponse(action="answer-b"),
    )

    assert runner.join(first.id, TIMEOUT)
    assert runner.join(second.id, TIMEOUT)
    assert first.result.summary["answer"] == "answer-a"
    assert second.result.summary["answer"] == "answer-b"


def test_checkpoint_same_target_concurrent_replace_never_corrupts_json(
    tmp_path,
):
    """동일 키를 반복 동시 저장해도 예외나 부분 문서가 없어야 한다."""

    store = CheckpointStore(tmp_path / "checkpoints")
    key = CheckpointKey("same-rune", "same-input")
    rounds = 40
    save_barrier = threading.Barrier(2, timeout=TIMEOUT)
    failures = []

    def save(owner):
        for iteration in range(rounds):
            try:
                save_barrier.wait()
                store.save(
                    key,
                    {"owner": owner, "iteration": iteration},
                    rune_version="1.0.0",
                )
            except BaseException as exc:  # pragma: no cover - assertion aid
                failures.append(exc)

    first = threading.Thread(target=save, args=("a",))
    second = threading.Thread(target=save, args=("b",))
    first.start()
    second.start()
    first.join(TIMEOUT)
    second.join(TIMEOUT)

    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    final = store.load(key, rune_version="1.0.0")
    assert final["owner"] in ("a", "b")
    assert final["iteration"] == rounds - 1
    documents = list(store.root.glob("*.json"))
    assert len(documents) == 1
    json.loads(documents[0].read_text(encoding="utf-8"))
    assert list(store.root.glob("*.tmp")) == []


def test_ocr_same_cache_key_concurrent_writes_are_atomic(tmp_path):
    """두 miss가 같은 캐시에 써도 완성된 JSON 하나만 남는다."""

    barrier = threading.Barrier(2, timeout=TIMEOUT)

    class BarrierProvider(FakeOcrProvider):
        def extract(self, path, prompt, schema):
            barrier.wait()
            return super().extract(path, prompt, schema)

    provider = BarrierProvider([{"rows": 1}, {"rows": 1}])
    cache_dir = tmp_path / "ocr_cache"
    logs = []
    gateway = OcrGateway(
        provider,
        cache_dir=cache_dir,
        cache_days=30,
        request_delay_seconds=0,
        retry_max=1,
        daily_limit=0,
        log=lambda level, message, **fields: logs.append(
            (level, message, fields)
        ),
    )
    source = tmp_path / "same.pdf"
    source.write_bytes(b"same content")
    results = []
    failures = []

    def extract():
        try:
            results.append(gateway.extract(source, "same prompt", dict))
        except BaseException as exc:  # pragma: no cover - assertion aid
            failures.append(exc)

    first = threading.Thread(target=extract)
    second = threading.Thread(target=extract)
    first.start()
    second.start()
    first.join(TIMEOUT)
    second.join(TIMEOUT)

    assert not first.is_alive() and not second.is_alive()
    assert failures == []
    assert results == [{"rows": 1}, {"rows": 1}]
    assert len(provider.calls) == 2
    documents = list(cache_dir.glob("*.json"))
    assert len(documents) == 1
    assert json.loads(documents[0].read_text(encoding="utf-8")) == {"rows": 1}
    assert list(cache_dir.glob("*.tmp")) == []
    assert all(
        level == "warning" and message == "OCR 캐시 저장 실패"
        for level, message, _fields in logs
    )


@needs("rune-packages/eonm_ipgo")
def test_installed_ins_singo_and_eonm_run_concurrently(
    tmp_path, monkeypatch
):
    """실제 설치·발견된 두 룬이 같은 러너와 OCR 캐시에서 겹쳐 돈다."""

    pytest.importorskip(
        "pywinauto",
        reason="eonm_ipgo 는 pywinauto 선택 의존성이 필요하다. pip install .[eonm]",
    )

    from vader.core.installer import install
    from vader.core.loader import discover

    rune_dir = tmp_path / "installed-runes"
    install(RUNE_SOURCES / "ins_singo", rune_dir)
    install(RUNE_SOURCES / "eonm_ipgo", rune_dir)
    loaded, failures = discover(rune_dir)
    assert failures == []
    runes = {entry.manifest.id: entry.rune for entry in loaded}
    assert set(runes) == {"ins_singo", "eonm_ipgo"}

    # 설치된 EONM 인스턴스가 공개한 주입 지점을 사용한다.
    eonm = runes["eonm_ipgo"]
    driver = RecordingDriver([])
    eonm._driver_factory = lambda _ctx: driver

    def installed_fake_process(data, store, wiki, *, log=None):
        return True, "none"

    eonm._process = installed_fake_process
    eonm._now = lambda: FROZEN_NOW

    row = {
        "page": 1,
        "date": "2026-07-30",
        "code": "M3011628",
        "item_name": "동시성 품목",
        "spec": "1CC",
        "quantity": 1,
        "unit_price": 100,
        "total_price": 100,
        "vendor": "테스트 거래처",
        "patient_info": "공용소모품",
    }
    provider_barrier = threading.Barrier(2, timeout=TIMEOUT)

    class BarrierProvider(FakeOcrProvider):
        def extract(self, path, prompt, schema):
            provider_barrier.wait()
            return super().extract(path, prompt, schema)

    provider = BarrierProvider([[dict(row)], [dict(row)]])
    gateway = OcrGateway(
        provider,
        cache_dir=tmp_path / "ocr_cache",
        cache_days=30,
        request_delay_seconds=0,
        retry_max=1,
        daily_limit=0,
    )
    monkeypatch.setattr(
        "vader.core.ocr.extract",
        lambda path, prompt, schema, *, log, assignment=None: gateway.extract(
            path, prompt, schema
        ),
    )

    ins_input = tmp_path / "ins.pdf"
    eonm_input = tmp_path / "eonm.pdf"
    ins_input.write_bytes(b"ins")
    eonm_input.write_bytes(b"eonm")
    master = tmp_path / "master.csv"
    master.write_text(
        "Q021_MATCODE,Q021_MATNAME,Q021_TYPE,Q021_SUGACD,Q021_INSUCLS\n",
        encoding="utf-8",
    )
    done_dir = tmp_path / "done"
    error_dir = tmp_path / "error"
    done_dir.mkdir()
    error_dir.mkdir()

    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    ins_job = runner.submit(
        runes["ins_singo"],
        [Input(path=ins_input)],
        params={
            "구입처": "테스트 거래처",
            "사업자등록번호": "0000000000",
            "2년 연장": 0,
            "선납": 1,
            "양도양수여부": "N",
        },
    )
    eonm_job = runner.submit(
        eonm,
        [Input(path=eonm_input)],
        params={
            "window_title": "입고/반납등록",  # manifest.target_apps 와 맞아야 submit 이 받는다
            "master_db": str(master),
            "done_dir": str(done_dir),
            "error_dir": str(error_dir),
            "target_start": 1,
        },
    )

    # 이온엠은 시작할 때 입고 종류를 묻는다 (2026-11-04) — 화면처럼 답한다
    deadline = time.monotonic() + TIMEOUT
    answered = False
    while not answered and time.monotonic() < deadline:
        for event in runner.drain_events():
            request = getattr(event, "request", None)
            if request is not None and request.context.get("kind") == "입고 종류":
                answered = runner.respond(eonm_job.id, event.correlation_id,
                                          UserActionResponse(action="ok", value="의료소모품"))
        time.sleep(0.02)
    assert answered
    assert runner.join(ins_job.id, TIMEOUT)
    assert runner.join(eonm_job.id, TIMEOUT)
    assert ins_job.state is JobState.SUCCEEDED
    assert eonm_job.state is JobState.SUCCEEDED
    assert ins_job.workdir != eonm_job.workdir
    assert ins_job.result.artifacts[0].parent == ins_job.workdir
    assert eonm_job.result.artifacts[0].parent == eonm_job.workdir
    assert len(provider.calls) == 2
    assert any(call["method"] == "execute_invoice_workflow" for call in driver.calls)


# --- 같은 입력을 두 번 제출하는 것 --------------------------------------------
#
# 체크포인트 락은 JSON 이 깨지는 것만 막는다. 같은 파일을 두 번 제출하면 둘 다
# progress 를 비어 있는 것으로 읽고 같은 버킷을 둘 다 넣는다 — ERP 에는 두 번
# 들어가고 체크포인트에는 한 번으로 남는다. #12·#14 와 같은 계열이다.


def test_the_same_input_cannot_run_twice_at_once(tmp_path):
    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    started = threading.Event()
    finish = threading.Event()

    def run(ctx, inputs):
        started.set()
        assert finish.wait(TIMEOUT)
        return RuneResult(ok=True)

    rune = rune_from(run, "erp")
    document = tmp_path / "입고.pdf"
    document.write_text("같은 문서", encoding="utf-8")

    first = runner.submit(rune, [Input(path=document)])
    assert started.wait(TIMEOUT)

    with pytest.raises(RuntimeError, match="이미 실행 중"):
        runner.submit(rune, [Input(path=document)])

    finish.set()
    assert runner.join(first.id, TIMEOUT)


def test_a_renamed_copy_is_the_same_input(tmp_path):
    """키는 파일 **내용**으로 만든다. 이름을 바꿔도 같은 문서다."""
    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    started = threading.Event()
    finish = threading.Event()

    def run(ctx, inputs):
        started.set()
        assert finish.wait(TIMEOUT)
        return RuneResult(ok=True)

    rune = rune_from(run, "erp")
    original = tmp_path / "입고.pdf"
    original.write_text("같은 문서", encoding="utf-8")
    renamed = tmp_path / "입고 (사본).pdf"
    renamed.write_text("같은 문서", encoding="utf-8")

    first = runner.submit(rune, [Input(path=original)])
    assert started.wait(TIMEOUT)

    with pytest.raises(RuntimeError, match="이미 실행 중"):
        runner.submit(rune, [Input(path=renamed)])

    finish.set()
    assert runner.join(first.id, TIMEOUT)


def test_the_door_opens_again_when_the_first_one_ends(tmp_path):
    """막는 것이 목적이 아니라 **겹치는 것**이 목적이다.

    끝난 뒤에도 막으면 재개 동선이 죽는다 — 실패한 파일을 다시 돌리는 것이
    이 룬의 정상 사용법이다.
    """
    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )

    def run(ctx, inputs):
        return RuneResult(ok=True)

    rune = rune_from(run, "erp")
    document = tmp_path / "입고.pdf"
    document.write_text("같은 문서", encoding="utf-8")

    first = runner.submit(rune, [Input(path=document)])
    assert runner.join(first.id, TIMEOUT)

    second = runner.submit(rune, [Input(path=document)])
    assert runner.join(second.id, TIMEOUT)


def test_a_blocked_submit_leaves_no_orphan_workdir(tmp_path):
    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    started = threading.Event()
    finish = threading.Event()

    def run(ctx, inputs):
        started.set()
        assert finish.wait(TIMEOUT)
        return RuneResult(ok=True)

    rune = rune_from(run, "erp")
    document = tmp_path / "입고.pdf"
    document.write_text("같은 문서", encoding="utf-8")

    first = runner.submit(rune, [Input(path=document)])
    assert started.wait(TIMEOUT)
    before = sorted(p.name for p in (tmp_path / "data" / "jobs").iterdir())

    with pytest.raises(RuntimeError):
        runner.submit(rune, [Input(path=document)])

    assert sorted(p.name for p in (tmp_path / "data" / "jobs").iterdir()) == before
    finish.set()
    assert runner.join(first.id, TIMEOUT)


def test_different_runes_on_the_same_file_still_run_together(tmp_path):
    """키에는 룬 id 가 들어간다. 같은 문서를 다른 룬이 보는 것은 정상이다."""
    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    both = threading.Barrier(2, timeout=TIMEOUT)

    def run(ctx, inputs):
        both.wait()
        return RuneResult(ok=True)

    document = tmp_path / "문서.pdf"
    document.write_text("같은 문서", encoding="utf-8")

    first = runner.submit(rune_from(run, "ins_singo"), [Input(path=document)])
    second = runner.submit(rune_from(run, "eonm_ipgo"), [Input(path=document)])

    assert runner.join(first.id, TIMEOUT)
    assert runner.join(second.id, TIMEOUT)


def test_two_different_searches_can_run_at_once(tmp_path):
    """입력이 없는 룬은 설정이 곧 대상이다.

    빈 입력끼리 신원이 같으면 검색어를 바꿔도 같은 체크포인트를 쓰고, 동시
    제출 차단이 **서로 다른 검색어끼리도 막는다.**
    """
    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    both = threading.Barrier(2, timeout=TIMEOUT)

    def run(ctx, inputs):
        both.wait()
        return RuneResult(ok=True)

    rune = rune_from(run, "web_brief")
    first = runner.submit(rune, [], params={"keywords": "의료기기"})
    second = runner.submit(rune, [], params={"keywords": "병원 구매"})

    assert runner.join(first.id, TIMEOUT)
    assert runner.join(second.id, TIMEOUT)


def test_the_same_search_still_cannot_run_twice(tmp_path):
    """설정까지 같으면 같은 대상이다. 같은 기사를 두 번 보낼 수 있다."""
    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    started = threading.Event()
    finish = threading.Event()

    def run(ctx, inputs):
        started.set()
        assert finish.wait(TIMEOUT)
        return RuneResult(ok=True)

    rune = rune_from(run, "web_brief")
    first = runner.submit(rune, [], params={"keywords": "의료기기"})
    assert started.wait(TIMEOUT)

    with pytest.raises(RuntimeError, match="이미 실행 중"):
        runner.submit(rune, [], params={"keywords": "의료기기"})

    finish.set()
    assert runner.join(first.id, TIMEOUT)


def test_config_does_not_unlock_a_duplicate_document(tmp_path):
    """입력이 있으면 설정은 신원에 안 들어간다.

    설정 한 글자로 같은 문서를 두 번 통과시키면 중복 입고가 돌아온다.
    """
    runner = JobRunner(
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        data_dir=tmp_path / "data",
    )
    started = threading.Event()
    finish = threading.Event()

    def run(ctx, inputs):
        started.set()
        assert finish.wait(TIMEOUT)
        return RuneResult(ok=True)

    rune = rune_from(run, "eonm_ipgo")
    document = tmp_path / "입고.pdf"
    document.write_text("같은 문서", encoding="utf-8")

    first = runner.submit(rune, [Input(path=document)], params={"target_start": 1})
    assert started.wait(TIMEOUT)

    with pytest.raises(RuntimeError, match="이미 실행 중"):
        runner.submit(rune, [Input(path=document)], params={"target_start": 99})

    finish.set()
    assert runner.join(first.id, TIMEOUT)
