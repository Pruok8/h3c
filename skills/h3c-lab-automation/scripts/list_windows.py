"""list_windows.py - 列出当前所有可见顶层窗口（PID / 类名 / 标题 / 是否对话框）。

用途：SecureCRT 这类 GUI 程序"发起命令后没反应"时，通常是弹了个模态对话框在
等人点。这个脚本不用看屏幕就能把窗口列表和对话框上的文字打出来。

用法： python list_windows.py [进程名关键字]
"""

from __future__ import annotations

import sys

import win32gui
import win32process

try:
    import win32con
except ImportError:          # pragma: no cover
    win32con = None

KIND = {
    "Button": "按钮",
    "Static": "文字",
    "Edit": "输入框",
    "ComboBox": "下拉框",
}


def child_texts(hwnd: int) -> list[str]:
    """把对话框里所有子控件的文字抓出来（按钮/标签的实际内容就在这里）。"""
    found: list[str] = []

    def walk(child, _):
        text = win32gui.GetWindowText(child)
        cls = win32gui.GetClassName(child)
        if text.strip():
            found.append(f"{KIND.get(cls, cls)}: {text}")
        return True

    try:
        win32gui.EnumChildWindows(hwnd, walk, None)
    except Exception:
        pass
    return found


def main() -> int:
    want = sys.argv[1].lower() if len(sys.argv) > 1 else None
    rows: list[tuple] = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        cls = win32gui.GetClassName(hwnd)
        rows.append((pid, cls, title, hwnd))
        return True

    win32gui.EnumWindows(visit, None)

    pids_of_interest = set()
    if want:
        import subprocess
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, errors="replace").stdout
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) > 1 and want in parts[0].lower():
                pids_of_interest.add(int(parts[1]))

    for pid, cls, title, hwnd in rows:
        if want and pid not in pids_of_interest:
            continue
        print(f"PID {pid:>6}  [{cls}]  {title}")
        for line in child_texts(hwnd):
            print(f"            {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
