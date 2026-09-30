#!/usr/bin/env python3
"""verify_calls.py - 把 README「验证结果」一节需要的**真实**工具调用跑一遍并存档。

它用子进程把 ``server.py`` 拉起来，逐个发 ``tools/call``，把每个工具的
原始返回（JSON）与可读文本写进 ``evidence/selftest-calls-<时间戳>/``。

用法::

    python verify_calls.py            # 全部 8 个工具都调用一次
    python verify_calls.py --only hcl_list_devices,hcl_get_facts

注意：HCL 没启动时，涉及设备的调用会返回真实的连接错误 —— 这是**预期结果**，
脚本不会伪造任何输出，也不会把错误改写成成功。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"

LINKS = [
    {"name": "SW1", "port": 30008, "intf": "GE1/0/20", "peer_port": 30001, "peer_intf": "GE0/0"},
    {"name": "SW2", "port": 30009, "intf": "GE1/0/21", "peer_port": 30010, "peer_intf": "GE1/0/18"},
]

CALLS = [
    ("hcl_list_devices", {"ports": list(range(30001, 30011))}),
    ("hcl_topology", {}),
    ("hcl_run_command", {"port": 30008, "command": "display m-lag summary", "timeout": 30}),
    ("hcl_get_facts", {"port": 30008}),
    ("hcl_apply_plan", {"plan_json": str(HERE / "examples" / "plan_example.json"), "dry_run": True}),
    ("hcl_verify", {"checklist_json": str(HERE / "examples" / "checklist_example.json")}),
    ("hcl_search_memory", {"keywords": ["m-lag", "consistency-check"], "max": 5}),
    ("hcl_link_watch", {"links": LINKS}),
]


class Client:
    """最小 MCP stdio 客户端（只用标准库）。"""

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(SERVER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(HERE), bufsize=0,
        )
        self.lock = threading.Lock()
        self.inbox: dict[object, dict] = {}
        self.stderr: list[str] = []
        for target in (self._read_stdout, self._read_stderr):
            threading.Thread(target=target, daemon=True).start()

    def _read_stdout(self) -> None:
        fd = self.proc.stdout.fileno()
        buf = b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                with self.lock:
                    self.inbox[obj.get("id")] = obj

    def _read_stderr(self) -> None:
        fd = self.proc.stderr.fileno()
        buf = b""
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self.stderr.append(raw.decode("utf-8", "replace").rstrip())

    def send(self, obj: dict) -> None:
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, msg_id, method: str, params=None, timeout: float = 300.0) -> dict:
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if msg_id in self.inbox:
                    return self.inbox.pop(msg_id)
            time.sleep(0.05)
        raise TimeoutError("等待 %s 超时" % method)

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="只调用这些工具，逗号分隔")
    args = ap.parse_args()

    out_dir = HERE / "evidence" / ("selftest-calls-%s" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = {s.strip() for s in args.only.split(",")} if args.only else None

    client = Client()
    try:
        client.request(1, "initialize", {"protocolVersion": "2024-11-05",
                                         "clientInfo": {"name": "verify_calls", "version": "1.0"}})
        client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        for i, (name, arguments) in enumerate(CALLS, start=2):
            if wanted and name not in wanted:
                continue
            started = time.monotonic()
            resp = client.request(i, "tools/call", {"name": name, "arguments": arguments})
            elapsed = time.monotonic() - started
            result = resp.get("result") or {}
            text = ""
            try:
                text = result["content"][0]["text"]
            except Exception:
                text = json.dumps(resp, ensure_ascii=False)
            (out_dir / ("%s.json" % name)).write_text(
                json.dumps(resp, ensure_ascii=False, indent=2), encoding="utf-8")
            (out_dir / ("%s.txt" % name)).write_text(text, encoding="utf-8")
            print("=" * 78)
            print("## %s   (%.1fs, isError=%s)" % (name, elapsed, result.get("isError")))
            print("参数: %s" % json.dumps(arguments, ensure_ascii=False))
            print("-" * 78)
            print(text)
            print("")
    finally:
        client.close()
        (out_dir / "server_stderr.log").write_text("\n".join(client.stderr), encoding="utf-8")
        print("原始返回已存档: %s" % out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
