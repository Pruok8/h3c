#!/usr/bin/env python3
"""linkwatch.py - 链路巡检：按名单检查端口是否 UP，发现掉线可**两端一起复位**。

为什么需要
----------
HCL 里 SW1/SW2 的 `GE1/0/20-22` 连同对端（PE1/PE2 的 `GE0/0+GE6/0`、Server 的 `GE1/0/17/18`）
会**整体自行掉线**（设备 uptime 不变，不是重启）。本次实验掉了两次，两次都是肉眼发现的。
让脚本定期查、发现就按已验证的手法复位，能省掉大量人肉巡检。

watch.json::

    {
      "links": [
        {"name": "SW1->PE1", "port": 30008, "intf": "GE1/0/20",
         "peer_port": 30001, "peer_intf": "GE0/0"},
        {"name": "SW2->Server", "port": 30009, "intf": "GE1/0/21",
         "peer_port": 30010, "peer_intf": "GE1/0/18"}
      ]
    }

用法::

    python linkwatch.py watch.json                  # 只检查，每链路一行
    python linkwatch.py watch.json --fix            # 掉线的两端 shutdown -> 等 15s -> undo shutdown
    python linkwatch.py watch.json --fix --delay 20
    python linkwatch.py watch.json --out watch.log  # 回显写文件

判据：`display interface brief` 里该端口第 2 列是 `UP` 即正常；
`ADM` 表示被人工 shutdown（按设计关闭，不算故障）。

退出码：0 全 UP；1 有 DOWN（已 --fix 修好的仍记 1，提醒你曾经掉过）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hcldrv import Console, ConsoleError, ConsoleTimeout  # noqa: E402


class Pool:
    """按端口复用连接。"""

    def __init__(self) -> None:
        self._c: dict[int, Console] = {}

    def get(self, port: int) -> Console:
        con = self._c.get(port)
        if con is None:
            con = Console(port=port, timeout=25.0)
            con.connect(timeout=3.0)
            con.prep(timeout=60)
            self._c[port] = con
        return con

    def close(self) -> None:
        for con in self._c.values():
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
        self._c.clear()


def state_of(con: Console, intf: str) -> str:
    """返回该端口的链路状态：UP / DOWN / ADM / ?"""
    out = con.command("display interface brief", timeout=25)
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0].upper() == intf.upper():
            link = parts[1].upper() if len(parts) > 1 else "?"
            if link == "UP":
                return "UP"
            if link.startswith("ADM"):
                return "ADM"
            return "DOWN"
    return "?"


def bounce(con: Console, intf: str, undo: bool) -> str:
    cmd = "undo shutdown" if undo else "shutdown"
    try:
        con.command("system-view", timeout=20)
        con.command("interface %s" % intf, timeout=20)
        con.command(cmd, timeout=40)
        con.command("return", timeout=20)
        return "ok"
    except (ConsoleError, ConsoleTimeout) as exc:
        return "%s: %s" % (type(exc).__name__, str(exc)[:60])


def main() -> int:
    ap = argparse.ArgumentParser(description="链路巡检（可选自动复位）")
    ap.add_argument("spec", help="watch.json")
    ap.add_argument("--fix", action="store_true", help="发现 DOWN 时两端复位")
    ap.add_argument("--delay", type=float, default=15.0, help="shutdown 与 undo shutdown 之间等待秒数")
    ap.add_argument("--out", help="把回显写进文件")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    links = spec.get("links") or []
    if not links:
        print("watch.json 里没有 links")
        return 2

    pool = Pool()
    raw: list[str] = []
    bad: list[dict] = []
    print("== 链路巡检 %s ==" % time.strftime("%H:%M:%S"))
    try:
        for lk in links:
            name = lk.get("name") or "%s %s" % (lk["port"], lk["intf"])
            try:
                a = state_of(pool.get(int(lk["port"])), lk["intf"])
            except (ConsoleError, ConsoleTimeout) as exc:
                a = "?"
                raw.append("%s 连不上: %s" % (name, exc))
            b = "-"
            if lk.get("peer_port"):
                try:
                    b = state_of(pool.get(int(lk["peer_port"])), lk["peer_intf"])
                except (ConsoleError, ConsoleTimeout) as exc:
                    b = "?"
                    raw.append("%s 对端连不上: %s" % (name, exc))
            print("  %-18s %-8s 本端 %-4s | 对端 %s" % (name, lk["intf"], a, b))
            if a == "DOWN" or b == "DOWN":
                bad.append(lk)

        if bad and args.fix:
            print("\n-- 复位 %d 条链路（两端一起）--" % len(bad))
            for lk in bad:
                for port, intf in ((int(lk["port"]), lk["intf"]),
                                   (int(lk.get("peer_port") or 0), lk.get("peer_intf"))):
                    if not port:
                        continue
                    r = bounce(pool.get(port), intf, undo=False)
                    print("  shutdown  %s %s -> %s" % (port, intf, r))
            print("  等待 %.0fs（HCL 链路重建需要时间）" % args.delay)
            time.sleep(args.delay)
            for lk in bad:
                for port, intf in ((int(lk["port"]), lk["intf"]),
                                   (int(lk.get("peer_port") or 0), lk.get("peer_intf"))):
                    if not port:
                        continue
                    r = bounce(pool.get(port), intf, undo=True)
                    print("  undo sh   %s %s -> %s" % (port, intf, r))
            print("\n-- 复位后复查 --")
            for lk in bad:
                try:
                    a = state_of(pool.get(int(lk["port"])), lk["intf"])
                except (ConsoleError, ConsoleTimeout):
                    a = "?"
                print("  %-18s %-8s 本端 %s" % (lk.get("name", ""), lk["intf"], a))
    finally:
        pool.close()

    if args.out:
        Path(args.out).write_text("\n".join(raw) or "(每条链路均已读取到状态)", encoding="utf-8")
        print("\n回显 -> %s" % args.out)
    print("\n合计 %d 条：异常 %d 条" % (len(links), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
