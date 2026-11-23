"""자동화 안전장치의 책임 경계.

비상정지는 Core의 JobRunner가 소유한다. 실행 중인 Job 전부에 협조적 취소를
요청하고, 명시적으로 재개할 때까지 새 Job을 받지 않는다. 스레드를 강제로
죽이지 않는 이유는 외부 시스템에 절반만 입력된 상태를 남기지 않기 위해서다.

dry-run은 **외부 시스템**에 대해서만 권고다. 이온엠에 키를 넣는 코드는 룬 안에
있으므로 Core가 가로챌 수 없고, 룬이 ``ctx.dry_run``일 때 건너뛰어야 한다
(``eonm_ipgo/rune.py`` 의 ``_workflow``). **Core 자신의 저장소는 Core가 막는다** —
``JobContext.save/clear_checkpoint`` 는 dry-run에서 아무것도 쓰지 않는다. 리허설이
기록을 남기면 다음 실제 실행이 하지도 않은 작업을 건너뛴다.

대상 앱 제한은 ``JobRunner.submit`` 이 강제한다. ``kind="window"`` 설정값을
``target_apps`` 와 대조하고 벗어나면 Job을 만들지 않는다. 룬마다 부르게 하면 안
부르는 룬이 생기고, 룬은 ``vader.sdk`` 만 import하므로 부를 수도 없다.
"""
from __future__ import annotations

from vader.sdk import check_target_app

__all__ = ["check_target_app"]
