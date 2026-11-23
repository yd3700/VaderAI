"""코어 크래시 보고의 저장·개인정보 경계."""
from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta

import pytest

from vader import __version__
from vader.core.crash import CrashReporter


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("크래시 보고는 네트워크를 쓰면 안 됩니다")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def test_report_omits_exception_message_and_sensitive_paths(tmp_path):
    reporter = CrashReporter(
        tmp_path / "crash",
        now=lambda: datetime(2026, 7, 31, 9, 10, 11),
    )
    secret_name = "김환자_19012739_태영메디칼.pdf"
    secret_path = rf"C:\Users\Admin\scans\{secret_name}"

    try:
        code = compile(
            f"raise FileNotFoundError({secret_path!r})",
            secret_path,
            "exec",
        )
        exec(code, {})
    except FileNotFoundError as exc:
        path = reporter.write(type(exc), exc, exc.__traceback__)

    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "VaderAI crash report" in text
    assert f"vader_version: {__version__}" in text
    assert "exception_type: builtins.FileNotFoundError" in text
    assert "exception_message: <omitted for privacy>" in text
    assert secret_name not in text
    assert "김환자" not in text
    assert "19012739" not in text
    assert "태영메디칼" not in text
    assert r"C:\Users\Admin" not in text
    assert 'File "<external>"' in text


def test_source_traceback_keeps_only_python_filename(tmp_path):
    reporter = CrashReporter(tmp_path / "crash")

    try:
        raise RuntimeError("message is deliberately omitted")
    except RuntimeError as exc:
        path = reporter.write(type(exc), exc, exc.__traceback__)

    text = path.read_text(encoding="utf-8")
    assert 'File "test_crash.py"' in text
    assert str(tmp_path) not in text
    assert "message is deliberately omitted" not in text


def test_prune_removes_only_expired_crash_files(tmp_path):
    now = datetime(2026, 7, 31, 12, 0, 0)
    reporter = CrashReporter(
        tmp_path / "crash",
        retention_days=30,
        now=lambda: now,
    )
    reporter.root.mkdir()
    old = reporter.root / "crash_old.txt"
    recent = reporter.root / "crash_recent.txt"
    old.write_text("old", encoding="utf-8")
    recent.write_text("recent", encoding="utf-8")
    old_time = (now - timedelta(days=31)).timestamp()
    recent_time = (now - timedelta(days=2)).timestamp()
    os.utime(old, (old_time, old_time))
    os.utime(recent, (recent_time, recent_time))

    assert reporter.prune() == 1
    assert not old.exists()
    assert recent.exists()


def test_report_failure_never_masks_the_original_exception(tmp_path):
    unusable = tmp_path / "not-a-directory"
    unusable.write_text("file", encoding="utf-8")
    reporter = CrashReporter(unusable)

    assert reporter.write(ValueError, ValueError("secret"), None) is None


def test_application_entrypoint_installs_hooks_and_closes_the_classic_screen(monkeypatch):
    """예전 관리 화면은 로그인·잠금이 없어 닫는다 (2단계). 훅은 그 전에 깔린다."""
    import vader.__main__ as entrypoint

    calls = []
    monkeypatch.setattr(entrypoint, "install_exception_hooks", lambda: calls.append("hooks"))
    with pytest.raises(SystemExit):
        entrypoint.main(['--classic'])
    assert calls == ["hooks"]
