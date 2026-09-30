#!/usr/bin/env python3
"""cleanup.py - 交付前审计"临时 / 旁路对象"：每个都确认是**保留**还是**清除**。

为什么需要
----------
实验过程中常会加临时 VLAN、测试子接口、旁路网段来绕平台限制。本次实例：
为绕过"DHCPv6 服务端不支持 VPN 绑定接口"，加了 `vlan 900` + `RAGG1.900`。
这类对象**不是垃圾，但必须在交付里说清**：保留就要写明为什么，不该保留就要删掉。
没有这个清单，它们就会含混地留在配置里，别人接手看不懂。

cleanup.json::

    {
      "objects": [
        {"desc": "IPv6 旁路网段（绕平台限制）", "device": "SW3-IRF1", "port": 30006,
         "check": "display interface brief", "pattern": "Vlan-interface900",
         "decision": "keep", "why": "DHCPv6 服务端不支持 VPN 绑定接口，故另开全局段"},

        {"desc": "调试用 LoopBack99", "device": "SW1", "port": 30008,
         "check": "display current-configuration | include LoopBack", "pattern": "LoopBack99",
         "decision": "remove", "remove": ["undo interface LoopBack 99"]}
      ]
    }

用法::

    python cleanup.py cleanup.json            # 审计：存在与否 + 决定 + 待执行命令
    python cleanup.py cleanup.json --apply    # 对 decision=remove 且确实存在的对象执行 remove

输出刻意简短：一行一个对象。审计结果同时写进 `--out`（默认 cleanup-audit.txt）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hcldrv import Console, ConsoleError, ConsoleTimeout  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="审计临时/旁路对象，决定保留还是清除")
    ap.add_argument("spec", help="cleanup.json")
    ap.add_argument("--apply", action="store_true", help="对 decision=remove 且存在的对象执行删除")
    ap.add_argument("--out", default="cleanup-audit.txt", help="审计明细文件")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    objs = spec.get("objects") or []
    if not objs:
        print("cleanup.json 里没有 objects")
        return 2

    conns: dict[int, Console] = {}
    lines: list[str] = ["# 临时/旁路对象审计  %s" % args.spec]
    todo: list[tuple[dict, list[str]]] = []
    n_keep = n_remove = n_gone = n_bad = 0

    try:
        for o in objs:
            port = int(o["port"])
            if port not in conns:
                con = Console(port=port, timeout=25.0)
                con.connect(timeout=3.0)
                con.prep(timeout=60)
                conns[port] = con
            con = conns[port]
            try:
                out = con.command(o["check"], timeout=30)
                present = bool(re.search(o["pattern"], out, re.MULTILINE))
            except (ConsoleError, ConsoleTimeout) as exc:
                present = False
                n_bad += 1
                lines.append("%s: 读取失败 %s" % (o.get("desc"), exc))
            decision = (o.get("decision") or "?").lower()
            tag = "存在" if present else "不存在"
            if not present:
                n_gone += 1
                print("  跳过  %-22s %-6s %s" % (o.get("desc", "")[:22], tag, "(配置里已没有)"))
            elif decision == "keep":
                n_keep += 1
                print("  保留  %-22s %-6s 理由: %s" % (o.get("desc", "")[:22], tag,
                                                       (o.get("why") or "未写理由!")[:52]))
            elif decision == "remove":
                n_remove += 1
                cmds = o.get("remove") or []
                print("  清除  %-22s %-6s 命令: %s" % (o.get("desc", "")[:22], tag,
                                                       "; ".join(cmds) or "(未给 remove 命令)"))
                if cmds:
                    todo.append((o, cmds))
            else:
                n_bad += 1
                print("  !!    %-22s %-6s decision 必须是 keep 或 remove" % (o.get("desc", "")[:22], tag))
            lines.append("%s | %s | %s | %s" % (o.get("device"), o.get("desc"), tag, decision))

        if args.apply and todo:
            print("\n-- 执行清除 --")
            for o, cmds in todo:
                con = conns[int(o["port"])]
                for c in cmds:
                    try:
                        con.command("system-view", timeout=20)
                        con.command(c, timeout=60)
                        con.command("return", timeout=20)
                        print("  %s -> ok" % c)
                        lines.append("APPLY ok: %s" % c)
                    except (ConsoleError, ConsoleTimeout) as exc:
                        print("  %s -> %s" % (c, str(exc)[:70]))
                        lines.append("APPLY fail: %s -> %s" % (c, exc))
        elif todo:
            print("\n（%d 个待清除对象：确认无误后加 --apply 执行）" % len(todo))
    finally:
        for con in conns.values():
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass

    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n合计 %d 个对象：保留 %d、待清除 %d、已不存在 %d、异常 %d" %
          (len(objs), n_keep, n_remove, n_gone, n_bad))
    print("审计明细 -> %s" % args.out)
    return 0 if n_bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
