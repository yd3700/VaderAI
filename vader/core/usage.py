"""API 사용량 원장. **실제로 나간 요청**을 한 줄씩 남긴다.

전에는 게이트웨이가 "오늘 만들어진 캐시 파일 수" 로 호출을 셌다. 그것이 세지
못하는 것이 셋이다.

- **재시도** — 한 번의 `extract` 가 과부하로 세 번 나가도 캐시 파일은 하나다
- **실패** — 끝내 실패하면 캐시 파일이 아예 안 생긴다. 그래도 할당량은 썼다
- **캐시 적중** — 파일을 안 만드니 안 세지만, 이건 반대로 **아낀 것**이라
  세어야 할 이유가 있다

그래서 캐시 파일이 아니라 호출을 센다. 하루에 파일 하나(`2026-08-05.jsonl`),
한 줄에 요청 하나다. JSONL 인 이유는 추가만 하면 되고 중간에 죽어도 앞줄이
멀쩡하기 때문이다.

이 원장이 Phase 8-A 의 역할×제공자 자동 배정과 Phase 10 의 대화 비용 통제가
읽을 바닥이다. 무엇을 얼마나 썼는지 모르면 어디로 옮길지도 정할 수 없다.

**보존은 무기한이다.** 하루 1400줄 × 150바이트 = 200KB, 1년에 73MB. 프롬프트나
문서 내용은 안 남는다 — 언제 어떤 제공자의 어떤 모델을 어떤 역할로 불렀는지만
남는다. 자동으로 안 지우는 것은 의도다. 이력이 지워지면 "왜 이 역할이 이
제공자에 붙어 있나" 를 나중에 설명할 수 없다.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

__all__ = ["Call", "UsageLedger"]


@dataclass(frozen=True)
class Call:
    """요청 하나. 게이트웨이가 아는 것만 담는다."""

    at: str
    provider: str
    model: str | None
    #: 지금은 `extract`(문서→값) 과 `infer`(텍스트 판단) 둘이다. Phase 8 이
    #: `chat`·`command` 를 더한다.
    role: str
    #: 쓸 수 있는 결과를 받았나. 스키마가 어긋난 응답은 통신이 성공해도
    #: **거짓이다** — 할당량은 썼고 얻은 것은 없다. 제공자가 던진 것이든
    #: 게이트웨이가 검증에서 걸러낸 것이든 같은 실패다.
    ok: bool
    #: 참이면 제공자에 나가지 않았다. 할당량을 안 쓰고, 비용도 없다.
    cached: bool = False
    error: str | None = None


#: **인스턴스가 아니라 모듈에 둔다.** 게이트웨이는 호출마다 새로 만들어지고
#: (`extract()` 가 `create_gateway` 를 매번 부른다) 그때마다 원장 객체도 새로
#: 생긴다. 락이 인스턴스에 있으면 서로 다른 락을 잡고 같은 파일에 쓴다 —
#: 잠근 것처럼 보이지만 아무것도 안 잠근다. 역할별 게이트웨이와 동시 룬
#: 실행이 이미 있어 가정이 아니라 실제 경로다.
#
# ponytail: 원장 전체에 락 하나. 한 줄 붙이는 동안만 잡으므로 경합이 없다.
# 여러 프로세스가 같은 폴더를 보면 이것도 부족하고, 그때는 파일 락이다.
_WRITE_LOCK = threading.Lock()


class UsageLedger:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def record(self, call: Call) -> bool:
        """한 줄 남긴다. **실패해도 던지지 않는다.**

        원장을 못 쓰는 것이 OCR 성공을 실패로 바꾸면 안 된다. 캐시 저장과
        같은 규칙이다. 다만 조용히 삼키지는 않는다 — 거짓을 돌려주고 부른
        쪽이 로그를 남긴다. 못 센 호출은 한도 경고를 **늦게** 울린다.
        """
        try:
            path = self._path(date.fromisoformat(call.at[:10]))
            line = json.dumps(asdict(call), ensure_ascii=False)
            with _WRITE_LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as stream:
                    # 앞줄이 줄바꿈 없이 끊겨 있으면(죽는 순간에 반쯤 쓰인 줄)
                    # 그대로 이어 붙는다. 그러면 깨진 줄이 **이 줄까지** 삼킨다.
                    if stream.tell() and not _ends_with_newline(path):
                        stream.write("\n")
                    stream.write(line + "\n")
            return True
        except Exception:
            return False

    def calls_on(self, day: date | None = None, *, provider: str | None = None) -> int:
        """그날 **제공자에 실제로 나간** 요청 수. 캐시 적중은 빼고 센다.

        `provider` 를 주면 그 제공자 것만 센다. 자동 배정이 "이 제공자가
        오늘 얼마나 남았나" 를 묻는 방식이다 — 한도는 제공자마다 따로다.
        """
        return sum(
            1
            for call in self.read(day)
            if not call.cached and (provider is None or call.provider == provider)
        )

    def read(self, day: date | None = None) -> list[Call]:
        """그날의 줄. **읽기 실패는 빈 목록이다.**

        `calls_on` 이 실제 호출 직전의 한도 경고에서 불린다. 여기서 던지면
        원장을 못 읽는 것이 **OCR 자체를 멈춘다** — 잠긴 파일, 깨진 인코딩,
        권한 하나가 업무를 세운다. 사용량을 모르는 것과 일을 못 하는 것 중
        전자가 낫다.
        """
        path = self._path(day or date.today())
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return []
        calls = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                calls.append(Call(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                # 죽는 순간에 반쯤 쓰인 줄이 있을 수 있다. 그 한 줄 때문에
                # 나머지를 못 읽으면 원장이 아니라 지뢰다.
                continue
        return calls

    def _path(self, day: date) -> Path:
        return self.directory / f"{day.isoformat()}.jsonl"


def _ends_with_newline(path: Path) -> bool:
    with path.open("rb") as stream:
        stream.seek(-1, 2)
        return stream.read(1) == b"\n"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
