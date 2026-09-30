"""hcl_ports.py - 发现当前在线的 HCL 设备控制台，输出"端口 -> 设备名/型号"映射。

HCL 给每台启动的设备分配一个本地 telnet 端口（30001、30002…），但**端口顺序
不由你决定**，跟拓扑里放设备的先后有关。所以拿到拓扑图后第一步永远是先跑这个
脚本，把"哪个端口是哪台设备"确定下来，后面才开始配置。

用法::

    python hcl_ports.py                          # 扫描并打印表格
    python hcl_ports.py --json ports.json        # 同时存成 JSON
    python hcl_ports.py --ports 30001-30020 --no-model   # 跳过 display version（快）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hcldrv import Console, ConsoleError, ConsoleTimeout, _PROMPT   # noqa: E402

MODEL_RE = re.compile(r"(?m)^\s*H3C\s+(\S+)\s+uptime is")


def parse_ports(spec: str) -> list[int]:
    ports: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            ports += list(range(int(lo), int(hi) + 1))
        elif part:
            ports.append(int(part))
    return ports


def probe(host: str, port: int, connect_timeout: float = 0.6,
          prompt_timeout: float = 25.0, want_model: bool = True,
          debug: bool = False) -> dict | None:
    """探测一个端口；没有服务返回 None，有服务就返回身份信息。"""
    con = Console(host=host, port=port, debug=debug)
    try:
        con.connect(timeout=connect_timeout)
    except OSError:
        con.close()
        return None

    info = {"port": port, "hostname": None, "model": None, "prompt": None,
            "auto_config_broken": 0, "error": None}
    try:
        con.prep(timeout=prompt_timeout)
        match = _PROMPT.search(con.text)
        if match:
            info["hostname"] = match.group(1)
            info["prompt"] = match.group(0).strip()
        info["auto_config_broken"] = con.auto_config_breaks
        if want_model:
            try:
                out = con.command("display version", timeout=30)
                model = MODEL_RE.search(out)
                info["model"] = model.group(1) if model else None
            except ConsoleTimeout:
                info["error"] = "display version 超时（仍可用，只是拿不到型号）"
    except (ConsoleError, ConsoleTimeout) as exc:
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        con.close()
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", default="30001-30010")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--json", default=None, help="把结果存成 JSON")
    ap.add_argument("--no-model", action="store_true", help="不跑 display version")
    ap.add_argument("--prompt-timeout", type=float, default=25.0)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    ports = parse_ports(args.ports)
    found: list[dict] = []
    for port in ports:
        info = probe(args.host, port, prompt_timeout=args.prompt_timeout,
                     want_model=not args.no_model, debug=args.debug)
        if info is None:
            continue
        found.append(info)
        print("  %d  %-12s %-10s %s" % (
            port,
            info["hostname"] or "?",
            info["model"] or "?",
            info["error"] or "",
        ), flush=True)

    print()
    if not found:
        print("没有任何 HCL 设备控制台在监听（扫过 %d-%d）。" % (ports[0], ports[-1]))
        print("→ HCL 没启动，或者拓扑还没点『启动』。")
    else:
        print("共发现 %d 台设备。" % len(found))
        print("端口: " + ", ".join(str(i["port"]) for i in found))

    if args.json:
        Path(args.json).write_text(
            json.dumps({"host": args.host, "devices": found},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("已写入 %s" % args.json)
    return 0 if found else 2


if __name__ == "__main__":
    raise SystemExit(main())
