#!/usr/bin/env python3
"""真机「下发→验证→还原」闭环验证（会改设备配置，但会还原）。

支持**单台**与**多台并发**两种模式：多台模式一次把 N 台设备写进同一个 plan，
交给服务器并发下发（`hcl_apply_plan` 的 workers，默认 4、上限 5），
用来验证"并发下发会不会互相打架"——各线程自建控制台连接、各自维护视图状态、
共用同一个证据目录。单台模式下这条路径是走不到的。

步骤（每台设备各自快照/对比）：
  1. 逐台存基线快照（hcl_cfgdiff action=snapshot）
  2. dry-run 预演（hcl_apply_plan dry_run=true）——不碰设备
  3. 真下发（dry_run=false，多台时并发）——改设备
  4. 逐台 diff：应恰好出现期望的改动行
  5. 下发反向命令还原（多台时并发）
  6. 逐台再 diff：应为 0 差异（除非本来就还原不了）

用法：
  python verify_plan_loop.py --port 30022 --name R3 --interface GigabitEthernet0/0
  python verify_plan_loop.py --ports 30014,30015,30016 --interface GigabitEthernet0/0
  python verify_plan_loop.py --ports 30014,30015,30016 --workers 1   # 对照：串行下发
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
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                     # pragma: no cover
    pass


def call(tool: str, arguments: dict, cfg_path: str, timeout: int = 600) -> str:
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


def parse_devices(args, ap) -> list[tuple[str, int]]:
    """把 --port / --ports / --names 解析成 [(名字, 端口), ...]。"""
    if args.ports:
        if args.port is not None:
            ap.error("--port 与 --ports 只能给一个")
        ports = [int(x) for x in re.split(r"[\s,]+", args.ports.strip()) if x.strip()]
    elif args.port is not None:
        ports = [args.port]
    else:
        ap.error("必须给 --port 或 --ports")
    if not ports:
        ap.error("没有解析出任何端口")
    names = [x.strip() for x in re.split(r"[,\s]+", (args.names or "").strip()) if x.strip()]
    out = []
    for index, port in enumerate(ports):
        name = names[index] if index < len(names) else "port-%d" % port
        out.append((name, port))
    return out


def plural_push_summary(text: str) -> tuple[int, int]:
    """从下发输出里抠出 (报错总数, 设备行数)。"""
    errors = 0
    match = re.search(r"合计报错\s+(\d+)\s+处", text)
    if match:
        errors = int(match.group(1))
    device_lines = len(re.findall(r"^\S+ 下发 \d+ 条 报错 \d+", text, re.M))
    return errors, device_lines


def main() -> int:
    ap = argparse.ArgumentParser(description="hcl_apply_plan 真下发闭环验证（支持多设备并发）")
    ap.add_argument("--port", type=int, default=None, help="单台设备控制台端口")
    ap.add_argument("--ports", default=None, help="多台设备端口，逗号分隔（并发下发）")
    ap.add_argument("--names", default=None, help="设备名，逗号分隔（可选）")
    ap.add_argument("--interface", default="GigabitEthernet0/0", help="用于写标记的接口")
    ap.add_argument("--marker", default=None, help="写入的 description 文本")
    ap.add_argument("--workers", type=int, default=None,
                    help="并发设备数（默认交给服务器=4，上限 5；给 1 可做串行对照）")
    ap.add_argument("--keep", action="store_true", help="验证完**不还原**（保留标记）")
    ap.add_argument("--plan-dir", default=None, help="计划文件放哪（默认临时目录）")
    args = ap.parse_args()

    devices = parse_devices(args, ap)
    marker = args.marker or "H3CLAB-VERIFY-DO-NOT-KEEP"
    plan_dir = Path(args.plan_dir) if args.plan_dir else Path(tempfile.mkdtemp(prefix="h3clab-plan-"))
    plan_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = plan_dir / "server-config.json"
    cfg: dict = {}
    if os.environ.get("DSH_HOME"):
        cfg["state_dir"] = str(Path(os.environ["DSH_HOME"]) / "h3clab")
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def cfg_call(tool, arguments):
        return call(tool, arguments, str(cfg_path))

    #: 进入接口视图的命令。传 `GigabitEthernet0/0` 要自动补 `interface ` 关键字，
    #: 否则设备会回 `% Wrong parameter`（实测踩过：caret 正好指在第一个词上）。
    entry_cmd = args.interface.strip()
    if not entry_cmd.lower().startswith("interface "):
        entry_cmd = "interface %s" % entry_cmd

    print("=" * 72)
    print("目标 %d 台设备：%s" % (len(devices), "、".join("%s(%d)" % (n, p) for n, p in devices)))
    print("=" * 72)

    # ---------- 1) 批量快照 ----------
    print()
    if len(devices) == 1:
        name, port = devices[0]
        print("步骤 1/6：存基线快照")
        print("  " + cfg_call("hcl_cfgdiff", {"action": "snapshot", "port": port, "name": name})
              .replace("\n", "\n  "))
    else:
        print("步骤 1/6：批量并发存基线快照（一次调用，%d 台）" % len(devices))
        print(cfg_call("hcl_cfgdiff", {
            "action": "snapshot",
            "ports": [port for _name, port in devices],
            "names": [name for name, _port in devices],
        }))

    def build_plan(tag: str, commands: list[str]) -> str:
        plan = {"devices": [{
            "name": name,
            "port": port,
            "commands": commands,
            "verify": ["display current-configuration interface %s" % args.interface],
        } for name, port in devices]}
        path = plan_dir / ("plan-%s.json" % tag)
        path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(path)

    apply_plan = build_plan("apply", [entry_cmd, "description %s" % marker, "quit"])
    undo_plan = build_plan("undo", [entry_cmd, "undo description", "quit"])

    def push(plan_path: str) -> tuple[str, float]:
        arguments = {"plan_json": plan_path, "dry_run": False}
        if args.workers is not None:
            arguments["workers"] = args.workers
        started = time.monotonic()
        text = cfg_call("hcl_apply_plan", arguments)
        return text, time.monotonic() - started

    # ---------- 2) dry-run ----------
    print()
    print("=" * 72)
    print("步骤 2/6：dry-run 预演（不碰设备）")
    print("=" * 72)
    preview = cfg_call("hcl_apply_plan", {"plan_json": apply_plan, "dry_run": True})
    print(preview)

    # ---------- 3) 真下发 ----------
    print()
    print("=" * 72)
    workers_label = "服务器默认(4)" if args.workers is None else str(args.workers)
    print("步骤 3/6：真下发（dry_run=false，workers=%s）" % workers_label)
    print("=" * 72)
    applied, applied_seconds = push(apply_plan)
    print(applied)
    print("  [耗时] 多设备下发 %.1f 秒" % applied_seconds)

    # ---------- 4) 逐台 diff ----------
    print()
    print("=" * 72)
    print("步骤 4/6：逐台与基线 diff（每台都应出现新增的 description 行）")
    print("=" * 72)
    diffs_after: dict[str, str] = {}
    for name, port in devices:
        text = cfg_call("hcl_cfgdiff", {"action": "diff", "port": port, "name": name})
        diffs_after[name] = text
        print("--- %s (port %d) ---" % (name, port))
        print(text)
        print()

    # ---------- 5) 还原 ----------
    restored: dict[str, str] = {}
    restore_seconds = 0.0
    if args.keep:
        print("（--keep：保留标记，不还原）")
    else:
        print("=" * 72)
        print("步骤 5/6：下反向命令还原（同样是并发）")
        print("=" * 72)
        undo_text, restore_seconds = push(undo_plan)
        print(undo_text)
        print("  [耗时] 多设备还原 %.1f 秒" % restore_seconds)

        print()
        print("=" * 72)
        print("步骤 6/6：逐台再 diff（都应为 0 差异）")
        print("=" * 72)
        for name, port in devices:
            text = cfg_call("hcl_cfgdiff", {"action": "diff", "port": port, "name": name})
            restored[name] = text
            print("--- %s (port %d) ---" % (name, port))
            print(text)
            print()

    # ---------- 判定 ----------
    print("=" * 72)
    print("判定")
    print("=" * 72)
    push_errors, device_lines = plural_push_summary(applied)
    checks: list[tuple[str, bool]] = [
        ("dry-run 明确说明未碰设备", "不会碰设备" in preview),
        ("dry-run 覆盖了全部 %d 台设备" % len(devices),
         ("%d 台设备" % len(devices)) in preview or len(devices) == 1),
        ("真下发零报错", push_errors == 0 and "% Unrecognized" not in applied
         and "% Wrong parameter" not in applied),
        ("真下发给每台都出了结果行（%d 行）" % len(devices), device_lines == len(devices)),
    ]
    for name, _port in devices:
        checks.append(("%s：diff 里出现新标记" % name, marker in diffs_after.get(name, "")))
    if not args.keep:
        undo_errors, undo_lines = plural_push_summary(undo_text)
        checks.append(("还原零报错", undo_errors == 0))
        checks.append(("还原给每台都出了结果行", undo_lines == len(devices)))
        for name, _port in devices:
            checks.append(("%s：还原后与基线 0 差异" % name,
                           "没有差异" in restored.get(name, "")))
    failed = [label for label, ok in checks if not ok]
    for label, ok in checks:
        print("  %s %s" % ("PASS" if ok else "FAIL", label))
    print()
    if len(devices) > 1:
        print("参考：若串行下发，单台约需数秒；本次 %d 台并发 %.1f 秒。"
              % (len(devices), applied_seconds))
    print("计划/配置目录：%s" % plan_dir)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
