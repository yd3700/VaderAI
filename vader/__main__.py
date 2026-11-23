"""``python -m vader``와 ``vaderai`` 콘솔 스크립트의 공통 진입점."""
import sys
import warnings

# ── UI 스레드는 STA 여야 한다 ─────────────────────────────────────
#
# Windows 셸 대화상자(`filedialog.askdirectory` → `SHBrowseForFolder`)는 호출
# 스레드가 STA 일 것을 요구한다. MTA 에서 부르면 대화상자를 **끝까지 만들어
# 놓고 표시하지 않은 채 데드락**한다. 예외도 안 나고 CPU 도 안 쓴다 — 앱이
# 그냥 굳는다.
#
# 그런데 룬이 이 스레드의 아파트먼트를 바꾼다. `discover()` 가 eonm_ipgo 를
# import 하면 `driver.py` 의 `pywinauto` 가 딸려오고, comtypes 가 기본값으로
# 스레드를 MTA 로 초기화한다. **EONM 룬이 설치된 순간부터 폴더 선택이 전부
# 죽는 이유가 이것이다** — 첫 설치는 되고 그다음부터 안 된다.
#
# comtypes 는 import 시점에 `sys.coinit_flags` 를 본다. 룬이 로드되기 전,
# 프로세스에서 가장 먼저 정해야 하므로 여기가 유일하게 맞는 자리다.
# 2 = COINIT_APARTMENTTHREADED.
#
# 이건 pywinauto 사정에 코어가 맞춰주는 것이 아니다. UI 스레드의 아파트먼트는
# 창을 소유한 코어의 것이고, 나중에 실리는 룬이 바꾸면 안 되는 값이다.
sys.coinit_flags = 2

# pywinauto 는 import 할 때 "Apply externally defined coinit_flags: 2" 라고
# 경고한다. **오류가 아니라 우리가 일부러 한 일의 알림이다** — 자기가 정하려던
# 값을 우리가 이미 정해 뒀다는 뜻이고, 그게 정확히 의도한 바다.
#
# 마스터가 실행할 때마다 콘솔에 두 줄씩 찍혀 오류로 보였다. 우리가 아는
# 경고를 남에게 걱정거리로 넘기지 않는다. **이 하나만** 막는다 — 다른
# UserWarning 은 그대로 보여야 한다.
warnings.filterwarnings(
    "ignore",
    message="Apply externally defined coinit_flags.*",
    category=UserWarning,
    module="pywinauto",
)

from vader.core.crash import install_exception_hooks  # noqa: E402


def main(argv=None) -> None:
    import argparse
    import importlib.util

    from vader.core import config
    if config.FROZEN:
        import velopack
        velopack.App().run()   # 설치·업데이트·제거 때 Velopack 이 부른다 — 할 일을 하고 여기서 끝날 수 있다
    argv = sys.argv[1:] if argv is None else argv
    config.REQUIRE_WORKSPACE = True   # 로그인·잠금 해제 전엔 업무 데이터를 못 연다 (2단계)
    if argv[:1] == ['--module'] and argv[1:2] and argv[1] in config.CHILD_MODULES:
        # 설치본의 `python -m` — MCP 서버·제작소 시험만 (vaderai-cli.exe 가 받는다)
        import runpy
        sys.argv = [argv[1], *argv[2:]]
        runpy.run_module(argv[1], run_name='__main__', alter_sys=True)
        return
    if config.FROZEN and _relaunched_as_admin(argv):
        return

    parser = argparse.ArgumentParser(description='VaderAI 데스크톱')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--classic', action='store_true', help='기존 관리 화면 사용')
    mode.add_argument('--desktop', action='store_true', help='새 디자인 화면 사용 (desktop extra 필요)')
    args = parser.parse_args(argv)
    install_exception_hooks()
    if not args.classic and importlib.util.find_spec('webview'):
        from vader.desktop.host import main as desktop_main
        desktop_main()
    elif args.desktop:
        parser.error('새 UI 설치: python -m pip install -e ".[desktop]"')
    else:
        # 예전 관리 화면에는 로그인·잠금이 없다 — 계정 작업공간을 우회하는 길이 되므로 닫는다 (2단계 codex1).
        # ponytail: 되살리려면 이 화면에도 같은 로그인·PIN 문을 붙인다.
        parser.exit(1, '예전 관리 화면은 더 이상 쓰지 않습니다. VaderAI 를 그냥 실행해 로그인하세요.\n')


def _relaunched_as_admin(argv) -> bool:
    """설치본은 관리자 권한으로 돈다 — Windows 확인 창(UAC)을 띄우고 승인되면 그쪽이 이어 켠다 (마스터 결정 2026-10-28).

    관리자로 뜬 프로그램(이온엠 등)에는 일반 권한 프로그램이 클릭·키를 보낼 수 없다.
    Velopack 의 설치·업데이트 호출은 위 `velopack.App().run()` 에서 먼저 끝나므로 여기까지 오지 않는다.
    확인 창에서 '아니요'를 누르면 켜지 않는다. 이미 관리자면 그대로 켠다.
    """
    import ctypes
    import subprocess
    if ctypes.windll.shell32.IsUserAnAdmin():
        return False
    ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable, subprocess.list2cmdline(argv), None, 1)
    return True


if __name__ == "__main__":
    main()
