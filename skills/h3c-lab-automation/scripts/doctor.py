#!/usr/bin/env python3
"""doctor.py - skill 自检：迁移到新终端后，一条命令确认"这套东西是健康的"。

检查项（全部离线，不需要 HCL 在跑）：
  1. 必需文件是否齐全（SKILL.md / references / scripts）
  2. 所有 .py 能否编译（py_compile）
  3. 代码里有没有**硬编码绝对路径**（注释与 docstring 不算）——迁移杀手
  4. scripts/ 下的 .json 能否解析（verify-example / plan 模板）
  5. remember.py 能否列出记忆条目（记忆机制可用）
  6. cases.md 索引表与正文 `## C-xxx` 是否一致
  7. Python 版本是否够（>=3.8，脚本用了 `from __future__ import annotations`）

用法::

    python doctor.py                # 全部检查，每项一行 PASS/FAIL
    python doctor.py --live         # 额外跑一次 hcl_ports.py（需要 HCL 正在运行）

退出码：0 全过；1 有 FAIL。
"""

from __future__ import annotations

import argparse
import json
import py_compile
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REQUIRED = [
    "SKILL.md",
    "references/environment.md",
    "references/gotchas.md",
    "references/cases.md",
    "references/comware-recipes.md",
    "references/securecrt-automation.md",
    "references/ts-playbook.md",
    "references/aliases.md",
    "scripts/hcldrv.py",
    "scripts/hcl_ports.py",
    "scripts/hcl_lab.py",
    "scripts/verify.py",
    "scripts/remember.py",
    "scripts/newcase.py",
    "scripts/lintmem.py",
    "scripts/net2map.py",
    "scripts/linkwatch.py",
    "scripts/cleanup.py",
    "scripts/cfgdiff.py",
    "scripts/restore.py",
    "scripts/report.py",
    "scripts/lab_state.py",
    "scripts/doctor.py",
]
_ABS = re.compile(r"[A-Za-z]:\\\\|[A-Za-z]:\\")
_CASE = re.compile(r"^##\s+(C-\d{3})\b", re.MULTILINE)
_ROW = re.compile(r"^\|\s*(C-\d{3})\s*\|", re.MULTILINE)

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))


def code_lines(path: Path):
    """逐行产出 (行号, 文本)，跳过注释与三引号 docstring。"""
    in_doc = False
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        stripped = line.strip()
        ticks = stripped.count('"""') + stripped.count("'''")
        if in_doc:
            if ticks % 2 == 1:
                in_doc = False
            continue
        if ticks % 2 == 1:
            in_doc = True
            continue
        if stripped.startswith("#") or not stripped:
            continue
        yield i, line


def main() -> int:
    ap = argparse.ArgumentParser(description="skill 自检")
    ap.add_argument("--live", action="store_true", help="额外跑 hcl_ports.py（需 HCL 在运行）")
    args = ap.parse_args()

    # 1 文件齐全
    missing = [f for f in REQUIRED if not (ROOT / f).exists()]
    check(not missing, "必需文件齐全", "缺: " + ", ".join(missing) if missing else "%d 个都在" % len(REQUIRED))

    # 2 可编译
    bad = []
    for py in sorted((ROOT / "scripts").glob("*.py")):
        try:
            py_compile.compile(str(py), cfile=str(Path(tempfile.gettempdir()) / (py.stem + ".pyc")),
                               doraise=True)
        except py_compile.PyCompileError as exc:
            bad.append("%s (%s)" % (py.name, str(exc).splitlines()[0][:60]))
    check(not bad, "脚本可编译", "; ".join(bad) if bad else "全部通过")

    # 3 无硬编码绝对路径
    hits = []
    for py in sorted((ROOT / "scripts").glob("*.py")):
        for ln, line in code_lines(py):
            if _ABS.search(line):
                hits.append("%s:%d" % (py.name, ln))
    check(not hits, "代码无硬编码绝对路径（迁移关键）",
          "命中: " + ", ".join(hits) if hits else "干净")

    # 4 JSON 可解析
    badj = []
    for js in sorted((ROOT / "scripts").glob("*.json")):
        try:
            json.loads(js.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            badj.append("%s (%s)" % (js.name, str(exc)[:50]))
    check(not badj, "json 模板可解析", "; ".join(badj) if badj else "全部通过")

    # 5 记忆可查
    try:
        out = subprocess.run([sys.executable, str(HERE / "remember.py"), "--list"],
                             capture_output=True, text=True, timeout=60, encoding="utf-8").stdout
        n = len(re.findall(r"^\s*\[", out, re.MULTILINE))
        check(n > 0, "记忆可检索（remember.py）", "%d 个条目" % n)
    except Exception as exc:  # noqa: BLE001
        check(False, "记忆可检索（remember.py）", str(exc)[:80])

    # 6 案例索引一致
    cases = (ROOT / "references" / "cases.md").read_text(encoding="utf-8")
    ids, rows = set(_CASE.findall(cases)), set(_ROW.findall(cases))
    diff = sorted(ids ^ rows)
    check(not diff, "cases.md 索引表与正文一致",
          "不一致: " + ", ".join(diff) if diff else "%d 个案例都在索引里" % len(ids))

    # 7 Python 版本
    check(sys.version_info >= (3, 8), "Python >= 3.8", sys.version.split()[0])

    width = max(len(n) for _, n, _ in results)
    fails = 0
    for ok, name, detail in results:
        if not ok:
            fails += 1
        print("%s  %-*s  %s" % ("PASS" if ok else "FAIL", width, name, detail))

    if args.live:
        print("\n-- live 检查（需要 HCL 在跑）--")
        try:
            out = subprocess.run([sys.executable, str(HERE / "hcl_ports.py")],
                                 capture_output=True, text=True, timeout=180, encoding="utf-8").stdout
            print(out.strip()[-600:] or "(无输出)")
        except Exception as exc:  # noqa: BLE001
            print("hcl_ports.py 失败：%s" % str(exc)[:120])
            fails += 1

    print("\n合计 %d 项：PASS %d，FAIL %d" % (len(results), len(results) - fails, fails))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
