#!/usr/bin/env python3
"""lintmem.py - 记忆体检：保证 cases.md / gotchas.md / aliases.md 结构没坏、能查到。

为什么需要
----------
记忆一旦结构坏了（索引行漏了、案例缺"根因"节、同义词表格式错），
`remember.py` 就查不到 —— 而你不会立刻发现，只会在下次踩坑时白踩一遍。
把它挂进收工流程（或 `doctor.py`），结构问题当场暴露。

检查项：
  cases.md   ① 索引表与正文 `## C-xxx` 一一对应
             ② 每个案例必须有 症状 / 根因 / 解法 / 怎么验证（或"验证"）/ 版本 五节
             ③ 索引行 5 列齐全、关键词列不能是空/`-`
             ④ 残留 TODO 提醒（骨架还没填完）
  gotchas.md ⑤ 每个 `### ` 小节正文要含 对策/原因/解法/根因/修法 之一
  aliases.md ⑥ 每组至少 2 个词、无重复词

用法::

    python lintmem.py            # 每类问题一行 + 例子
    python lintmem.py --strict   # 把"提醒级"也算失败（残留 TODO 直接当错）

退出码：0 通过；1 有问题。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF = ROOT / "references"
_CASE = re.compile(r"^##\s+(C-\d{3})\b(.*)$", re.MULTILINE)
_ROW = re.compile(r"^\|\s*(C-\d{3})\s*\|(.*)$", re.MULTILINE)
_H3 = re.compile(r"^###\s+(.+)$", re.MULTILINE)
_NEED = ["症状", "根因", "解法", "验证", "版本"]
_CURE = ("对策", "原因", "解法", "根因", "修法", "处置")

problems: list[tuple[str, list[str]]] = []


def add(kind: str, items: list[str]) -> None:
    if items:
        problems.append((kind, items))


def main() -> int:
    ap = argparse.ArgumentParser(description="记忆结构体检")
    ap.add_argument("--strict", action="store_true", help="残留 TODO 也算失败")
    args = ap.parse_args()

    # ---- cases.md ----
    cp = REF / "cases.md"
    if cp.exists():
        text = cp.read_text(encoding="utf-8")
        ids = [m.group(1) for m in _CASE.finditer(text)]
        rows = [m.group(1) for m in _ROW.finditer(text)]
        add("cases 索引与正文不一致", sorted(set(ids) ^ set(rows)))
        add("cases 有重复编号", sorted({i for i in ids if ids.count(i) > 1}))

        # 逐块检查必备小节
        blocks = list(_CASE.finditer(text))
        for i, m in enumerate(blocks):
            end = blocks[i + 1].start() if i + 1 < len(blocks) else len(text)
            body = text[m.start():end]
            lack = [k for k in _NEED if k not in body]
            if lack:
                add("cases 案例缺小节", ["%s 缺 %s" % (m.group(1), "/".join(lack))])
            if re.search(r"^TODO\s*$", body, re.MULTILINE):
                add("cases 案例仍有 TODO（提醒）", [m.group(1)])

        for m in _ROW.finditer(text):
            cols = [c.strip() for c in m.group(2).split("|")]
            if len(cols) < 4 or cols[-2] in ("", "-"):
                add("cases 索引行缺关键词", [m.group(1)])

    # ---- gotchas.md ----
    gp = REF / "gotchas.md"
    if gp.exists():
        text = gp.read_text(encoding="utf-8")
        heads = list(_H3.finditer(text))
        for i, m in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            body = text[m.start():end]
            if not any(k in body for k in _CURE):
                add("gotchas 小节没写处置", [m.group(1)[:48]])

    # ---- aliases.md ----
    ap_ = REF / "aliases.md"
    if ap_.exists():
        seen: dict[str, int] = {}
        for ln, line in enumerate(ap_.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#") or "|" not in s:
                continue
            terms = [t.strip() for t in s.split("|") if t.strip()]
            if len(terms) < 2:
                add("aliases 组内不足 2 词", ["第 %d 行" % ln])
            for t in terms:
                if t.lower() in seen:
                    add("aliases 词重复", ["%s（第 %d 与 %d 行）" % (t, seen[t.lower()], ln)])
                seen[t.lower()] = ln

    if not problems:
        print("PASS  记忆结构完好（cases.md / gotchas.md / aliases.md）")
        return 0

    fails = 0
    for kind, items in problems:
        soft = "提醒" in kind or "没写处置" in kind
        if not soft or args.strict:
            fails += 1
        print("%s  %-26s %d 处：%s" % ("WARN" if soft else "FAIL", kind, len(items),
                                       "; ".join(items[:5]) + (" …" if len(items) > 5 else "")))
    print("\n合计 %d 类问题（%s）" % (len(problems), "严格模式" if args.strict else "宽松模式"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
