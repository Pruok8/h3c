#!/usr/bin/env python3
"""selftest.py - 用子进程自测 h3c-lab-mcp 的 MCP 服务器。

做法：把 ``server.py`` 当**独立子进程**拉起，用 stdin 喂 JSON-RPC 请求
（一行一个 JSON），从 stdout 读响应（也是一行一个 JSON），
逐项验证协议行为，最后真实调用一次 ``hcl_list_devices``。

依次检查：
  1. ``initialize``          -> 协议版本回显 + capabilities.tools + serverInfo
  2. ``notifications/initialized`` -> **不回复**（通知不能有响应）
  3. ``tools/list``          -> 恰好 8 个工具，且名字/输入 schema 齐全
  4. ``ping``                -> 空结果
  5. 未知方法                 -> JSON-RPC error -32601
  6. ``tools/call hcl_list_devices``（默认 ports [30008,30009]）-> 打印真实结果
     （连不上也算通过：这个用例只验证"工具被调用且返回了可读文本"）

``--mock`` 模式下额外做一轮**设备侧行为的真实验证**：起 3 台假 HCL 设备
（``mockdev.py``，纯标准库 TCP），把每个工具真跑一遍并断言输出，
覆盖 HCL 没启动时无法验证的那部分（提示符/回显解析、报错检出、接口 brief 解析、
只读白名单、dry-run 不碰设备、证据落盘等）。

另外验证 stdout 里**只有 JSON**（每行都能解析），日志一律在 stderr。

用法::

    python selftest.py                       # 全量（含 30008/30009 真实探测）
    python selftest.py --ports 30001-30010   # 指定要探测的端口
    python selftest.py --mock                # 额外用假设备验证全部 8 个工具
    python selftest.py --skip-devices        # 只测协议，不碰设备

退出码：0 全部通过；1 有失败。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"

EXPECTED_TOOLS = [
    "hcl_list_devices",
    "hcl_topology",
    "hcl_run_command",
    "hcl_get_facts",
    "hcl_apply_plan",
    "hcl_verify",
    "hcl_search_memory",
    "hcl_link_watch",
]

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label))
    print("%s  %s%s" % ("PASS" if ok else "FAIL", label,
                        ("  | %s" % detail) if detail else ""), flush=True)
    return bool(ok)


def parse_ports(spec: str) -> list[int]:
    ports: list[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            ports += list(range(int(lo), int(hi) + 1))
        elif part:
            ports.append(int(part))
    return ports


class Server:
    """把 server.py 当子进程跑，按行收发 JSON-RPC。"""

    def __init__(self, timeout: float = 90.0) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(SERVER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(HERE), bufsize=0,
        )
        self.timeout = timeout
        self.lock = threading.Lock()
        self.inbox: dict[object, dict] = {}
        self.stderr_lines: list[str] = []
        self.bad_stdout: list[str] = []
        self._alive = True
        self._t_out = threading.Thread(target=self._read_stdout, daemon=True)
        self._t_err = threading.Thread(target=self._read_stderr, daemon=True)
        self._t_out.start()
        self._t_err.start()

    # ---------------- 后台读取 ----------------
    def _read_stdout(self) -> None:
        """用 os.read 读**原始字节**再自己切行。

        注意：不能在 ``for line in proc.stdout``（TextIOWrapper）的同时把 fd 设成
        非阻塞 —— 两者会互相打架，行会被吞掉。所以这里统一走 os.read。
        """
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
                self._on_line(raw)
        if buf.strip():
            self._on_line(buf)
        self._alive = False

    def _on_line(self, raw: bytes) -> None:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return
        try:
            obj = json.loads(line)
        except Exception as exc:
            self.bad_stdout.append("非 JSON 输出: %r (%s)" % (line[:200], exc))
            return
        if not isinstance(obj, dict):
            self.bad_stdout.append("非 JSON 对象: %r" % line[:200])
            return
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
                self.stderr_lines.append(raw.decode("utf-8", "replace").rstrip())
        if buf.strip():
            self.stderr_lines.append(buf.decode("utf-8", "replace").rstrip())

    # ---------------- 收发 ----------------
    def send(self, obj: dict) -> None:
        stream = self.proc.stdin
        assert stream is not None
        stream.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        stream.flush()

    def request(self, msg_id, method: str, params: dict | None = None,
                timeout: float | None = None) -> dict:
        msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)
        limit = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            with self.lock:
                if msg_id in self.inbox:
                    return self.inbox.pop(msg_id)
            if self.proc.poll() is not None and not self._alive:
                break
            time.sleep(0.05)
        raise TimeoutError("等待 %s 的响应超时（%.0fs）" % (method, limit))

    def text_of(self, response: dict) -> str:
        try:
            return response["result"]["content"][0]["text"]
        except Exception:
            return json.dumps(response, ensure_ascii=False)

    def wait_for_no_response(self, delay: float = 1.5) -> list[dict]:
        """等一会儿，返回这期间收到的、id 为 None 的消息（通知不该有响应）。"""
        time.sleep(delay)
        with self.lock:
            stray = [v for k, v in self.inbox.items() if k is None]
            for k in [k for k in self.inbox if k is None]:
                self.inbox.pop(k, None)
        return stray

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
    ap = argparse.ArgumentParser(description="h3c-lab-mcp 自测（协议 + 真实工具调用）")
    ap.add_argument("--ports", default="30008,30009", help="hcl_list_devices 要探测的端口（默认 30008,30009）")
    ap.add_argument("--skip-devices", action="store_true", help="跳过真实设备探测")
    ap.add_argument("--mock", action="store_true",
                    help="额外起假设备（mockdev.py，端口 39xxx）把 8 个工具全跑一遍")
    ap.add_argument("--timeout", type=float, default=120.0, help="单个请求等待上限（秒）")
    args = ap.parse_args()

    print("== h3c-lab-mcp selftest ==")
    print("server : %s" % SERVER)
    print("python : %s" % sys.executable)
    print("")

    srv = Server(timeout=args.timeout)
    try:
        # ---- 1. initialize ----
        resp = srv.request(1, "initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "selftest", "version": "1.0"},
        })
        res = resp.get("result") or {}
        info = res.get("serverInfo") or {}
        check(res.get("protocolVersion") == "2024-11-05",
              "initialize: protocolVersion 回显 2024-11-05",
              "got %r" % res.get("protocolVersion"))
        check("tools" in (res.get("capabilities") or {}), "initialize: capabilities.tools 存在",
              json.dumps(res.get("capabilities"), ensure_ascii=False))
        check(info.get("name") == "h3c-hcl-mcp" and info.get("version") == "1.0.0",
              "initialize: serverInfo = h3c-hcl-mcp 1.0.0",
              json.dumps(info, ensure_ascii=False))

        # ---- 2. notifications/initialized 不回复 ----
        srv.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        stray = srv.wait_for_no_response(1.5)
        check(not stray, "notifications/initialized 无响应", "收到 %d 条意外消息" % len(stray))

        # ---- 3. tools/list ----
        resp = srv.request(2, "tools/list", {})
        tools = ((resp.get("result") or {}).get("tools")) or []
        names = [t.get("name") for t in tools]
        check(len(tools) == 8, "tools/list 返回 8 个工具", "实际 %d 个: %s" % (len(tools), ", ".join(names)))
        check(sorted(names) == sorted(EXPECTED_TOOLS),
              "tools/list 工具名与要求一致",
              "缺失: %s" % (set(EXPECTED_TOOLS) - set(names)) if set(EXPECTED_TOOLS) - set(names) else "")
        bad_schema = [t.get("name") for t in tools
                      if not isinstance(t.get("inputSchema"), dict)
                      or not t.get("description")]
        check(not bad_schema, "每个工具都有 description + inputSchema",
              "不合格: %s" % bad_schema if bad_schema else "")

        # ---- 4. ping ----
        resp = srv.request(3, "ping", {})
        check(resp.get("result") == {} and "error" not in resp, "ping 返回空结果",
              json.dumps(resp, ensure_ascii=False)[:120])

        # ---- 5. 未知方法 ----
        resp = srv.request(4, "no/such/method", {})
        err = resp.get("error") or {}
        check(err.get("code") == -32601, "未知方法返回 -32601",
              json.dumps(err, ensure_ascii=False)[:120])

        # ---- 6. hcl_list_devices（真实调用） ----
        if args.skip_devices:
            print("SKIP  hcl_list_devices（--skip-devices）")
        else:
            ports = parse_ports(args.ports)
            resp = srv.request(5, "tools/call",
                               {"name": "hcl_list_devices", "arguments": {"ports": ports}},
                               timeout=args.timeout)
            res = resp.get("result") or {}
            text = srv.text_of(resp)
            check("content" in (resp.get("result") or {}) and "isError" in res,
                  "hcl_list_devices: 返回 content/isError 结构")
            check(isinstance(text, str) and text.strip() != "",
                  "hcl_list_devices: 有可读文本结果")
            print("")
            print("---- hcl_list_devices ports=%s 的真实输出 ----" % ports)
            for line in text.splitlines():
                print("    %s" % line)
            print("---- 输出结束 ----")
            print("")

        # ---- 7. stdout 干净性 ----
        check(not srv.bad_stdout, "stdout 每一行都是合法 JSON",
              "; ".join(srv.bad_stdout[:3]))

        # ---- 8. 进程还活着 ----
        check(srv.proc.poll() is None, "服务器进程仍然存活（异常没有让它退出）",
              "exit=%s" % srv.proc.poll())

        # ---- 9. --mock：用假设备把 8 个工具全跑一遍 ----
        if args.mock:
            run_mock_checks(srv)
    except Exception as exc:
        check(False, "自测过程中出现异常: %s" % exc)
        import traceback
        traceback.print_exc()
    finally:
        srv.close()

    passed = sum(1 for ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("")
    print("合计 %d 项：PASS %d，FAIL %d" % (len(RESULTS), passed, failed))
    if failed:
        print("失败项：")
        for ok, label in RESULTS:
            if not ok:
                print("  - %s" % label)
    if srv.stderr_lines:
        print("")
        print("---- 服务器 stderr（日志，最多 20 行）----")
        for line in srv.stderr_lines[:20]:
            print("    %s" % line)
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------
# --mock：假设备驱动的全工具验证
# --------------------------------------------------------------------------

_NET_FIXTURE = """[[S6850 SW1]]
    device_id = 1
    slot0 = S6850
    GE_0/20 = PE1 GE_0/0
    GE_0/17 = SW2 GE_0/17

