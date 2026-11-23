"""화면 자동화 smoke 측정 (3단계 3c) — 합성 앱에서 엔진·중개 구간만 잰다. pytest 가 모으지 않는다.

    python tools/bench_screen.py [회수=20]

- look: `screen.look` 한 번 (창 찾기·snapshot·창 주인 재확인·정제·게시).
- click: 준비 look 은 **재지 않는다**. 잰 구간은 propose → decide(승인 즉시) → 엔진 click → 결과.
  준비 look 이 실패하면 그 회는 skipped — 클릭을 안 했으니 표본에 넣지 않는다.
  사람이 카드를 보고 누르는 시간은 빠져 있다 — 사용자 체감 전체 지연이 아니다.
- 대상은 합성 앱의 '저장' 버튼(상태 글자만 바뀐다).
- 첫 회(cold — 앱이 뜬 걸 확인한 look 한 번 뒤의 첫 측정)는 따로 적는다. warm 의 n·ok·fail·timeout·skipped·분위수는
  같은 표본(첫 회 뒤)을 가리킨다. 분위수는 실행한 표본(ok+fail+timeout)의 경과로 nearest-rank, 표본이 없으면 null.
  20회는 초기 smoke 수치 — 성능 보증이 아니다.
- 설정·작업 폴더는 임시 폴더로 바꾼다(실제 ~/.vaderai 에 아무것도 안 남긴다).
"""
from __future__ import annotations

import json
import math
import platform
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vader.core import config, screen  # noqa: E402

TIMED_OUT = "제때 끝나지 않았어요"      # screen.run 의 시간 초과 문장


def isolate() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vader-bench-"))
    settings = tmp / "settings.json"
    settings.write_text(json.dumps({"processing": {"data_dir": str(tmp / "data")}}), encoding="utf-8")
    config.SETTINGS_PATH, config._cache, config.HOME = settings, None, tmp / "home"
    screen.gate.session_gen = lambda: "bench"
    screen.hold = nullcontext


def rank(xs: list[float], q: float) -> float | None:
    xs = sorted(xs)
    return xs[max(0, math.ceil(q * len(xs)) - 1)] if xs else None


def summarize(records: list[tuple[str, float]]) -> dict:
    """[(상태 ok|fail|timeout|skipped, 실행 구간 초)] — 첫 회는 cold, 나머지는 warm."""
    def ms(x):
        return None if x is None else round(x * 1000)
    warm = records[1:]
    ran = [t for st, t in warm if st != "skipped"]
    count = {k: sum(1 for st, _ in warm if st == k) for k in ("ok", "fail", "timeout", "skipped")}
    cold = {"status": records[0][0], "ms": None if records[0][0] == "skipped" else ms(records[0][1])} if records else None
    return {"cold": cold, "n": len(warm), **count, "executed_n": len(ran),
            "p50_ms": ms(rank(ran, .5)), "p95_ms": ms(rank(ran, .95))}


def _status(exc: screen.ScreenError) -> str:
    return "timeout" if TIMED_OUT in str(exc) else "fail"


def one_look() -> tuple[str, float]:
    began = time.perf_counter()
    try:
        ok = bool(screen.look("시험 화면")["count"])
        return ("ok" if ok else "fail"), time.perf_counter() - began
    except screen.ScreenError as exc:
        return _status(exc), time.perf_counter() - began


def one_click() -> tuple[str, float]:
    try:
        ref = {x["label"]: x["ref"] for x in screen.look("시험 화면")["nodes"]}["저장"]   # 준비 — 재지 않는다
    except (screen.ScreenError, KeyError):
        return "skipped", 0.0
    began = time.perf_counter()
    try:
        pid = screen.propose("시험 화면", ref, "click")["pending"]
        got = screen.decide(pid, screen.pending()["digest"], True)
    except screen.ScreenError as exc:
        return _status(exc), time.perf_counter() - began
    spent = time.perf_counter() - began
    if got["result"] == screen.DONE:
        return "ok", spent
    return ("timeout" if TIMED_OUT in got["message"] else "fail"), spent


def main(n: int) -> None:
    isolate()
    app = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(ROOT / "tests" / "fixtures" / "screen_app.ps1")], creationflags=screen.NO_WINDOW)
    try:
        for _ in range(40):
            try:
                screen.look("시험 화면")
                break
            except screen.ScreenError:
                time.sleep(.25)
        looks = summarize([one_look() for _ in range(n + 1)])
        clicks = summarize([one_click() for _ in range(n + 1)])
        print(json.dumps({"engine": screen.ENGINE_VERSION, "os": platform.platform(), "python": platform.python_version(),
                          "look": looks, "click_without_look": clicks}, ensure_ascii=False, indent=1))
    finally:
        app.kill()
        screen.sweep()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
