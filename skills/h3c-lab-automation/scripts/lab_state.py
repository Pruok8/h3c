#!/usr/bin/env python3
"""lab_state.py - 实验状态落盘（`state.json`）：端口映射、地址规划、各项验证的最近结果。

为什么需要
----------
铁律 1 要求"状态落盘、新一轮先读它"。没有这个脚本，那条规则就落不了地：
每次都要重新 `hcl_ports.py` + 重新推导地址表，既慢又烧 token。

state.json 的推荐结构（`init` 会建好骨架）::

    {
      "lab": "lab4_ts",
      "net": "D:\\NET\\ie\\e\\kongpei\\lab4_ts.net",
      "devices": { "SW1": {"type": "S6850", "port": 30008}, ... },
      "address_plan": { "VLAN30": "10.10.0.0/29 SW1.1 SW2.2 VIP.3 PE1.4", ... },
      "verify": { "MLAG-SW1": "PASS 2026-09-17", ... },
      "notes": ["..."]
    }

用法::

    python lab_state.py init --lab lab4_ts --net "<lab>.net"
    python lab_state.py set address_plan '{"VLAN30":"10.10.0.0/29 ..."}'   # 值是 JSON 就按 JSON 存
    python lab_state.py set lab lab4_ts
    python lab_state.py get verify
    python lab_state.py keys
    python lab_state.py show
    python lab_state.py del notes
    python lab_state.py merge other.json        # 浅合并（后写覆盖）

默认文件 `state.json`（可用 `--file` 指定）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKELETON = {
    "lab": "",
    "net": "",
    "devices": {},
    "address_plan": {},
    "verify": {},
    "notes": ["状态文件：端口映射/地址规划/验证结果。每轮先读它，别重新推导。"],
}


def load(path: Path) -> dict:
    if not path.exists():
        return dict(SKELETON)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(SKELETON)
    except Exception as exc:  # noqa: BLE001
        print("state.json 解析失败（按空状态处理）：%s" % exc)
        return dict(SKELETON)


def save(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="实验状态落盘")
    ap.add_argument("action", choices=["init", "set", "get", "del", "keys", "show", "merge"])
    ap.add_argument("key", nargs="?", help="set/get/del 的键")
    ap.add_argument("value", nargs="?", help="set 的值（能当 JSON 解析就按 JSON 存）")
    ap.add_argument("--file", default="state.json")
    ap.add_argument("--lab", help="init 时写入 lab 名")
    ap.add_argument("--net", help="init 时写入 .net 路径")
    ap.add_argument("--devices", help="init 时从 net2map 的 json 读设备表")
    args = ap.parse_args()

    path = Path(args.file)
    data = load(path)

    if args.action == "init":
        if args.lab:
            data["lab"] = args.lab
        if args.net:
            data["net"] = args.net
        if args.devices:
            try:
                dev = json.loads(Path(args.devices).read_text(encoding="utf-8")).get("devices", [])
                data["devices"] = {d["name"]: {"type": d.get("type"), "port": d.get("console_port")}
                                   for d in dev}
            except Exception as exc:  # noqa: BLE001
                print("读取 --devices 失败：%s" % exc)
                return 2
        for k, v in SKELETON.items():
            data.setdefault(k, v)
        save(path, data)
        print("已初始化 %s（lab=%s，设备 %d 台）" % (path, data.get("lab") or "-", len(data["devices"])))
        return 0

    if args.action == "keys":
        print(" ".join(sorted(data)))
        return 0

    if args.action == "show":
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    if args.action == "merge":
        if not args.key:
            print("merge 需要给出要合并的 json 文件")
            return 2
        other = json.loads(Path(args.key).read_text(encoding="utf-8"))
        data.update(other)
        save(path, data)
        print("已合并 %s（现有键：%s）" % (args.key, " ".join(sorted(data))))
        return 0

    if args.action == "get":
        if not args.key:
            print("get 需要键名")
            return 2
        if args.key not in data:
            print("没有这个键：%s（现有：%s）" % (args.key, " ".join(sorted(data))))
            return 1
        val = data[args.key]
        print(json.dumps(val, ensure_ascii=False, indent=2) if not isinstance(val, str) else val)
        return 0

    if args.action == "del":
        if args.key not in data:
            print("没有这个键：%s" % args.key)
            return 1
        data.pop(args.key)
        save(path, data)
        print("已删除 %s" % args.key)
        return 0

    # set
    if not args.key:
        print("set 需要 键 值")
        return 2
    raw = args.value if args.value is not None else ""
    try:
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        parsed = raw
    data[args.key] = parsed
    save(path, data)
    print("已写入 %s（%s）" % (args.key, type(parsed).__name__))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
