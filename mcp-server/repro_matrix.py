#!/usr/bin/env python3
"""跑 N 遍完整 63 条清单，统计超时率与分布；并对每次超时抓"提示符被日志挤走"的铁证。

铁证的做法：给 ``Console.read_until`` 打桩，在它**即将抛 ConsoleTimeout** 时，
检查缓冲区里是不是**存在**提示符、但**不落在行尾**（`_PROMPT` 是 `[>\\]]\\s*$` 锚定的）。
若存在，就打印最后一个提示符之后的内容 —— 那就是把提示符挤走的异步日志行。

用法::

    python repro_matrix.py [runs] [workers] [timeout]
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import server as S                                          # noqa: E402
from hcldrv import Console, ConsoleTimeout, _PROMPT          # noqa: E402

CHECKLIST = r"D:\DSH\NET\lab20\verify-final.generated.json"
PROMPT_ANY = re.compile(r"[<\[](?:[^<>\[\]\r\n]{0,64})[>\]]")
LOG_LINE = re.compile(r"^%\w{3}\s+\d+\s+\d+:\d+:\d+:\d+\s+\d{4}\s+\S+")

#: 每次超时的"法证记录"
FORENSICS: list[dict] = []
_orig_read_until = Console.read_until


def _read_until(self, patterns, timeout=None, start=None, answer_more=True):  # noqa: ANN001
    try:
        return _orig_read_until(self, patterns, timeout=timeout, start=start,
                                answer_more=answer_more)
    except ConsoleTimeout as exc:
        _record(exc, start)
        raise


def _record(exc: ConsoleTimeout, start) -> None:            # noqa: ANN001
    buf = exc.transcript
    last = None
    for m in PROMPT_ANY.finditer(buf):
        last = m
    rec = {"buffer_len": len(buf), "strict_hit": bool(_PROMPT.search(buf)),
           "prompt_present": last is not None, "tail_after_prompt": "", "is_log": False}
    if last is not None:
        after = buf[last.end():]
        rec["tail_after_prompt"] = after[-200:]
        rec["is_log"] = bool(LOG_LINE.match(after.strip()))
        rec["prompt"] = last.group(0)
    FORENSICS.append(rec)


Console.read_until = _read_until


def parse_report(path: Path) -> list[dict]:
    """从证据报告里解析每一条 check 的结果（含证据文本）。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    rows = []
    cur_port = None
    for block in re.split(r"\n(?=- (?:PASS|FAIL|TIMEOUT|ERROR|SKIP) )", text):
        m = re.match(r"## (\S+) \(port (\d+)\)", block)
        if m:
            cur_port = int(m.group(2))
        head = re.match(r"- (PASS|FAIL|TIMEOUT|ERROR|SKIP) (\S+)\s+(.*)", block.split("\n", 1)[0])
        if not head:
            continue
        ev = re.search(r"\n  证据: (.*)", block)
        cmd = re.search(r"\n  cmd: (.*)", block)
        rows.append({"port": cur_port, "status": head.group(1), "id": head.group(2),
                     "evidence": (ev.group(1) if ev else "").strip(),
                     "cmd": (cmd.group(1) if cmd else "").strip()})
    return rows


def main() -> int:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 90.0

    spec = json.loads(Path(CHECKLIST).read_text(encoding="utf-8-sig"))
    order: dict[int, list[str]] = {}
    for c in spec["checks"]:
        order.setdefault(int(c["port"]), []).append(c["id"])

    all_rows: list[dict] = []
    for r in range(runs):
        print("\n########## run %d/%d  workers=%d timeout=%.0fs ##########"
              % (r + 1, runs, workers, timeout), flush=True)
        FORENSICS.clear()
        t0 = time.time()
        out = S.tool_hcl_verify({"checklist_json": CHECKLIST, "timeout": timeout,
                                 "workers": workers})
        el = time.time() - t0
        print("  耗时 %.1fs" % el, flush=True)
        last_line = [ln for ln in out.splitlines() if ln.startswith("合计")]
        print("  %s" % (last_line[0] if last_line else "(没有合计行)"), flush=True)
        ev = re.search(r"证据: (.*)", out)
        rep = Path(ev.group(1).strip()) if ev else None
        rows = parse_report(rep) if rep and rep.is_file() else []
        for row in rows:
            row["run"] = r + 1
        all_rows.extend(rows)
        bad = [x for x in rows if x["status"] != "PASS"]
        for x in bad:
            idx = order.get(x["port"], []).index(x["id"]) if x["id"] in order.get(x["port"], []) else -1
            print("   %-7s %-24s port=%-6s 第%d条  ev=%s"
                  % (x["status"], x["id"], x["port"], idx + 1, x["evidence"][:44]), flush=True)
        print("   -- 法证（每次超时的缓冲区状态）--", flush=True)
        for f in FORENSICS:
            print("     提示符在缓冲区里? %-5s  严格匹配(_PROMPT)=%-5s  之后是日志行? %-5s  尾部=%r"
                  % (f["prompt_present"], f["strict_hit"], f["is_log"], f["tail_after_prompt"][-90:]), flush=True)

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 90)
    print("汇总：%d 次运行，共 %d 项" % (runs, len(all_rows)))
    from collections import Counter
    print("  状态分布：%s" % dict(Counter(x["status"] for x in all_rows)))
    bad = [x for x in all_rows if x["status"] != "PASS"]
    print("  非 PASS 明细（按 端口/命令 聚合）：")
    agg = Counter((x["port"], x["cmd"]) for x in bad)
    for (port, cmd), n in agg.most_common():
        print("    %-6s x%-2d %s" % (port, n, cmd))
    print("  非 PASS 落在端口上的分布：%s" % dict(Counter(x["port"] for x in bad)))
    print("  非 PASS 的「第几条 check」分布：", end="")
    idxs = []
    for x in bad:
        seq = order.get(x["port"], [])
        idxs.append(seq.index(x["id"]) + 1 if x["id"] in seq else -1)
    print(dict(Counter(idxs)))
    # 逐条断言在多次运行中的稳定性
    print("\n  每条 check 的历史：只有非全 PASS 的才列")
    hist: dict[str, Counter] = {}
    for x in all_rows:
        hist.setdefault(x["id"], Counter())[x["status"]] += 1
    for cid, cnt in hist.items():
        if set(cnt) != {"PASS"}:
            print("    %-24s %s" % (cid, dict(cnt)))
    print("\n  超时法证统计：%d 次超时中，%d 次'提示符在缓冲区但不落在行尾'，%d 次'其后就是异步日志行'"
          % (len(FORENSICS),
             sum(1 for f in FORENSICS if f["prompt_present"] and not f["strict_hit"]),
             sum(1 for f in FORENSICS if f["is_log"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
