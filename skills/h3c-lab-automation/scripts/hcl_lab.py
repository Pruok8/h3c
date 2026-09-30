"""hcl_lab.py - 按计划批量给 HCL 设备下发配置，并回读验证、落证据。

这是"拿到拓扑图 + 需求之后"的主力工具：计划文件描述"哪台设备要下发哪些命令"，
脚本负责定位设备、进入 system-view、逐条下发、检出报错、必要时保存、跑验证命令，
最后把全过程写成证据文件。

计划文件（JSON）::

    {
      "devices": [
        {
          "name": "SW1",
          "hostname": "SW1",              // 用提示符里的主机名定位（推荐）
          "port": 30004,                  // 也可以直接写端口；两者都有则以 port 为准
          "commands": [                   // 只写配置正文，system-view 由脚本处理
            "sysname SW1",
            "vlan 10",
            "quit"
          ],
          "verify": [                     // 可选：用户视图下的验证命令
            "display drni summary"
          ]
        }
      ]
    }

用法::

    python hcl_lab.py plan.json                      # 预演，只打印将做什么
    python hcl_lab.py plan.json --apply              # 真正下发
    python hcl_lab.py plan.json --apply --save       # 下发后 save force
    python hcl_lab.py plan.json --apply --only SW1   # 只做某几台
    python hcl_lab.py plan.json --apply --snapshot   # 额外抓下发前的 current-configuration

设计原则：**默认不发任何东西**。必须显式 --apply 才会碰设备。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hcldrv import Console, ConsoleError, ConsoleTimeout, _PROMPT   # noqa: E402

# Comware 的报错长相
ERROR_PATTERNS = [
    re.compile(r"%\s*Unrecognized command", re.I),
    re.compile(r"%\s*Wrong parameter", re.I),
    re.compile(r"%\s*Incomplete command", re.I),
    re.compile(r"%\s*Ambiguous command", re.I),
    re.compile(r"%\s*Too many parameters", re.I),
    re.compile(r"%\s*Unmatched", re.I),
    re.compile(r"%\s*Not enough", re.I),
    re.compile(r"%\s*Please input", re.I),
    re.compile(r"^\s*Error:", re.I | re.M),
    # 否定式拒绝：Comware 大量报错不长成 % 开头，例如
    #   Can't assign the port to the aggregation group because its attribute
    #   configurations are different than the aggregate interface.
    re.compile(r"^\s*Can't\b", re.I | re.M),
    re.compile(r"^\s*Failed\b", re.I | re.M),
    re.compile(r"^\s*Invalid\b", re.I | re.M),
    re.compile(r"Permission denied", re.I),
    re.compile(r"\bis not allowed\b", re.I),
    re.compile(r"\bdoes not exist\b", re.I),
    re.compile(r"\bnot supported\b", re.I),
]
CONFIRM_RE = re.compile(r"\[Y/N\]|\(y/n\)|\[yes/no\]", re.I)
SKIP_AS_ENTER = {"system-view", "system view"}


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


def in_user_view(con: Console) -> bool:
    """提示符 <H3C> = 用户视图；[H3C] / [H3C-GE1/0/1] = 系统视图或子视图。"""
    match = _PROMPT.search(con.text)
    if not match:
        return False
    return match.group(0).lstrip().startswith("<")


def find_port_by_hostname(host: str, ports: list[int], wanted: str,
                          timeout: float = 0.6):
    """扫描端口，找到提示符主机名等于 wanted 的那台。"""
    for port in ports:
        con = Console(host=host, port=port)
        try:
            con.connect(timeout=timeout)
        except OSError:
            con.close()
            continue
        try:
            con.prep(timeout=15)
            match = _PROMPT.search(con.text)
            if match and match.group(1).lower() == wanted.lower():
                return port
        except (ConsoleError, ConsoleTimeout):
            pass
        finally:
            con.close()
    return None


def check_errors(text: str) -> list[str]:
    hits: list[str] = []
    for line in text.splitlines():
        for pattern in ERROR_PATTERNS:
            if pattern.search(line):
                hits.append(line.strip())
                break
    return hits


def apply_device(dev: dict, host: str, ports: list[int], apply: bool,
                 do_save: bool, snapshot: bool, auto_confirm: bool,
                 out_dir: Path, timeout: float, log) -> dict:
    name = dev.get("name") or dev.get("hostname") or "device"
    result = {"name": name, "port": None, "errors": [], "warnings": [],
              "applied": 0, "verify_ok": 0}

    port = dev.get("port")
    if port is None:
        wanted = dev.get("hostname") or name
        port = find_port_by_hostname(host, ports, wanted)
        if port is None:
            result["errors"].append("找不到主机名为 %r 的设备（扫过 %s）"
                                    % (wanted, ports))
            return result
    result["port"] = port

    con = Console(host=host, port=port, timeout=timeout, auto_confirm=auto_confirm)
    # 必须在 try 之前初始化：否则早期失败时 finally 里引用它们会抛 NameError，
    # 把"失败也要留证据"这条兜底自己吃掉（p2b 那次就是这么丢的证据）。
    transcript: list[str] = []
    verify_out: list[str] = []
    try:
        con.connect(timeout=2.0)
        con.prep(timeout=60)
        banner = _PROMPT.search(con.text)
        result["prompt_before"] = banner.group(0).strip() if banner else None
        log("  [%s] 已连接 %s:%d  提示符=%s" % (name, host, port, result["prompt_before"]))

        # 关掉本控制台的日志输出。LACP 超时、接口 up/down 这类 %日志 会被插进
        # 回显里，把提示符冲散，command() 就等不到提示符而超时 —— 第一次下发
        # SW1/SW2 中途断掉就是这个原因之一。
        try:
            was_in_sys = not in_user_view(con)
            if was_in_sys:
                con.command("return", timeout=20)
            con.command("undo terminal monitor", timeout=15)
            if was_in_sys:
                con.command("system-view", timeout=20)
        except (ConsoleError, ConsoleTimeout):
            pass

        if snapshot:
            result["snapshot"] = con.command("display current-configuration", timeout=120)

        commands = [c for c in (dev.get("commands") or []) if c.strip()]

        if apply and commands:
            if in_user_view(con):
                con.command("system-view", timeout=30)
                transcript.append("$ system-view")
            for raw in commands:
                line = raw.strip()
                if line.lower() in SKIP_AS_ENTER:
                    continue                      # 已进系统视图，跳过重复的 system-view
                out = con.command(line, timeout=timeout)
                transcript.append("$ %s" % line)
                if out:
                    transcript.append(out)
                result["applied"] += 1
                hits = check_errors(out)
                if hits:
                    result["errors"] += ["%s -> %s" % (line, h) for h in hits]
                    log("      !! %s" % hits[0])
                # 命令里的 quit 可能把我们踢回用户视图，下一轮再进去
                if not in_user_view(con):
                    if CONFIRM_RE.search(out):
                        if auto_confirm:
                            con.command("Y", timeout=timeout)
                            transcript.append("> Y (auto)")
                        else:
                            result["warnings"].append("%s 需要 Y/N 确认，已跳过" % line)
            if not in_user_view(con):
                con.command("return", timeout=30)
                transcript.append("$ return")

        if apply and do_save:
            out = con.command("save force", timeout=180)
            transcript.append("$ save force")
            transcript.append(out)
            hits = check_errors(out)
            if hits:
                result["errors"] += ["save force -> %s" % h for h in hits]
            log("  [%s] 已保存配置" % name)

        for cmd in (dev.get("verify") or []):
            try:
                out = con.command(cmd, timeout=180)
            except ConsoleTimeout as exc:
                out = "[超时] %s" % exc.transcript[-400:]
                result["warnings"].append("%s 超时" % cmd)
            verify_out.append("$ %s" % cmd)
            verify_out.append(out)
            result["verify_ok"] += 1

        evidence = ["### %s  host=%s port=%s" % (name, host, port),
                    "### 下发前提示符: %s" % result.get("prompt_before"),
                    ""]
        if result.get("snapshot"):
            # 改前有底：快照必须落盘，否则"能回滚"只是口头承诺
            evidence += ["--- 下发前 current-configuration ---",
                         result["snapshot"], ""]
        if transcript:
            evidence += ["--- 下发 ---"] + transcript + [""]
        if verify_out:
            evidence += ["--- 验证 ---"] + verify_out + [""]
        evidence += ["--- 结论 ---",
                     "下发命令 %d 条" % result["applied"],
                     "验证命令 %d 条" % result["verify_ok"],
                     "报错 %d 处" % len(result["errors"])]
        evidence += ["  " + e for e in result["errors"]]
        evidence += ["警告 %d 处" % len(result["warnings"])]
        evidence += ["  " + w for w in result["warnings"]]
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / ("%s.txt" % name)
        path.write_text("\n".join(evidence), encoding="utf-8")
        result["evidence"] = str(path)
    except (ConsoleError, ConsoleTimeout) as exc:
        result["errors"].append("%s: %s" % (type(exc).__name__, exc))
        log("      !! %s: %s" % (type(exc).__name__, exc))
    finally:
        # 证据必须无条件落盘 —— 中途失败时才是最需要它的时候
        try:
            tail = ["### %s  host=%s port=%s" % (name, host, port),
                    "### 下发前提示符: %s" % result.get("prompt_before"),
                    ""]
            if result.get("snapshot"):
                tail += ["--- 下发前 current-configuration ---",
                         result["snapshot"], ""]
            if transcript:
                tail += ["--- 下发 ---"] + transcript + [""]
            if verify_out:
                tail += ["--- 验证 ---"] + verify_out + [""]
            tail += ["--- 结论 ---",
                     "下发命令 %d 条" % result["applied"],
                     "验证命令 %d 条" % result["verify_ok"],
                     "报错 %d 处" % len(result["errors"])]
            tail += ["  " + e for e in result["errors"]]
            tail += ["警告 %d 处" % len(result["warnings"])]
            tail += ["  " + w for w in result["warnings"]]
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / ("%s.txt" % name)
            path.write_text("\n".join(tail), encoding="utf-8")
            result["evidence"] = str(path)
        except Exception as exc:          # 证据写不进去也不能掩盖真正的错
            result["warnings"].append("证据落盘失败: %s" % exc)
        con.close()
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("plan", help="计划 JSON")
    ap.add_argument("--apply", action="store_true", help="真正下发（默认只预演）")
    ap.add_argument("--save", action="store_true", help="下发后执行 save force")
    ap.add_argument("--snapshot", action="store_true", help="抓下发前的 current-configuration")
    ap.add_argument("--only", default=None, help="只处理这些设备名，逗号分隔")
    ap.add_argument("--ports", default="30001-30010")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--out", default=None, help="证据目录（默认 lab-evidence-<时间>）")
    ap.add_argument("--timeout", type=float, default=60.0, help="单条命令超时")
    ap.add_argument("--auto-confirm", action="store_true", help="遇到 [Y/N] 自动答 Y")
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    devices = plan.get("devices") or []
    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        devices = [d for d in devices if (d.get("name") in wanted)]
    if not devices:
        print("计划里没有要处理的设备。")
        return 1

    out_dir = Path(args.out) if args.out else Path("lab-evidence-%s"
                                                   % time.strftime("%Y%m%d-%H%M%S"))
    ports = parse_ports(args.ports)

    def log(msg: str) -> None:
        print(msg, flush=True)

    log("计划: %s" % args.plan)
    log("模式: %s%s" % ("下发" if args.apply else "预演（不会碰设备，加 --apply 才下发）",
                        " + save force" if (args.apply and args.save) else ""))
    log("证据目录: %s" % out_dir)
    log("")

    results = []
    for dev in devices:
        name = dev.get("name") or dev.get("hostname") or "device"
        cmds = [c for c in (dev.get("commands") or []) if c.strip()]
        log("[%s] %d 条配置, %d 条验证" % (name, len(cmds), len(dev.get("verify") or [])))
        if not args.apply:
            for c in cmds:
                log("    %s" % c)
            for c in (dev.get("verify") or []):
                log("    (verify) %s" % c)
            continue
        results.append(apply_device(dev, args.host, ports, args.apply, args.save,
                                    args.snapshot, args.auto_confirm, out_dir,
                                    args.timeout, log))

    if not args.apply:
        log("")
        log("预演结束。确认无误后加 --apply 真正下发。")
        return 0

    log("")
    log("=" * 66)
    total_errors = 0
    for r in results:
        total_errors += len(r["errors"])
        log("%-10s port=%-6s 下发 %d 条  报错 %d  证据=%s"
            % (r["name"], r.get("port"), r["applied"], len(r["errors"]),
               r.get("evidence", "-")))
    log("=" * 66)
    log("合计报错 %d 处" % total_errors)
    return 1 if total_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
