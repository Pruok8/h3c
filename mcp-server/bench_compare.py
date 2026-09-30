#!/usr/bin/env python3
"""bench_compare.py - 优化前后现场对比：从 git 取旧版 server.py，两个版本各跑一遍。

为什么要这样比：拿"几天前记的数字"对照容易受设备预热/负载影响。这里把**旧版
server.py 从 git 里原样取出**，与新版在**同一会话、同一批设备、几分钟内**各跑一次，
差异只可能来自代码。

对比内容（都是只读操作，不改设备配置）：
  · hcl_list_devices       新旧都并发，用来确认没有退化
  · hcl_verify             旧版一次调用内部串行；新版跨设备并发（workers，默认 8）
  · hcl_cfgdiff snapshot   旧版一次只能一台（N 次调用）；新版支持批量（1 次调用并发）

用法：
  python bench_compare.py --ports 30001,30006,30009,30014,30015,30016
  python bench_compare.py --ports 30001,30006 --old-rev b24e5a9 --repeat 2
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
NEW_SERVER = HERE / "server.py"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                     # pragma: no cover
    pass


def repo_root(start: Path) -> Path | None:
    """向上找 git 仓库根。"""
    cur = start
    for _ in range(6):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def extract_from_git(root: Path, rev: str, rel: str, dest: Path) -> bool:
    """把 <rev>:<rel> 的内容写到 dest。"""
    proc = subprocess.run(["git", "-C", str(root), "show", "%s:%s" % (rev, rel)],
                          capture_output=True, timeout=120)
    if proc.returncode != 0:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(proc.stdout)
    return True


def call(server: Path, tool: str, arguments: dict, cfg_path: Path, timeout: int = 900) -> tuple[str, float]:
    """调一次工具，返回 (文本, 墙钟秒数)。每台设备实例化一个独立子进程。"""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "bench-compare", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": tool, "arguments": arguments}},
    ]
    env = dict(os.environ)
    env["H3C_MCP_CONFIG"] = str(cfg_path)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    started = time.monotonic()
    proc = subprocess.run([sys.executable, str(server)],
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
            return "\n".join(b.get("text", "") for b in (result.get("content") or [])
                             if isinstance(b, dict)), elapsed
    return "(没有收到响应) stderr=" + proc.stderr[-400:], elapsed


def one_line(text: str, limit: int = 90) -> str:
    return " ".join(line.strip() for line in text.splitlines() if line.strip())[:limit]


def main() -> int:
    ap = argparse.ArgumentParser(description="优化前后真机速度对比")
    ap.add_argument("--ports", required=True, help="设备端口，逗号分隔")
    ap.add_argument("--old-rev", default="HEAD~1",
                    help="对比用的旧版提交（默认 HEAD~1，即上一个提交）")
    ap.add_argument("--repeat", type=int, default=1, help="每项重复次数，取最小值")
    ap.add_argument("--checklist", default=None, help="自定义验证清单；默认自动生成")
    args = ap.parse_args()

    ports = [int(x) for x in re.split(r"[\s,]+", args.ports.strip()) if x.strip()]
    if not ports:
        print("没有解析出端口"); return 2

    root = repo_root(HERE)
    tmp = Path(tempfile.mkdtemp(prefix="h3clab-compare-"))
    cfg_path = tmp / "cfg.json"
    # 状态/证据目录都指到临时目录，别污染用户真实的 <DSH_HOME>\h3clab
    cfg_path.write_text(json.dumps({
        "state_dir": str(tmp / "state"),
        "evidence_root": str(tmp / "evidence"),
    }, ensure_ascii=False), encoding="utf-8")

    # ---------- 准备旧版 ----------
    old_dir = tmp / "old"
    old_server = old_dir / "server.py"
    have_old = False
    if root is not None:
        ok_py = extract_from_git(root, args.old_rev, "mcp-server/server.py", old_server)
        ok_drv = extract_from_git(root, args.old_rev, "mcp-server/hcldrv.py", old_dir / "hcldrv.py")
        have_old = ok_py and ok_drv
    if not have_old:
        # 退化：直接用当前版本（那就没有对比，只剩新版数字）
        shutil.copy2(NEW_SERVER, old_server)
        shutil.copy2(HERE / "hcldrv.py", old_dir / "hcldrv.py")
        print("⚠ 取不到旧版（%s），下面两个版本会一样——只作参考。" % args.old_rev)

    # ---------- 清单 ----------
    if args.checklist:
        checklist = Path(args.checklist)
    else:
        checks = []
        for port in ports:
            checks.append({"id": "VER-%d" % port, "name": "p%d" % port, "port": port,
                           "desc": "版本含 Comware", "cmd": "display version",
                           "expect": ["Comware"]})
            checks.append({"id": "IF-%d" % port, "name": "p%d" % port, "port": port,
                           "desc": "接口表可读", "cmd": "display interface brief",
                           "expect": ["GE1/0/"]})
        checklist = tmp / "checklist.json"
        checklist.write_text(json.dumps({"checks": checks}, ensure_ascii=False), encoding="utf-8")

    results: dict[str, float] = {}

    def bench(label: str, server: Path, tool: str, arguments: dict, group: str) -> str:
        best = float("inf")
        text = ""
        for _ in range(max(1, args.repeat)):
            text, elapsed = call(server, tool, arguments, cfg_path)
            best = min(best, elapsed)
        results["%s|%s" % (group, label)] = best     # 必须按组分开存，否则新旧同名会互相覆盖
        print("  [%s] %-44s %7.2f s   %s" % (group, label, best, one_line(text, 60)))
        return text

    print("=" * 92)
    print("优化前后现场对比 | 旧版 = %s | %d 台设备 | 每项跑 %d 次取最小" % (args.old_rev, len(ports), args.repeat))
    print("=" * 92)

    # ---------- 旧版 ----------
    print()
    print("【优化前】%s" % args.old_rev)
    bench("hcl_list_devices（%d 端口）" % len(ports), old_server, "hcl_list_devices",
          {"ports": ports}, "旧")
    bench("hcl_verify（%d 台 / %d 项）" % (len(ports), len(ports) * 2), old_server, "hcl_verify",
          {"checklist_json": str(checklist)}, "旧")

    started = time.monotonic()
    for port in ports:
        call(old_server, "hcl_cfgdiff",
             {"action": "snapshot", "port": port, "name": "old-%d" % port}, cfg_path)
    old_seq = time.monotonic() - started
    results["旧|hcl_cfgdiff snapshot（逐台 %d 次调用）" % len(ports)] = old_seq
    print("  [旧] %-44s %7.2f s   （%d 次独立调用）"
          % ("hcl_cfgdiff snapshot（逐台 %d 次调用）" % len(ports), old_seq, len(ports)))

    # ---------- 新版 ----------
    print()
    print("【优化后】当前工作区 %s" % NEW_SERVER)
    bench("hcl_list_devices（%d 端口）" % len(ports), NEW_SERVER, "hcl_list_devices",
          {"ports": ports}, "新")
    bench("hcl_verify（%d 台 / %d 项）" % (len(ports), len(ports) * 2), NEW_SERVER, "hcl_verify",
          {"checklist_json": str(checklist)}, "新")
    bench("hcl_verify（workers=1 串行对照）", NEW_SERVER, "hcl_verify",
          {"checklist_json": str(checklist), "workers": 1}, "新")
    bench("hcl_cfgdiff snapshot（批量 %d 台并发）" % len(ports), NEW_SERVER, "hcl_cfgdiff",
          {"action": "snapshot", "ports": ports,
           "names": ["new-%d" % p for p in ports]}, "新")
    bench("hcl_cfgdiff snapshot（workers=1 串行对照）", NEW_SERVER, "hcl_cfgdiff",
          {"action": "snapshot", "ports": ports, "workers": 1,
           "names": ["new1-%d" % p for p in ports]}, "新")

    # ---------- 汇总 ----------
    print()
    print("=" * 92)
    print("对比")
    print("=" * 92)

    def speedup(old_label: str, new_label: str, title: str) -> None:
        old = results.get("旧|" + old_label)
        new = results.get("新|" + new_label)
        if old is None or new is None or new <= 0:
            print("  %-34s 数据不足" % title)
            return
        print("  %-34s 旧 %6.2f s  ->  新 %6.2f s     %5.2fx" % (title, old, new, old / new))

    verify_label = "hcl_verify（%d 台 / %d 项）" % (len(ports), len(ports) * 2)
    speedup(verify_label, verify_label, "hcl_verify")
    speedup("hcl_cfgdiff snapshot（逐台 %d 次调用）" % len(ports),
            "hcl_cfgdiff snapshot（批量 %d 台并发）" % len(ports), "hcl_cfgdiff")
    devices_label = "hcl_list_devices（%d 端口）" % len(ports)
    old_dev = results.get("旧|" + devices_label)
    new_dev = results.get("新|" + devices_label)
    if old_dev is not None and new_dev is not None and new_dev > 0:
        print("  %-34s 旧 %6.2f s  ->  新 %6.2f s     %5.2fx  （该路径未改动，应≈1.0）"
              % ("hcl_list_devices", old_dev, new_dev, old_dev / new_dev))
    print()
    print("新版内部对照（证明收益来自并发，而不是环境）：")
    for title, serial_label, parallel_label in (
        ("hcl_verify", "hcl_verify（workers=1 串行对照）", verify_label),
        ("hcl_cfgdiff", "hcl_cfgdiff snapshot（workers=1 串行对照）",
         "hcl_cfgdiff snapshot（批量 %d 台并发）" % len(ports)),
    ):
        serial = results.get("新|" + serial_label)
        parallel = results.get("新|" + parallel_label)
        if serial and parallel and parallel > 0:
            print("  %-34s 串行 %6.2f s  ->  并发 %6.2f s     %5.2fx"
                  % (title, serial, parallel, serial / parallel))
    print()
    print("临时目录：%s" % tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
