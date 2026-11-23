"""처리되지 않은 코어 예외를 로컬 진단 파일로 남긴다.

룬 예외는 JobRunner가 ``Failure``로 바꾼다. 이 모듈은 그 바깥에서 앱 자체가
끝나는 경우만 다룬다. 어디로도 전송하지 않으며, 환자명이나 문서명이 섞일 수
있는 예외 메시지와 외부 경로는 기록하지 않는다.
"""
from __future__ import annotations

import os
import platform
import sys
import tempfile
import threading
import traceback
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType

from vader import __version__
from vader.core import config


Now = Callable[[], datetime]


class CrashReporter:
    """개인정보를 줄인 크래시 파일 저장소."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        retention_days: int = 30,
        now: Now = datetime.now,
    ) -> None:
        self.root = (
            Path(root) if root is not None else config.HOME / "crash"   # 기기 층 — 로그인 전 오류도 남긴다
        )
        self.retention_days = retention_days
        self._now = now

    def write(
        self,
        exc_type: type[BaseException],
        exc_value: BaseException,
        tb: TracebackType | None,
    ) -> Path | None:
        """크래시 파일을 원자적으로 쓰고 경로를 돌려준다.

        보고 자체가 실패해 원래 예외를 가리면 안 되므로 저장 오류는 ``None``으로
        줄인다. ``exc_value``는 시그니처 호환을 위해 받지만 메시지는 기록하지
        않는다 — 사용자 파일 경로와 문서명이 들어갈 수 있기 때문이다.
        """

        del exc_value
        try:
            now = self._now()
            self.root.mkdir(parents=True, exist_ok=True)
            self.prune(now=now)
            target = self.root / (
                f"crash_{now:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.txt"
            )
            content = self._format(exc_type, tb, now)

            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.root,
                    prefix=f".{target.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            except BaseException:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                raise
            return target
        except BaseException:
            return None

    def prune(self, *, now: datetime | None = None) -> int:
        """보존 기간보다 오래된 크래시 파일을 지운다."""

        if not self.root.exists():
            return 0
        cutoff = (now or self._now()) - timedelta(days=self.retention_days)
        removed = 0
        for path in self.root.glob("crash_*.txt"):
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime)
                if modified < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def _format(
        self,
        exc_type: type[BaseException],
        tb: TracebackType | None,
        now: datetime,
    ) -> str:
        lines = [
            "VaderAI crash report",
            f"recorded_at: {now.isoformat(timespec='seconds')}",
            f"vader_version: {__version__}",
            f"python_version: {platform.python_version()}",
            (
                "platform: "
                f"{platform.system()} {platform.release()} {platform.machine()}"
            ),
            (
                "exception_type: "
                f"{exc_type.__module__}.{exc_type.__qualname__}"
            ),
            "exception_message: <omitted for privacy>",
            "traceback:",
        ]
        if tb is None:
            lines.append("  <unavailable>")
        else:
            for frame, lineno in traceback.walk_tb(tb):
                lines.append(
                    f'  File "{_safe_frame_name(frame.f_code.co_filename)}", '
                    f"line {lineno}, in {frame.f_code.co_name}"
                )
        return "\n".join(lines) + "\n"


def _safe_frame_name(filename: str) -> str:
    """소스 파일명만 남기고 사용자 문서처럼 보이는 경로는 감춘다."""

    name = Path(filename).name
    if name.lower().endswith((".py", ".pyw", ".pyc")):
        return name
    return "<external>"


def install_exception_hooks(
    reporter: CrashReporter | None = None,
) -> CrashReporter:
    """메인·백그라운드 스레드의 미처리 예외를 기록한다."""

    subject = reporter or CrashReporter()
    previous_sys = sys.excepthook
    previous_thread = threading.excepthook

    def sys_hook(exc_type, exc_value, tb):
        subject.write(exc_type, exc_value, tb)
        previous_sys(exc_type, exc_value, tb)

    def thread_hook(args):
        subject.write(args.exc_type, args.exc_value, args.exc_traceback)
        previous_thread(args)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook
    return subject


__all__ = ["CrashReporter", "install_exception_hooks"]