[[S6850 SW2]]
    device_id = 2
    slot0 = S6850
    GE_0/17 = SW1 GE_0/17

[[MSR36-20 PE1]]
    device_id = 3
    slot0 = MSR36
    GE_0/0 = SW1 GE_0/20
"""

_PLAN_FIXTURE = {
    "devices": [
        {"name": "SW1", "port": 39101,
         "commands": ["sysname SW1", "vlan 10", "bad-command"],
         "verify": ["display vlan 10"]},
    ]
}

_CHECKLIST_FIXTURE = {
    "checks": [
        {"id": "MLAG", "name": "SW1", "port": 39101,
         "desc": "M-LAG 汇总含 UP", "cmd": "display m-lag summary",
         "expect": ["BAGG2\\s+UP"]},
        {"id": "LINK", "name": "SW1", "port": 39101,
         "desc": "GE1/0/20 UP", "cmd": "display interface brief",
         "expect": ["GE1/0/20\\s+UP"]},
        {"id": "LINK-DOWN", "name": "SW1", "port": 39101,
         "desc": "GE1/0/22 是 DOWN", "cmd": "display interface brief",
         "expect": ["GE1/0/22\\s+DOWN"]},
        {"id": "NEG", "name": "SW1", "port": 39101,
         "desc": "负向：BAGG2 不该报隔离（Isolated）",
         "cmd": "display m-lag summary",
         "expect_not": ["BAGG2\\s+Isolated"]},
    ]
}


def _write_fixture(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def run_mock_checks(srv: "Server") -> None:
    """起假设备，逐个工具真跑一遍并断言输出。"""
    import mockdev                                     # 本地模块，纯标准库

    work = HERE / "evidence" / "_selftest_fixtures"
    net_path = _write_fixture(work / "mock_topology.net", _NET_FIXTURE)
    plan_path = _write_fixture(work / "mock_plan.json",
                               json.dumps(_PLAN_FIXTURE, ensure_ascii=False, indent=2))
    list_path = _write_fixture(work / "mock_checklist.json",
                               json.dumps(_CHECKLIST_FIXTURE, ensure_ascii=False, indent=2))
    ctl_path = _write_fixture(work / "mock_control_plan.json",
                              json.dumps({"devices": [{"name": "SW1", "port": 39101,
                                                       "commands": ["vlan 20"]}]},
                                         ensure_ascii=False, indent=2))

    fleet = mockdev.MockFleet()
    next_id = 100

    def call(name: str, arguments: dict, timeout: float = 90.0):
        nonlocal next_id
        next_id += 1
        resp = srv.request(next_id, "tools/call",
                           {"name": name, "arguments": arguments}, timeout=timeout)
        result = resp.get("result") or {}
        return srv.text_of(resp), bool(result.get("isError")), result

    print("")
    print("---- --mock：假设备 %s 上的全工具验证 ----" % fleet.ports)
    with fleet:
        # hcl_list_devices
        text, is_err, _ = call("hcl_list_devices",
                               {"ports": [39101, 39102, 39999]})
        low = text.lower()
        check(not is_err and "39101  sw1  s6850  up" in low
              and "39102  sw2  s6850  up" in low and "39999" in text,
              "mock hcl_list_devices: 紧凑列出主机名/型号/状态，失败的端口单独列出",
              (text.splitlines() or [""])[0])

        # hcl_run_command：只读白名单
        text, is_err, _ = call("hcl_run_command", {"port": 39101, "command": "display version"})
        check(not is_err and "S6850" in text and "Comware" in text,
              "mock hcl_run_command: 正常回显只读命令",
              " / ".join((text.splitlines() or ["(空)"])[1:3])[:70])
        text, is_err, _ = call("hcl_run_command", {"port": 39101, "command": "reboot"})
        check(not is_err and "拒绝执行" in text and "reboot" in text,
              "mock hcl_run_command: 非白名单命令被拒绝", text.splitlines()[0][:70])
        text, is_err, _ = call("hcl_run_command",
                               {"port": 39101, "command": "display interface brief",
                                "max_chars": 80})
        check("已截断" in text, "mock hcl_run_command: max_chars 触达时标注被截断",
              text.splitlines()[-1][:70])

        # hcl_get_facts
        text, is_err, _ = call("hcl_get_facts", {"port": 39101})
        check(not is_err and len(text.splitlines()) <= 26
              and "型号: S6850" in text and "软件版本: Comware 7.1.070" in text
              and "设备时间: 09:41:23" in text,
              "mock hcl_get_facts: 提炼摘要（≤25 行，含型号/软件版本/设备时间）",
              " / ".join(text.splitlines()[1:4])[:90])

        # hcl_topology
        text, is_err, _ = call("hcl_topology", {"net_file": str(net_path)})
        check(not is_err and "SW1" in text and "30001" in text
              and "GE_0/20" in text and "3 台设备、2 条连线" in text,
              "mock hcl_topology: 设备表 + 连线表（端口=30000+device_id）",
              [ln for ln in text.splitlines() if "合计" in ln][0][:70])
        text, is_err, _ = call("hcl_topology", {"net_file": str(work / "nope.net")})
        check(not is_err and "找不到" in text,
              "mock hcl_topology: 文件不存在时返回可读错误", text.splitlines()[0][:70])

        # hcl_apply_plan：dry_run 默认不碰设备
        before = len(fleet.commands_of("SW1"))
        text, is_err, _ = call("hcl_apply_plan", {"plan_json": str(plan_path)})
        after = len(fleet.commands_of("SW1"))
        check(not is_err and "[预演 dry-run]" in text and before == after,
              "mock hcl_apply_plan: 默认 dry_run 不碰设备（设备未收到任何命令）",
              "命令数 %d -> %d" % (before, after))

        # hcl_apply_plan：dry_run=false 真下发 + 报错检出 + 证据落盘
        text, is_err, _ = call("hcl_apply_plan",
                               {"plan_json": str(plan_path), "dry_run": False, "timeout": 30})
        check(not is_err and "下发 3 条 报错 1" in text,
              "mock hcl_apply_plan: 真下发并检出 Comware 报错", text.splitlines()[0][:80])
        ev = None
        for line in text.splitlines():
            if "证据:" in line:
                ev = Path(line.split("证据:", 1)[1].strip())
        check(ev is not None and ev.is_file() and "bad-command" in ev.read_text(encoding="utf-8"),
              "mock hcl_apply_plan: 完整回显写到 evidence\\<时间戳>\\SW1.txt",
              str(ev) if ev else "(没拿到路径)")

        # hcl_apply_plan：save force
        text, is_err, _ = call("hcl_apply_plan",
                               {"plan_json": str(ctl_path), "dry_run": False, "save": True,
                                "timeout": 30})
        check(not is_err and "已保存" in text
              and any("save force" in c for c in fleet.commands_of("SW1")),
              "mock hcl_apply_plan: save=true 会在设备上执行 save force",
              text.splitlines()[0][:80])

        # hcl_verify
        text, is_err, _ = call("hcl_verify", {"checklist_json": str(list_path), "timeout": 20})
        lines = text.splitlines()
        check(not is_err and any(ln.startswith("PASS  MLAG") for ln in lines)
              and any(ln.startswith("PASS  LINK") for ln in lines)
              and any(ln.startswith("PASS  NEG") for ln in lines)
              and "合计 4 项：PASS 4，FAIL 0" in text,
              "mock hcl_verify: expect/expect_not 语义正确（4 项全 PASS）",
              lines[-2][:80] if len(lines) > 1 else text[:80])

        # hcl_search_memory
        text, is_err, _ = call("hcl_search_memory",
                               {"keywords": ["m-lag", "consistency-check"], "max": 3})
        check(not is_err and "命中" in text and "cases.md:" in text
              and "consistency-check" in text,
              "mock hcl_search_memory: 同义词扩展 + 命中行 + 文件行号",
              [ln for ln in text.splitlines() if "同义词扩展" in ln][:1][0][:90])
        text, is_err, _ = call("hcl_search_memory", {"keywords": ["绝不存在的关键词zzz"]})
        check(not is_err and "没有命中" in text,
              "mock hcl_search_memory: 无命中时给出下一步指引")

        # hcl_link_watch
        text, is_err, _ = call("hcl_link_watch", {"links": [
            {"name": "SW1", "port": 39101, "intf": "GE1/0/20",
             "peer_port": 39103, "peer_intf": "GE0/0"},
            {"name": "SW1", "port": 39101, "intf": "GE1/0/22",
             "peer_port": 39103, "peer_intf": "GE0/1"},
            {"name": "SW1", "port": 39101, "intf": "GE1/0/23",
             "peer_port": 39103, "peer_intf": "GE0/2"},
        ]})
        check(not is_err and "SW1 GE1/0/20 本端 UP" in text
              and "SW1 GE1/0/22 本端 DOWN" in text
              and "SW1 GE1/0/23 本端 ADM" in text
              and "本端 UP 1，DOWN 1，ADM 1" in text,
              "mock hcl_link_watch: UP/DOWN/ADM 判定与合计正确",
              [ln for ln in text.splitlines() if "合计" in ln][0][:80])

        # 连接失败也必须可读（不让异常冒泡成进程崩溃）
        text, is_err, _ = call("hcl_run_command", {"port": 39998, "command": "display version"})
        check(is_err and "ConnectionRefusedError" in text or "拒绝" in text,
              "mock 连接失败: isError=True 且是真实错误文本（进程不崩）",
              text.splitlines()[0][:80])


if __name__ == "__main__":
    raise SystemExit(main())
