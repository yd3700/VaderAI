"""UI 스레드의 COM 아파트먼트.

Windows 셸 대화상자(`askdirectory` → `SHBrowseForFolder`)는 STA 를 요구한다.
MTA 에서 부르면 대화상자를 **끝까지 만들어 놓고 표시하지 않은 채 데드락**한다.
예외도 안 나고 CPU 도 안 쓴다 — 사용자에게는 앱이 굳은 것으로 보인다.

그런데 룬이 이 스레드를 MTA 로 바꾼다. `discover()` 가 eonm_ipgo 를 import
하면 `pywinauto` 가 딸려오고 comtypes 가 기본값으로 MTA 를 잡는다. 실제로
이것 때문에 "룬 설치" 버튼이 앱을 멈췄다.

`sys.coinit_flags` 는 comtypes 가 **import 시점에 한 번** 읽는다. 그래서 순서가
전부다 — 별도 프로세스로 진짜 순서를 재현해서 확인한다.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="COM 아파트먼트는 Windows 개념이다"
)

#: `CoGetApartmentType` 의 APTTYPE. 1 이 MTA 이고, 그 값이면 대화상자가 죽는다.
APTTYPE_MTA = 1

PROBE = """
import sys
sys.path.insert(0, {root!r})
{setup}
import ctypes
a = ctypes.c_int(); q = ctypes.c_int()
hr = ctypes.windll.ole32.CoGetApartmentType(ctypes.byref(a), ctypes.byref(q))
print(hr, a.value)
"""


def _apartment(setup: str) -> tuple[int, int]:
    code = PROBE.format(root=str(ROOT), setup=setup)
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    hr, apartment = done.stdout.split()[-2:]
    return int(hr), int(apartment)


def test_entrypoint_keeps_the_ui_thread_out_of_mta():
    """`vader.__main__` 을 지나면 pywinauto 를 들여도 MTA 가 아니다."""
    pytest.importorskip("pywinauto", reason="pip install .[eonm]")

    _hr, apartment = _apartment(
        "import vader.__main__\nimport pywinauto.application"
    )

    assert apartment != APTTYPE_MTA, (
        "UI 스레드가 MTA 다. 이 상태로 askdirectory 를 부르면 앱이 데드락한다"
    )


def test_pywinauto_alone_really_does_take_the_thread_to_mta():
    """고쳐야 할 상태가 실재하는지 확인한다.

    이게 통과하지 않으면 위 테스트는 아무것도 증명하지 않는다 — pywinauto 가
    더 이상 MTA 를 잡지 않게 바뀐 것이고, 그때는 진입점의 설정을 지워도 된다.
    """
    pytest.importorskip("pywinauto", reason="pip install .[eonm]")

    _hr, apartment = _apartment("import pywinauto.application")

    assert apartment == APTTYPE_MTA


def test_the_expected_pywinauto_warning_is_silenced():
    """pywinauto 는 import 할 때 "Apply externally defined coinit_flags: 2" 라고
    경고한다. **오류가 아니라 우리가 일부러 한 일의 알림이다.**

    마스터 콘솔에 실행할 때마다 두 줄씩 찍혀 오류로 보였다. 우리가 아는
    경고를 남에게 걱정거리로 넘기지 않는다.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import vader.__main__"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0
    assert "coinit_flags" not in result.stderr


def test_only_that_one_warning_is_silenced():
    """다른 UserWarning 은 그대로 보여야 한다. 넓게 막으면 진짜를 놓친다."""
    source = (ROOT / "vader/__main__.py").read_text(encoding="utf-8")

    assert 'message="Apply externally defined coinit_flags.*"' in source
    assert 'module="pywinauto"' in source
    assert 'filterwarnings("ignore")' not in source
