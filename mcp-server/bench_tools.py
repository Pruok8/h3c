#!/usr/bin/env python3
"""bench_tools.py - 真机基准：量关键工具的墙钟耗时，用于优化前后对比。

只跑**只读**工具（hcl_list_devices / hcl_verify / hcl_cfgdiff snapshot），
不会改任何设备配置。

用法：
  python bench_tools.py --ports 30001,30006,30009,30014,30015,30016
  python bench_tools.py --ports 30001,30006 --repeat 2
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                     # pragma: no cover
    pass


def call(tool: str, arguments: dict, cfg_path: str, timeout: int = 900) -> tuple[str, float]:
    """返回 (工具返回文本, 墙钟秒数)。"""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "bench", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": tool, "arguments": arguments}},
    ]
    env = dict(os.environ)
    env["H3C_MCP_CONFIG"] = cfg_path
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(SERVER)],
                          input="".join(json.dumps(m) + "\n" for m in messages),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=timeout)
    elapsed = time.monotonic() - started
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if isinstance(msg, dict) and msg.get("id") == 2:
            result = msg.get("result") or {}
            text = "\n".join(b.get("text", "") for b in (result.get("content") or [])
                             if isinstance(b, dict))
            return text, elapsed
    return "(没有收到响应) stderr=" + proc.stderr[-400:], elapsed


def summarize(text: str, limit: int = 120) -> str:
    flat = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def main() -> int:
    ap = argparse.ArgumentParser(description="真机只读基准")
    ap.add_argument("--ports", default=None, help="参与基准的设备端口，逗号分隔")
    ap.add_argument("--all-ports", action="store_true", help="用拓扑/配置里的全部端口")
    ap.add_argument("--repeat", type=int, default=1, help="每项重复次数，取最小值")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="h3clab-bench-"))
    cfg: dict = {}
    if os.environ.get("DSH_HOME"):
        cfg["state_dir"] = str(Path(os.environ["DSH_HOME"]) / "h3clab")
        cfg["evidence_root"] = str(Path(os.environ["DSH_HOME"]) / "h3clab" / "evidence")
    cfg_path = tmp / "server-config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    ports = [int(x) for x in re.split(r"[\s,]+", (args.ports or "").strip()) if x.strip()]

    print("=" * 78)
    print("基准：只读工具墙钟耗时（每项跑 %d 次，取最小值）" % args.repeat)
    print("=" * 78)

    results: list[tuple[str, float, str]] = []

    def bench(label: str, tool: str, arguments: dict) -> str:
        best = float("inf")
        text = ""
        for _ in range(max(1, args.repeat)):
            text, elapsed = call(tool, arguments, str(cfg_path))
            best = min(best, elapsed)
        results.append((label, best, summarize(text)))
        print("  %-42s %7.2f s" % (label, best))
        return text

    # 1) 全量设备探测
    if args.all_ports or not ports:
        bench("hcl_list_devices（全部端口，model=true）", "hcl_list_devices", {})
    else:
        bench("hcl_list_devices（%d 个端口，model=true）" % len(ports),
              "hcl_list_devices", {"ports": ports})

    if not ports:
        print("\n（没给 --ports，后面几项跳过）")
    else:
        # 2) 验证矩阵：每台 2 项
        checks = []
        for port in ports:
            checks.append({"id": "VER-%d" % port, "name": "p%d" % port, "port": port,
                           "desc": "版本含 Comware", "cmd": "display version",
                           "expect": ["Comware"]})
            checks.append({"id": "IF-%d" % port, "name": "p%d" % port, "port": port,
                           "desc": "接口表可读", "cmd": "display interface brief",
                           "expect": ["GE1/0/"]})
        checklist = tmp / "bench-checklist.json"
        checklist.write_text(json.dumps({"checks": checks}, ensure_ascii=False), encoding="utf-8")
        bench("hcl_verify（%d 台 / %d 项）" % (len(ports), len(checks)),
              "hcl_verify", {"checklist_json": str(checklist)})
        bench("hcl_verify（同上，workers=1 串行对照）",
              "hcl_verify", {"checklist_json": str(checklist), "workers": 1})

        # 3) 配置快照：逐台（单台 API，对照组）
        started = time.monotonic()
        for port in ports:
            call("hcl_cfgdiff", {"action": "snapshot", "port": port, "name": "bench-%d" % port},
                 str(cfg_path))
        total = time.monotonic() - started
        results.append(("hcl_cfgdiff snapshot（逐台 %d 次调用）" % len(ports), total, ""))
        print("  %-42s %7.2f s" % ("hcl_cfgdiff snapshot（逐台 %d 次调用）" % len(ports), total))

        # 4) 配置快照：批量并发（一次调用）+ 批量串行对照
        bench("hcl_cfgdiff snapshot（批量 %d 台并发）" % len(ports),
              "hcl_cfgdiff",
              {"action": "snapshot", "ports": ports,
               "names": ["benchb-%d" % port for port in ports]})
        bench("hcl_cfgdiff snapshot（批量 %d 台，workers=1 串行对照）" % len(ports),
              "hcl_cfgdiff",
              {"action": "snapshot", "ports": ports, "workers": 1,
               "names": ["benchw1-%d" % port for port in ports]})

    print()
    print("=" * 78)
    print("合计 %.2f 秒" % sum(item[1] for item in results))
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
