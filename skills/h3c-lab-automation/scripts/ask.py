"""ask.py - 向 HCL 设备问命令语法（发一连串命令，原样打印每个命令的回显）。

用途：不确定某条命令的确切写法时，别猜，直接问设备。Comware 的 `?` 会在你
敲空格后立刻打印帮助，然后回车会报"不完整命令"——那行报错是正常的，忽略即可。

用法::

    python ask.py 30008 "system-view|drni ?"
    python ask.py 30008 "system-view|interface Bridge-Aggregation 100|port drni ?"
    python ask.py 30008 "display drni ?"

用 | 分隔要依次发送的命令。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hcldrv import Console, ConsoleError, ConsoleTimeout, _PROMPT   # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    port = int(sys.argv[1])
    script = sys.argv[2].split("|")

    con = Console(port=port, timeout=20.0)
    try:
        con.connect(timeout=3.0)
        con.prep(timeout=60)
    except (ConsoleError, ConsoleTimeout) as exc:
        print("连接失败：%s: %s" % (type(exc).__name__, exc))
        return 2

    # 控制台会话的视图状态会跨连接保持，必须先归位到用户视图，
    # 否则"某条命令不识别"其实是视图不对。要进系统视图请在 script 里显式写。
    match = _PROMPT.search(con.text)
    view = match.group(0).strip() if match else "?"
    print("当前提示符: %s" % view)
    if view.startswith("["):
        try:
            con.command("return", timeout=20)
            print("已 exit 到用户视图")
        except (ConsoleError, ConsoleTimeout):
            pass

    try:
        for line in script:
            line = line.strip()
            if not line:
                continue
            print("\n$ %s" % line)
            try:
                out = con.command(line, timeout=8.0)
                print(out)
            except ConsoleTimeout as exc:
                # `?` 帮助后可能等不到提示符，把已收到的打出来即可
                print("[已收到，未等到提示符]")
                print(exc.transcript[-1500:])
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
