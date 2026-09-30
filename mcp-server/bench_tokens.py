#!/usr/bin/env python3
"""bench_tokens.py - 量"工具输出"进上下文的字符数（token 的大头在这里）。

为什么单独量：工具 schema 是每轮固定开销，而**输出**是随调用规模增长的。
`hcl_cfgdiff` 的 diff 以前没有上限——一次几千行的配置变更就能把上下文灌爆。
本脚本把旧版 server.py 从 git 取出来，对同一组"变更行数"分别量新旧版返回的字符数。

差分行数用**合成配置**制造，不碰任何设备。

用法：
  python bench_tokens.py                          # 默认 5,50,500,5000 行
  python bench_tokens.py --lines 10,100,1000 --old-rev b24e5a9
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
NEW_SERVER = HERE / "server.py"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                     # pragma: no cover
    pass


def repo_root(start: Path) -> Path | None:
    cur = start
    for _ in range(6):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def extract_from_git(root: Path, rev: str, rel: str, dest: Path) -> bool:
    proc = subprocess.run(["git", "-C", str(root), "show", "%s:%s" % (rev, rel)],
                          capture_output=True, timeout=120)
    if proc.returncode != 0:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(proc.stdout)
    return True


#: 子进程里跑的探针：猴补取配置的函数，制造一个"基线 + N 行新增"的 diff。
PROBE = r'''
import json, sys
sys.path.insert(0, %(here)r)
import server as S

baseline = "\n".join("line%%d" %% i for i in range(200))
extra = "\n".join("added-%%d" %% i for i in range(%(n)d))
S._capture_running_config = lambda port, timeout, max_chars: baseline + ("\n" + extra if %(n)d else "")

snap = S._snapshots_dir()
(snap / "PROBE-20260101-000000.cfg").write_text(baseline, encoding="utf-8")
text = S.tool_hcl_cfgdiff({"action": "diff", "port": 1, "name": "PROBE"})
print(json.dumps({"chars": len(text), "lines": len(text.splitlines()), "head": text[:80]}))
'''


def measure(server_dir: Path, n_lines: int, tmp: Path) -> dict:
    """在一个独立子进程里量一次，返回 {"chars","lines"}。"""
    tag = server_dir.name
    cfg_path = tmp / "cfg.json"
    cfg_path.write_text(json.dumps({
        "state_dir": str(tmp / ("state-" + tag)),
        "evidence_root": str(tmp / ("evidence-" + tag)),
    }, ensure_ascii=False), encoding="utf-8")
    code = PROBE % {"here": str(server_dir), "n": n_lines}
    env = dict(os.environ)
    env["H3C_MCP_CONFIG"] = str(cfg_path)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, cwd=str(server_dir), timeout=300)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                continue
    return {"chars": -1, "lines": -1, "head": (proc.stderr or "")[-200:]}


#: 子进程里跑的探针：量 hcl_report 返回的字符数（旧版给全文，新版默认只给预览）。
REPORT_PROBE = r'''
import json, sys
sys.path.insert(0, %(here)r)
import server as S
text = S.tool_hcl_report({"title": "token bench", "sections": %(sections)r, "net_file": %(net)r})
print(json.dumps({"chars": len(text), "lines": len(text.splitlines())}))
'''


def measure_report(server_dir: Path, net_file: str, tmp: Path, inline: bool) -> dict:
    tag = server_dir.name + ("-inline" if inline else "-preview")
    cfg_path = tmp / ("cfg-report-%s.json" % tag)
    cfg_path.write_text(json.dumps({
        "state_dir": str(tmp / ("state-" + tag)),
        "evidence_root": str(tmp / ("evidence-" + tag)),
    }, ensure_ascii=False), encoding="utf-8")
    code = REPORT_PROBE % {"here": str(server_dir), "sections": "topology", "net": net_file}
    env = dict(os.environ)
    env["H3C_MCP_CONFIG"] = str(cfg_path)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, cwd=str(server_dir), timeout=300)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                continue
    return {"chars": -1, "lines": -1}


def main() -> int:
    ap = argparse.ArgumentParser(description="工具输出字符数对比（token 大头）")
    ap.add_argument("--lines", default="5,50,500,5000", help="要模拟的变更行数，逗号分隔")
    ap.add_argument("--old-rev", default="HEAD~2",
                    help="旧版提交（默认 HEAD~2 = 0.3.0 性能改动之前）")
    ap.add_argument("--net-file", default=None,
                    help="给定时额外量 hcl_report（用这个 .net 拓扑）")
    args = ap.parse_args()

    sizes = [int(x) for x in args.lines.split(",") if x.strip()]
    root = repo_root(HERE)
    tmp = Path(tempfile.mkdtemp(prefix="h3clab-tokens-"))

    old_dir = tmp / "old"
    new_dir = tmp / "new"
    have_old = False
    if root is not None:
        have_old = (extract_from_git(root, args.old_rev, "mcp-server/server.py", old_dir / "server.py")
                    and extract_from_git(root, args.old_rev, "mcp-server/hcldrv.py", old_dir / "hcldrv.py"))
    if not have_old:
        old_dir = new_dir
        print("⚠ 取不到旧版（%s），只量新版。" % args.old_rev)
    new_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(NEW_SERVER, new_dir / "server.py")
    shutil.copy2(HERE / "hcldrv.py", new_dir / "hcldrv.py")

    print("=" * 84)
    print("hcl_cfgdiff 的 diff 输出：进上下文的字符数（合成配置，不碰设备）")
    print("旧版 = %s | 换算假设：4 字符 ≈ 1 token（英文/配置文本）" % args.old_rev)
    print("=" * 84)
    print("%-12s %14s %14s %10s   %s" % ("变更行数", "旧版字符", "新版字符", "降幅", "新版返回尾部"))
    print("-" * 84)

    for n in sizes:
        old = measure(old_dir, n, tmp) if have_old else {"chars": None, "lines": None}
        new = measure(new_dir, n, tmp)
        old_c = old.get("chars")
        new_c = new.get("chars")
        if old_c is None:
            drop = "-"
        elif old_c > 0:
            drop = "%d%%" % round((1 - new_c / old_c) * 100)
        else:
            drop = "?"
        tail = ""
        print("%-12d %14s %14s %10s   %s"
              % (n, old_c if old_c is not None else "-", new_c, drop, tail))

    print()
    print("说明：新版超过 max_lines（默认 200）就截断，完整 diff 落到证据目录并给出路径；")
    print("      旧版把整份 diff 原样返回。变更行数 ≤ ~208 时两者一致（截断有净收益门槛）。")

    if args.net_file:
        print()
        print("=" * 84)
        print("hcl_report（sections=topology）：旧版返回全文，新版默认只返回前 40 行预览")
        print("=" * 84)
        old_rep = measure_report(old_dir, args.net_file, tmp, False) if have_old else {"chars": None}
        new_rep = measure_report(new_dir, args.net_file, tmp, False)
        old_c, new_c = old_rep.get("chars"), new_rep.get("chars")
        print("%-18s %12s %12s %10s" % ("", "旧版字符", "新版字符", "降幅"))
        drop = "-" if not old_c or old_c <= 0 else "%d%%" % round((1 - new_c / old_c) * 100)
        print("%-18s %12s %12s %10s" % ("hcl_report", old_c if old_c else "-", new_c, drop))
        print("（要全文仍可传 inline=true；报告本身一直在文件里，不丢数据）")

    print()
    print("临时目录：%s" % tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
