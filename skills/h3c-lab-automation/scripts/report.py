#!/usr/bin/env python3
"""report.py - 把 verify 报告 + 拓扑 + 证据目录 + 说明，合成一份交付 Markdown。

为什么需要
----------
验证矩阵以前是手写进对话和文档的，又长又容易漏。现在：
`verify.py` 产出机器可读的报告 → 本脚本把它转成交付文档（表格化、带证据索引）。

spec.json::

    {
      "title": "LAB4_TS MPLS Option B 跨域 + M-LAG",
      "subtitle": "HCL 5.10.3 / S6850 + S5820V2 + MSR36",
      "net": "D:\\NET\\ie\\e\\kongpei\\lab4_ts.net",
      "verify_report": "verify-report.txt",
      "evidence_dirs": ["r60", "r61", "save23"],
      "evidence_root": "D:\\DSH\\NET\\lab4",
      "notes": ["MAD 口按参考解法在实验环境关闭（mad 开启会卡顿）",
                "DHCPv6 走 IRF↔PC 之间的全局旁路网段 vlan 900"],
      "out": "交付报告.md"
    }

用法::

    python report.py spec.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_ROW = re.compile(r"^-\s+(PASS|FAIL)\s+(\S+)\s*(.*)$")
_DEV = re.compile(r"^##\s+(.+?)\s*$")
_SUM = re.compile(r"合计\s+(\d+)\s+项：PASS\s+(\d+)，FAIL\s+(\d+)")


def parse_verify(path: Path) -> tuple[list[tuple[str, str, str, str]], str]:
    rows: list[tuple[str, str, str, str]] = []
    summary = ""
    dev = "-"
    if not path.exists():
        return rows, "(找不到验证报告：%s)" % path
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _DEV.match(line)
        if m and not line.startswith("## "):
            dev = m.group(1)
            continue
        if line.startswith("## "):
            dev = line[3:].strip()
            continue
        m = _ROW.match(line.strip())
        if m:
            rows.append((dev, m.group(2), m.group(1), m.group(3).strip()))
        m2 = _SUM.search(line)
        if m2:
            summary = "共 %s 项：PASS %s，FAIL %s" % m2.groups()
    return rows, summary


def main() -> int:
    ap = argparse.ArgumentParser(description="合成交付报告")
    ap.add_argument("spec", help="spec.json")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    out = Path(spec.get("out") or "交付报告.md")
    md: list[str] = ["# %s" % spec.get("title", "交付报告"), ""]
    if spec.get("subtitle"):
        md += ["*%s*" % spec["subtitle"], ""]

    # ---- 验证矩阵 ----
    rows, summary = parse_verify(Path(spec.get("verify_report") or "verify-report.txt"))
    md += ["## 一、验证矩阵", "",
           summary or "(未提供 verify 报告)", ""]
    if rows:
        md += ["| 设备 | 项 | 结果 | 说明 |", "|---|---|---|---|"]
        for dev, cid, res, desc in rows:
            md.append("| %s | %s | %s | %s |" % (dev, cid, "✅" if res == "PASS" else "❌", desc))
        md.append("")
        fails = [r for r in rows if r[2] == "FAIL"]
        if fails:
            md += ["**未通过项**：%s" % "、".join("%s(%s)" % (r[1], r[0]) for r in fails), ""]

    # ---- 拓扑 ----
    if spec.get("net"):
        try:
            from net2map import dump_mermaid, dump_tables, parse_net  # type: ignore
            devs = parse_net(Path(spec["net"]))
            md += ["## 二、拓扑", "", "来源：`%s`" % spec["net"], "",
                   dump_tables(devs), "", dump_mermaid(devs), ""]
        except Exception as exc:  # noqa: BLE001
            md += ["## 二、拓扑", "", "（解析 %s 失败：%s）" % (spec["net"], exc), ""]

    # ---- 证据 ----
    if spec.get("evidence_dirs"):
        root = Path(spec.get("evidence_root") or ".")
        md += ["## 三、证据文件", "", "| 目录 | 文件数 |", "|---|---|"]
        for d in spec["evidence_dirs"]:
            p = root / d
            n = len(list(p.glob("*"))) if p.exists() else 0
            md.append("| `%s` | %d |" % (d, n))
        md.append("")

    # ---- 说明与未决项 ----
    notes = spec.get("notes") or []
    md += ["## 四、说明与未决项", ""]
    md += ["- %s" % n for n in notes] if notes else ["（无）"]
    md.append("")

    out.write_text("\n".join(md), encoding="utf-8")
    print("交付报告 -> %s（验证项 %d，%s）" % (out, len(rows), summary or "无汇总"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
