"""click_dialog.py - 不用看屏幕，按文字定位对话框并点按钮。

SecureCRT 这类 MFC 程序出错时会弹模态对话框把整个实例卡住。本脚本枚举所有
可见顶层窗口，从子控件文字里找到目标对话框，再点掉指定按钮（默认 OK）。

用法::

    python click_dialog.py                      # 列出所有对话框的文字，不动手
    python click_dialog.py "意外的参数"          # 找到含该文字的对话框并点 OK
    python click_dialog.py "意外的参数" 确定      # 指定按钮文字
"""

from __future__ import annotations

import sys
import time

import win32con
import win32gui
import win32process

BM_CLICK = 0x00F5
BUTTON_CLASSES = ("Button",)


def children(hwnd: int) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []

    def walk(child, _):
        text = win32gui.GetWindowText(child)
        if text.strip():
            found.append((child, win32gui.GetClassName(child), text))
        return True

    win32gui.EnumChildWindows(hwnd, walk, None)
    return found


def top_level() -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            if title:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                rows.append((hwnd, pid, title))
        return True

    win32gui.EnumWindows(visit, None)
    return rows


def main() -> int:
    deadline = time.monotonic() + 10
    needle = sys.argv[1] if len(sys.argv) > 1 else None
    button_label = sys.argv[2] if len(sys.argv) > 2 else None

    while True:
        for hwnd, pid, title in top_level():
            kids = children(hwnd)
            blob = title + " | " + " | ".join(t for _, _, t in kids)
            if needle is None:
                if kids:
                    print(f"PID {pid:>6}  [{title}]")
                    for _, cls, text in kids:
                        print(f"            {cls}: {text}")
                continue
            if needle not in blob:
                continue

            print(f"命中对话框：PID {pid} 标题={title!r}")
            buttons = [(h, t) for h, cls, t in kids if cls in BUTTON_CLASSES]
            target = None
            for h, text in buttons:
                if button_label is None or button_label in text:
                    target = (h, text)
                    break
            if target is None:
                print(f"  没找到按钮 {button_label!r}，可选：{[t for _, t in buttons]}")
                return 3
            print(f"  点击按钮 {target[1]!r}")
            win32gui.SendMessage(target[0], BM_CLICK, 0, 0)
            time.sleep(0.5)
            return 0

        if needle is None:
            return 0
        if time.monotonic() >= deadline:
            print(f"没找到含 {needle!r} 的对话框")
            return 2
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
