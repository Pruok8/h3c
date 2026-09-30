#!/usr/bin/env python3
"""真机「下发→验证→还原」完整闭环验证（会改设备配置，但会还原）。

用途：证明写入路径（hcl_apply_plan 的真下发）真的能用，并且能用 hcl_cfgdiff
逐行确认"改了什么"和"确实还原了"。

步骤：
  1. 存基线快照（hcl_cfgdiff action=snapshot）
  2. dry-run 预演（hcl_apply_plan dry_run=true）——不碰设备
  3. 真下发（dry_run=false）——改设备
  4. 再取快照并 diff：应恰好出现期望的改动行
  5. 下发反向命令还原
  6. 再 diff：应为 0 差异（除非本来就还原不了）

用法：
  python verify_plan_loop.py --port 30022 --name R3 --interface GigabitEthernet0/0
  python verify_plan_loop.py --port 30022 --name R3 --interface GigabitEthernet0/0 --keep
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                     # pragma: no cover
    pass


def call(tool: str, arguments: dict, cfg_path: str, timeout: int = 300) -> str:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "verify-plan-loop", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": tool, "arguments": arguments}},
    ]
    env = dict(os.environ)
    env["H3C_MCP_CONFIG"] = cfg_path
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run([sys.executable, str(SERVER)],
                          input="".join(json.dumps(m) + "\n" for m in messages),
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=timeout)
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
                             if isinstance(b, dict))
    return "(没有收到响应) stderr=" + proc.stderr[-800:]


def main() -> int:
    ap = argparse.ArgumentParser(description="hcl_apply_plan 真下发闭环验证")
    ap.add_argument("--port", type=int, required=True, help="目标设备控制台端口")
    ap.add_argument("--name", default=None, help="设备名（快照文件名前缀）")
    ap.add_argument("--interface", default="GigabitEthernet0/0", help="用于写标记的接口")
    ap.add_argument("--marker", default=None, help="写入的 description 文本")
    ap.add_argument("--keep", action="store_true", help="验证完**不还原**（保留标记）")
    ap.add_argument("--plan-dir", default=None, help="计划文件放哪（默认临时目录）")
    args = ap.parse_args()

    name = args.name or ("port-%d" % args.port)
    marker = args.marker or "H3CLAB-VERIFY-DO-NOT-KEEP"
    plan_dir = Path(args.plan_dir) if args.plan_dir else Path(tempfile.mkdtemp(prefix="h3clab-plan-"))
    plan_dir.mkdir(parents=True, exist_ok=True)

    cfg = {}
    cfg_path = plan_dir / "server-config.json"
    if os.environ.get("DSH_HOME"):
        cfg["state_dir"] = str(Path(os.environ["DSH_HOME"]) / "h3clab")
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def cfg_call(tool, arguments):
        return call(tool, arguments, str(cfg_path))

    print("=" * 72)
    print("步骤 1/6：存基线快照")
    print("=" * 72)
    print(cfg_call("hcl_cfgdiff", {"action": "snapshot", "port": args.port, "name": name}))

    #: 进入接口视图的命令。传 `GigabitEthernet0/0` 时要自动补 `interface ` 关键字，
    #: 否则设备会回 `% Wrong parameter`（实测踩过：caret 正好指在第一个词上）。
    entry_cmd = args.interface.strip()
    if not entry_cmd.lower().startswith("interface "):
        entry_cmd = "interface %s" % entry_cmd

    apply_plan = {"devices": [{
        "name": name,
        "port": args.port,
        "commands": [entry_cmd, "description %s" % marker, "quit"],
        "verify": ["display current-configuration interface %s" % args.interface],
    }]}
    plan_file = plan_dir / "plan-apply.json"
    plan_file.write_text(json.dumps(apply_plan, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 72)
    print("步骤 2/6：dry-run 预演（不碰设备）")
    print("=" * 72)
    preview = cfg_call("hcl_apply_plan", {"plan_json": str(plan_file), "dry_run": True})
    print(preview)

    print()
    print("=" * 72)
    print("步骤 3/6：真下发（dry_run=false）")
    print("=" * 72)
    applied = cfg_call("hcl_apply_plan", {"plan_json": str(plan_file), "dry_run": False})
    print(applied)

    print()
    print("=" * 72)
    print("步骤 4/6：与基线 diff（应出现新增的 description 行）")
    print("=" * 72)
    diff_after = cfg_call("hcl_cfgdiff", {"action": "diff", "port": args.port, "name": name})
    print(diff_after)

    restored_ok = None
    if args.keep:
        print()
        print("（--keep：保留标记，不还原）")
    else:
        undo_plan = {"devices": [{
            "name": name,
            "port": args.port,
            "commands": [entry_cmd, "undo description", "quit"],
            "verify": ["display current-configuration interface %s" % args.interface],
        }]}
        undo_file = plan_dir / "plan-undo.json"
        undo_file.write_text(json.dumps(undo_plan, ensure_ascii=False, indent=2), encoding="utf-8")

        print()
        print("=" * 72)
        print("步骤 5/6：下反向命令还原")
        print("=" * 72)
        print(cfg_call("hcl_apply_plan", {"plan_json": str(undo_file), "dry_run": False}))

        print()
        print("=" * 72)
        print("步骤 6/6：再 diff（应 0 差异）")
        print("=" * 72)
        diff_restore = cfg_call("hcl_cfgdiff", {"action": "diff", "port": args.port, "name": name})
        print(diff_restore)
        restored_ok = "没有差异" in diff_restore

    print()
    print("=" * 72)
    print("判定")
    print("=" * 72)
    checks = [
        ("dry-run 明确说明未碰设备", "不会碰设备" in preview),
        ("真下发零报错（看 '合计报错 N 处' 是否为 0）",
         "合计报错 0 处" in applied and "% Unrecognized" not in applied
         and "% Wrong parameter" not in applied),
        ("diff 里出现了新标记", marker in diff_after),
    ]
    if restored_ok is not None:
        checks.append(("反向命令后与基线 0 差异（已还原）", restored_ok))
    failed = [label for label, ok in checks if not ok]
    for label, ok in checks:
        print("  %s %s" % ("PASS" if ok else "FAIL", label))
    print()
    print("计划/配置目录：%s" % plan_dir)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
