"""
JobRunner — 룬을 워커 스레드에서 실행하고 상태를 관리한다.

스레드 규칙:

  UI 스레드                          워커 스레드
  ---------                          -----------
  submit()  ─ 스레드 시작 ─────────►  rune.run(ctx, inputs)
  drain_events()  ◄── Queue ────────  ctx.log / report_progress / request_user_action
  respond(correlation_id, ...)  ───►  블록 해제
  cancel(job_id)  ─ Event.set ─────►  다음 check_cancelled()에서 RuneCancelled

취소는 협조적이다. 강제 중단(thread kill)은 지원하지 않는다 — 파이썬에는
안전한 방법이 없고, 외부 시스템에 절반만 입력된 상태로 끊는 것이 더 나쁘다.
사용자에게는 "현재 항목 완료 후 중단합니다"로 보여야 한다.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from vader.core import config, journal, seal
from vader.core.context import CheckpointStore, JobContext
from vader.core.events import Finished, JobEvent, LogLine, StateChanged
from vader.core.job import Job, JobState
from vader.core.safety import check_target_app
from vader.sdk import (
    CheckpointKey,
    Failure,
    Input,
    Rune,
    RuneCancelled,
    RuneResult,
    UserActionResponse,
)


def _reject_foreign_target(manifest, params: dict[str, Any]) -> None:
    """``kind="window"`` 설정값이 룬의 ``target_apps`` 안인지 본다.

    룬마다 부르게 하면 안 부르는 룬이 생긴다. 모든 실행이 지나는 자리에 한 번
    둔다 — 룬은 ``vader.sdk`` 만 import 하면 되고 이 검사를 몰라도 된다.

    이걸 안 걸면 창 제목 설정 하나로 아무 창에나 키 입력이 들어간다.
    ``check_target_app`` 은 Phase 4부터 있었지만 부르는 곳이 테스트뿐이었다.
    """
    for field in manifest.required_fields:
        if field.kind != "window":
            continue
        title = str(params.get(field.name, "") or "")
        if not check_target_app(manifest, title):
            raise RuntimeError(
                f"'{title}' 은(는) {manifest.name} 의 대상 앱이 아닙니다. "
                f"허용: {', '.join(manifest.target_apps)}"
            )


def _reject_unsealed(rune_id: str, dry_run: bool) -> None:
    """봉인 안 된 룬은 **연습 실행만.** Phase 12 의 첫 자물쇠. 실행 직전마다 디스크의 지문을 다시 본다.

    약속이 "누구나 룬을 만든다" 에서 **"누구나 말로 요청하고, 봉인된 것만
    운영에 들어간다"** 로 바뀌었다. 말로 만든 초안이 곧바로 병원 ERP 에
    입고를 넣으면 그 약속은 이름만 남는다.

    **화면이 아니라 여기서 막는다.** 버튼만 잠그면 버튼을 안 거치는 길이
    남고, Phase 13 의 원격 명령이 정확히 그 길이다.

    ## 이 자물쇠가 못 막는 것

    `Rune` 객체를 코드로 직접 만들어 넘기면 안 걸린다. 설치 폴더가 없으니
    봉인할 대상도 없다. **배포되는 앱에는 그런 길이 없다** — 룬은 설치를
    거쳐야 목록에 뜨고, 목록에 없으면 실행할 수도 없다. 테스트가 쓰는
    길이고, 그래서 여기 적어 둔다. `permission-model.md` 와 같은 태도다 —
    **막는 것은 실수지 공격자가 아니다.**
    """
    # 목록에서 받은 룬: 회수·만료·설치 미완료면 연습·실전·재개를 막는다 (신뢰 계약 v2 5절)
    from vader.core import catalog

    allowed = catalog.verdict(rune_id)
    if not allowed["practice" if dry_run else "live"]:
        raise RuntimeError(allowed["reason"] or "목록 정보로 이 실행이 막혔습니다.")
    source = config.rune_dir() / rune_id
    if not source.is_dir():
        # 설치된 룬이 아니다. 봉인할 소스가 없으니 볼 것도 없다.
        return
    # 연습도 막는다 — 사람이 확인한 지문이 아니면 이 PC 에서 돌지 않는다 (2026-10-14 합의).
    if not seal.loadable(rune_id, source):
        raise RuntimeError("설치한 뒤 룬 파일이 바뀌었습니다. 룬 설치로 다시 설치해야 실행할 수 있습니다.")
    if dry_run or seal.sealed(rune_id, source):
        return
    raise RuntimeError(
        f"{seal.why(rune_id, source)} "
        "`연습 실행 (dry-run)` 을 켜면 지금 바로 돌려볼 수 있습니다."
    )


class _Running:
    """실행 중인 Job 하나에 딸린 것들."""

    def __init__(
        self,
        job: Job,
        ctx: JobContext,
        cancel: threading.Event,
        key: CheckpointKey,
    ) -> None:
        self.job = job
        self.ctx = ctx
        self.cancel = cancel
        #: 같은 입력이 두 번 도는 것을 막는 데 쓴다. `ctx` 안에도 있지만
        #: 그쪽은 사적이다.
        self.key = key
        self.thread: threading.Thread | None = None


class JobRunner:
    def __init__(
        self,
        checkpoints: CheckpointStore,
        data_dir: Path | None = None,
    ) -> None:
        self._checkpoints = checkpoints
        self._root = data_dir if data_dir is not None else config.data_dir()
        self._events: Queue[JobEvent] = Queue()
        self._jobs: dict[str, _Running] = {}
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        #: 악성 회수가 실행 중인 룬에 닿았다 — 화면이 앱을 다시 시작하라고 알린다 (계약 8절). `{rune_id, message}`
        self.exit_required: dict | None = None
        from vader.core import catalog

        catalog.on_change(self._catalog_changed)

    def _catalog_changed(self) -> None:
        """목록 정보가 바뀌었다 — 실행 중인 작업을 같은 판정표로 다시 본다.

        막히면 취소를 건다. 취소는 협조적이라 룬의 다음 안전 지점(`check_cancelled`)에서 멈춘다.
        ponytail: 악성이면 앱 재시작을 **안내**까지 한다 — 이미 띄운 외부 프로세스를 끝냈다고 보지 않는다.
        스스로 종료하는 것은 데스크톱 쪽 종료 절차가 생기면 그때.
        """
        from vader.core import catalog

        with self._lock:
            live = [(job_id, r) for job_id, r in self._jobs.items() if not r.job.is_terminal]
        for job_id, running in live:
            allowed = catalog.verdict(running.job.rune_id)
            if not allowed["practice" if running.ctx.dry_run else "live"]:
                self.cancel(job_id)
            if allowed.get("level") == "malicious":
                self.exit_required = {"rune_id": running.job.rune_id,
                                      "message": f"{allowed['reason']} 중단을 요청했지만 끝났는지 확인하지 못했습니다. "
                                                 "앱을 닫았다가 다시 여세요. 이미 넣은 업무 결과는 확인이 필요합니다."}

    # ── UI 스레드에서 부르는 것 ────────────────────────────────

    def submit(
        self,
        rune: Rune,
        inputs: list[Input],
        params: dict[str, Any] | None = None,
        dry_run: bool = False,
    ) -> Job:
        """Job을 만들고 워커 스레드를 띄운다. 즉시 돌아온다."""
        if self._stopped.is_set():
            raise RuntimeError("비상정지 상태입니다. resume() 후 다시 실행하세요.")

        rune_id = rune.manifest.id
        params = params if params is not None else config.rune_config(rune_id)
        _reject_foreign_target(rune.manifest, params)
        _reject_unsealed(rune_id, dry_run)
        job_id = uuid.uuid4().hex
        job = Job(
            rune_id=rune_id,
            inputs=list(inputs),
            workdir=self._make_workdir(job_id),
            id=job_id,
        )

        key = CheckpointKey.from_inputs(rune_id, job.inputs, params)
        cancel = threading.Event()
        ctx = JobContext(
            job=job,
            events=self._events,
            cancel=cancel,
            params=params,
            checkpoints=self._checkpoints,
            checkpoint_key=key,
            rune_version=rune.manifest.version,
            dry_run=dry_run,
            datadir=self._make_datadir(rune_id),
        )
        running = _Running(job, ctx, cancel, key)

        with self._lock:
            # 첫 검사 뒤 비상정지가 들어오는 경합에서도 이 Job은 시작하지 않는다.
            if self._stopped.is_set():
                job.workdir.rmdir()
                raise RuntimeError("비상정지 상태입니다. resume() 후 다시 실행하세요.")
            twin = self._live_twin(key)
            if twin is not None:
                job.workdir.rmdir()
                raise RuntimeError(
                    "같은 룬에 같은 입력이 이미 실행 중입니다 "
                    f"(Job {twin.job.id[:8]}). 끝나거나 취소된 뒤에 다시 하세요."
                )
            self._jobs[job.id] = running

        if dry_run:
            ctx.log(
                "warning",
                "DRY-RUN 활성화: 룬은 외부 시스템의 쓰기 동작을 건너뛰어야 합니다.",
            )

        thread = threading.Thread(
            target=self._run, args=(running, rune), name=f"rune-{rune_id}-{job.id[:8]}", daemon=True
        )
        running.thread = thread
        thread.start()
        return job

    def _live_twin(self, key: CheckpointKey) -> _Running | None:
        """같은 입력으로 아직 도는 Job. **락 안에서만 부른다.**

        체크포인트 락은 JSON 이 깨지는 것만 막는다. 같은 파일을 두 번 제출하면
        둘 다 progress 를 비어 있는 것으로 읽고 **같은 버킷을 둘 다 넣는다** —
        ERP 에는 두 번 들어가고 체크포인트에는 한 번으로 남는다. #12·#14 와
        같은 계열이고, 이 검사가 그 문을 닫는다.

        룬과 입력이 같으면 같은 키다(`CheckpointKey.from_inputs` 가 파일
        **내용**으로 만든다). 이름을 바꿔 다시 넣어도 같은 것으로 본다.
        """
        return next(
            (
                running
                for running in self._jobs.values()
                if not running.job.is_terminal and running.key == key
            ),
            None,
        )

    def emergency_stop(self) -> int:
        """새 Job을 막고 살아 있는 모든 Job에 협조적 취소를 요청한다."""
        with self._lock:
            self._stopped.set()
            job_ids = [
                job_id
                for job_id, running in self._jobs.items()
                if not running.job.is_terminal
            ]
        return sum(self.cancel(job_id) for job_id in job_ids)

    def is_stopped(self) -> bool:
        return self._stopped.is_set()

    def resume(self) -> None:
        """비상정지 상태를 풀어 새 Job 제출을 다시 허용한다."""
        with self._lock:
            self._stopped.clear()

    def cancel(self, job_id: str) -> bool:
        """취소를 요청한다. 실제 중단은 룬의 다음 안전 지점에서 일어난다.

        이미 끝난 Job에는 아무 일도 하지 않는다.
        """
        running = self._get(job_id)
        if running is None or running.job.is_terminal:
            return False

        running.cancel.set()
        if running.job.try_transition(JobState.CANCEL_REQUESTED):
            self._emit_state(running.job)
        return True

    def respond(self, job_id: str, correlation_id: str, response: UserActionResponse) -> bool:
        """사용자 응답을 워커에 전달한다."""
        running = self._get(job_id)
        if running is None:
            return False
        # 기다리는 동안 회수·만료됐으면 계속하지 않는다 — 답 대신 중단을 보낸다 (codex1 2026-10-29 P2-4)
        from vader.core import catalog

        allowed = catalog.verdict(running.job.rune_id)
        if not allowed["practice" if running.ctx.dry_run else "live"]:
            self.cancel(job_id)
            response = UserActionResponse(action="abort")
        delivered = running.ctx.deliver(correlation_id, response)
        if delivered and running.job.try_transition(JobState.RUNNING):
            self._emit_state(running.job)
        return delivered

    def drain_events(self) -> list[JobEvent]:
        """쌓인 이벤트를 전부 가져간다. UI가 타이머에서 부른다."""
        out: list[JobEvent] = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except Empty:
                return out

    def join(self, job_id: str, timeout: float | None = None) -> bool:
        """워커가 끝날 때까지 기다린다. 테스트와 종료 처리용."""
        running = self._get(job_id)
        if running is None or running.thread is None:
            return True
        running.thread.join(timeout)
        return not running.thread.is_alive()

    def get(self, job_id: str) -> Job | None:
        running = self._get(job_id)
        return running.job if running else None

    @property
    def checkpoints(self) -> CheckpointStore:
        """체크포인트 저장소.

        UI 가 "이어서 실행됩니다" 를 보여주고 "처음부터" 를 제공하려면 저장소를
        직접 봐야 한다. 러너를 거치는 래퍼를 두는 것보다 저장소를 내주는 편이
        솔직하다 — 러너가 하는 일이 아니다.
        """
        return self._checkpoints

    # ── 워커 스레드 ────────────────────────────────────────────

    def _run(self, running: _Running, rune: Rune) -> None:
        job = running.job

        # 취소가 시작 전에 들어왔을 수 있다.
        if running.cancel.is_set():
            job.try_transition(JobState.CANCELLED)
            self._finish(job)
            return

        job.transition(JobState.RUNNING)
        self._emit_state(job)

        try:
            result = rune.run(running.ctx, job.inputs)
        except RuneCancelled:
            job.try_transition(JobState.CANCEL_REQUESTED)
            job.try_transition(JobState.CANCELLED)
            self._finish(job)
            return
        except Exception as exc:  # noqa: BLE001 — 룬의 어떤 예외도 Job을 넘어가면 안 된다
            job.failure = Failure(
                kind=type(exc).__name__,
                message=str(exc),
                detail={"rune_id": job.rune_id},
            )
            self._events.put(
                LogLine(job.id, "error", f"룬이 예외로 종료했습니다: {exc}", {"kind": type(exc).__name__})
            )
            job.try_transition(JobState.FAILED)
            self._finish(job)
            return

        # 룬이 결과를 돌려줬어도 그 사이 취소를 요청받았을 수 있다. 그때도
        # 결과는 남긴다 — 무엇이 끝났는지 잃지 않는다.
        job.result = result
        if not isinstance(result, RuneResult):
            job.failure = Failure(
                kind="ContractViolation",
                message=f"run()이 RuneResult가 아닌 {type(result).__name__}를 돌려줬습니다",
                detail={"rune_id": job.rune_id},
            )
            job.try_transition(JobState.FAILED)
        elif result.ok:
            job.try_transition(JobState.SUCCEEDED)
        else:
            # **룬이 담아 보낸 사유를 Job 에 옮긴다.** 예외로 죽을 때만 채우고
            # 있어서, 룬이 `RuneResult(ok=False, failures=[...])` 로 얌전히
            # 돌려주면 화면에 "실패" 네 글자만 뜨고 왜인지는 어디에도 안 나왔다.
            if result.failures:
                job.failure = result.failures[0]
            job.try_transition(JobState.FAILED)
        self._finish(job)

    # ── 내부 ───────────────────────────────────────────────────

    def _get(self, job_id: str) -> _Running | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _make_workdir(self, job_id: str) -> Path:
        """Job 전용 폴더.

        **입력은 복사하지 않는다.** 룬은 원본 경로를 그대로 받고, 이 폴더는
        산출물 자리다. 복사하면 큰 PDF가 두 벌이 되고 무엇이 원본인지가
        흐려진다. IchorAI가 전역 `scans/`에 복사하다 두 룬 동시 실행이
        불가능해진 것이 같은 뿌리다.
        """
        path = self._root / "jobs" / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _make_datadir(self, rune_id: str) -> Path:
        """룬 전용 영속 폴더. Job 이 아니라 룬 단위다.

        workdir 는 Job 과 함께 생기고 사라지지만 여기는 학습 데이터가 산다.
        룬을 제거해도 남는다 — 다시 설치하면 학습이 그대로 있어야 한다.

        **`runes/` 가 아니라 `rune-data/` 인 이유.** `installer.install` 이
        `rune_dir()/{id}` 를 통째로 rename 해 백업하고 소스에서 새로 copytree 한 뒤
        백업을 지운다. 여기를 `runes/{id}` 에 두면 그 폴더가 설치 대상과 같은
        폴더라 **재설치 한 번에 학습이 통째로 사라졌다.** 위 문단이 원래 의도였고
        코드가 정반대였다. 룬 코드와 룬이 만든 데이터는 다른 곳에 산다.
        """
        path = self._root / "rune-data" / rune_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _emit_state(self, job: Job) -> None:
        self._events.put(StateChanged(job.id, job.state))

    def _finish(self, job: Job) -> None:
        # 결말을 일지에 남긴다. 룬의 로그만 있으면 "그래서 어떻게 끝났나" 를
        # 마지막 줄에서 추측해야 한다 — 취소와 실패는 로그가 똑같이 조용하다.
        journal.record(
            journal.Entry(
                at=journal.now(),
                job_id=job.id,
                rune_id=job.rune_id,
                level="error" if job.failure else "info",
                message=f"실행이 {job.state.name} 로 끝났습니다"
                + (f": {job.failure.message}" if job.failure else ""),
                fields={},
            )
        )
        self._events.put(StateChanged(job.id, job.state))
        self._events.put(Finished(job.id, job.state))
