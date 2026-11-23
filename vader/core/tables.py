"""표 룬의 파일 입출력 중개 — 룬은 경로를 만지지 않고 표만 받고 넘긴다 (2026-10-14 합의 2번).

`profile="table"` 룬은 `open`·경로·pandas 를 쓰지 못한다 (`vader/core/profile.py`). 그 대신
`ctx.read_table(입력)` 으로 이번 작업에 주어진 입력만 읽고, `ctx.write_table(이름, 행)` 으로
이번 작업 폴더에만 쓴다. 위치·이름·형식·크기는 여기서 검사한다.

행은 `list[list]` 다. 첫 행이 머리글이면 머리글이다 — 해석은 룬이 한다.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

#: 읽는 파일 하나의 크기 상한.
MAX_INPUT_BYTES = 30 * 1024 * 1024
#: 쓰는 표 하나의 셀 수 상한.
MAX_OUTPUT_CELLS = 2_000_000
READABLE = {".csv", ".tsv", ".xlsx", ".xlsm"}   # .tsv: 화면 표 읽기가 저장한 파일
WRITABLE = {".csv", ".xlsx"}
#: 파일 이름 하나 — 폴더 구분자·드라이브·Windows 금지 문자 없이.
_NAME = re.compile(r'[^\\/:*?"<>|\x00-\x1f]+')


class TableError(ValueError):
    """룬(과 사람)에게 그대로 보여 줄 수 있는 거절."""


def read_table(path: Path, sheet: str | None = None) -> list[list]:
    """csv 는 글자 그대로, 엑셀은 셀 값(수식이면 마지막 계산값)으로. 완전히 빈 행은 뺀다."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in READABLE:
        raise TableError(f"표 파일(.csv, .xlsx)만 읽을 수 있습니다: {path.name}")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise TableError(f"표 파일이 너무 큽니다 ({MAX_INPUT_BYTES // 1024 // 1024}MB 까지): {path.name}")
    if suffix in (".csv", ".tsv"):
        if sheet is not None:
            raise TableError("csv 에는 시트가 없습니다")
        for encoding in ("utf-8-sig", "cp949"):   # 엑셀이 저장한 한글 csv 는 cp949 인 경우가 많다
            try:
                with path.open(encoding=encoding, newline="") as f:
                    return [row for row in csv.reader(f, delimiter="	" if suffix == ".tsv" else ",")
                            if any(cell.strip() for cell in row)]
            except UnicodeDecodeError:
                continue
        raise TableError(f"csv 글자 인코딩을 읽지 못했습니다 (UTF-8·CP949 아님): {path.name}")
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet is None:
            page = book.worksheets[0]
        elif sheet in book.sheetnames:
            page = book[sheet]
        else:
            raise TableError(f"'{sheet}' 시트가 없습니다. 있는 시트: {', '.join(book.sheetnames)}")
        return [list(row) for row in page.iter_rows(values_only=True)
                if any(v is not None and str(v).strip() for v in row)]
    finally:
        book.close()


def write_table(folder: Path, name: str, rows) -> str:
    """`folder/name` 에 쓴다. 이름은 파일 이름 하나뿐이다. 결과 파일 자리를 문자열로 돌려준다.

    `Path` 가 아니라 문자열이다 — Path 를 주면 `/ ".."` 로 작업 폴더 밖에 쓸 길이 생긴다.
    """
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name.startswith(".") or name.strip() != name:
        raise TableError(f"결과 파일 이름은 폴더 없이 이름 하나만 씁니다 (예: 결과.xlsx): {name!r}")
    suffix = Path(name).suffix.lower()
    if suffix not in WRITABLE:
        raise TableError(f"결과는 .csv 또는 .xlsx 로만 씁니다: {name}")
    rows = [list(row) for row in rows]
    if sum(len(row) for row in rows) > MAX_OUTPUT_CELLS:
        raise TableError(f"결과 표가 너무 큽니다 (셀 {MAX_OUTPUT_CELLS:,}개 까지)")
    out = Path(folder) / name
    if suffix == ".csv":
        with out.open("w", encoding="utf-8-sig", newline="") as f:   # 엑셀이 한글을 바로 읽게 BOM
            csv.writer(f).writerows(rows)
    else:
        from openpyxl import Workbook

        book = Workbook(write_only=True)
        page = book.create_sheet()
        for row in rows:
            page.append(row)
        book.save(out)
    return str(out)
