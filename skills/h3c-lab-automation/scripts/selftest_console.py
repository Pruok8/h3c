"""selftest_console.py - 在没有 HCL 的情况下验证 hcldrv.Console 是否可靠。

思路：起一个假 Comware 设备（mock_device.MockComware），用真驱动连它，
把"连上 -> 等提示符 -> 关分页 -> 取回显 -> 长输出自动翻页"整条链跑一遍。
任何一个环节坏了，这里就会失败。

用法： python selftest_console.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hcldrv import Console, ConsoleTimeout          # noqa: E402
from mock_device import MockComware                 # noqa: E402

OK = "  [ok]"
FAIL = "  [FAIL]"
failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"{OK} {label}")
    else:
        print(f"{FAIL} {label} {detail}")
        failures.append(label)


def main() -> int:
    dev = MockComware(paging=8)
    dev.start()
    print(f"mock device  ->  127.0.0.1:{dev.port}  (paging=8)\n")

    con = Console(port=dev.port, timeout=8.0)
    con.connect()

    # 1) 等提示符 + 关分页
    con.prep(timeout=6.0)
    check("连上后等到提示符 <H3C>", "<H3C>" in con.text, repr(con.text[:80]))
    check("telnet 控制字节没漏进可见文本", "\xff" not in con.text)

    # 2) 普通命令：取回显
    ver = con.command("display version")
    check("display version 拿到输出", "Comware Software" in ver, repr(ver[:80]))
    check("回显行被剥掉", not ver.startswith("display version"), repr(ver[:40]))
    check("结尾提示符被剥掉", not ver.rstrip().endswith("<H3C>"), repr(ver[-30:]))
    print("\n--- display version (驱动返回的正文) ---")
    print("\n".join(ver.splitlines()[:6]))
    print("...\n")

    # 3) 长输出：考验 ---- More ---- 自动翻页
    before = con.answered_more
    cfg = con.command("display current-configuration", timeout=20.0)
    paged = con.answered_more - before
    check("长输出触发了自动翻页", paged >= 5, f"answered_more={paged}")
    check("翻页标记没残留在结果里", "More" not in cfg)
    check("分页后内容完整", len(cfg.splitlines()) > 200, f"{len(cfg.splitlines())} 行")
    check("分页后正常回到提示符", cfg.rstrip().endswith("return"), repr(cfg[-20:]))
    print(f"--- display current-configuration: {len(cfg.splitlines())} 行, "
          f"自动翻页 {paged} 次 ---")
    print("\n".join(cfg.splitlines()[:4]))
    print("...")
    print("\n".join(cfg.splitlines()[-2:]))
    print()

    # 4) 多条命令连发，确认 start-offset 逻辑不会误命中上一个提示符
    seq = [con.command("display interface brief"), con.command("display version")]
    check("连续多条命令各自拿到正确输出",
          "GE1/0/1" in seq[0] and "Comware Software" in seq[1])

    # 5) 未识别命令
    bad = con.command("foobar")
    check("未识别命令能拿到报错", "Unrecognized" in bad, repr(bad[:60]))

    # 6) 超时行为可诊断
    try:
        con.read_until(["###永远不会出现###"], timeout=1.0)
        check("超时抛 ConsoleTimeout", False, "居然没超时")
    except ConsoleTimeout as exc:
        check("超时抛 ConsoleTimeout 且带现场", bool(exc.transcript))

    con.close()
    dev.stop()

    print()
    if failures:
        print(f"结果：{len(failures)} 项失败 -> {failures}")
        return 1
    print("结果：全部通过 —— 驱动可用，可以拿去连真实 HCL 设备。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
