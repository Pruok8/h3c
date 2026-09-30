#!/usr/bin/env python3
"""restore.py - 从 `display current-configuration` 快照生成可下发的 plan.json（整机或单节）。

用途：配置改坏了，从 `--snapshot` 存的快照里把**那一节**（通常是一个 interface）回滚。
比重新推导便宜得多，也比"凭记忆重配"可靠。

用法::

    python restore.py SW1.txt --list-sections                 # 先看快照里有哪些节
    python restore.py SW1.txt --section "interface GigabitEthernet 1/0/20" \
        --device SW1 --port 30008 --out plan-restore.json     # 生成 plan（不下发）
    python restore.py SW1.txt --section "interface Vlan-interface 30" --apply

    # 整机快照（一般只在 reset saved-configuration + reboot 之后才用）
    python restore.py SW1.txt --device SW1 --port 30008 --whole

安全：**默认只生成 plan.json，不下发**；`--apply` 才会调 hcl_lab.py。
整机恢复必须显式加 `--whole`。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_JUNK = ("---- More ----", "screen-length disable", "return", "#", "$ ",
         "当前提示符:", "已 exit 到用户视图", "[已收到，未等到提示符]")


def load_sections(path: Path) -> list[list[str]]:
    """按 `#` 分节；丢掉命令回显与结束标记。"""
    sections: list[list[str]] = []
    cur: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = raw.strip()
        if not s or s.startswith(_JUNK):
            if s == "#" and cur:
                sections.append(cur)
                cur = []
            continue
        cur.append(s)
    if cur:
        sections.append(cur)
    return [sec for sec in sections if sec]


def main() -> int:
    ap = argparse.ArgumentParser(description="从快照生成可下发的 plan.json")
    ap.add_argument("snapshot", help="display current-configuration 的快照文件")
    ap.add_argument("--section", help="只恢复这一节（前缀匹配，如 'interface Vlan-interface 30'）")
    ap.add_argument("--list-sections", action="store_true", help="列出快照里的节")
    ap.add_argument("--device", default="DEV", help="设备名（写进 plan）")
    ap.add_argument("--port", type=int, required=False, help="控制台端口（--apply 时必需）")
    ap.add_argument("--out", help="输出 plan.json（默认 plan-restore.json）")
    ap.add_argument("--whole", action="store_true", help="整机恢复（不加 --section 时也允许）")
    ap.add_argument("--apply", action="store_true", help="生成后立刻下发")
    args = ap.parse_args()

    secs = load_sections(Path(args.snapshot))
    if not secs:
        print("快照里没解析出配置节：%s" % args.snapshot)
        return 2

    if args.list_sections:
        print("共 %d 节：" % len(secs))
        for i, sec in enumerate(secs):
            print("  [%2d] %s%s" % (i, sec[0][:80], "  (+%d 行)" % (len(sec) - 1) if len(sec) > 1 else ""))
        return 0

    if args.section:
        want = args.section.strip().lower()
        hit = next((s for s in secs if s[0].lower().startswith(want)), None)
        if hit is None:
            hit = next((s for s in secs if want in "\n".join(s).lower()), None)
        if hit is None:
            print("快照里找不到匹配 '%s' 的节（先用 --list-sections 看有哪些）" % args.section)
            return 2
        cmds = hit
    elif args.whole:
        cmds = [ln for sec in secs for ln in sec]
    else:
        print("要么给 --section（推荐），要么显式加 --whole（整机恢复）")
        return 2

    out = Path(args.out or "plan-restore.json")
    plan = {"devices": [{"name": args.device, "port": args.port, "commands": cmds,
                         "verify": []}]}
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print("生成 %s：%s 共 %d 条命令（首条：%s）" % (out, args.device, len(cmds), cmds[0][:60]))
    print("建议先预演：python hcl_lab.py %s" % out)

    if args.apply:
        if not args.port:
            print("--apply 需要 --port")
            return 2
        print("\n-- 下发 --")
        r = subprocess.run([sys.executable, str(HERE / "hcl_lab.py"), str(out),
                            "--apply", "--auto-confirm"], text=True, encoding="utf-8")
        return r.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
