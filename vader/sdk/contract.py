"""
룬과 코어 사이의 유일한 계약.

의존 방향:

    vader.core  ──►  vader.sdk  ◄──  runes.*

룬은 이 모듈만 import한다. `vader.core`를 import하면 안 된다.
코어도 이 모듈을 import한다. sdk는 어느 쪽도 import하지 않는다.
이 방향은 tests/test_dependency_direction.py 가 강제한다.

설계 근거는 wiki/architecture/core-rune-boundary.md 참조.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# 체크포인트 **코어** 스키마 버전. 문서 자체의 모양이 바뀔 때 올린다.
#
# 룬이 업그레이드돼 payload 구조가 바뀌는 것은 이 값으로 다루지 않는다 — 여기를
# 올리면 모든 룬의 체크포인트가 같이 무효가 되고, 그건 잘못된 단위다. 코어는
# 체크포인트에 룬 버전을 함께 기록하고 다르면 무시한다 (`checkpoint.py`).
# 코어는 버전이 다르면 체크포인트를 무시하고 처음부터 실행한다.
CHECKPOINT_SCHEMA_VERSION = 1


class OcrError(RuntimeError):
    """`ctx.ocr()`이 실패했을 때 룬이 잡는 오류.

    코어가 아니라 여기 있는 이유는 룬이 `vader.core`를 import할 수 없기 때문이다.
    코어 쪽에만 두면 룬은 이 예외를 이름으로 잡을 방법이 없어 bare except를 쓰게 된다.
    """


class HttpError(RuntimeError):
    """`ctx.http_get`·`ctx.http_post` 가 실패했을 때 룬이 잡는 오류.

    `OcrError` 와 같은 이유로 여기 산다 — 룬은 `vader.core` 를 import 할 수
    없어서, 코어 쪽에만 두면 bare except 를 쓰게 된다.
    """


class OcrProviderUnavailable(OcrError):
    """제공자를 쓸 수 없다. 설정 문제이므로 재시도해도 같다."""


class OcrProviderError(OcrError):
    """제공자 호출이 실패했거나 재시도를 소진했다. 다시 해볼 만하다."""


class OcrSchemaError(OcrError):
    """응답을 요청한 스키마로 해석할 수 없다. 문서 쪽 문제다."""


class RuneCancelled(Exception):
    """사용자가 취소를 요청했을 때 `ctx.check_cancelled()`가 던진다.

    협조적 취소다. 룬은 이 예외를 삼키면 안 된다. 코어가 잡아서
    Job을 cancelled 상태로 마감한다. 강제 중단(thread kill)은 지원하지 않는다.
    """


# ── 입력 ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Input:
    """룬에 명시적으로 전달되는 입력 하나.

    전역 폴더를 스캔해서 입력을 발견하는 방식은 금지한다.
    무엇을 처리할지는 항상 Job 생성 시점에 결정된다.
    """

    path: Path
    kind: str = "file"
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """파일 이름만 — 표 룬은 `.path` 를 못 쓰지만 결과에 출처를 적을 수 있게 (2026-11-19)."""
        return Path(self.path).name


# ── 출력 ────────────────────────────────────────────────────────

@dataclass
class Failure:
    """한 건의 실패. 사람이 읽을 문구는 코어가 만든다."""

    kind: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuneResult:
    ok: bool
    artifacts: list[Path] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    failures: list[Failure] = field(default_factory=list)


# ── 사용자 상호작용 ─────────────────────────────────────────────

@dataclass
class UserActionRequest:
    """룬이 진행하려면 사람의 판단이 필요할 때 보낸다.

    kind    : 코어가 어떤 UI를 띄울지 고르는 키 ("confirm", "choice", "correct_value", "edit_rows")
    prompt  : 사용자에게 보여줄 질문
    options : kind가 "choice"일 때의 선택지
    context : 화면에 함께 보여줄 값 (품목명, 찾던 셀렉터 등)

    `edit_rows` 는 표를 고치게 한다 (2026-10-29). context 에 `columns` ([키, 이름] 목록),
    `rows`, `editable` (고칠 수 있는 키), 선택으로 `can_add`·`pages`·`sum_column`·`target`·
    `target_label`·`ui_total`. 답은 action "corrected" 와 value = 고친 rows 의 JSON 문자열.
    행의 `_i` 는 원래 순번이라 그대로 돌아온다 — 새 행에는 없다. **룬이 다시 검사한다.**
    """

    kind: str
    prompt: str
    options: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class UserActionResponse:
    """`request_user_action()`의 반환값.

    action : "ok" | "skip" | "retry" | "abort" | "corrected"
    value  : 사용자가 고르거나 입력한 값

    `value`는 두 경우에 담긴다 — kind가 "correct_value"일 때 입력한 값,
    "choice"일 때 고른 선택지. 선택지를 돌려줄 자리가 따로 없어서 같은 필드를
    쓴다. 창을 닫으면 action은 "abort"다 — 답하지 않겠다는 뜻이므로 룬이
    멈추는 것이 맞다.

    `fields`는 한 대화상자가 값을 둘 이상 받을 때 쓴다. EONM 의 교정 대화상자가
    코드와 "이 매핑을 학습할까요?"를 함께 묻는 것이 그 경우다. `value` 하나로는
    담을 수 없어 열었다. 없으면 룬이 자기 기본값을 쓴다 — 코어는 채우지 않는다.
    """

    action: str
    value: Any = None
    fields: dict[str, Any] = field(default_factory=dict)


# ── 체크포인트 ──────────────────────────────────────────────────

@dataclass(frozen=True)
class CheckpointKey:
    """체크포인트의 신원.

    job_id로 키잉하면 안 된다. 재시도는 항상 새 Job이므로 이어하기가
    원리적으로 불가능해진다. 입력 내용이 같으면 이어서 실행하고,
    내용이 바뀌면 새 체크포인트가 된다. 파일명은 표시용일 뿐 신원이 아니다.
    """

    rune_id: str
    input_identity: str

    @classmethod
    def from_inputs(
        cls,
        rune_id: str,
        inputs: list[Input],
        params: dict | None = None,
    ) -> "CheckpointKey":
        """룬과 그 실행이 무엇을 대상으로 하는지의 신원.

        입력 파일은 **내용**으로 센다 — 이름을 바꿔 다시 넣어도 같은 문서다
        (결함 #14 에서 배웠다).

        **입력이 없는 룬은 설정이 곧 대상이다.** `web_brief` 처럼 파일 없이
        도는 룬은 입력이 빈 목록이라 모든 실행의 신원이 같아졌다. 그러면
        검색어를 바꿔도 같은 체크포인트를 쓰고, 동시 제출 차단이 **서로 다른
        검색어끼리도 막는다.** 설정을 신원에 넣어 그 둘을 가른다.

        입력이 있으면 설정은 안 본다 — 같은 문서를 같은 룬이 두 번 넣는 것을
        설정 한 글자로 통과시키면 중복 입고가 돌아온다.
        """
        digest = hashlib.sha256()
        for item in sorted(inputs, key=lambda i: str(i.path)):
            digest.update(Path(item.path).read_bytes())
        if not inputs and params:
            # 값은 다이제스트로만 들어간다. 토큰이 신원 문자열로 새지 않는다.
            digest.update(
                json.dumps(params, sort_keys=True, default=str).encode("utf-8")
            )
        return cls(rune_id=rune_id, input_identity=digest.hexdigest())


# ── 매니페스트 ──────────────────────────────────────────────────

@dataclass(frozen=True)
class RuneField:
    """룬이 사용자에게 받아야 하는 설정 하나.

    `kind`가 셋업 마법사의 위젯을 고른다. 값은 IchorAI의 마법사가 이미 그리는
    것과 같다 — 없는 종류를 만들지 않았다.

        "text"    한 줄 입력
        "number"  숫자 입력
        "choice"  `options` 중에서 고르기
        "window"  살아 있는 창 목록에서 고르기 (RPA 룬용)
        "path"    파일을 지목 (사용자가 이미 갖고 있는 데이터)
        "dir"     폴더를 지목 (룬이 산출물을 쓰거나 입력을 옮길 곳)

    모르는 `kind`는 마법사가 "text"로 그린다. 코어는 검사하지 않는다 — 룬이
    오타를 냈다고 설치를 막으면 얻는 것보다 잃는 게 많다.

    `default`가 있으면 마법사가 미리 채운다. 그래도 사용자가 확인해야 한다 —
    설치기는 값이 **설정에 있는지**만 보고 기본값을 대신 써 주지 않는다.
    """

    name: str
    #: `text` · `number` · `choice` · `window` · `path` · `dir` · `secret`.
    #: 모르는 값은 `text` 로 그린다 — 룬이 오타를 냈다고 설치를 막지 않는다.
    #: `secret` 은 API 키·토큰이다. 화면에서 가리고, **로그에 안 남는다.**
    kind: str = "text"
    #: 화면에 보일 이름. 없으면 `name`을 그대로 쓴다.
    label: str | None = None
    default: Any = None
    #: `kind`가 "choice"일 때의 선택지.
    options: list[str] = field(default_factory=list)
    #: 비워 둬도 되는 값인가. **기본은 거짓** — 대부분의 설정은 없으면 룬이
    #: 못 돈다.
    #:
    #: 참이면 마법사가 빈 값을 받고 실행 검사도 이 필드를 안 따진다. 룬은
    #: 빈 값을 "그 기능을 안 쓴다" 로 읽는다. 이온엠의 `exe_path` 가 그렇다 —
    #: 넣으면 프로그램을 대신 띄우고, 비우면 사람이 띄운 창에 붙는다.
    optional: bool = False


@dataclass
class RuneManifest:
    """룬이 자기를 코어에 소개하는 값.

    permissions는 이제 설치 시점에 강제된다 (2026-07-30). 룬 소스를 AST로 훑어
    선언하지 않은 능력을 쓰면 설치를 거부한다. **샌드박스는 아니다** — 룬은
    인프로세스 임의 파이썬이라 런타임에 막을 수 없다. 막는 것은 선언하지 않고
    조용히 쓰는 것뿐이고, 무엇을 못 막는지는
    `wiki/architecture/permission-model.md`에 적었다.

    required_fields는 셋업 마법사가 무엇을 물어야 하는지 알려준다. 이름만으로는
    입력 위젯을 고를 수 없어 RuneField로 종류와 기본값을 함께 받는다.
    """

    id: str
    name: str
    version: str = "1.0.0"
    target_apps: list[str] = field(default_factory=list)
    required_fields: list[RuneField] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    #: 실행 프로필. `"table"` 이면 표 룬 — 허용된 이름만 쓰고 파일은 `ctx.read_table`·`ctx.write_table`
    #: 로만 다룬다. 설치기가 검사한다 (`vader/core/profile.py`). 비어 있으면 기존 방식(제한 없음,
    #: 동료 공유·공개 대상 아님). 2026-10-14 codex1 합의.
    profile: str = ""


# ── 실행 컨텍스트 ───────────────────────────────────────────────

@runtime_checkable
class RuneContext(Protocol):
    """코어가 룬에 주입하는 실행 채널.

    룬은 print()나 input()을 쓰지 않는다. sys.stdout/sys.stdin을 건드리지
    않는다. 모든 바깥과의 대화는 이 객체를 통한다.
    """

    @property
    def workdir(self) -> Path:
        """이 Job 전용 작업 폴더. 룬은 이 밖에 파일을 쓰지 않는다."""
        ...

    @property
    def params(self) -> dict[str, Any]:
        """settings의 runes.{rune_id} 블록. 코어는 내용을 해석하지 않는다."""
        ...

    @property
    def datadir(self) -> Path:
        """이 룬 전용 영속 폴더. Job 이 끝나도 남고 다음 Job 이 같은 것을 본다.

        `workdir` 와 다르다 — 그쪽은 Job 하나의 산출물 자리이고 여기는 룬이
        관리하는 상태(학습 데이터 등)가 산다. 룬을 제거해도 남는다. 다시
        설치하면 학습이 그대로 있어야 한다.

        사용자가 이미 갖고 있는 파일은 여기 두지 않는다 — 그건 설정으로 받는다
        (`RuneField(kind="path")`). 복사하면 원본이 갱신될 때 두 벌이 어긋난다.
        """
        ...

    @property
    def dry_run(self) -> bool:
        """참이면 룬이 자기 쓰기 동작을 건너뛴다.

        **코어는 강제하지 않는다.** 외부 시스템을 만지는 코드가 룬 안에 있어
        가로챌 자리가 없다. 지키는 것은 룬의 책임이다. vader/core/safety.py 참조.
        """
        ...

    def log(self, level: str, message: str, **fields: Any) -> None:
        """진단 로그. level은 "debug" | "info" | "warning" | "error"."""
        ...

    def report_progress(
        self, current: int, total: int | None, message: str = ""
    ) -> None:
        """진행률. total이 None이면 개수를 모르는 작업이다."""
        ...

    def request_user_action(
        self, request: UserActionRequest
    ) -> UserActionResponse:
        """사람에게 묻고 답을 기다린다. **동기 블로킹이다.**

        룬은 워커 스레드에서 돌고 이 호출은 UI 스레드로 왕복한다.
        비동기로 바꾸면 기존 복구 로직의 코드 형태가 전부 깨지므로
        블로킹 의미론은 계약의 일부다.
        """
        ...

    def load_checkpoint(self) -> dict | None:
        """이전 실행이 남긴 진행 상태. 없으면 None."""
        ...

    def save_checkpoint(self, data: dict) -> None:
        """진행 상태 저장. 코어가 schema_version을 함께 기록한다."""
        ...

    def clear_checkpoint(self) -> None:
        """정상 완료 시 호출. 다음 실행이 처음부터 돌게 한다."""
        ...

    def check_cancelled(self) -> None:
        """취소 요청이 있으면 RuneCancelled를 던진다.

        룬은 안전하게 중단할 수 있는 지점마다 호출한다. 호출 간격이
        곧 취소 지연이다. 외부 시스템에 절반만 입력된 상태로 끊기지
        않을 지점에서만 부른다.
        """
        ...

    def read_table(self, source: Input, sheet: str | None = None) -> list[list]:
        """이번 작업에 주어진 입력 하나를 표(행 목록)로 읽는다. csv·xlsx. 완전히 빈 행은 뺀다.

        표 룬(`profile="table"`)은 파일을 이것으로만 읽는다 — 경로를 만지지 않는다.
        """
        ...

    def write_table(self, name: str, rows: list[list]) -> str:
        """이번 작업 폴더에 표를 쓰고 결과 파일 자리를 돌려준다. `RuneResult.artifacts` 에 그대로 넣는다.

        `name` 은 폴더 없는 파일 이름 하나(.csv·.xlsx)다.
        """
        ...

    def ocr(self, path: Path, prompt: str, schema: type) -> object:
        """OCR 게이트웨이. 키·캐시·재시도·일일한도는 코어가 관리한다.

        프롬프트와 스키마는 룬이 소유한다. 코어는 어떤 문서인지 모른다.
        """
        ...

    def http_get(self, url: str, *, headers: dict | None = None) -> Any:
        """외부 시스템을 읽는다. 코어가 시간 제한·재시도·로그를 맡는다.

        룬이 `network` 권한을 선언하고 직접 `urllib` 을 쓰는 것을 막으려고
        둔다. 직접 쓰면 재시도와 로그가 룬마다 흩어지고, **보낸 주소를
        아무도 모른다.** 카톡으로 보내는 룬이라면 그게 곧 개인정보 경계다.
        """
        ...

    def http_post(self, url: str, *, data=None, json=None, headers=None) -> Any:
        """외부 시스템에 쓴다. **연습 실행에서는 나가지 않는다.**

        `dry_run` 이 처음으로 권고가 아니게 되는 자리다. EONM 은 쓰기가 룬
        안에 있어 코어가 가로챌 자리가 없었고, 지키는지 테스트로만 확인할 수
        있었다. 통로가 코어에 있으면 코어가 막는다.

        연습 실행에서는 `skipped=True` 인 응답이 돌아온다. 룬은 성공으로 보고
        계속하면 된다.
        """
        ...

    def replay(self, name: str, stop=None) -> bool:
        """녹화해 둔 시연을 다시 눌러 준다. **코어의 기능이다.**

        마스터 결정 (2026-08-11):

        > 녹화를 룬으로 굳히는 게 아닌 core 의 기능으로 남겨둬야해. 그래야
        > 룬을 만들 때 수월하게 만들 수 있잖아.

        룬이 녹화 파일을 직접 읽으면 룬마다 저장 위치·형식·창 찾기를 다시
        짜야 하고, **그 순간 녹화는 룬 하나의 사정이 된다.** 여기 두면 새
        룬은 이름 하나만 알면 된다.

        어느 창에 누를지는 **녹화가 스스로 안다** — 녹화할 때 창 제목을 같이
        적었다. 룬이 창을 찾아 넘겨 줄 필요가 없다.

        없는 녹화면 거짓을 돌려준다. **던지지 않는다** — 녹화가 없다는 것은
        고장이 아니라 "아직 안 가르쳤다" 이고, 룬이 다른 길로 갈 수 있다.
        """
        ...

    def infer(self, prompt: str, schema: type) -> object:
        """텍스트로 물어 구조화 결과를 받는다. 파일이 없는 `ocr()` 이다.

        같은 게이트웨이를 쓴다 — 제공자 선택, 캐시, 재시도, 일일 한도가 같다.
        다른 것은 입력뿐이다.

        새 권한이 아니다. 룬은 이미 텍스트 파일을 만들어 `ocr()` 에 넘기면 같은
        일을 할 수 있었다. 이건 그 우회로를 없애고 의도를 드러낸다.

        룬이 제공자를 직접 부르는 것을 막으려고 둔다 — 그러면 캐시·재시도·한도가
        룬마다 흩어지고, 그게 IchorAI 의 문제였다.
        """
        ...


# ── 룬 ──────────────────────────────────────────────────────────

@runtime_checkable
class Rune(Protocol):
    """모든 룬의 형태.

    진입점은 run() 하나다. pipeline/direct 같은 실행 모드 구분은 없다.
    입력을 코어가 항상 명시적으로 주므로 구분할 이유가 사라졌다.
    """

    manifest: RuneManifest

    def run(self, ctx: RuneContext, inputs: list[Input]) -> RuneResult:
        ...


def check_target_app(manifest: RuneManifest, window_title: str) -> bool:
    """창 제목이 룬의 허용 목록 중 하나를 포함하는지 검사한다.

    빈 ``target_apps``는 제한 없음이다.

    **부분 일치인 이유.** 실제 창 제목은 설정에 적는 값보다 길다. IchorAI가
    ``"입고/반품등록(수기)"``로 찾는 창의 실제 제목에는 앞뒤로 다른 문자열이
    붙는다(``eonm_driver.py:31``이 ``title_re=".*{...}.*"``로 연결하는 이유다).
    정확 일치로 두면 Phase 5에서 연결하는 순간 실제 창을 전부 거부한다.

    느슨해서 생기는 위험은 크지 않다. ``target_apps``는 룬 작성자가 쓰는 값이지
    바깥에서 들어오는 입력이 아니다.
    """

    if not manifest.target_apps:
        return True
    return any(allowed in window_title for allowed in manifest.target_apps)
