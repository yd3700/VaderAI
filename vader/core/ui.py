"""
최소 실행 화면 — 룬 고르기, 입력 고르기, 실행, 진행률, 취소.

`python -m vader` 로 띄운다.

**이 파일에는 판단이 없다.** 이벤트를 화면 상태로 접는 것은 `view.py` 가 하고
여기서는 위젯에 옮기기만 한다. tkinter 코드는 테스트하기 어려우니 틀릴 수 있는
로직을 그쪽에 두고 이쪽은 눈으로 확인할 수 있게 남긴다.

큐를 `after()` 로 비운다. 룬은 워커 스레드에서 도는데 tkinter 위젯을 다른
스레드에서 만지면 조용히 깨진다 — 그래서 코어가 콜백이 아니라 큐를 준다.

한 번에 한 Job 만 다룬다. 동시 실행은 러너가 이미 되지만 화면이 그걸 보여줄
자리가 없다 — 필요해지면 목록으로 바꾼다.
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable, Iterable

from vader.core import config, seal
from vader.core.character import derive
from vader.core.checkpoint import CheckpointStore
from vader.core.events import Finished, UserActionRequested
from vader.core.installer import InstallError, install, uninstall
from vader.core.loader import LoadedRune, discover
from vader.core.mapping_ui import attach_mapping_maintenance
from vader.core.observe import ObservationStore, SessionObserver
from vader.core.ocr import (
    Assignment,
    Candidate,
    OcrProviderUnavailable,
    candidates,
    resolve,
)
from vader.core.runner import JobRunner
from vader.core.view import (
    FieldError,
    JobView,
    apply_all,
    checkpoint_key_for,
    coerce_field,
    is_resumable,
    SealState,
    seal_state,
)
from vader.core.windows import list_window_titles
from vader.sdk import (
    CheckpointKey,
    Input,
    RuneField,
    UserActionRequest,
    UserActionResponse,
)

#: 큐를 비우는 주기. 100ms 면 진행률이 끊겨 보이지 않고 CPU 도 놀지 않는다.
POLL_MS = 100

INPUT_TYPES = [
    ("지원하는 파일", "*.pdf *.png *.jpg *.jpeg *.webp *.xls *.xlsx"),
    ("모든 파일", "*.*"),
]

# 룬에 공개된 OCR 진입점과 같은 역할 목록이다. 설정에 역할 블록이 없어도
# 둘 다 전역 제공자를 상속하므로 화면에서 빠지면 안 된다.
OCR_ROLES = ("extract", "infer")


@dataclass(frozen=True)
class AssignmentRow:
    """배정 표의 한 줄. Tk 없이 상태 문구까지 검증하기 위한 표시 모델."""

    role: str
    provider: str
    usage: str
    status: str


def assignment_rows(
    roles: Iterable[str] = OCR_ROLES,
    *,
    candidate_lookup: Callable[[str], list[Candidate]] = candidates,
    resolver: Callable[[str], Assignment] = resolve,
) -> list[AssignmentRow]:
    """코어가 공개한 배정 결과를 읽기 전용 표로 바꾼다.

    자동 후보의 수치와 차단 문구는 가공하거나 다시 세지 않는다. 수동 배정은
    ``candidates()`` 가 수치를 주지 않으므로 없는 정보를 추측하지 않고 대시로
    표시한다.
    """
    rows: list[AssignmentRow] = []
    for role in roles:
        looked = candidate_lookup(role)
        if not looked:
            chosen = resolver(role)
            rows.append(AssignmentRow(role, chosen.provider, "—", "사용 중"))
            continue

        try:
            selected = resolver(role).provider
        except OcrProviderUnavailable:
            selected = None

        for index, candidate in enumerate(looked):
            # 지금 뽑힌 것이 "사용 중" 이다. 나머지는 차례를 기다린다.
            # 뒤집으면 화면이 "왜 이걸 쓰고 있나" 에 거꾸로 답한다.
            status = candidate.blocked or (
                "사용 중 (자동)" if candidate.provider == selected else "대기"
            )
            rows.append(
                AssignmentRow(
                    role if index == 0 else "",
                    candidate.provider,
                    f"{candidate.calls_today} / {candidate.daily_limit}",
                    status,
                )
            )
    return rows


def _enable(widget: ttk.Widget, on: bool) -> None:
    """ttk 버튼을 켜고 끈다.

    ttk 의 `state()` 는 `"normal"` 을 모른다 — 켜는 것은 `disabled` 를 지우는
    것이고 부정 접두사를 쓴다. `tk.NORMAL` 을 넘기면 TclError 다.
    """
    widget.state(["!disabled"] if on else ["disabled"])


def _who() -> str:
    """봉인한 사람. **누가 승인했는지가 원장의 절반이다.**

    로그인 이름을 못 얻는 환경이 있어 실패해도 봉인은 진행한다 — 이름을
    모르는 것과 봉인을 못 하는 것 중 전자가 낫다.
    """
    import getpass

    try:
        return getpass.getuser() or "알 수 없음"
    except Exception:
        return "알 수 없음"


class SetupDialog:
    """룬이 요구하는 설정을 받는 창.

    `RuneField.kind`가 위젯을 고른다. `window`는 실행 중인 최상위 창 제목을
    콤보박스로 보여준다.
    """

    def __init__(self, parent: tk.Misc, manifest, current: dict, rune=None) -> None:
        self.manifest = manifest
        self.result: dict | None = None
        self._vars: list[tuple[RuneField, tk.StringVar]] = []

        self.top = tk.Toplevel(parent)
        self.top.title(f"{manifest.name} 설정")
        self.top.transient(parent)
        self.top.grab_set()

        frame = ttk.Frame(self.top, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        for row, field in enumerate(manifest.required_fields):
            label = field.label or field.name
            if field.kind == "window":
                label += " (창 제목)"
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky=tk.W, pady=4)

            existing = current.get(field.name, field.default)
            var = tk.StringVar(value="" if existing is None else str(existing))
            self._widget(frame, field, var).grid(row=row, column=1, sticky=tk.EW, padx=(10, 0), pady=4)
            self._vars.append((field, var))

        button_row = attach_mapping_maintenance(
            frame, rune, current, len(manifest.required_fields)
        ) if rune is not None else len(manifest.required_fields)
        buttons = ttk.Frame(frame)
        buttons.grid(row=button_row, column=0, columnspan=2, sticky=tk.E, pady=(12, 0))
        ttk.Button(buttons, text="취소", command=self.top.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="저장", command=self._save).pack(side=tk.RIGHT, padx=(0, 8))

        frame.columnconfigure(1, weight=1)

    @staticmethod
    def _pick_path(owner: tk.Misc, field: RuneField, var: tk.StringVar) -> None:
        """파일이나 폴더를 골라 var 에 넣는다. 취소하면 기존 값을 유지한다.

        `parent` 를 반드시 넘긴다 — 없으면 소유자 없는 창이 되어 이 대화상자
        뒤로 숨는다. 여기는 `grab_set` 까지 걸려 있어 더 확실히 숨는다.
        """
        title = field.label or field.name
        chosen = (
            filedialog.askdirectory(title=title, parent=owner)
            if field.kind == "dir"
            else filedialog.askopenfilename(title=title, parent=owner)
        )
        if chosen:
            var.set(chosen)

    @staticmethod
    def _widget(parent: tk.Misc, field: RuneField, var: tk.StringVar) -> ttk.Widget:
        if field.kind == "number":
            return ttk.Spinbox(parent, textvariable=var, from_=0, to=10_000_000, width=30)
        if field.kind == "choice" and field.options:
            return ttk.Combobox(parent, textvariable=var, values=field.options,
                                state="readonly", width=28)
        if field.kind == "window":
            return ttk.Combobox(
                parent,
                textvariable=var,
                values=list_window_titles(),
                width=28,
            )
        if field.kind == "secret":
            # API 키와 토큰이다. 설정 화면은 어깨 너머로도 보인다.
            return ttk.Entry(parent, textvariable=var, width=30, show="•")
        if field.kind in ("path", "dir"):
            # 경로는 손으로 치면 틀린다. 입력란은 남기되 찾기 버튼을 붙인다.
            box = ttk.Frame(parent)
            ttk.Entry(box, textvariable=var, width=24).pack(side=tk.LEFT, fill=tk.X, expand=True)
            ttk.Button(box, text="찾기…", width=6,
                       command=lambda: SetupDialog._pick_path(parent, field, var)).pack(side=tk.LEFT, padx=(4, 0))
            return box
        return ttk.Entry(parent, textvariable=var, width=30)

    def _save(self) -> None:
        values: dict = {}
        for field, var in self._vars:
            try:
                values[field.name] = coerce_field(field, var.get())
            except FieldError as exc:
                messagebox.showwarning("설정", str(exc), parent=self.top)
                return
        self.result = values
        self.top.destroy()

    def ask(self) -> dict | None:
        self.top.wait_window()
        return self.result


class AskDialog:
    """룬이 사람에게 묻는 창.

    `UserActionRequest.kind`가 무엇을 그릴지 정한다. 항목 정보(`context`)를 같이
    보여주는 것은 IchorAI 복구 대화상자와 같다 — 무엇을 보고 판단하는지가 없으면
    사용자가 답할 수 없다.

    창을 닫으면 "abort" 다. 답하지 않겠다는 뜻이므로 룬이 멈추는 것이 맞다.
    닫아도 아무 일이 없으면 워커가 계속 매달려 있는다.
    """

    def __init__(self, parent: tk.Misc, request: UserActionRequest) -> None:
        self.request = request
        self.response = UserActionResponse(action="abort")

        self.top = tk.Toplevel(parent)
        self.top.title("확인 필요")
        self.top.transient(parent)
        self.top.grab_set()
        self.top.protocol("WM_DELETE_WINDOW", self._abort)

        frame = ttk.Frame(self.top, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text=request.prompt, wraplength=380, justify=tk.LEFT).pack(
            anchor=tk.W, pady=(0, 8)
        )

        if request.context:
            box = ttk.LabelFrame(frame, text="항목 정보", padding=(8, 6))
            box.pack(fill=tk.X, pady=(0, 8))
            for key, value in request.context.items():
                ttk.Label(box, text=f"{key}: {value}").pack(anchor=tk.W)

        self._entry: ttk.Entry | None = None
        if request.kind == "correct_value":
            self._entry = ttk.Entry(frame, width=40)
            self._entry.pack(fill=tk.X, pady=(0, 8))
            self._entry.focus_set()

        self._buttons(frame)

    def _buttons(self, frame: ttk.Frame) -> None:
        row = ttk.Frame(frame)
        row.pack(fill=tk.X)
        ttk.Button(row, text="중단", command=self._abort).pack(side=tk.LEFT)
        ttk.Button(row, text="건너뜀", command=self._skip).pack(side=tk.LEFT, padx=(8, 0))

        if self.request.kind == "choice" and self.request.options:
            # 선택지를 버튼으로 낸다. 콤보박스 + 확인보다 한 번 덜 누른다.
            for option in reversed(self.request.options):
                ttk.Button(
                    row, text=option, command=lambda o=option: self._choose(o)
                ).pack(side=tk.RIGHT, padx=(8, 0))
        else:
            ttk.Button(row, text="확인", command=self._ok).pack(side=tk.RIGHT)

    def _finish(self, response: UserActionResponse) -> None:
        self.response = response
        self.top.destroy()

    def _abort(self) -> None:
        self._finish(UserActionResponse(action="abort"))

    def _skip(self) -> None:
        self._finish(UserActionResponse(action="skip"))

    def _choose(self, option: str) -> None:
        self._finish(UserActionResponse(action="ok", value=option))

    def _ok(self) -> None:
        value = self._entry.get().strip() if self._entry is not None else None
        if self._entry is not None:
            if value == "":
                messagebox.showwarning("확인 필요", "값을 입력하세요.", parent=self.top)
                return
            self._finish(UserActionResponse(action="corrected", value=value))
        else:
            self._finish(UserActionResponse(action="ok"))

    def ask(self) -> UserActionResponse:
        self.top.wait_window()
        return self.response


class AssignmentWindow:
    """현재 OCR 역할 배정을 보여주는 비모달 읽기 전용 창."""

    def __init__(self, parent: tk.Misc) -> None:
        self.top = tk.Toplevel(parent)
        self.top.title("OCR 배정")
        self.top.transient(parent)
        # 차단 사유는 코어 문장을 그대로 보여주므로 상태 열을 넉넉히 둔다.
        self.top.minsize(780, 240)

        frame = ttk.Frame(self.top, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame,
            text="현재 설정과 오늘 사용량입니다. 이 화면에서는 배정을 바꿀 수 없습니다.",
        ).pack(anchor=tk.W, pady=(0, 8))

        columns = ("role", "provider", "usage", "status")
        self.table = ttk.Treeview(frame, columns=columns, show="headings", height=7)
        headings = {
            "role": "역할",
            "provider": "제공자",
            "usage": "오늘 사용량",
            "status": "상태",
        }
        widths = {"role": 90, "provider": 100, "usage": 110, "status": 430}
        for column in columns:
            self.table.heading(column, text=headings[column])
            self.table.column(column, width=widths[column], anchor=tk.W)
        self.table.pack(fill=tk.BOTH, expand=True)

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(buttons, text="새로고침", command=self._refresh).pack(side=tk.LEFT)
        ttk.Button(buttons, text="닫기", command=self.top.destroy).pack(side=tk.RIGHT)
        self._refresh()

    def _refresh(self) -> None:
        try:
            rows = assignment_rows()
        except (ValueError, OSError) as exc:
            messagebox.showerror("배정을 읽을 수 없습니다", str(exc), parent=self.top)
            return

        self.table.delete(*self.table.get_children())
        for row in rows:
            self.table.insert(
                "", tk.END, values=(row.role, row.provider, row.usage, row.status)
            )


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.runner = JobRunner(checkpoints=CheckpointStore())
        self.runes: list[LoadedRune] = []
        self.inputs: list[Path] = []
        self.job = None
        self.view = JobView()

        # 관찰은 자기 스레드를 만들지 않는다. 아래 _pump 가 대신 표본을 뜬다.
        self.observations = ObservationStore()
        self.observer = SessionObserver(self.observations)

        root.title("VaderAI")
        root.minsize(560, 420)
        self._build()
        self._load_runes()
        self.observer.start()
        root.protocol("WM_DELETE_WINDOW", self._close)
        self._render_character()
        self._pump()

    # ── 화면 ────────────────────────────────────────────────────

    def _build(self) -> None:
        pad = {"padx": 10, "pady": 6}
        frame = ttk.Frame(self.root)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="룬").grid(row=0, column=0, sticky=tk.W, **pad)
        self.rune_box = ttk.Combobox(frame, state="readonly", width=40)
        self.rune_box.grid(row=0, column=1, sticky=tk.EW, **pad)
        self.rune_box.bind("<<ComboboxSelected>>", lambda _e: self._refresh_resume())

        rune_buttons = ttk.Frame(frame)
        rune_buttons.grid(row=0, column=2, sticky=tk.W, **pad)
        ttk.Button(rune_buttons, text="룬 설치…", command=self._install).pack(side=tk.LEFT)
        ttk.Button(rune_buttons, text="대화…", command=self._show_chat).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Button(rune_buttons, text="배정…", command=self._show_assignments).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        self.setup_btn = ttk.Button(rune_buttons, text="설정…", command=self._setup)
        self.setup_btn.pack(side=tk.LEFT, padx=(6, 0))
        # 자물쇠에는 열쇠가 있어야 한다. 봉인 없이는 운영 실행이 안 되므로
        # 봉인을 찍는 자리가 화면에 있어야 한다.
        self.seal_btn = ttk.Button(rune_buttons, text="승인…", command=self._seal)
        self.seal_btn.pack(side=tk.LEFT, padx=(6, 0))
        self.remove_btn = ttk.Button(rune_buttons, text="제거", command=self._uninstall)
        self.remove_btn.pack(side=tk.LEFT, padx=(6, 0))

        self.pick_btn = ttk.Button(frame, text="입력 고르기…", command=self._pick)
        self.pick_btn.grid(row=1, column=0, sticky=tk.W, **pad)
        self.input_label = ttk.Label(frame, text="고른 파일 없음")
        self.input_label.grid(row=1, column=1, sticky=tk.W, **pad)

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky=tk.W, **pad)
        self.run_btn = ttk.Button(buttons, text="실행", command=self._run)
        self.run_btn.pack(side=tk.LEFT)
        self.cancel_btn = ttk.Button(
            buttons, text="취소", command=self._cancel, state=tk.DISABLED
        )
        self.cancel_btn.pack(side=tk.LEFT, padx=(8, 0))
        self.clear_btn = ttk.Button(
            buttons, text="처음부터", command=self._clear_checkpoint, state=tk.DISABLED
        )
        self.clear_btn.pack(side=tk.LEFT, padx=(8, 0))
        # 리허설 스위치. 이게 없으면 dry-run 은 파이썬으로 submit 을 부를 때만
        # 쓸 수 있어서, 실제 사용자에게는 첫 실행이 곧 운영 입력이었다.
        self.dry_run = tk.BooleanVar(value=False)
        self.dry_run_box = ttk.Checkbutton(
            buttons, text="연습 실행 (dry-run)", variable=self.dry_run
        )
        self.dry_run_box.pack(side=tk.LEFT, padx=(16, 0))
        # **매 실행마다 바뀌는 값이라 설정이 아니라 여기 있다.** 설정 마법사에
        # 두면 고칠 때마다 설정 → 수정 → 저장 → 실행 네 단계를 거쳐야 한다.
        ttk.Label(buttons, text="시작 쪽").pack(side=tk.LEFT, padx=(16, 4))
        self.start_page = tk.StringVar(value="1")
        ttk.Spinbox(
            buttons, textvariable=self.start_page, from_=1, to=999, width=4
        ).pack(side=tk.LEFT)

        self.resume_label = ttk.Label(frame, text="")
        self.resume_label.grid(row=2, column=1, sticky=tk.E, **pad)

        # 봉인 안 된 룬은 왜 잠겼는지 여기 뜬다. **그냥 안 눌리면 사용자는
        # 고장으로 안다.**
        self.seal_label = ttk.Label(frame, text="", foreground="#c01c28")
        self.seal_label.grid(row=2, column=0, sticky=tk.W, **pad)

        self.bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.bar.grid(row=3, column=0, columnspan=2, sticky=tk.EW, **pad)
        self.status = ttk.Label(frame, text="대기")
        self.status.grid(row=4, column=0, columnspan=2, sticky=tk.W, **pad)

        self.log = tk.Text(frame, height=12, state=tk.DISABLED, wrap=tk.WORD)
        self.log.grid(row=5, column=0, columnspan=2, sticky=tk.NSEW, **pad)
        self.log.tag_configure("artifact", foreground="#1c71d8", underline=True)
        self.log.tag_bind("artifact", "<Enter>", lambda _e: self.log.config(cursor="hand2"))
        self.log.tag_bind("artifact", "<Leave>", lambda _e: self.log.config(cursor=""))

        # 캐릭터는 룬과 독립이다 — 룬이 0개여도 이 줄은 자란다.
        character_row = ttk.Frame(frame)
        character_row.grid(row=6, column=0, columnspan=2, sticky=tk.EW, **pad)
        self.character_label = ttk.Label(character_row, text="")
        self.character_label.pack(side=tk.LEFT)
        ttk.Button(
            character_row, text="관찰 지우기", command=self._reset_observations
        ).pack(side=tk.RIGHT)

        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(5, weight=1)

    def _load_runes(self) -> None:
        self.runes, failures = discover()
        self.rune_box["values"] = [f"{r.manifest.name} ({r.manifest.id})" for r in self.runes]

        has_runes = bool(self.runes)
        if has_runes:
            self.rune_box.current(0)
        else:
            # 룬 0개는 정상 상태다. 무엇을 해야 하는지 알려준다.
            self._append("설치된 룬이 없습니다. ‘룬 설치…’ 로 추가하세요.")
            self._append(f"룬 폴더: {config.rune_dir()}")
        _enable(self.run_btn, has_runes)
        _enable(self.setup_btn, has_runes)
        _enable(self.remove_btn, has_runes)

        for failure in failures:
            self._append(f"[error] 룬 로드 실패 {failure.rune_id}: {failure.reason}")

    # ── 동작 ────────────────────────────────────────────────────

    def _show_chat(self) -> None:
        from vader.core.chat_ui import ChatWindow

        ChatWindow(self.root)

    def _show_assignments(self) -> None:
        AssignmentWindow(self.root)

    def _install(self) -> None:
        source = filedialog.askdirectory(
            title="룬 폴더 고르기 (rune-packages/{id})", parent=self.root
        )
        if not source:
            return
        try:
            result = install(Path(source))
        except InstallError as exc:
            messagebox.showerror("설치 실패", str(exc), parent=self.root)
            return

        self._append(f"설치: {result.manifest.name} ({result.manifest.id})")
        self._load_runes()
        self._select(result.manifest.id)

        # 설치만으로는 못 돈다. 빠진 설정을 바로 받는다 — 나중에 실행하다
        # 막히는 것보다 지금 채우는 게 낫다.
        if result.missing_settings:
            self._append(f"필요한 설정: {', '.join(result.missing_settings)}")
            self._setup()

    def _uninstall(self) -> None:
        rune = self._current_rune()
        if rune is None:
            return

        # 되돌리려면 다시 설치해야 하므로 한 번 묻는다.
        if not messagebox.askyesno(
            "룬 제거",
            f"{rune.manifest.name} 을 제거할까요?\n\n"
            "설정과 체크포인트는 남습니다. 다시 설치하면 그대로 씁니다.",
            parent=self.root,
        ):
            return

        if uninstall(rune.manifest.id):
            self._append(f"제거: {rune.manifest.name} ({rune.manifest.id})")
        else:
            self._append(f"[error] 제거 실패 — 설치돼 있지 않습니다: {rune.manifest.id}")
        self._load_runes()
        self._refresh_resume()

    def _setup(self) -> None:
        rune = self._current_rune()
        if rune is None:
            return
        rune_id = rune.manifest.id
        values = SetupDialog(
            self.root, rune.manifest, config.rune_config(rune_id), rune.rune
        ).ask()
        if values is None:
            return
        config.set_rune_configs(rune_id, values, secret=[f.name for f in rune.manifest.required_fields if f.kind == "secret"])
        self._append(f"설정 저장: {rune_id}")

    def _current_rune(self) -> LoadedRune | None:
        index = self.rune_box.current()
        return self.runes[index] if 0 <= index < len(self.runes) else None

    def _select(self, rune_id: str) -> None:
        for index, rune in enumerate(self.runes):
            if rune.manifest.id == rune_id:
                self.rune_box.current(index)
                return

    def _pick(self) -> None:
        chosen = filedialog.askopenfilenames(
            title="처리할 파일", filetypes=INPUT_TYPES, parent=self.root
        )
        self.inputs = [Path(p) for p in chosen]
        self.input_label.config(
            text=f"{len(self.inputs)}개 선택" if self.inputs else "고른 파일 없음"
        )
        self._refresh_resume()

    def _checkpoint_key(self) -> CheckpointKey | None:
        """지금 고른 룬·입력의 체크포인트 키.

        키는 입력 **내용**으로 만든다(`CheckpointKey.from_inputs`). 그래서
        재시도가 새 Job 이어도 같은 자리를 가리킨다 — 그게 이어하기의 근거다.
        """
        rune = self._current_rune()
        if rune is None:
            return None
        # 판정은 `view.py` 가 한다. 성공한 실행은 입력을 done_dir 로 옮기므로
        # 완료 직후 여기서 다시 해시하면 FileNotFoundError 다 — 그 예외가
        # 폴링을 죽였다.
        return checkpoint_key_for(rune.manifest.id, self.inputs)

    def _refresh_resume(self) -> None:
        """이어서 실행될지 사용자에게 알린다.

        재시도는 원래도 됐지만(실행을 다시 누르면 새 Job 이고 체크포인트가
        저절로 이어진다) 그걸 알 방법이 없었다. 모르면 처음부터 도는 줄 안다.
        """
        key = self._checkpoint_key()
        resumable = key is not None and is_resumable(self.runner.checkpoints.load(key))

        self.resume_label.config(
            text="이전 진행 지점에서 이어서 실행합니다" if resumable else ""
        )
        _enable(self.clear_btn, resumable)
        self._refresh_seal()

    def _seal(self) -> None:
        """이 룬을 봉인한다. **사람이 승인하는 자리다.**

        골든 특성화는 저장소의 CI 가 돌린다. 이 기계에는 골든 표본이 없다 —
        병원 문서라 깃에 안 올라간다. 그래서 여기서 자동으로 검증할 수 있는
        것은 없고, **사람이 "연습 실행으로 확인했다" 고 말하는 것**이 곧
        승인이다. 그 말을 누가 언제 했는지 원장에 남는다.

        자동 봉인을 안 만드는 것은 `golden-baseline.md` 의 규칙과 같다 —
        "스냅샷 자동 갱신 기능은 만들지 않는다. 자동 갱신이 있으면 회귀가
        조용히 승인된다."
        """
        rune = self._current_rune()
        if rune is None:
            return
        rune_id = rune.manifest.id
        source = config.rune_dir() / rune_id
        if not source.is_dir():
            messagebox.showerror(
                "승인할 수 없습니다",
                f"{rune_id} 의 설치 폴더를 찾지 못했습니다.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "실사용 승인",
            f"{rune.manifest.name} 을(를) 승인하면 실사용 실행이 열립니다.\n\n"
            "연습 실행 (dry-run) 으로 결과를 직접 확인하셨습니까?\n"
            "확인하지 않았다면 아니오를 누르고 먼저 연습 실행하세요.",
            parent=self.root,
        ):
            return
        note = simpledialog.askstring(
            "승인 메모", "무엇을 확인했는지 적어 주세요", parent=self.root
        )
        if note is None:
            return
        try:
            seal.seal(rune_id, source, by=_who(), note=note)
        except Exception as exc:  # noqa: BLE001 — 원장 쓰기 실패를 사용자에게 보인다
            messagebox.showerror("승인 실패", str(exc), parent=self.root)
            return
        self._append(f"{rune.manifest.name} 을(를) 승인했습니다. 실사용 실행이 열립니다.")
        self._refresh_seal()

    def _refresh_seal(self) -> None:
        """봉인 안 된 룬이면 연습 실행으로 고정한다.

        **버튼을 잠그는 것이 아니라 연습 쪽으로 고정한다.** 실행 자체를 막으면
        만든 것을 시험할 길이 없어지고, 그러면 사용자는 봉인을 우회할 방법을
        찾는다. 러너가 어차피 운영 실행을 거부하므로(`_reject_unsealed`)
        여기서 잠그는 것은 **설명**이지 방어가 아니다.
        """
        rune = self._current_rune()
        state = seal_state(rune.manifest.id) if rune else SealState(locked=False)
        self.seal_label.config(text=state.reason)
        if state.locked:
            self.dry_run.set(True)
            self.dry_run_box.state(["disabled"])
        else:
            self.dry_run_box.state(["!disabled"])

    def _clear_checkpoint(self) -> None:
        key = self._checkpoint_key()
        if key is None:
            return
        self.runner.checkpoints.clear(key)
        self._append("체크포인트를 지웠습니다. 다음 실행은 처음부터 돕니다.")
        self._refresh_resume()

    def _run(self) -> None:
        if not self.runes:
            return
        if not self.inputs:
            messagebox.showinfo("VaderAI", "처리할 파일을 먼저 고르세요.", parent=self.root)
            return

        rune = self.runes[self.rune_box.current()]

        # 설정이 비면 실행 중에 죽는다. 시작하기 전에 말해준다.
        params = config.rune_config(rune.manifest.id)
        missing = [
            f.name
            for f in rune.manifest.required_fields
            if f.name not in params and not getattr(f, "optional", False)
        ]
        if missing:
            messagebox.showwarning(
                "설정 필요",
                f"{rune.manifest.name} 설정이 없습니다:\n" + "\n".join(missing),
                parent=self.root,
            )
            return

        self.view = JobView()
        self._clear_log()
        try:
            self.job = self.runner.submit(
                rune.rune,
                [Input(path=p) for p in self.inputs],
                params={**params, **self._run_overrides()},
                dry_run=self.dry_run.get(),
            )
        except RuntimeError as exc:
            # 대상 앱 거부와 비상정지가 여기로 온다. 둘 다 사용자가 고칠 수 있는
            # 상태다 — 크래시 대화상자로 보내면 무엇을 고쳐야 하는지 안 보인다.
            messagebox.showerror("실행할 수 없습니다", str(exc), parent=self.root)
            return
        if self.dry_run.get():
            self._append("연습 실행: 이온엠 입력·파일 이동·체크포인트를 건너뜁니다")
        _enable(self.run_btn, False)

    def _run_overrides(self) -> dict:
        """이번 실행에만 쓰는 값. 저장된 설정을 건드리지 않는다."""
        raw = self.start_page.get().strip()
        try:
            page = max(1, int(raw))
        except ValueError:
            page = 1
        return {"start_page": page}

    def _cancel(self) -> None:
        if self.job:
            self.runner.cancel(self.job.id)

    # ── 이벤트 펌프 ─────────────────────────────────────────────

    def _pump(self) -> None:
        """폴링 한 틱. **재예약은 어떤 경우에도 한다.**

        예전에는 재예약이 본문 맨 끝에 있어서 **예외 한 번에 폴링이 영구히
        멎었다.** 실제로 그랬다 — 성공한 실행이 입력을 옮긴 뒤 `_refresh_resume`
        가 사라진 파일을 다시 해시하다 죽었고, 그 뒤로 다음 작업의 로그도
        버튼도 갱신되지 않았다. ERP 입력은 정상이었으므로 사용자에게는
        **작업은 되는데 화면만 죽은** 상태로 보였고, 다시 실행하면 중복 입고다.

        예외는 삼키지 않는다. tkinter 의 `report_callback_exception` 이 크래시
        리포트를 남기던 경로를 그대로 탄다 — 여기서는 **살아남는 것**만 맡는다.
        """
        try:
            self._pump_once()
        finally:
            self.root.after(POLL_MS, self._pump)

    def _pump_once(self) -> None:
        events = self.runner.drain_events()
        if events:
            before = len(self.view.logs)
            apply_all(self.view, events)
            self._render(new_logs=self.view.logs[before:])
            # 질문은 화면을 갱신한 **뒤에** 띄운다. 창이 wait_window 로 막는 동안
            # 사용자가 진행 상황을 볼 수 있어야 한다.
            for event in events:
                if isinstance(event, UserActionRequested):
                    self._ask(event)
                elif isinstance(event, Finished):
                    self.observer.job_finished()

        # 실제 기록 주기는 observe.sample_seconds() 가 정한다. 여기서 자주 불러도
        # 관찰자가 알아서 걸러낸다 — 폴링 주기와 관찰 주기를 묶지 않는다.
        if self.observer.sample():
            self._render_character()

    def _render_character(self) -> None:
        character = derive(self.observations.load())
        self.character_label.config(
            text=f"Lv.{character.level} ({character.percent}%) — {character.personality}"
        )

    def _reset_observations(self) -> None:
        if not messagebox.askyesno(
            "관찰 지우기",
            "지금까지의 관찰과 성장을 모두 지웁니다. 되돌릴 수 없습니다.\n계속할까요?",
            parent=self.root,
        ):
            return
        self.observations.reset()
        self._render_character()

    def _close(self) -> None:
        self.observer.stop()
        self.root.destroy()

    def _ask(self, event: UserActionRequested) -> None:
        """룬이 물었다. 답을 받아 워커를 깨운다.

        워커는 이 답을 기다리며 블록돼 있다. 답하지 않고 놔두면 취소로만 풀린다.
        """
        response = AskDialog(self.root, event.request).ask()
        self.runner.respond(event.job_id, event.correlation_id, response)
        self._append(f"응답: {response.action}" + (f" ({response.value})" if response.value else ""))

    def _render(self, new_logs: list[str]) -> None:
        self.status.config(text=self.view.status_text)

        percent = self.view.percent
        if percent is None:
            self.bar.config(mode="indeterminate")
        else:
            self.bar.config(mode="determinate", value=percent)

        _enable(self.cancel_btn, self.view.can_cancel)

        # 룬이 남긴 로그를 먼저 쏟고 결과를 맨 끝에 붙인다. 반대로 하면
        # 마지막 틱에 "결과: ..." 가 룬의 완료 로그보다 위에 찍힌다.
        for line in new_logs:
            self._append(line)

        if self.view.is_done:
            _enable(self.run_btn, True)
            self._report_result()
            # 실패·취소로 끝났으면 체크포인트가 남아 있다. 다음에 이어질지 알려준다.
            self._refresh_resume()

    def _report_result(self) -> None:
        if self.job is None:
            return
        job, self.job = self.job, None

        if job.result and job.result.artifacts:
            for path in job.result.artifacts:
                self._append(f"결과: {path}", path=Path(path))
            # 화면(HTML)을 내는 룬은 그 화면이 곧 결과다 — 끝나면 바로 연다.
            # 나머지 파일은 위 줄을 눌러 연다.
            pages = [Path(a) for a in job.result.artifacts if Path(a).suffix.lower() == ".html"]
            if pages:
                os.startfile(pages[0])
        if job.failure:
            self._append(f"[error] {job.failure.kind}: {job.failure.message}")

    # ── 로그 ────────────────────────────────────────────────────

    def _append(self, line: str, *, path: Path | None = None) -> None:
        """`path` 가 있으면 그 줄을 누르면 파일이 열린다 (결과 파일)."""
        self.log.config(state=tk.NORMAL)
        if path is None:
            self.log.insert(tk.END, line + "\n")
        else:
            tag = f"artifact-{self.log.index(tk.END)}"
            self.log.insert(tk.END, line, ("artifact", tag))
            self.log.insert(tk.END, "\n")
            self.log.tag_bind(tag, "<Button-1>", lambda _e, p=path: os.startfile(p))
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self.log.config(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.config(state=tk.DISABLED)


def main(*, crash_reporter=None) -> None:
    root = tk.Tk()
    if crash_reporter is not None:
        def report_callback_exception(exc_type, exc_value, tb):
            crash_reporter.write(exc_type, exc_value, tb)
            sys.__excepthook__(exc_type, exc_value, tb)

        root.report_callback_exception = report_callback_exception
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
