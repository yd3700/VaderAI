# PyInstaller — VaderAI 설치본 (런칭 0단계, 2026-10-21). 빌드: packaging\build.ps1
#
# 한 폴더(onedir)에 실행 파일 셋:
#   VaderAI.exe         Rust 껍데기 (shell/) — 사람이 켜는 것. build.ps1 이 cargo 로 만들어 여기 넣는다
#   vaderai-worker.exe  파이썬 일꾼 — 콘솔 exe(표준 입출력이 살아 있어야 한다, codex1 r1 P1-3).
#                       껍데기가 CREATE_NO_WINDOW 로 띄워 창은 안 뜬다
#   vaderai-cli.exe     콘솔 — MCP 서버·제작소 시험처럼 표준 입출력으로 도는 자식 프로세스 (`--module`)
#
# 룬은 설치 뒤에 들어오는 소스라 PyInstaller 가 import 를 못 본다. 그래서 표준 라이브러리 전부와
# 룬이 쓰는 서드파티(pandas·pywinauto …)를 통째로 넣는다.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "vader/desktop/assets"), "vader/desktop/assets"),
    (str(ROOT / "vader/core/workshop_guide.md"), "vader/core"),
    (str(ROOT / "settings.template.json"), "."),
]
# 업데이트 피드 주소(비공개 저장소 + 공용 업데이트 키). 저장소에 올리지 않는 파일이라 있을 때만 싣는다 — 없으면 자동 업데이트 꺼짐.
if (ROOT / "packaging/update_feed.txt").exists():
    datas.append((str(ROOT / "packaging/update_feed.txt"), "."))
# 기본 룬 — **git 이 추적하는 파일만** 싣는다. 시험 자료·녹화·업무 파일(추적 안 함)이 설치본에 섞이지 않게.
import subprocess
for rune in ("table_merge",):   # 정식 배포 기본 룬은 표 합치기 하나 (2026-11-19). 병원 업무 룬은 광장·직접 설치로
    # -z: 한글 이름이 따옴표·8진 이스케이프로 나오지 않게 (codex1 구현 1차 P1)
    tracked = [f for f in subprocess.run(["git", "ls-files", "-z", f"rune-packages/{rune}"], cwd=ROOT, check=True,
                                         capture_output=True).stdout.decode("utf-8").split("\0") if f]
    datas += [(str(ROOT / f), str(Path("runes", *Path(f).parts[1:-1]))) for f in tracked]
binaries = []
hidden = collect_submodules("vader")

# 룬이 import 할 수 있는 표준 라이브러리 — 개발·시험용 모듈은 뺀다
SKIP = {"idlelib", "turtledemo", "test", "lib2to3", "ensurepip", "venv", "pydoc_data", "this", "antigravity"}
hidden += [m for m in sorted(sys.stdlib_module_names) if m not in SKIP and not m.startswith("_")]

# 룬·제공자가 실행 중에 import 하는 서드파티 (pyproject 의 desktop·eonm·supply·gemini)
for package in ("webview", "winpty", "mcp", "cryptography", "openpyxl", "xlrd", "pandas",
                "pywinauto", "comtypes", "PIL", "pypdfium2", "thefuzz", "rapidfuzz", "google.genai", "velopack",
                "vodozemac", "pystray"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hidden += h

a = Analysis(
    [str(ROOT / "packaging/entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    # 개발 PC 에 깔려 있어 선택 import 로 딸려오던 것들 (torch 만 330MB) — VaderAI 는 안 쓴다
    excludes=["pytest", "IPython", "matplotlib", "torch", "torchvision", "torchaudio", "transformers",
              "tokenizers", "huggingface_hub", "hf_xet", "cv2", "scipy", "sklearn", "google.genai.tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)

icon = str(ROOT / "vader/desktop/assets/vaderai.ico")
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="vaderai-worker", console=True, icon=icon)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="vaderai-cli", console=True, icon=icon)
COLLECT(gui, cli, a.binaries, a.datas, name="VaderAI")
