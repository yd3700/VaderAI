"""역할에 제공자를 고르는 두 방식 — 수동과 자동.

목적은 **API 절약**이다. 한도에 닿은 역할만 다른 제공자로 옮기고, 나머지는
건드리지 않는다. 그래서 이 파일이 가장 조심하는 것은 "안 옮겨야 할 때
옮기지 않는가" 다.
"""
from __future__ import annotations

import json
import socket
from datetime import date

import pytest

from vader.core import config
from vader.core.ocr import OcrProviderUnavailable, candidates, resolve
from vader.core.usage import Call, UsageLedger, now


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)


@pytest.fixture(autouse=True)
def every_provider_is_available(monkeypatch):
    """키와 패키지 유무는 여기서 볼 것이 아니다. 따로 보는 테스트가 있다."""
    for name in ("gemini", "claude"):
        monkeypatch.setattr(
            f"vader.core.ocr.{name}.{name.capitalize()}Provider.is_available",
            lambda self: True,
        )


def settings(path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")
    config.invalidate()


def used(ledger: UsageLedger, provider: str, times: int) -> None:
    for _ in range(times):
        ledger.record(
            Call(at=now(), provider=provider, model=None, role="extract", ok=True)
        )


def test_manual_does_not_move_even_at_the_limit(isolated_settings, tmp_path):
    """못 박은 것을 코어가 말없이 옮기면 그게 조용한 데이터 변경이다."""
    settings(
        isolated_settings,
        {
            "api": {
                "roles": {"extract": {"provider": "gemini"}},
                "providers": {"gemini": {"daily_limit": 3}},
            }
        },
    )
    ledger = UsageLedger(tmp_path)
    used(ledger, "gemini", 99)

    chosen = resolve("extract", ledger=ledger)

    assert chosen.provider == "gemini"
    assert chosen.automatic is False


def test_automatic_takes_the_first_one_with_room(isolated_settings, tmp_path):
    settings(
        isolated_settings,
        {"api": {"roles": {"extract": {"providers": ["gemini", "claude"]}}}},
    )
    ledger = UsageLedger(tmp_path)

    chosen = resolve("extract", ledger=ledger)

    assert (chosen.provider, chosen.automatic) == ("gemini", True)


def test_automatic_moves_on_when_the_first_is_spent(isolated_settings, tmp_path):
    """이 한 줄이 이 기능의 목적이다 — 한도에 닿은 역할만 옮긴다."""
    settings(
        isolated_settings,
        {
            "api": {
                "roles": {"extract": {"providers": ["gemini", "claude"]}},
                "providers": {"gemini": {"daily_limit": 5}},
            }
        },
    )
    ledger = UsageLedger(tmp_path)
    used(ledger, "gemini", 5)

    assert resolve("extract", ledger=ledger).provider == "claude"


def test_another_providers_usage_does_not_push_this_one_out(
    isolated_settings, tmp_path
):
    """한도는 제공자마다 따로다. 원장이 제공자를 안 가르면 여기서 틀린다."""
    settings(
        isolated_settings,
        {
            "api": {
                "roles": {"extract": {"providers": ["gemini", "claude"]}},
                "providers": {"gemini": {"daily_limit": 5}},
            }
        },
    )
    ledger = UsageLedger(tmp_path)
    used(ledger, "claude", 100)

    assert resolve("extract", ledger=ledger).provider == "gemini"


def test_cache_hits_do_not_push_a_provider_out(isolated_settings, tmp_path):
    """캐시 적중은 할당량을 안 쓴다. 그걸 세면 멀쩡한 제공자를 버린다."""
    settings(
        isolated_settings,
        {
            "api": {
                "roles": {"extract": {"providers": ["gemini", "claude"]}},
                "providers": {"gemini": {"daily_limit": 2}},
            }
        },
    )
    ledger = UsageLedger(tmp_path)
    for _ in range(50):
        ledger.record(
            Call(
                at=now(),
                provider="gemini",
                model=None,
                role="extract",
                ok=True,
                cached=True,
            )
        )

    assert resolve("extract", ledger=ledger).provider == "gemini"


def test_running_out_fails_instead_of_continuing_quietly(isolated_settings, tmp_path):
    """그 지점에서 사람이 뭔가 해야 한다. 성공한 척하면 아무도 안 한다."""
    settings(
        isolated_settings,
        {
            "api": {
                "roles": {"extract": {"providers": ["gemini", "claude"]}},
                "providers": {
                    "gemini": {"daily_limit": 1},
                    "claude": {"daily_limit": 1},
                },
            }
        },
    )
    ledger = UsageLedger(tmp_path)
    used(ledger, "gemini", 1)
    used(ledger, "claude", 1)

    with pytest.raises(OcrProviderUnavailable, match="한도"):
        resolve("extract", ledger=ledger)


def test_both_kinds_of_assignment_at_once_is_an_error(isolated_settings, tmp_path):
    """어느 쪽이 이기는지 조용히 정하면 설정이 무시된 줄 모른다."""
    settings(
        isolated_settings,
        {
            "api": {
                "roles": {
                    "extract": {"provider": "gemini", "providers": ["claude"]}
                }
            }
        },
    )

    with pytest.raises(ValueError, match="하나만"):
        resolve("extract", ledger=UsageLedger(tmp_path))


def test_no_assignment_still_rides_the_global_provider(isolated_settings, tmp_path):
    settings(isolated_settings, {"api": {"ocr_provider": "claude"}})

    chosen = resolve("extract", ledger=UsageLedger(tmp_path))

    assert (chosen.provider, chosen.automatic) == ("claude", False)


def test_an_unusable_provider_is_skipped_with_a_reason(
    isolated_settings, tmp_path, monkeypatch
):
    settings(
        isolated_settings,
        {"api": {"roles": {"extract": {"providers": ["gemini", "claude"]}}}},
    )
    monkeypatch.setattr(
        "vader.core.ocr.gemini.GeminiProvider.is_available", lambda self: False
    )
    ledger = UsageLedger(tmp_path)

    assert resolve("extract", ledger=ledger).provider == "claude"
    looked = candidates("extract", ledger=ledger)
    assert [(c.provider, c.usable) for c in looked] == [
        ("gemini", False),
        ("claude", True),
    ]
    assert "키" in looked[0].blocked


def test_the_screen_sees_the_same_numbers_the_choice_used(
    isolated_settings, tmp_path
):
    """배정 화면이 다른 숫자를 보여주면 사용자가 왜 옮겨졌는지 못 읽는다."""
    settings(
        isolated_settings,
        {
            "api": {
                "roles": {"extract": {"providers": ["gemini", "claude"]}},
                "providers": {"gemini": {"daily_limit": 7}},
            }
        },
    )
    ledger = UsageLedger(tmp_path)
    used(ledger, "gemini", 7)

    chosen = resolve("extract", ledger=ledger)
    looked = candidates("extract", ledger=ledger)

    assert chosen.provider == "claude"
    assert (looked[0].calls_today, looked[0].daily_limit) == (7, 7)
    assert "7/7" in looked[0].blocked


# --- Job 하나 동안 안 바뀌는가 -----------------------------------------------


def test_one_job_keeps_one_provider_even_as_the_limit_falls(
    isolated_settings, tmp_path, monkeypatch
):
    """자동 배정이 작업 도중에 흔들리면 한 작업의 앞뒤가 다른 모델에서 나온다.

    제공자마다 OCR 이 다르게 나오므로 이건 가용성이 아니라 조용한 데이터
    변경이다. **문서 하나의 절반이 다른 모델에서 나오면 아무도 못 잡는다.**
    """
    import threading
    from queue import Queue

    from vader.core.checkpoint import CheckpointStore
    from vader.core.context import JobContext
    from vader.core.job import Job
    from vader.sdk import CheckpointKey

    settings(
        isolated_settings,
        {
            "api": {
                "roles": {"extract": {"providers": ["gemini", "claude"]}},
                "providers": {"gemini": {"daily_limit": 2}},
            }
        },
    )
    ledger = UsageLedger(tmp_path / "usage")
    workdir = tmp_path / "work"
    workdir.mkdir()
    ctx = JobContext(
        job=Job(rune_id="r", inputs=[], workdir=workdir, id="j"),
        events=Queue(),
        cancel=threading.Event(),
        params={},
        checkpoints=CheckpointStore(tmp_path / "checkpoints"),
        checkpoint_key=CheckpointKey(rune_id="r", input_identity="i"),
    )

    seen = []
    monkeypatch.setattr(
        "vader.core.ocr.extract",
        lambda path, prompt, schema, *, log, assignment: seen.append(
            assignment.provider
        ),
    )
    monkeypatch.setattr("vader.core.ocr.assign.UsageLedger", lambda directory: ledger)

    ctx.ocr(tmp_path / "a.pdf", "prompt", dict)
    used(ledger, "gemini", 99)   # 도중에 한도를 넘긴다
    ctx.ocr(tmp_path / "b.pdf", "prompt", dict)

    assert seen == ["gemini", "gemini"]


def test_the_next_job_re_reads_the_ledger(isolated_settings, tmp_path, monkeypatch):
    """붙잡는 것은 Job 안에서만이다. 다음 실행은 다시 판단해야 한다."""
    import threading
    from queue import Queue

    from vader.core.checkpoint import CheckpointStore
    from vader.core.context import JobContext
    from vader.core.job import Job
    from vader.sdk import CheckpointKey

    settings(
        isolated_settings,
        {
            "api": {
                "roles": {"extract": {"providers": ["gemini", "claude"]}},
                "providers": {"gemini": {"daily_limit": 2}},
            }
        },
    )
    ledger = UsageLedger(tmp_path / "usage")
    monkeypatch.setattr("vader.core.ocr.assign.UsageLedger", lambda directory: ledger)
    seen = []
    monkeypatch.setattr(
        "vader.core.ocr.extract",
        lambda path, prompt, schema, *, log, assignment: seen.append(
            assignment.provider
        ),
    )

    def fresh_job(name: str) -> JobContext:
        workdir = tmp_path / name
        workdir.mkdir()
        return JobContext(
            job=Job(rune_id="r", inputs=[], workdir=workdir, id=name),
            events=Queue(),
            cancel=threading.Event(),
            params={},
            checkpoints=CheckpointStore(tmp_path / "checkpoints"),
            checkpoint_key=CheckpointKey(rune_id="r", input_identity=name),
        )

    fresh_job("first").ocr(tmp_path / "a.pdf", "prompt", dict)
    used(ledger, "gemini", 99)
    fresh_job("second").ocr(tmp_path / "b.pdf", "prompt", dict)

    assert seen == ["gemini", "claude"]
