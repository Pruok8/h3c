#!/usr/bin/env python3
"""newcase.py - 往 `references/cases.md` 追加一个案例骨架，并**自动插入索引表**。

为什么需要
----------
"收工必须写回记忆"这条铁律，如果写回很麻烦就不会被坚持。
本脚本把最费事的两步（编编号、插索引行）自动化，你只要填内容。

用法
----
::

    python newcase.py "MSTP 根桥不对" --symptom "SW3 抢走了根桥，SW1 本该是根" \
        --keywords "stp,根桥,root,priority" --scene "园区网 3 台接入"

    python newcase.py --list                # 看看现在有哪些案例
    python newcase.py "标题" --dry-run       # 只看会写成什么样，不落盘

生成后请**手写填充** 根因 / 解法 / 证据 三节 —— 机器生成不了那部分。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASES = HERE.parent / "references" / "cases.md"
_ID = re.compile(r"^##\s+(C-\d{3})\b(.*)$", re.MULTILINE)
_ROW = re.compile(r"^\|\s*(C-\d{3})\s*\|", re.MULTILINE)


def next_id(text: str) -> str:
    nums = [int(m.group(1).split("-")[1]) for m in _ID.finditer(text)]
    return "C-%03d" % (max(nums) + 1 if nums else 1)


def insert_row(text: str, row: str) -> str:
    """插到索引表最后一行的后面；找不到就追加到表头后面。"""
    lines = text.splitlines()
    last = -1
    for i, ln in enumerate(lines):
        if _ROW.match(ln):
            last = i
    if last >= 0:
        lines.insert(last + 1, row)
        return "\n".join(lines) + "\n"
    for i, ln in enumerate(lines):
        if ln.startswith("|---"):
            lines.insert(i + 1, row)
            return "\n".join(lines) + "\n"
    return text.rstrip() + "\n" + row + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="给 cases.md 追加案例并更新索引表")
    ap.add_argument("title", nargs="?", help="案例标题（一句话）")
    ap.add_argument("--scene", default="", help="场景：拓扑/版本/需求")
    ap.add_argument("--symptom", default="", help="一句话症状（写报错原文最利于检索）")
    ap.add_argument("--keywords", default="", help="逗号分隔的关键词（会写进索引表）")
    ap.add_argument("--list", action="store_true", help="列出现有案例")
    ap.add_argument("--dry-run", action="store_true", help="只打印不落盘")
    ap.add_argument("--file", default=str(CASES), help="cases.md 路径")
    args = ap.parse_args()

    path = Path(args.file)
    text = path.read_text(encoding="utf-8") if path.exists() else "# 案例记忆\n\n## 索引\n\n| 编号 | 日期 | 场景 | 一句话症状 | 关键词 |\n|---|---|---|---|---|\n"

    if args.list:
        ids = [(m.group(1), m.group(2).strip()) for m in _ID.finditer(text)]
        print("现有案例 %d 个：" % len(ids))
        for cid, title in ids:
            print("  %s  %s" % (cid, title))
        return 0

    if not args.title:
        ap.error("需要标题（或用 --list）")

    cid = next_id(text)
    today = date.today().isoformat()
    row = "| %s | %s | %s | %s | %s |" % (cid, today, args.scene or "-", args.symptom or "-",
                                          args.keywords or "-")
    block = """
## {cid} {title}

**场景**：{scene}

### 症状
{symptom}

（把**报错原文 / 你的原话**贴在这里，越原样越好检索。）

### 根因
TODO

### 解法（可直接复制的命令）
```
TODO
```

### 怎么验证
```
TODO
```

### 版本 / 环境
TODO（设备型号、软件版本、模拟器/真机；如是"官方文档 vs 实际表现不一致"，两边都写清）

### 沉淀
TODO（可复用的部分记得提炼进 references/gotchas.md）
""".format(cid=cid, title=args.title, scene=args.scene or "TODO", symptom=args.symptom or "TODO")

    new_text = insert_row(text, row).rstrip() + "\n" + block
    if args.dry_run:
        print("--- 索引行 ---\n%s\n--- 新增块 ---%s" % (row, block))
        return 0
    path.write_text(new_text, encoding="utf-8")
    print("已写入 %s：%s（索引表已更新，共 %d 个案例）"
          % (path, cid, len(list(_ID.finditer(new_text)))))
    print("下一步：填充 根因 / 解法 / 怎么验证 / 版本环境 四节，再跑 lintmem.py 自检。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
