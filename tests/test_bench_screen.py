"""측정 도구의 집계 — 준비 실패는 표본에 안 넣고, n·실패·시간 초과·분위수가 같은 표본을 가리킨다 (codex1 3a r1 P2-1)."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("bench_screen", Path(__file__).parents[1] / "tools" / "bench_screen.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def test_all_ok():
    got = bench.summarize([("ok", .9)] + [("ok", .1 * i) for i in range(1, 21)])
    assert got == {"cold": {"status": "ok", "ms": 900}, "n": 20, "ok": 20, "fail": 0, "timeout": 0, "skipped": 0,
                   "executed_n": 20, "p50_ms": 1000, "p95_ms": 1900}


def test_cold_failure_stays_out_of_warm_counts():
    got = bench.summarize([("fail", 5.0), ("ok", .2), ("ok", .3)])
    assert got["cold"] == {"status": "fail", "ms": 5000} and got["fail"] == 0 and got["n"] == 2


def test_skipped_preparation_is_not_a_fast_sample():
    got = bench.summarize([("ok", .5), ("skipped", 0.0), ("skipped", 0.0), ("ok", .8)])
    assert got["skipped"] == 2 and got["p50_ms"] == 800 and got["p95_ms"] == 800


def test_timeouts_count_with_their_elapsed_time():
    got = bench.summarize([("ok", .5), ("timeout", 15.0), ("ok", .4)])
    assert got["timeout"] == 1 and got["p95_ms"] == 15000


def test_no_executed_samples_means_no_quantiles():
    got = bench.summarize([("skipped", 0.0)] + [("skipped", 0.0)] * 20)
    assert got["cold"] == {"status": "skipped", "ms": None} and got["p50_ms"] is None and got["p95_ms"] is None


@pytest.mark.parametrize("message,status", [("화면 자동화가 제때 끝나지 않았어요. 화면에서 결과를 확인해 주세요.", "timeout"),
                                            ("창이 바뀌었어요. 보내지 않았어요.", "fail")])
def test_click_outcome_is_classified(monkeypatch, message, status):
    s = bench.screen
    monkeypatch.setattr(s, "look", lambda key: {"nodes": [{"label": "저장", "ref": "@s1:e1"}]})
    monkeypatch.setattr(s, "propose", lambda *a: {"pending": "p"})
    monkeypatch.setattr(s, "pending", lambda: {"digest": "d"})
    monkeypatch.setattr(s, "decide", lambda *a: {"result": s.UNKNOWN, "message": message})
    assert bench.one_click()[0] == status
    monkeypatch.setattr(s, "look", lambda key: (_ for _ in ()).throw(s.ScreenError("x")))
    assert bench.one_click() == ("skipped", 0.0)
