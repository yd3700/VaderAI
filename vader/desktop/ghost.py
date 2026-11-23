"""가상 커서 — 화면 자동화가 누를 곳을 보여 주는 그림일 뿐이다. 진짜 마우스·키보드는 건드리지 않는다.

`python -m vader.desktop.ghost` 로 떠서 표준 입력 한 줄 "x y"(화면 좌표)마다 그 자리로 미끄러져 가 한 번 눌린다.
클릭은 통과하고(WS_EX_TRANSPARENT) 포커스를 뺏지 않는다(WS_EX_NOACTIVATE). 입력이 닫히면(앱 종료) 끝난다.
"""
from __future__ import annotations

import ctypes
import queue
import sys
import threading
import tkinter as tk

KEY = "#ff00fe"                       # 투명으로 뺄 바탕색
SCALE = 1.4                           # 진짜 커서보다 조금 크게 — 둘을 헷갈리지 않게
W, H = round(22 * SCALE), round(32 * SCALE)
ARROW = [(x * SCALE, y * SCALE) for x, y in [(0, 0), (0, 25), (6, 19), (11, 30), (15, 28), (10, 18), (18, 18)]]   # 끝(0,0)이 누르는 점
FRAMES, TICK, IDLE = 18, 16, 4000     # 미끄러짐 ≈0.3초, 4초 쉬면 숨는다


def _inside(x: float, y: float) -> bool:
    hit, j = False, len(ARROW) - 1
    for i, (xi, yi) in enumerate(ARROW):
        xj, yj = ARROW[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            hit = not hit
        j = i
    return hit


def picture() -> list[list[str]]:
    """행마다 색 — 안은 빨강→검정 대각 그라데이션, 가장자리는 흰 테, 밖은 KEY."""
    rows = []
    for y in range(H):
        row = []
        for x in range(W):
            if not _inside(x + .5, y + .5):
                row.append(KEY)
            elif not all(_inside(x + .5 + dx, y + .5 + dy) for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, -1), (1, 1))):
                row.append("#ffffff")
            else:
                t = min(1.0, (x + y) / (W + H) * 1.6)
                row.append("#%02x%02x%02x" % (round(235 * (1 - t)), round(20 * (1 - t)), round(30 * (1 - t))))
        rows.append(row)
    return rows


def main() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)     # 엔진 좌표(물리 픽셀)와 맞춘다
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-transparentcolor", KEY)
    root.configure(bg=KEY)
    img = tk.PhotoImage(width=W, height=H)
    img.put(" ".join("{" + " ".join(r) + "}" for r in picture()))
    tk.Label(root, image=img, bg=KEY, bd=0).pack()
    root.geometry(f"{W}x{H}+-100+-100")
    root.update_idletasks()
    user32 = ctypes.windll.user32
    hwnd = user32.GetAncestor(root.winfo_id(), 2)
    user32.SetWindowLongW(hwnd, -20, user32.GetWindowLongW(hwnd, -20) | 0x08000000 | 0x80000 | 0x80 | 0x20)
    root.withdraw()

    moves: queue.Queue = queue.Queue()

    def read():
        for line in sys.stdin:
            try:
                x, y = (int(float(v)) for v in line.split()[:2])
            except ValueError:
                continue
            moves.put((x, y))
        moves.put(None)
    threading.Thread(target=read, daemon=True).start()

    state = {"at": None, "hide": None}

    def place(x, y):
        root.geometry(f"+{x + 1}+{y + 1}")                  # 누를 점 자체는 덮지 않는다 — 엔진이 가려진 요소로 본다

    def glide(src, dst, k=1):
        e = 1 - (1 - k / FRAMES) ** 3                        # 끝에서 느려진다
        place(round(src[0] + (dst[0] - src[0]) * e), round(src[1] + (dst[1] - src[1]) * e))
        if k < FRAMES:
            root.after(TICK, glide, src, dst, k + 1)
        else:
            state["at"] = dst
            place(dst[0] + 2, dst[1] + 2)                    # 눌림
            root.after(140, place, *dst)
            root.after(160, poll)

    def poll():
        try:
            dst = moves.get_nowait()
        except queue.Empty:
            root.after(30, poll)
            return
        if dst is None:
            root.destroy()
            return
        if state["hide"]:
            root.after_cancel(state["hide"])
        src = state["at"] or (dst[0] - 160, dst[1] - 90)
        root.deiconify()
        root.attributes("-topmost", True)
        state["hide"] = root.after(IDLE + FRAMES * TICK, root.withdraw)
        glide(src, dst)

    root.after(30, poll)
    root.mainloop()


if __name__ == "__main__":
    main()
