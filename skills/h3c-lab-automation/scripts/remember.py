#!/usr/bin/env python3
"""remember.py - 查"记忆"。出问题的**第一动作**：先跑它，再查官方文档。

记忆分两层，都在本 skill 里（跟着目录一起迁移）：
  references/cases.md    逐次作业的案例台账（症状原文、根因、可复制命令、证据）
  references/gotchas.md  按症状索引的知识库（所有踩过的坑）

用法::

    python remember.py --list                  # 列出所有条目标题，先扫一眼有没有对得上的
    python remember.py m-lag DOWN              # 多个关键词=必须全部命中（更准）
    python remember.py m-lag --any             # 任一关键词命中即可（更宽）
    python remember.py consistency --file cases # 只在案例台账里搜
    python remember.py LACP --context 3        # 每个命中多显示几行上下文
    python remember.py 卡顿 --max 3            # 最多输出 3 个条目

输出刻意做得很短：只给「条目标题 + 命中行及其上下文」，
全文请直接看对应文件的那一节（标题里带了文件名与行号）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REF = HERE.parent / "references"
FILES = {"cases": REF / "cases.md", "gotchas": REF / "gotchas.md"}
ALIASES = REF / "aliases.md"
_HEAD = re.compile(r"^(#{3,4})\s+(.*)$")
_H2 = re.compile(r"^##\s+(.*)$")


def load_aliases() -> list[list[str]]:
    """读 references/aliases.md：每行一组同义词（用 | 分隔）。"""
    groups: list[list[str]] = []
    if not ALIASES.exists():
        return groups
    for line in ALIASES.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "|" not in s:
            continue
        terms = [t.strip() for t in s.split("|") if t.strip()]
        if len(terms) >= 2:
            groups.append(terms)
    return groups


def expand(keys: list[str], groups: list[list[str]]) -> list[list[str]]:
    """每个查询词扩展成一个同义词组；组内任一命中即算该词命中（组间仍是 AND）。"""
    out: list[list[str]] = []
    for k in keys:
        hit = next((g for g in groups
                    if any(k.lower() == t.lower() or k.lower() in t.lower() or t.lower() in k.lower()
                           for t in g)), None)
        out.append(sorted({k} | set(hit or [])))
    return out


class Block:
    __slots__ = ("file", "line", "section", "heading", "body")

    def __init__(self, file: str, line: int, section: str, heading: str) -> None:
        self.file = file
        self.line = line
        self.section = section
        self.heading = heading
        self.body: list[str] = []

    @property
    def title(self) -> str:
        return ("%s / " % self.section if self.section else "") + self.heading


def parse(path: Path, key: str) -> list[Block]:
    blocks: list[Block] = []
    section = ""
    cur: Block | None = None
    if not path.exists():
        return blocks
    for i, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = raw.rstrip()
        m2 = _H2.match(line)
        if m2 and not _HEAD.match(line):
            section = m2.group(1).strip()
            continue
        m = _HEAD.match(line)
        if m:
            cur = Block(key, i, section, m.group(2).strip())
            blocks.append(cur)
            continue
        if cur is not None:
            cur.body.append(line)
    return blocks


def main() -> int:
    ap = argparse.ArgumentParser(description="在 skill 的记忆里按关键词检索")
    ap.add_argument("keywords", nargs="*", help="关键词；默认要求全部命中")
    ap.add_argument("--file", choices=["cases", "gotchas", "all"], default="all")
    ap.add_argument("--any", action="store_true", help="任一关键词命中即可")
    ap.add_argument("--list", action="store_true", help="只列出所有条目标题")
    ap.add_argument("--context", type=int, default=1, help="命中行上下各显示几行（默认 1）")
    ap.add_argument("--max", type=int, default=6, help="最多输出几个条目（默认 6）")
    ap.add_argument("--brief", action="store_true",
                    help="只列命中条目的标题+行号，不打印正文（先看命中哪些，再决定看哪一条，省上下文）")
    args = ap.parse_args()

    keys = [k for k in args.keywords if k]
    groups = expand(keys, load_aliases())
    terms = sorted({t for g in groups for t in g})
    targets = list(FILES) if args.file == "all" else [args.file]
    index: list[tuple[str, Block]] = []
    for key in targets:
        for b in parse(FILES[key], key):
            index.append((key, b))

    if args.list or not keys:
        print("== 记忆条目索引（%d 条）==" % len(index))
        for key, b in index:
            print("  [%s] %s:%d  %s" % (key, FILES[key].name, b.line, b.title[:90]))
        if not keys:
            print("\n提示：加上关键词再跑一次，例如  python remember.py m-lag DOWN")
        return 0

    scored: list[tuple[int, tuple[str, Block, list[tuple[int, str]]]]] = []
    for key, b in index:
        text = b.title + "\n" + "\n".join(b.body)
        low = text.lower()
        hitg = [g for g in groups if any(t.lower() in low for t in g)]
        if args.any:
            if not hitg:
                continue
        elif len(hitg) != len(groups):
            continue
        lines = (b.title + "|||" + "\n".join(b.body)).splitlines()
        hitlines = [(i, ln) for i, ln in enumerate(lines)
                    if any(t.lower() in ln.lower() for t in terms)]
        scored.append((len(hitg), (key, b, hitlines)))
    scored.sort(key=lambda t: -t[0])
    multi = [g for g in groups if len(g) > 1]
    if multi and scored:
        print("（同义词已扩展：%s）" % "；".join("%s→%s" % (g[0], "/".join(g[1:4])) for g in multi[:4]))

    if not scored:
        print("记忆里没有命中：%s" % " ".join(keys))
        print("\n按铁律 2 的顺序继续：")
        print("  1) 换关键词再查一次（试试报错原文里的英文词，或设备名/协议名）")
        print("  2) 查厂商官方文档（H3C 配置指导/命令参考；设备上 `display xxx ?` 是最权威的现场文档）")
        print("  3) 官方文档没解决再搜同厂商案例/外部资料")
        print("  ★ 问题解决后，把这次的经验写回 references/cases.md（可复用的提炼进 gotchas.md）")
        return 1

    print("== 命中 %d 个条目（显示前 %d 个）==" % (len(scored), min(args.max, len(scored))))
    if args.brief:
        # --brief：只报「命中在哪」，选好再看全文 —— 命中多时能省掉大量正文
        for _score, (key, b, _hits) in scored[: args.max]:
            print("  [%s] %s:%d  %s" % (key, FILES[key].name, b.line, b.title))
        print("\n（--brief：只看标题。去掉 --brief 看命中原文，或加 --max 1 只看一条）")
        return 0
    for _score, (key, b, hitlines) in scored[: args.max]:
        print("\n### [%s] %s:%d  %s" % (key, FILES[key].name, b.line, b.title))
        shown: set[int] = set()
        n = 0
        for i, ln in hitlines:
            for j in range(max(0, i - args.context), min(len(b.body) + 1, i + args.context + 1)):
                if j in shown:
                    continue
                shown.add(j)
                text = b.title if j == 0 else (b.body[j - 1] if j - 1 < len(b.body) else "")
                if text.strip():
                    print("    %s" % text.strip()[:160])
                n += 1
                if n >= 22:
                    break
            if n >= 22:
                print("    …（截断，全文见 %s 第 %d 行）" % (FILES[key].name, b.line))
                break
    print("\n(全文：references/%s)" % " / references/".join(FILES[t].name for t in targets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
