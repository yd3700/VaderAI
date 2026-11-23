"""펫 대화 창.

`ui.py` 에 안 넣고 여기 둔다. 실행 화면은 이미 복잡하고, 대화는 실행과
성격이 다르다 — 실행은 시작과 끝이 있고 대화는 계속된다.

**대화는 워커 스레드에서 한다.** LLM 한 턴이 몇 초씩 걸리는데 UI 스레드에서
부르면 그동안 창이 얼어 취소도 못 누른다. `JobRunner` 가 룬을 그렇게 돌리는
것과 같은 이유다.
"""
from __future__ import annotations

import re
import threading
import tkinter as tk
from queue import Empty, Queue
from tkinter import messagebox, ttk

from vader.core import conversation, pet as pet_module
from vader.core.chat import ConversationSession
from vader.core.pet import TONES, Pet
from vader.core.view import MAX_DROP_BUTTONS, recording_choices, recording_lines

POLL_MS = 100

#: `**굵게**` 와 `http…` 주소. **이 둘뿐이다.**
#:
#: 전체 마크다운 엔진을 넣으면 표·인용·중첩목록까지 따라오고, 그건 대화
#: 창이 할 일이 아니다. 모델이 별표를 쓰는 것과 주소를 주는 것만 받는다.
_MARK = __import__("re").compile(r"\*\*(.+?)\*\*|(https?://\S+)")


def split(text: str):
    """(조각, 태그) 로 쪼갠다. 태그가 없으면 None."""
    at = 0
    for match in _MARK.finditer(text):
        if match.start() > at:
            yield text[at : match.start()], None
        if match.group(1) is not None:
            yield match.group(1), "strong"
        else:
            yield match.group(2), "link"
        at = match.end()
    if at < len(text):
        yield text[at:], None


