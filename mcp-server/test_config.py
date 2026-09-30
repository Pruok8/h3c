#!/usr/bin/env python3
"""server.py 配置层与拓扑定位的回归测试（离线，不需要 HCL 启动）。

为什么单独有这个文件：配置层过去**一条测试都没有**，于是两个真 bug 一直活着 ——

1. ``load_config()`` 调用了尚未定义的 ``log()``：只要存在配置文件，服务器就在
   import 期 ``NameError`` 崩掉。也就是说 ``H3C_MCP_CONFIG`` / ``h3c_lab_mcp.json``
   这条配置通道**从未真正可用过**（每次都被异常吞掉或直接崩）。
2. 读配置文件用 ``encoding="utf-8"``：Windows 记事本 / ``Set-Content -Encoding UTF8``
   / ``Out-File`` 都会写 UTF-8 BOM，``json.loads`` 会直接抛 JSONDecodeError。
   同一个坑还存在于计划文件、清单文件和 ``.net`` 拓扑文件。

本文件把这两条，以及"不猜拓扑""端口从拓扑推导""配置坏了必须硬失败"等行为钉死。

用法：
    python test_config.py          # 全部通过时退出码 0
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
PROTOCOL = "2024-11-05"

try:                                    # Windows 控制台默认 cp936，中文会乱码
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                       # pragma: no cover
    pass

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = "%s  %s" % (mark, name)
    if detail and not ok:
        line += "\n        %s" % detail.replace("\n", "\n        ")
    print(line)


def base_env(extra: dict | None = None, dsh_home: Path | None = None) -> dict:
    """干净的环境：丢掉外部 H3C_MCP_CONFIG，强制 UTF-8 输出。"""
    env = dict(os.environ)
    env.pop("H3C_MCP_CONFIG", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if dsh_home is not None:
        env["DSH_HOME"] = str(dsh_home)
    env.update(extra or {})
    return env


def run_server(args: list[str], env: dict, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SERVER)] + args, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, cwd=str(HERE), timeout=timeout)


def run_py(code: str, env: dict, timeout: int = 60) -> subprocess.CompletedProcess:
    """在 server.py 所在目录里跑一段 python（用于直接调内部函数）。"""
    return subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, cwd=str(HERE), timeout=timeout)


def show_config(env: dict) -> tuple[dict | None, subprocess.CompletedProcess]:
    proc = run_server(["--show-config"], env)
    try:
        return json.loads(proc.stdout), proc
    except Exception:
        return None, proc


def mcp_call(tool: str, arguments: dict, env: dict, timeout: int = 90) -> tuple[bool, str, str]:
    """真的按 MCP 协议喂一行一个 JSON，返回 (是否收到响应, isError, text)。"""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                    "clientInfo": {"name": "test_config", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": tool, "arguments": arguments}},
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run([sys.executable, str(SERVER)], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, cwd=str(HERE), timeout=timeout)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if isinstance(msg, dict) and msg.get("id") == 2:
            result = msg.get("result") or {}
            blocks = result.get("content") or []
            text = "\n".join(b.get("text", "") for b in blocks if isinstance(b, dict))
            return True, bool(result.get("isError")), text
    return False, False, proc.stderr


# --------------------------------------------------------------------------
# 测试
# --------------------------------------------------------------------------

def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="h3clab-cfgtest-"))
    dsh_home = tmp / "dsh-home"
    dsh_home.mkdir(parents=True, exist_ok=True)
    try:
        test_no_config(dsh_home)
        test_valid_config(tmp, dsh_home)
        test_bad_config(tmp, dsh_home)
        test_topology(tmp, dsh_home)
        test_addressing(dsh_home)
        test_memory_dir(tmp, dsh_home)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("-" * 60)
    print("总计：%d 通过 / %d 失败" % (passed, total - passed))
    return 0 if passed == total else 1


def test_no_config(dsh_home: Path) -> None:
    """没有任何配置文件时的默认行为。"""
    env = base_env(dsh_home=dsh_home)

    proc = run_server(["--version"], env)
    check("无配置：--version 正常退出", proc.returncode == 0 and "h3c-hcl-mcp" in proc.stderr,
          "rc=%s stderr=%s" % (proc.returncode, proc.stderr[-300:]))

    conf, proc = show_config(env)
    check("无配置：--show-config 输出合法 JSON", conf is not None, proc.stdout[:300] + proc.stderr[-300:])
    if conf is None:
        return

    check("无配置：config_source 标为内置默认值", conf.get("config_source") == "(内置默认值)",
          repr(conf.get("config_source")))
    check("无配置：不泄漏内部字段 ports_explicit", "ports_explicit" not in conf)
    check("无配置：未配 net_file 时不写入该键（不猜拓扑）", "net_file" not in conf)

    evidence = Path(str(conf.get("evidence_root", "")))
    refs = Path(str(conf.get("references_dir", "")))
    check("无配置：evidence_root 落在 DSH_HOME 下（不再写进插件包）",
          str(evidence).lower().startswith(str(dsh_home).lower()),
          str(evidence))
    check("无配置：evidence_root 不在包目录内",
          str(HERE).lower() not in str(evidence).lower(), str(evidence))
    check("无配置：references_dir 落在 DSH_HOME 下",
          str(refs).lower().startswith(str(dsh_home).lower()), str(refs))
    check("无配置：host 默认只连本机", conf.get("host") == "127.0.0.1", repr(conf.get("host")))
    check("无配置：内置 devices 为空（不替用户认定某套 lab 的设备名）",
          conf.get("devices") == {}, repr(conf.get("devices")))


def test_valid_config(tmp: Path, dsh_home: Path) -> None:
    """合法配置：含 BOM、重复端口、未知键。"""
    cfg = tmp / "cfg-bom.json"
    net = tmp / "lab.net"
    net.write_text("[[S5820V2 SW1]]\ndevice_id = 1\n", encoding="utf-8")
    payload = {
        "host": "127.0.0.1",
        "ports": [30009, 30002, 30002, "30001"],
        "devices": {"SW1": 30008, "SW2": 30009},
        "net_file": str(net),
        "evidence_root": str(tmp / "ev"),
        "references_dir": str(tmp / "refs"),
        "未来才有的键": True,
    }
    # ★ 故意写成 UTF-8 BOM：旧实现用 utf-8 读，这里必炸。
    cfg.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8-sig")

    env = base_env({"H3C_MCP_CONFIG": str(cfg)}, dsh_home=dsh_home)
    conf, proc = show_config(env)
    check("BOM 配置：能加载（回归 NameError / BOM 崩溃）", conf is not None,
          "rc=%s out=%s err=%s" % (proc.returncode, proc.stdout[:200], proc.stderr[-400:]))
    if conf is None:
        return
    check("BOM 配置：ports 去重升序", conf.get("ports") == [30001, 30002, 30009], repr(conf.get("ports")))
    check("BOM 配置：devices 生效", conf.get("devices") == {"SW1": 30008, "SW2": 30009},
          repr(conf.get("devices")))
    check("BOM 配置：net_file 生效", conf.get("net_file") == str(net), repr(conf.get("net_file")))
    check("BOM 配置：evidence_root 生效", conf.get("evidence_root") == str(tmp / "ev"),
          repr(conf.get("evidence_root")))
    check("BOM 配置：references_dir 生效", conf.get("references_dir") == str(tmp / "refs"),
          repr(conf.get("references_dir")))
    check("BOM 配置：config_source 指向该文件", conf.get("config_source") == str(cfg),
          repr(conf.get("config_source")))
    check("BOM 配置：未知键只告警不报错", proc.returncode == 0 and "无法识别的键" in proc.stderr,
          "rc=%s err=%s" % (proc.returncode, proc.stderr[-300:]))

    # 手工兜底路径：没有 H3C_MCP_CONFIG 时读 <本目录>/h3c_lab_mcp.json
    legacy = HERE / "h3c_lab_mcp.json"
    if legacy.exists():
        check("手工配置：本次跳过（已存在 h3c_lab_mcp.json，不覆盖用户文件）", True)
    else:
        try:
            legacy.write_text(json.dumps({"ports": [30042]}), encoding="utf-8-sig")
            conf2, proc2 = show_config(base_env(dsh_home=dsh_home))
            check("手工配置：无 H3C_MCP_CONFIG 时读本目录 h3c_lab_mcp.json",
                  conf2 is not None and conf2.get("ports") == [30042],
                  "rc=%s out=%s err=%s" % (proc2.returncode, proc2.stdout[:200], proc2.stderr[-300:]))
        finally:
            legacy.unlink(missing_ok=True)


def test_bad_config(tmp: Path, dsh_home: Path) -> None:
    """配置坏了必须硬失败，绝不静默回落。"""
    cases = [
        ("文件不存在", str(tmp / "nope.json"), None),
        ("非法 JSON", None, "这不是 JSON"),
        ("顶层是数组", None, "[1,2,3]"),
        ("ports 含越界值", None, json.dumps({"ports": [70000]})),
        ("ports 不是数组", None, json.dumps({"ports": 30001})),
        ("devices 不是对象", None, json.dumps({"devices": [1, 2]})),
        ("devices 端口非法", None, json.dumps({"devices": {"SW1": "abc"}})),
    ]
    for index, (label, explicit_path, body) in enumerate(cases):
        path = explicit_path
        if path is None:
            path = str(tmp / ("bad-%d.json" % index))
            Path(path).write_text(body or "", encoding="utf-8-sig")
        env = base_env({"H3C_MCP_CONFIG": path}, dsh_home=dsh_home)
        proc = run_server(["--show-config"], env)
        check("坏配置（%s）退出码=2 且有可读提示" % label,
              proc.returncode == 2 and "配置错误" in proc.stderr,
              "rc=%s err=%s" % (proc.returncode, proc.stderr[-300:]))

    # 缺文件时错误信息要指出是哪个路径
    env = base_env({"H3C_MCP_CONFIG": str(tmp / "nope.json")}, dsh_home=dsh_home)
    proc = run_server(["--show-config"], env)
    check("坏配置：错误信息里带上路径", "nope.json" in proc.stderr, proc.stderr[-300:])


def test_topology(tmp: Path, dsh_home: Path) -> None:
    """拓扑定位：不猜、可显式传、可从拓扑推导端口、能扛 BOM。"""
    env = base_env(dsh_home=dsh_home)

    ok, is_error, text = mcp_call("hcl_topology", {}, env)
    check("拓扑：未配置 net_file 时明确报错（不再按 mtime 猜）",
          ok and not is_error and "找不到 .net 拓扑文件" in text and "两种修法" in text,
          "ok=%s is_error=%s text=%s" % (ok, is_error, text[:400]))
    check("拓扑：错误信息里不出现 \"None\" 这种伪路径",
          ok and "不存在：None" not in text, text[:400])

    # 带 BOM 的 .net：第一台设备不能被 \ufeff 吃掉
    net = tmp / "bom.net"
    net.write_bytes("\ufeff[[S5820V2 SW1]]\ndevice_id = 1\nGE_0/0 = SW2 GE_0/0\n\n"
                    "[[S5820V2 SW2]]\ndevice_id = 2\nGE_0/0 = SW1 GE_0/0\n".encode("utf-8"))
    cfg = tmp / "net-cfg.json"
    cfg.write_text(json.dumps({"net_file": str(net)}), encoding="utf-8-sig")
    env2 = base_env({"H3C_MCP_CONFIG": str(cfg)}, dsh_home=dsh_home)

    ok, is_error, text = mcp_call("hcl_topology", {}, env2)
    check("拓扑：配置了 net_file 就能读到设备表",
          ok and not is_error and "SW1" in text and "SW2" in text, text[:400])
    check("拓扑：带 BOM 的 .net 也能解析出两台设备（回归 \\ufeff 吞设备）",
          ok and not is_error and "合计 2 台设备" in text, text[:400])

    code = ("import sys; sys.path.insert(0, %r)\n"
            "import server\n"
            "print(server.effective_ports())\n" % str(HERE))
    proc = run_py(code, env2)
    check("拓扑：端口由 device_id 推导（30000 + id）",
          proc.returncode == 0 and proc.stdout.strip() == "[30001, 30002]",
          "rc=%s out=%r err=%s" % (proc.returncode, proc.stdout.strip(), proc.stderr[-300:]))

    proc = run_py(code, env)
    check("拓扑：未配置拓扑时端口回落到内置兜底范围",
          proc.returncode == 0 and proc.stdout.strip() == str(list(range(30001, 30011))),
          "rc=%s out=%r" % (proc.returncode, proc.stdout.strip()))

    # 显式端口优先于拓扑推导
    cfg3 = tmp / "ports-cfg.json"
    cfg3.write_text(json.dumps({"ports": [30500], "net_file": str(net)}), encoding="utf-8-sig")
    proc = run_py(code, base_env({"H3C_MCP_CONFIG": str(cfg3)}, dsh_home=dsh_home))
    check("拓扑：显式 ports 优先于拓扑推导",
          proc.returncode == 0 and proc.stdout.strip() == "[30500]",
          "rc=%s out=%r" % (proc.returncode, proc.stdout.strip()))


def test_addressing(dsh_home: Path) -> None:
    """按主机名寻址：重名必须报错，绝不"随便挑一台"。

    实测背景：HCL 出厂配置下**所有**设备的提示符都是默认的 ``H3C``。
    老实现是"扫到第一个同名就返回"，等于随机挑一台设备去下发配置。
    """
    code = (
        "import json, sys\n"
        "sys.path.insert(0, %r)\n"
        "import server\n"
        "out = {}\n"
        "out['devices_default'] = server.CONFIG['devices']\n"
        "server._scan_for_hostname = lambda name: [30001, 30002, 30006]\n"
        "p, e = server._resolve_port({'name': 'H3C'})\n"
        "out['ambiguous_port'], out['ambiguous_err'] = p, e\n"
        "server._scan_for_hostname = lambda name: [30008]\n"
        "p, e = server._resolve_port({'name': 'SW1'})\n"
        "out['single_port'], out['single_err'] = p, e\n"
        "server._scan_for_hostname = lambda name: []\n"
        "p, e = server._resolve_port({'name': 'NOPE'})\n"
        "out['missing_port'], out['missing_err'] = p, e\n"
        "out['spec_two'] = server._ports_spec([30001, 30002])\n"
        "out['spec_many'] = server._ports_spec(list(range(30001, 30023)))\n"
        "out['spec_sparse'] = server._ports_spec([30003, 30004, 30005, 30011, 30012, 30018, 30019, 30020, 30021])\n"
        "server.CONFIG['devices'] = {'SW1': 30008}\n"
        "server._scan_for_hostname = lambda name: [30001, 30002]\n"
        "p, e = server._resolve_port({'name': 'sw1'})\n"
        "out['configured_port'], out['configured_err'] = p, e\n"
        "server.CONFIG['devices'] = {}\n"
        "p, e = server._resolve_port({'name': 'H3C'})\n"
        "out['no_config_ambiguous_port'] = p\n"
        "p, e = server._resolve_port({'port': 30042})\n"
        "out['explicit_port'] = p\n"
        "print(json.dumps(out))\n" % str(HERE)
    )
    proc = run_py(code, base_env(dsh_home=dsh_home))
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        check("寻址：无法取得内部测试输出", False,
              "rc=%s stdout=%r stderr=%s" % (proc.returncode, proc.stdout[-300:], proc.stderr[-500:]))
        return

    check("寻址：内置 devices 默认空", data.get("devices_default") == {},
          repr(data.get("devices_default")))
    check("寻址：重名时**不**返回端口（不再随便挑一台）",
          data.get("ambiguous_port") is None and "同时存在" in (data.get("ambiguous_err") or ""),
          "port=%r err=%r" % (data.get("ambiguous_port"), data.get("ambiguous_err")))
    check("寻址：重名错误里列出全部候选端口",
          "30001" in (data.get("ambiguous_err") or "") and "30006" in (data.get("ambiguous_err") or ""),
          repr(data.get("ambiguous_err")))
    check("寻址：唯一同名时正常解析", data.get("single_port") == 30008 and data.get("single_err") is None,
          "port=%r err=%r" % (data.get("single_port"), data.get("single_err")))
    check("寻址：找不到主机名时给出可读错误",
          data.get("missing_port") is None and "找不到主机名" in (data.get("missing_err") or ""),
          repr(data.get("missing_err")))
    check("寻址：配置的 devices 优先于扫描",
          data.get("configured_port") == 30008 and data.get("configured_err") is None,
          "port=%r err=%r" % (data.get("configured_port"), data.get("configured_err")))
    check("寻址：清空 devices 后同名端口回到「重名报错」",
          data.get("no_config_ambiguous_port") is None, repr(data.get("no_config_ambiguous_port")))
    check("寻址：显式 port 直接可用", data.get("explicit_port") == 30042, repr(data.get("explicit_port")))
    check("寻址：连续端口折叠成区间并带总数",
          data.get("spec_two") == "30001-30002" and "共 22 个" in (data.get("spec_many") or ""),
          "two=%r many=%r" % (data.get("spec_two"), data.get("spec_many")))
    check("寻址：稀疏端口不折叠成区间（免得以为中间那些也在探测）",
          "、" in (data.get("spec_sparse") or "") and "共 9 个" in (data.get("spec_sparse") or "")
          and "-" not in (data.get("spec_sparse") or ""),
          repr(data.get("spec_sparse")))


def test_memory_dir(tmp: Path, dsh_home: Path) -> None:
    """记忆库目录不存在时要给出可操作的错误，而不是空结果。"""
    cfg = tmp / "mem-cfg.json"
    cfg.write_text(json.dumps({"references_dir": str(tmp / "no-such-refs")}), encoding="utf-8-sig")
    ok, is_error, text = mcp_call("hcl_search_memory", {"keywords": ["M-LAG"]},
                                  base_env({"H3C_MCP_CONFIG": str(cfg)}, dsh_home=dsh_home))
    check("记忆库：目录不存在时报错并提示 references_dir",
          ok and "不存在" in text and "references" in text,
          "ok=%s text=%s" % (ok, text[:400]))


if __name__ == "__main__":
    raise SystemExit(main())
