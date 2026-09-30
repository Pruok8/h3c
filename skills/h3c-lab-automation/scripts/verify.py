#!/usr/bin/env python3
"""verify.py - 一条命令跑完整个验证矩阵；**每项只打印一行 PASS/FAIL**，明细落文件。

为什么要有它
------------
逐项手敲 ``display`` 再把全量回显拉进上下文，是实验自动化里最烧 token 的做法。
本脚本把「连接、下发、断言匹配、汇总」全放在本地做，只把结论打出来；
完整回显写进报告文件，需要时再用 grep / read 取那几行。

checklist.json 格式
-------------------
::

    {
      "out": "verify-report.txt",
      "checks": [
        {"id": "MLAG", "port": 30008, "desc": "SW1 M-LAG 三组 UP",
         "cmd": "display m-lag summary",
         "expect": ["BAGG2\\s+2\\s+UP", "BAGG3\\s+3\\s+UP", "BAGG4\\s+4\\s+UP"]},

        {"id": "E2E-1", "port": 30005, "desc": "PC -> Server VM1 0% 丢包",
         "cmd": "ping -c 2 10.10.1.3",
         "expect": ["0\\.0% packet loss"]},

        {"id": "ISO-1", "port": 30005, "desc": "PC 访问不到隔离网段（负向验证）",
         "cmd": "ping -c 2 192.168.99.1",
         "expect": ["100\\.0% packet loss"]},

        {"id": "NEG-1", "port": 30008, "desc": "业务网段上没有 OSPF 邻居（负向验证）",
         "cmd": "display ospf 1 peer",
         "expect_not": ["Vlan-interface10", "Vlan-interface20"]}
      ]
    }

规则
----
* ``expect`` 里的正则**全部命中**才算 PASS；
* ``expect_not`` 给了就要求**一个都不命中**（用于"不该出现的东西不在"这类负向验证）；
* 两者可以同时给；都不给则只检查该命令没有报错（``%`` 开头的行）；
* 同一台设备（同 port）的多项只连一次，顺序跑完再断开 —— 省时间也省回显。

用法
----
::

    python verify.py checklist.json                     # 跑全部
    python verify.py checklist.json --only MLAG,E2E     # 只跑指定 id（前缀匹配）
    python verify.py checklist.json --out report.txt    # 指定明细文件
    python verify.py checklist.json --timeout 20        # 单条命令超时（秒）

退出码：``0`` 全过；``1`` 有 FAIL；``2`` 有设备连不上。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hcldrv import Console, ConsoleError, ConsoleTimeout, _PROMPT  # noqa: E402

_ERR_LINE = re.compile(r"^\s*%\s*\S+", re.MULTILINE)


def _one_line(text: str, limit: int = 70) -> str:
    """把多行回显压成一行，便于单行输出。"""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return "(空输出)"


def _to_user_view(con: Console, timeout: float) -> None:
    """控制台视图状态跨连接保持；先归位用户视图，避免"命令不识别"其实是视图不对。"""
    match = _PROMPT.search(con.text)
    if match and match.group(0).strip().startswith("["):
        try:
            con.command("return", timeout=timeout)
        except (ConsoleError, ConsoleTimeout):
            pass


def _run_check(con: Console, check: dict, timeout: float) -> tuple[bool, str, str]:
    """返回 (是否通过, 证据片段, 完整输出或错误说明)。"""
    cmd = check.get("cmd", "")
    # 单项可覆盖超时：ping / save / undo interface 这类天生慢的命令务必单独放大
    per = float(check.get("timeout") or timeout)
    try:
        out = con.command(cmd, timeout=per)
    except ConsoleTimeout as exc:
        return False, "命令超时(%.0fs)" % per, "ConsoleTimeout: %s\n%s" % (exc, exc.transcript[-2000:])
    except ConsoleError as exc:
        return False, "命令失败", "%s: %s" % (type(exc).__name__, exc)

    expect = check.get("expect") or []
    forbid = check.get("expect_not") or []

    ok = True
    snippet = ""
    if expect:
        for pat in expect:
            m = re.search(pat, out, re.MULTILINE)
            if not m:
                ok = False
                snippet = "未匹配: %s" % pat
                break
            if not snippet:
                snippet = m.group(0).strip().replace("\n", " ")[:70]
    if ok and forbid:
        for pat in forbid:
            m = re.search(pat, out, re.MULTILINE)
            if m:
                ok = False
                snippet = "不该出现: %s" % m.group(0).strip()[:70]
                break
    if ok and not expect and not forbid:
        # 没给断言：退化为"没有 Comware 报错"
        bad = _ERR_LINE.search(out)
        if bad:
            ok = False
            snippet = bad.group(0).strip()
        else:
            snippet = _one_line(out)
    if not snippet:
        snippet = _one_line(out)
    return ok, snippet, out


def main() -> int:
    ap = argparse.ArgumentParser(description="跑验证矩阵，只打印 PASS/FAIL 一行一项")
    ap.add_argument("checklist", help="checklist.json 路径")
    ap.add_argument("--out", help="明细报告文件（默认取 checklist 里的 out，或 verify-report.txt）")
    ap.add_argument("--only", help="只跑指定 id，逗号分隔，前缀匹配")
    ap.add_argument("--timeout", type=float, default=15.0, help="单条命令超时秒数（默认 15）")
    ap.add_argument("--quiet", action="store_true",
                    help="只打印 FAIL 行与合计：全过时输出只有 2 行（日常回归首选，省上下文）")
    args = ap.parse_args()

    spec = json.loads(Path(args.checklist).read_text(encoding="utf-8"))
    checks = spec.get("checks") or []
    if args.only:
        wanted = [s.strip() for s in args.only.split(",") if s.strip()]
        checks = [c for c in checks if any(str(c.get("id", "")).startswith(w) for w in wanted)]
    if not checks:
        print("没有可执行的检查项")
        return 1

    out_path = Path(args.out or spec.get("out") or "verify-report.txt")

    # 按设备分组：同 port 只连一次
    by_port: dict[int, list[dict]] = {}
    for c in checks:
        by_port.setdefault(int(c["port"]), []).append(c)

    passed = failed = unreachable = 0
    report: list[str] = ["# verify 报告  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "checklist: %s" % args.checklist, ""]

    for port, items in by_port.items():
        tag = items[0].get("name") or str(port)
        con = Console(port=port, timeout=max(20.0, args.timeout))
        try:
            con.connect(timeout=3.0)
            con.prep(timeout=60)
        except (ConsoleError, ConsoleTimeout) as exc:
            # 整台设备连不上：该设备下所有项都记 FAIL，但只打一行汇总
            unreachable += 1
            failed += len(items)
            print("FAIL  %-10s %-28s | 连不上设备 %s: %s" % (tag, "（%d 项）" % len(items), port, exc))
            report.append("## %s (port %s) 连接失败: %s\n" % (tag, port, exc))
            for c in items:
                report.append("- FAIL %s %s | 连接失败" % (c.get("id"), c.get("desc", "")))
            try:
                con.close()
            except Exception:
                pass
            continue

        _to_user_view(con, args.timeout)
        report.append("## %s (port %s)\n" % (tag, port))
        for c in items:
            cid = str(c.get("id", "?"))
            desc = str(c.get("desc", ""))
            ok, snippet, detail = _run_check(con, c, args.timeout)
            if ok:
                passed += 1
            else:
                failed += 1
            if not ok or not args.quiet:
                print("%s  %-10s %-30s | %s" % ("PASS" if ok else "FAIL", cid, desc, snippet))
            report.append("- %s %s  %s\n  cmd: %s\n  证据: %s\n  --- 回显 ---\n%s\n"
                          % ("PASS" if ok else "FAIL", cid, desc, c.get("cmd", ""), snippet, detail))
        con.close()

    total = passed + failed
    summary = "\n合计 %d 项：PASS %d，FAIL %d%s\n明细: %s" % (
        total, passed, failed,
        "，%d 台连不上" % unreachable if unreachable else "",
        out_path)
    if args.quiet and failed == 0:
        # 干净跑完只回一行，避免把 20 行 PASS 灌进上下文
        print("全部通过：%d 项 PASS    明细: %s" % (total, out_path))
    else:
        print(summary)
    report.append(summary)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(report), encoding="utf-8")
    return 0 if failed == 0 else (2 if unreachable else 1)


if __name__ == "__main__":
    raise SystemExit(main())
