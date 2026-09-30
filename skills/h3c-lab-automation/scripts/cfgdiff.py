#!/usr/bin/env python3
"""cfgdiff.py - 多台设备配置差异对比，重点解决「**两台都缺**」这个盲区。

为什么需要
----------
本次实验我用 `Compare-Object` 手工 diff SW1/SW2，差点漏掉一个致命盲区：
**如果某条命令两台都缺失，普通 diff 是看不出来的**（两边都没有 = 无差异）。
所以本工具除了 diff，还支持 `expect_both`：逐条断言"这条应该同时存在于每台设备"，
缺在哪台（或两台都缺）都会明确报出来。

spec.json::

    {
      "devices": [{"name": "SW1", "port": 30008}, {"name": "SW2", "port": 30009}],
      "ignore": ["^sysname", "router-id", "^ip address 10\\.10\\.",
                 "m-lag system-number", "keepalive ip destination", "^vrrp .* priority"],
      "expect_both": ["port m-lag peer-link 1", "m-lag system-mac 0001-0001-0001",
                      "m-lag consistency-check disable"],
      "out": "cfgdiff.txt"
    }

`ignore` 里的正则命中的差异行会被当作"预期差异"跳过；剩下的就是**需要解释的差异**。

用法::

    python cfgdiff.py spec.json
    python cfgdiff.py spec.json --max-diff 40      # 最多打印多少行差异（默认 25）

退出码：0 无异常差异且 expect_both 全在；1 有需要解释的差异或缺失。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hcldrv import Console, ConsoleError, ConsoleTimeout  # noqa: E402

_SKIP = re.compile(r"^(#|return|screen-length disable|\$ |\s*$)")


def clean(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        s = line.strip()
        if _SKIP.match(s) or s.startswith("---- More ----"):
            continue
        out.append(s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="多设备配置差异对比（含'两台都缺'检查）")
    ap.add_argument("spec", help="spec.json")
    ap.add_argument("--max-diff", type=int, default=25, help="最多打印多少行差异")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    devs = spec.get("devices") or []
    if len(devs) < 1:
        print("spec 里要有 devices")
        return 2
    ignore = [re.compile(p) for p in (spec.get("ignore") or [])]
    expect = spec.get("expect_both") or []

    cfgs: dict[str, list[str]] = {}
    for d in devs:
        con = Console(port=int(d["port"]), timeout=30.0)
        try:
            con.connect(timeout=3.0)
            con.prep(timeout=60)
            out = con.command("display current-configuration", timeout=90)
        except (ConsoleError, ConsoleTimeout) as exc:
            print("取 %s 配置失败：%s" % (d["name"], str(exc)[:80]))
            con.close()
            return 2
        finally:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
        cfgs[d["name"]] = clean(out)

    names = list(cfgs)
    base = names[0]
    report: list[str] = ["# 配置差异  base=%s  对比=%s" % (base, ", ".join(names[1:]) or "-")]
    bad = 0

    # ---- 差异（去掉预期项）----
    for other in names[1:]:
        only_a = [l for l in cfgs[base] if l not in cfgs[other]]
        only_b = [l for l in cfgs[other] if l not in cfgs[base]]
        unexp_a = [l for l in only_a if not any(p.search(l) for p in ignore)]
        unexp_b = [l for l in only_b if not any(p.search(l) for p in ignore)]
        print("== %s vs %s：差异 %d 行（其中需解释 %d 行）"
              % (base, other, len(only_a) + len(only_b), len(unexp_a) + len(unexp_b)))
        for l in unexp_a[: args.max_diff]:
            print("  仅 %s: %s" % (base, l[:110]))
        for l in unexp_b[: args.max_diff]:
            print("  仅 %s: %s" % (other, l[:110]))
        report += ["", "## %s vs %s" % (base, other),
                   "-- 仅 %s --" % base] + unexp_a + ["-- 仅 %s --" % other] + unexp_b
        bad += len(unexp_a) + len(unexp_b)

    # ---- expect_both：抓"两台都缺" ----
    if expect:
        print("\n== expect_both 检查（'缺'含**每台都缺**的情况）==")
        for pat in expect:
            rx = re.compile(pat)
            missing = [n for n in names if not any(rx.search(l) for l in cfgs[n])]
            if not missing:
                print("  OK    %s" % pat[:70])
            else:
                flag = "两台都缺! " if len(missing) == len(names) else ""
                print("  缺    %s%s（%s）" % (flag, pat[:60], ", ".join(missing)))
                bad += 1
                report.append("MISSING %s -> %s" % (pat, ", ".join(missing)))

    out = spec.get("out") or "cfgdiff.txt"
    Path(out).write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n合计需解释项：%d\n明细 -> %s" % (bad, out))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
