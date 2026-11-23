"""설치본 진입점.

- vaderai-worker.exe: Rust 껍데기(VaderAI.exe)가 띄우는 파이썬 일꾼 (vader/desktop/worker.py)
- vaderai-cli.exe:    `python -m vader` 와 같다 — MCP 서버·제작소 시험 (`--module`)
"""
import sys
from pathlib import Path

if Path(sys.executable).stem.lower() == "vaderai-worker":
    from vader.desktop.worker import main
else:
    from vader.__main__ import main

main()
