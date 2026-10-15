#!/usr/bin/env python3
"""从 verify-report.txt 里抽出 FAIL 块，逐条判定"超时是不是因为缓冲区尾部有日志行"。

判定规则（与 hcldrv._PROMPT 的行尾锚定一致）：
  * `_PROMPT.search(缓冲区)` —— 有没有一个**落在缓冲区末尾**的提示符；
  * 缓冲区里**最后一个提示符**之后还剩下什么 —— 若剩下的是 `%MMM DD hh:mm:ss:mmm YYYY HOST ...`
    形状的异步日志行，则 `_PROMPT` 永远匹配不上，超时必然发生（干等到 timeout）。

用法::

    python analyze_verify_report.py <verify-report.txt> [more-reports...]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hcldrv import _PROMPT                                    # noqa: E402

PROMPT_ANY = re.compile(r"[<\[](?:[^<>\[\]\r\n]{0,64})[>\]]")
LOG_LINE = re.compile(r"^%\w{3}\s+\d+\s+\d+:\d+:\d+:\d+\s+\d{4}\s+\S+")


def analyze(path: Path) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    stats = {"file": str(path), "total": 0, "fail": 0, "timeout": 0,
             "log_after_prompt": 0, "no_prompt": 0, "rows": []}
    for block in re.split(r"\n(?=- (?:PASS|FAIL) )", text):
        head = re.match(r"- (PASS|FAIL) (\S+)\s+(.*)", block.split("\n", 1)[0])
        if not head:
            continue
        stats["total"] += 1
        if head.group(1) != "FAIL":
            continue
        stats["fail"] += 1
        cid = head.group(2)
        ev = re.search(r"\n  证据: (.*)", block)
        body = block.split("--- 回显 ---", 1)[1] if "--- 回显 ---" in block else ""
        if not (ev and "命令超时" in ev.group(1)):
            stats["rows"].append((cid, "FAIL(非超时)", (ev.group(1) if ev else "")[:60], ""))
            continue
        stats["timeout"] += 1
        # 缓冲区 = ConsoleTimeout 的 transcript（报告里跟在 "ConsoleTimeout: 等待超时" 之后）
        buf = body.split("ConsoleTimeout: 等待超时", 1)[-1]
        last = None
        for m in PROMPT_ANY.finditer(buf):
            last = m
        if last is None:
            stats["no_prompt"] += 1
            stats["rows"].append((cid, "超时(缓冲区无提示符)", "?", buf.strip()[-60:]))
            continue
        after = buf[last.end():].strip()
        is_log = bool(LOG_LINE.match(after))
        if is_log:
            stats["log_after_prompt"] += 1
        stats["rows"].append(
            (cid,
             "超时(提示符后被日志行占位)" if is_log else "超时(其它)",
             "末尾提示符 %s" % last.group(0),
             after[-90:]))
    return stats


def main() -> int:
    for arg in sys.argv[1:]:
        p = Path(arg)
        if not p.is_file():
            print("跳过（不存在）: %s" % p)
            continue
        st = analyze(p)
        print("\n" + "=" * 100)
        print("报告: %s" % st["file"])
        print("  总项 %d / FAIL %d / 其中命令超时 %d" % (st["total"], st["fail"], st["timeout"]))
        print("  ★ 超时中【提示符被异步日志行挤到非行尾】= %d" % st["log_after_prompt"])
        print("     超时中【缓冲区里根本没有提示符】= %d" % st["no_prompt"])
        for cid, kind, a, b in st["rows"]:
            print("    - %-26s %-30s %s | 尾部: %r" % (cid, kind, a, b))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
