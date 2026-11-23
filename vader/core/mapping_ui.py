"""룬이 선택적으로 제공하는 품목 매핑 교정 기능을 설정 창에 붙인다.

어떤 룬의 CSV인지, 어떤 판별식을 쓰는지는 코어가 모른다. 룬 객체가
``mapping_maintenance`` 팩토리를 제공할 때만 이 패널이 나타난다.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from vader.core import config


def attach_mapping_maintenance(parent, rune, current: dict, row: int) -> int:
    factory = getattr(rune, "mapping_maintenance", None)
    if not callable(factory):
        return row

    master_db = Path(str(current.get("master_db", "")))
    datadir = config.data_dir() / "rune-data" / rune.manifest.id
    service = factory(datadir, config.data_dir() / "jobs", master_db)
    MappingMaintenancePanel(parent, service).frame.grid(
        row=row, column=0, columnspan=2, sticky=tk.NSEW, pady=(12, 0)
    )
    return row + 1


class MappingMaintenancePanel:
    def __init__(self, parent, service) -> None:
        self.service = service
        self.review_items = []
        self.learned_items = []
        self.frame = ttk.LabelFrame(parent, text="품목 매핑 교정", padding=8)

        ttk.Label(
            self.frame,
            text="최근 작업의 확인필요·AI추정 항목을 고칩니다. 저장 층위는 자동 판별됩니다.",
        ).grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 6))

        self.review = ttk.Treeview(
            self.frame,
            columns=("review", "insurance", "name", "current", "scope"),
            show="headings", height=5,
        )
        for key, label, width in (
            ("review", "검토", 75), ("insurance", "보험코드", 95),
            ("name", "품목명", 180), ("current", "현재 코드", 85),
            ("scope", "저장", 75),
        ):
            self.review.heading(key, text=label)
            self.review.column(key, width=width, stretch=key == "name")
        self.review.grid(row=1, column=0, columnspan=4, sticky=tk.EW)
        self.review.bind("<<TreeviewSelect>>", self._select_review)

        ttk.Label(self.frame, text="올바른 내부코드").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.correct_code = tk.StringVar()
        ttk.Entry(self.frame, textvariable=self.correct_code, width=18).grid(
            row=2, column=1, sticky=tk.W, padx=(6, 6)
        )
        ttk.Button(self.frame, text="교정 저장", command=self._correct).grid(
            row=2, column=2, sticky=tk.W
        )
        ttk.Button(self.frame, text="새로고침", command=self.refresh).grid(
            row=2, column=3, sticky=tk.E
        )

        ttk.Separator(self.frame).grid(row=3, column=0, columnspan=4, sticky=tk.EW, pady=6)
        ttk.Label(
            self.frame, text="학습 DB 정리 — 선택 기준은 코드가 아니라 품목명입니다."
        ).grid(row=4, column=0, columnspan=4, sticky=tk.W)
        self.learned = ttk.Treeview(
            self.frame, columns=("vendor", "name", "code"), show="headings", height=5
        )
        for key, label, width in (
            ("vendor", "거래처", 100), ("name", "품목명", 250), ("code", "코드", 100)
        ):
            self.learned.heading(key, text=label)
            self.learned.column(key, width=width, stretch=key == "name")
        self.learned.grid(row=5, column=0, columnspan=4, sticky=tk.EW, pady=(4, 0))
        self.learned.bind("<<TreeviewSelect>>", self._select_learned)

        self.learned_code = tk.StringVar()
        ttk.Entry(self.frame, textvariable=self.learned_code, width=18).grid(
            row=6, column=1, sticky=tk.W, padx=(6, 6), pady=5
        )
        ttk.Button(self.frame, text="선택 품목 수정", command=self._update_learning).grid(
            row=6, column=2, sticky=tk.W
        )
        ttk.Button(self.frame, text="선택 품목 삭제", command=self._delete_learning).grid(
            row=6, column=3, sticky=tk.E
        )
        self.frame.columnconfigure(2, weight=1)
        self.refresh()

    def _selected_index(self, tree) -> int | None:
        selection = tree.selection()
        return int(selection[0]) if selection else None

    def _select_review(self, _event=None) -> None:
        index = self._selected_index(self.review)
        if index is not None:
            self.correct_code.set(self.review_items[index].current_code)

    def _select_learned(self, _event=None) -> None:
        index = self._selected_index(self.learned)
        if index is not None:
            self.learned_code.set(self.learned_items[index]["mapped_code"])

    def refresh(self) -> None:
        for tree in (self.review, self.learned):
            tree.delete(*tree.get_children())
        self.review_items = self.service.review_items()
        for index, item in enumerate(self.review_items):
            self.review.insert("", tk.END, iid=str(index), values=(
                item.review, item.insurance_code, item.learning_key, item.current_code,
                "보험코드" if item.scope == "insurance" else "품목명",
            ))
        self.learned_items = self.service.learned_rows()
        for index, item in enumerate(self.learned_items):
            self.learned.insert("", tk.END, iid=str(index), values=(
                item["vendor"], item["item_name"], item["mapped_code"],
            ))

    def _correct(self) -> None:
        index = self._selected_index(self.review)
        if index is None:
            messagebox.showwarning("품목 매핑", "교정할 항목을 선택해 주세요", parent=self.frame)
            return
        try:
            scope = self.service.correct(self.review_items[index], self.correct_code.get())
        except ValueError as exc:
            messagebox.showwarning("품목 매핑", str(exc), parent=self.frame)
            return
        messagebox.showinfo(
            "품목 매핑", "보험코드 규칙에 저장했습니다" if scope == "insurance" else "품목명 학습에 저장했습니다",
            parent=self.frame,
        )
        self.refresh()

    def _update_learning(self) -> None:
        index = self._selected_index(self.learned)
        if index is None:
            messagebox.showwarning("품목 매핑", "수정할 품목명을 선택해 주세요", parent=self.frame)
            return
        item = self.learned_items[index]
        try:
            changed = self.service.update_learning(
                item["vendor"], item["item_name"], self.learned_code.get()
            )
        except ValueError as exc:
            messagebox.showwarning("품목 매핑", str(exc), parent=self.frame)
            return
        if changed:
            self.refresh()

    def _delete_learning(self) -> None:
        index = self._selected_index(self.learned)
        if index is None:
            messagebox.showwarning("품목 매핑", "삭제할 품목명을 선택해 주세요", parent=self.frame)
            return
        item = self.learned_items[index]
        if not messagebox.askyesno(
            "품목 매핑", f"{item['item_name']} 학습 행만 삭제할까요?", parent=self.frame
        ):
            return
        if self.service.delete_learning(item["vendor"], item["item_name"]):
            self.refresh()