class ChatWindow:
    def __init__(self, parent: tk.Misc) -> None:
        self.pet = pet_module.load()
        # **대화는 화면이 아니라 기계가 갖는다** (Phase 15). 폰이 자기 대화를
        # 따로 가지면 그건 다른 펫이다. 승인 창은 이 창이 띄운다 — 열려
        # 있는 데스크톱이 원격 변경의 승인자다.
        self.session = conversation.session(approve=self._approve)
        # 펫이 녹화 패널을 연다. 세션은 살아 있는 창을 모르므로 여기서 준다.
        self.session.show_recorder = self.open_recorder
        self.session.pet = self.pet
        self._answers: Queue = Queue()
        self._busy = False
        #: 링크 태그 → 진짜 주소.
        #:
        #: 카드의 제목은 **보이는 글자와 주소가 다르다.** 전에는 눌린 범위의
        #: 글자를 그대로 열어서 "목향" 이 주소로 갔다. 브라우저 주소창에
        #: `목향/` 이 찍혔다. 답변 본문의 링크는 글자가 곧 주소라 우연히
        #: 맞았을 뿐이다.
        self._urls: dict[str, str] = {}
        #: 지금 도는 녹화기. **팝업이 아니라 대화창 안에서 다룬다** —
        #: 창이 하나 더 뜨면 그때부터 사용자는 두 곳을 본다 (마스터 2026-08-12).
        self._recorder_obj = None

        self.top = tk.Toplevel(parent)
        self.top.title("대화")
        self.top.geometry("560x520")
        self._build()
        self._pump()

        if not self.pet.created:
            self._ask_name()

    # ── 화면 ────────────────────────────────────────────────────

    def _build(self) -> None:
        frame = ttk.Frame(self.top, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        top_row = ttk.Frame(frame)
        top_row.pack(fill=tk.X)
        self.name_label = ttk.Label(top_row, text="", font=("", 10, "bold"))
        self.name_label.pack(side=tk.LEFT)
        ttk.Button(top_row, text="이름·말투…", command=self._ask_name).pack(
            side=tk.RIGHT
        )
        # **녹화는 대화창에 있다** (마스터 요청 2026-08-11). 실행 화면이
        # 아니라 여기인 이유: 대화하다 "이거 다시 알려줄게" 가 나오는
        # 자리가 여기고, 펫이 직접 이 창을 연다.
        ttk.Button(top_row, text="녹화…", command=self._record).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        # 폰에서 같은 펫을 만난다. **누른 동안만 서버가 산다.**
        ttk.Button(top_row, text="기기 연결…", command=self._pair).pack(
            side=tk.RIGHT, padx=(0, 6)
        )
        # 기억을 지우는 버튼이 보이는 자리에 있어야 한다. 무엇을 기억하는지
        # 사용자가 보고 지울 수 있는 것이 이 단계의 약속이다.
        ttk.Button(top_row, text="대화 지우기", command=self._forget).pack(
            side=tk.RIGHT, padx=(0, 6)
        )

        self.log = tk.Text(frame, wrap=tk.WORD, state=tk.DISABLED, height=20)
        self.log.pack(fill=tk.BOTH, expand=True, pady=(8, 8))
        self.log.tag_configure("me", foreground="#1a5fb4")
        self.log.tag_configure("pet", foreground="#26a269")
        self.log.tag_configure("problem", foreground="#c01c28")
        # 근거 줄. 눈에 띄되 답을 가리지 않게 흐리게 둔다.
        self.log.tag_configure("source", foreground="#77767b")
        self.log.tag_configure("strong", font=("", 9, "bold"))
        # 카드. 항목마다 제목이 링크이고 출처·시각이 그 아래 붙는다.
        self.log.tag_configure("card", font=("", 9, "bold"), foreground="#1c71d8")
        self.log.tag_configure("cardinfo", foreground="#77767b")
        self.log.tag_configure("link", foreground="#1c71d8", underline=True)
        self.log.tag_bind("link", "<Button-1>", self._open_link)
        self.log.tag_bind(
            "link", "<Enter>", lambda _e: self.log.config(cursor="hand2")
        )
        self.log.tag_bind("link", "<Leave>", lambda _e: self.log.config(cursor=""))

        # **고르는 것은 대화창 안에서 한다** (마스터 2026-08-12).
        # 팝업을 띄우면 창이 하나 더 생기고, 그때부터 사용자는 두 곳을 본다.
        self.choices = ttk.Frame(frame)
        self.choices.pack(fill=tk.X, pady=(0, 6))
        # **개수는 라벨에 쓴다. 버튼 글자에 쓰지 않는다.**
        # 버튼 글자에 쓰면 0.5초마다 버튼을 지우고 다시 만들게 되고,
        # 누르는 순간 사라진 버튼은 클릭이 안 먹는다 (마스터 2026-08-12).
        self.status = ttk.Label(frame, text="")
        self.status.pack(fill=tk.X)

        bottom = ttk.Frame(frame)
        bottom.pack(fill=tk.X)
        self.entry = ttk.Entry(bottom)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry.bind("<Return>", lambda _event: self._send())
        self.send_btn = ttk.Button(bottom, text="보내기", command=self._send)
        self.send_btn.pack(side=tk.LEFT, padx=(6, 0))

        self._refresh_name()
        self.entry.focus_set()

    def _refresh_name(self) -> None:
        if self.pet.created:
            self.name_label.config(text=f"{self.pet.name} · {self.pet.tone}")
        else:
            self.name_label.config(text="아직 이름이 없습니다")

    def _append(self, who: str, text: str, tag: str) -> None:
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, f"{who}  ", tag)
        if tag == "pet":
            # 굵게와 주소만 그린다. **룬이 tkinter 의 한계를 알고 별표를
            # 지울 일이 아니다** — 그건 표현 계층의 일이고, 폰 화면이 생기면
            # 거기서 다시 그린다 (codex).
            for piece, kind in split(text):
                self.log.insert(tk.END, piece, kind or ())
        else:
            self.log.insert(tk.END, text)
        self.log.insert(tk.END, "\n\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _open_link(self, event) -> None:
        import webbrowser

        index = self.log.index(f"@{event.x},{event.y}")
        # 카드면 태그가 주소를 안다. 본문 링크면 글자가 곧 주소다.
        for tag in self.log.tag_names(index):
            if tag in self._urls:
                webbrowser.open(self._urls[tag])
                return
        span = self.log.tag_prevrange("link", f"{index}+1c")
        if span:
            webbrowser.open(self.log.get(*span))

    # ── 동작 ────────────────────────────────────────────────────

    def _ask_name(self) -> None:
        NameDialog(self.top, self.pet, on_save=self._named)

    def _named(self, pet: Pet) -> None:
        self.pet = pet
        self.session.pet = pet
        pet_module.save(pet)
        self._refresh_name()

    def _approve(self, title: str, body: str) -> bool:
        """사람에게 묻는다. **워커 스레드에서 불린다.**

        tkinter 를 다른 스레드에서 만지면 안 되지만 `messagebox` 는 모달로
        돌고 여기서 답을 기다려야 한다 — 대화 흐름이 승인 결과에 달렸다.
        `after_idle` 로 UI 스레드에 넘기고 결과를 받는다.

        **통로는 하나다.** 설정 변경도 녹화 저장도 같은 모양으로 묻는다 —
        도구마다 다른 인자로 부르면 하나 붙일 때마다 여기가 깨진다. 실제로
        녹화 도구를 붙이며 그럴 뻔했다.
        """
        import queue

        answer: queue.Queue = queue.Queue(maxsize=1)

        def ask() -> None:
            answer.put(messagebox.askyesno(title, body, parent=self.top))

        self.top.after_idle(ask)
        try:
            return bool(answer.get(timeout=120))
        except queue.Empty:
            return False

    def _ask_choice(self, options: list) -> None:
        """대화창 안에 고를 것을 그린다. `options` 는 `(글자, 함수)` 목록.

        **모델을 안 거친다.** 버튼은 누른 즉시 동작한다 — 한 번 더 LLM 을
        돌리면 느리고, 무엇보다 모델이 또 요약한다.
        """
        for child in self.choices.winfo_children():
            child.destroy()
        for label, action in options:
            ttk.Button(self.choices, text=label, command=action).pack(
                side=tk.LEFT, padx=(0, 6)
            )

    def _clear_choices(self) -> None:
        for child in self.choices.winfo_children():
            child.destroy()

    # ── 녹화 (대화창 안에서) ────────────────────────────────────

    def open_recorder(self, recorder_obj) -> None:
        """펫이 부른다. **워커 스레드에서 오므로 UI 스레드로 넘긴다.**"""
        def begin() -> None:
            # 화면이 들고 있던 옛 녹화기를 먼저 정리한다. 펫이 새로 시작한
            # 것과 둘이 남으면 어느 것을 저장하는지가 운에 달린다.
            if self._recorder_obj is not None and self._recorder_obj is not recorder_obj:
                tool = getattr(self.session, "_recorder", None)
                if tool is not None and hasattr(tool, "finish_recorder"):
                    tool.finish_recorder(self._recorder_obj)
                self._done()
            self._recording(recorder_obj)

        self.top.after_idle(begin)

    def _record(self) -> None:
        """`녹화…` 버튼. 펫을 안 거치고 바로 시작한다."""
        from vader.core import recorder as recorder_module

        # **돌던 것을 반드시 멈춘다.** 안 멈추면 옛 녹화기의 스레드가 계속
        # 클릭을 담고, 두 개가 같은 화면을 보며 서로 다른 것을 저장한다.
        self._done()
        made = recorder_module.Recorder()
        made.start()
        self._recording(made)

    def _recording(self, made) -> None:
        """녹화 중. **버튼 하나뿐이다** — 끝났다고 말하는 것."""
        self._recorder_obj = made
        # **회차 번호.** 연달아 녹화하면 폴링 사슬이 겹친다. 번호가 다르면
        # 옛 사슬은 조용히 죽는다 — 안 그러면 셋이 같이 돌며 버튼을
        # 지웠다 만들었다 한다 (마스터 2026-08-12).
        self._take = getattr(self, "_take", 0) + 1
        self._append(
            "",
            "녹화 중입니다. 평소대로 눌러 보여 주세요. "
            "글자 키는 기록하지 않습니다 (방향키·Enter 는 기록합니다).",
            "source",
        )
        self._reviewing = False
        self._ask_choice([("녹화 끝", self._recorded)])
        self._watch(self._take)

    def _watch(self, take: int) -> None:
        """잡히는 개수를 보여 준다. **안 보이면 다들 다시 누른다.**

        개수는 **라벨**에 쓴다. 버튼 글자에 쓰면 매 틱마다 버튼을 다시
        만들어야 하고, 그러면 누르는 순간 사라진다.
        """
        if take != getattr(self, "_take", 0) or not self.top.winfo_exists():
            return
        made = self._recorder_obj
        if made is None:
            self.status.config(text="")
            return
        if made.running:
            self.status.config(text=f"녹화 중 · {len(made.steps)}단계")
            self.top.after(500, lambda: self._watch(take))

    def _recorded(self) -> None:
        """멈추고 **잡은 것을 대화에 그대로 적는다.**

        모델에게 맡겼더니 "이대로 저장하시겠어요?" 한 줄로 요약했다
        (마스터 실측). 목록은 화면이 그린다.
        """
        made = self._recorder_obj
        if made is None:
            return
        made.stop()
        lines = [f"{len(made.steps)}단계를 잡았습니다."]
        for index, step in enumerate(made.steps, start=1):
            mark = "" if step.findable() else "   ⚠ 좌표로만 찾음"
            lines.append(f"{index}. {step.label()}{mark}")
        # **버튼으로는 넷까지다.** 나머지는 **쳐서** 뺀다 — 마스터가 15번을
        # 빼려다 "그건 제가 알 수 없습니다" 를 들었다 (2026-08-19). 녹화를
        # 다듬지 못하면 쓸 수 없는 녹화가 그대로 저장된다.
        if len(made.steps) > MAX_DROP_BUTTONS:
            lines.append("")
            lines.append(f"{MAX_DROP_BUTTONS + 1}번부터는 번호를 쳐서 빼세요 — 예: 15번 빼줘")
        self._append("", "\n".join(lines), "source")
        self._reviewing = True

        options = [("이대로 저장", self._save_recording)]
        # 사지선답이라 줄이 많으면 다 못 그린다. 앞의 넷만 버튼으로 준다.
        for index in range(min(MAX_DROP_BUTTONS, len(made.steps))):
            options.append(
                (f"{index + 1}번 빼기", lambda i=index: self._drop_step(i))
            )
        options.append(("버리기", self._done))
        self._ask_choice(options)

    def _drop_step(self, index: int) -> None:
        made = self._recorder_obj
        if made is None:
            return
        if not 0 <= index < len(made.steps):
            # **조용히 무시하지 않는다.** 뺐다고 믿은 채로 저장하게 된다.
            self._append("", f"{index + 1}번은 없습니다. 1~{len(made.steps)}번입니다.", "source")
            return
        dropped = made.steps.pop(index)
        self._append("", f"{index + 1}번을 뺐습니다: {dropped.label()}", "source")
        self._recorded()

    def _save_recording(self) -> None:
        self._reviewing = False
        from vader.core import recorder as recorder_module

        made = self._recorder_obj
        if made is None or not made.steps:
            # **말없이 돌아가지 않는다.** 눌렀는데 아무 일도 안 일어나는
            # 것이 "적용이 안 된다" 로 보인다 (마스터 2026-08-12).
            self._append("", "저장할 녹화가 없습니다. 다시 녹화해 주세요.", "source")
            self._done()
            return
        name = recorder_module.PURPOSES[0][0]
        purpose = recorder_module.PURPOSES[0][1]
        recorder_module.save(name, made.steps)
        self._append(
            "",
            f"'{name}' 으로 저장했습니다 — {purpose}. "
            "**다음 실행부터 이 순서로 누릅니다.**",
            "source",
        )
        # **도구에게 끝났다고 알린다** (codex). 화면은 녹화기를 빌릴 뿐이고
        # 회차 상태는 도구가 소유한다. 안 알리면 펫이 나중에 같은 것을 또
        # 저장하려 들거나 "저장할 녹화가 없습니다" 를 실패처럼 말한다.
        tool = getattr(self.session, "_recorder", None)
        if tool is not None and hasattr(tool, "finish_recorder"):
            tool.finish_recorder(made, saved_as=name)
        self._done()

    def _done(self) -> None:
        made = self._recorder_obj
        if made is not None:
            made.stop()
        self._recorder_obj = None
        # 회차를 넘겨 옛 폴링 사슬을 끊는다.
        self._take = getattr(self, "_take", 0) + 1
        self.status.config(text="")
        self._clear_choices()

    def _pair(self) -> None:
        PairDialog(self.top, approve=self._approve)

    def _forget(self) -> None:
        conversation.forget()
        self._urls.clear()
        self.log.config(state=tk.NORMAL)
        self.log.delete("1.0", tk.END)
        self.log.config(state=tk.DISABLED)

    #: 줄 번호만 적힌 입력. `15`, `15번`, `15번 빼줘` 를 모두 받는다.
    _DROP = re.compile(r"\s*(\d+)\s*번?\s*(?:빼\S*|지워\S*|삭제\S*|제거\S*)?\s*\Z")

    def _send(self) -> None:
        text = self.entry.get().strip()
        if not text or self._busy:
            return
        # **녹화를 검토하는 중이면 모델을 안 거친다.** 버튼과 같은 규칙이다.
        # 거치면 "그건 제가 알 수 없습니다" 가 나온다 — 실제로 그랬다.
        if getattr(self, "_reviewing", False):
            match = self._DROP.match(text)
            if match:
                self.entry.delete(0, tk.END)
                self._append("나", text, "me")
                self._drop_step(int(match.group(1)) - 1)
                return
        if not self.pet.created:
            messagebox.showinfo(
                "이름이 먼저입니다",
                "이름을 지어 주세요. 이름이 있어야 대화를 시작합니다.",
                parent=self.top,
            )
            self._ask_name()
            return

        self.entry.delete(0, tk.END)
        self._append("나", text, "me")
        self._set_busy(True)
        threading.Thread(target=self._ask, args=(text,), daemon=True).start()

    def _ask(self, text: str) -> None:
        """워커 스레드. **여기서 tkinter 를 만지지 않는다.**"""
        try:
            self._answers.put(("ok", conversation.say(text)))
        except Exception as exc:
            self._answers.put(("problem", str(exc)))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        self.send_btn.config(state=state, text="…" if busy else "보내기")
        self.entry.config(state=state)

    def _pump(self) -> None:
        # 예외가 나도 다음 폴링을 반드시 다시 건다. `ui.py::_pump` 에서
        # 배운 것이다 — 한 번 죽으면 화면이 영영 멈춘다.
        try:
            while True:
                kind, text = self._answers.get_nowait()
                self._set_busy(False)
                if kind == "ok":
                    self._append(self.pet.name, text, "pet")
                else:
                    self._append("문제", text, "problem")
        except Empty:
            pass
        finally:
            if self.top.winfo_exists():
                self.top.after(POLL_MS, self._pump)


class NameDialog:
    """이름과 말투. **자유 입력은 이름뿐이다** — 말투는 프리셋이다.

    말투를 자유 입력으로 두면 시스템 프롬프트에 무엇이든 넣는 통로가 된다.
    """

    def __init__(self, parent: tk.Misc, pet: Pet, *, on_save) -> None:
        self._pet = pet
        self._on_save = on_save
        self.top = tk.Toplevel(parent)
        self.top.title("펫")
        self.top.transient(parent)
        self.top.grab_set()

        frame = ttk.Frame(self.top, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="이름").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.name = tk.StringVar(value=pet.name)
        ttk.Entry(frame, textvariable=self.name, width=24).grid(
            row=0, column=1, pady=4, padx=(8, 0)
        )

        ttk.Label(frame, text="말투").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.tone = tk.StringVar(value=pet.tone)
        ttk.Combobox(
            frame, textvariable=self.tone, values=list(TONES),
            state="readonly", width=22,
        ).grid(row=1, column=1, pady=4, padx=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky=tk.E, pady=(10, 0))
        ttk.Button(buttons, text="취소", command=self.top.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="저장", command=self._save).pack(
            side=tk.RIGHT, padx=(0, 8)
        )

    def _save(self) -> None:
        name = self.name.get().strip()
        if not name:
            messagebox.showwarning(
                "이름이 필요합니다", "이름을 지어 주세요.", parent=self.top
            )
            return
        self._pet.name = name
        self._pet.tone = self.tone.get() or self._pet.tone
        self._on_save(self._pet)
        self.top.destroy()


class PairDialog:
    """폰에서 같은 펫을 만난다. Phase 15.

    **이 창이 열려 있는 동안만 서버가 산다.** 사내망에 포트를 상시로 열어
    두는 것과 필요할 때만 여는 것은 다른 이야기다. 닫으면 코드도 같이
    죽는다 — 창을 닫았는데 코드가 도는 것이 더 이상하다.
    """

    def __init__(self, parent: tk.Misc, *, approve=None) -> None:
        from vader.core import pairing
        from vader.core.server import PetServer

        self._pairing = pairing
        self.server = PetServer(approve=approve)

        self.top = tk.Toplevel(parent)
        self.top.title("기기 연결")
        self.top.transient(parent)
        self.top.protocol("WM_DELETE_WINDOW", self._close)

        frame = ttk.Frame(self.top, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            frame,
            text=(
                "같은 와이파이의 폰에서 아래 주소를 열고 코드를 넣으세요.\n"
                "이 창을 닫으면 연결 통로도 닫힙니다."
            ),
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(0, 8))

        self.url_label = ttk.Label(frame, text="", font=("", 11, "bold"))
        self.url_label.pack(anchor=tk.W)
        self.code_label = ttk.Label(frame, text="", font=("", 22, "bold"))
        self.code_label.pack(anchor=tk.W, pady=(6, 0))
        self.left_label = ttk.Label(frame, text="")
        self.left_label.pack(anchor=tk.W, pady=(0, 8))

        ttk.Label(frame, text="붙어 있는 기기").pack(anchor=tk.W)
        self.devices = tk.Listbox(frame, height=4)
        self.devices.pack(fill=tk.X, pady=(2, 6))

        row = ttk.Frame(frame)
        row.pack(fill=tk.X)
        ttk.Button(row, text="새 코드", command=self._new_code).pack(side=tk.LEFT)
        # 전화기를 잃어버렸을 때 할 일이 "기다리기" 여서는 안 된다.
        ttk.Button(row, text="선택한 기기 끊기", command=self._revoke).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Button(row, text="닫기", command=self._close).pack(side=tk.RIGHT)

        self.server.start()
        self.url_label.config(text=self.server.url())
        self._new_code()
        self._list()
        self._tick()

    def _new_code(self) -> None:
        pending = self.server.show_code()
        self.code_label.config(text=f"{pending.code[:3]} {pending.code[3:]}")

    def _list(self) -> None:
        self.devices.delete(0, tk.END)
        self._devices = self._pairing.devices()
        for device in self._devices:
            self.devices.insert(tk.END, f"{device.name}  ({device.paired_at})")

    def _revoke(self) -> None:
        picked = self.devices.curselection()
        if not picked:
            return
        self._pairing.revoke(self._devices[picked[0]].token)
        self._list()

    def _tick(self) -> None:
        """남은 시간을 센다. **코드가 죽으면 화면도 죽은 것을 보여준다.**"""
        if not self.top.winfo_exists():
            return
        pending = self.server.pending
        if pending is None:
            self.code_label.config(text="—")
            self.left_label.config(text="코드가 쓰였습니다. 새로 붙이려면 `새 코드`.")
            self._list()
        elif pending.alive:
            self.left_label.config(text=f"{pending.seconds_left}초 남음")
        else:
            self.code_label.config(text="—")
            self.left_label.config(text="코드가 만료됐습니다.")
        self.top.after(1000, self._tick)

    def _close(self) -> None:
        self.server.stop()
        self.top.destroy()
