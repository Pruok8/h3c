#!/usr/bin/env python3
"""net2map.py - 解析 HCL 的 `.net` 拓扑文件 → 设备表 / 端口↔对端映射 / Mermaid 图。

为什么需要
----------
排查"某条链路为什么不通"时，`.net` 是**唯一权威**的连线来源：
`display interface brief` 只告诉你端口 UP/DOWN，不会告诉你它连到谁。
本次真实教训：以为 PE 上联用的是 `GE5/0`（那个口根本没接线），实际是 `GE0/0` + `GE6/0`。

端口公式：**控制台端口 = 30000 + device_id**（.net 里每个 `[[型号 名字]]` 段都有 `device_id`）。

用法
----
::

    python net2map.py lab4_ts.net                      # 打印设备表 + 连线表
    python net2map.py lab4_ts.net --port-of PE1        # 只看某台的端口与对端
    python net2map.py lab4_ts.net --md topology.md     # 导出 Markdown（含 Mermaid 图）
    python net2map.py lab4_ts.net --json topology.json # 导出 JSON（给别的脚本用）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SECTION = re.compile(r"^\[\[(.+?)\]\]\s*$")
_LINKKEY = re.compile(r"^[A-Za-z][A-Za-z-]*_\d+/\d+$")
_SKIP_TYPE = {"NOTE", "SHAPE"}


class Dev:
    def __init__(self, name: str, dtype: str) -> None:
        self.name = name
        self.dtype = dtype
        self.did: int | None = None
        self.slot = ""
        self.links: list[tuple[str, str, str]] = []   # (本端端口, 对端设备, 对端端口)

    @property
    def port(self) -> int | None:
        return None if self.did is None else 30000 + self.did

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.dtype, "device_id": self.did,
                "console_port": self.port, "slot0": self.slot,
                "links": [{"local": a, "peer": b, "peer_port": c} for a, b, c in self.links]}


def parse_net(path: Path) -> dict[str, Dev]:
    devs: dict[str, Dev] = {}
    cur: Dev | None = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        m = _SECTION.match(line)
        if m:
            head = m.group(1)
            dtype, _, name = head.partition(" ")
            cur = None
            if dtype.upper() not in _SKIP_TYPE and name:
                cur = Dev(name, dtype)
                devs[name] = cur
            continue
        if cur is None or "=" not in line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key == "device_id":
            try:
                cur.did = int(val)
            except ValueError:
                pass
        elif key == "slot0":
            cur.slot = val
        elif _LINKKEY.match(key):
            parts = val.split()
            if len(parts) >= 2:
                cur.links.append((key, parts[0], parts[-1]))
            else:
                cur.links.append((key, val or "?", "?"))
    return devs


def dump_tables(devs: dict[str, Dev]) -> str:
    out = ["== 设备表（控制台端口 = 30000 + device_id）==",
           "| 名称 | 型号 | device_id | 控制台端口 |", "|---|---|---|---|"]
    for d in devs.values():
        out.append("| %s | %s | %s | %s |" % (d.name, d.dtype, d.did, d.port))
    out += ["", "== 连线表（.net 原文，权威）=="]
    seen: set[frozenset] = set()
    for d in devs.values():
        for lport, peer, pport in d.links:
            key = frozenset({(d.name, lport), (peer, pport)})
            if key in seen:
                continue
            seen.add(key)
            out.append("  %-14s %-12s <-->  %-14s %s" % (d.name, lport, peer, pport))
    n_link = len(seen)
    out.append("  （共 %d 个设备、%d 条链路）" % (len(devs), n_link))
    return "\n".join(out)


def dump_device(devs: dict[str, Dev], name: str) -> str:
    d = next((x for x in devs.values() if x.name.lower() == name.lower()), None)
    if d is None:
        return "没有这台设备：%s（现有：%s）" % (name, ", ".join(x.name for x in devs.values()))
    out = ["%s（%s，device_id=%s，控制台 127.0.0.1:%s）" % (d.name, d.dtype, d.did, d.port)]
    for lport, peer, pport in d.links:
        out.append("  %-12s <--> %s %s" % (lport, peer, pport))
    if not d.links:
        out.append("  （该设备在 .net 里没有连任何线）")
    return "\n".join(out)


def dump_mermaid(devs: dict[str, Dev]) -> str:
    out = ["```mermaid", "graph LR"]
    for d in devs.values():
        out.append('  %s["%s<br/>%s<br/>:%s"]' % (d.name.replace("-", "_"), d.name, d.dtype, d.port))
    seen: set[frozenset] = set()
    for d in devs.values():
        for lport, peer, pport in d.links:
            key = frozenset({(d.name, lport), (peer, pport)})
            if key in seen:
                continue
            seen.add(key)
            out.append('  %s ---|"%s ↔ %s"| %s'
                       % (d.name.replace("-", "_"), lport, pport, peer.replace("-", "_")))
    out.append("```")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="解析 HCL .net 拓扑 → 设备表/连线/Mermaid")
    ap.add_argument("net", help=".net 文件路径")
    ap.add_argument("--md", help="同时写 Markdown（含 Mermaid）到该文件")
    ap.add_argument("--json", dest="json_out", help="同时写 JSON 到该文件")
    ap.add_argument("--port-of", help="只查某台设备的端口与对端")
    ap.add_argument("--quiet", action="store_true", help="不打印表格（只写文件）")
    args = ap.parse_args()

    p = Path(args.net)
    if not p.exists():
        print("找不到 .net 文件：%s" % p)
        return 2
    devs = parse_net(p)
    if not devs:
        print("没解析出设备 —— 确认这是 HCL 的 .net（形如 [[型号 名字]] / GE_0/0 = PEER PORT）")
        return 2

    if args.port_of:
        print(dump_device(devs, args.port_of))
    elif not args.quiet:
        print(dump_tables(devs))

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"devices": [d.to_dict() for d in devs.values()]},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print("JSON -> %s" % args.json_out)
    if args.md:
        Path(args.md).write_text("# 拓扑（来自 %s）\n\n%s\n\n## 拓扑图\n\n%s\n"
                                 % (p.name, dump_tables(devs), dump_mermaid(devs)), encoding="utf-8")
        print("Markdown -> %s" % args.md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
