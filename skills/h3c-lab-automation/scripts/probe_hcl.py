"""probe_hcl.py - 连真实 HCL 设备控制台做最小验证。

HCL 每启动一台设备，就在本机起一个 telnet 控制台：127.0.0.1:30001、30002…
（依据 D:\\HCL\\Log\\HCLLog 里的 ``create_telnet_server ... telnet_port:3000N``）。

重要：扫描端口时**不能用一次性的裸连接去探**。HCL 的设备控制台同一时刻只服务
一个客户端，裸连一下就断会把它搅乱，随后真正的连接会被对端关闭。所以这里
直接拿真连接去试，连上就是它。

用法::

    python probe_hcl.py                        # 扫一遍，没设备就退出
    python probe_hcl.py --wait 900             # 最多等 900 秒，设备一起来就连上
    python probe_hcl.py --user admin --password admin     # 设备有登录认证时
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hcldrv import Console, ConsoleError, ConsoleTimeout, _PROMPT   # noqa: E402

_LOGIN = re.compile(r"(?i)(username\s*:|login\s*:|login authentication)")
_PASSWORD = re.compile(r"(?i)password\s*:")

READ_ONLY_CMDS = [
    "display version",
    "display clock",
    "display ip interface brief",
]


def parse_ports(spec: str) -> list[int]:
    ports: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            ports += list(range(int(lo), int(hi) + 1))
        elif part:
            ports.append(int(part))
    return ports


def try_connect(host: str, port: int, connect_timeout: float = 0.6,
                **kwargs) -> Console | None:
    """真连接去试某个端口；连不上返回 None，连上就把连接交出去。"""
    con = Console(host=host, port=port, **kwargs)
    try:
        con.connect(timeout=connect_timeout)
    except OSError:
        con.close()
        return None
    return con


def wait_for_device(ports: list[int], host: str, wait: float,
                    **kwargs) -> Console | None:
    deadline = time.monotonic() + wait
    announced = False
    while True:
        for port in ports:
            con = try_connect(host, port, **kwargs)
            if con is not None:
                return con
        if time.monotonic() >= deadline:
            return None
        if not announced:
            print(f"等待 HCL 设备控制台出现（最多 {int(wait)}s，每 3s 扫一次 "
                  f"{ports[0]}-{ports[-1]}）...", flush=True)
            announced = True
        time.sleep(3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ports", default="30001-30010")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--wait", type=float, default=0.0, help="等待设备出现的秒数")
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--prompt-timeout", type=float, default=180.0,
                    help="设备启动/回显慢时给足时间")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    ports = parse_ports(args.ports)
    con = wait_for_device(ports, args.host, args.wait,
                          timeout=10.0, debug=args.debug)
    if con is None:
        print(f"没有任何 HCL 设备控制台在监听（扫过 {ports[0]}-{ports[-1]}）。")
        print("HCL 没运行，或者拓扑还没点启动。")
        return 2

    print(f"连上设备控制台：{args.host}:{con.port}")
    print("=" * 68)
    try:
        # 有的设备（如 F1060 防火墙）控制台要登录
        try:
            needs_login = con.read_until([_LOGIN], timeout=5.0) is not None
        except ConsoleTimeout:
            needs_login = False
        if needs_login:
            if not args.user:
                print("!! 设备要求登录，但没给 --user/--password。已收到：")
                print(con.text[-500:])
                return 3
            con.send_raw(args.user.encode() + b"\r")
            con.read_until([_PASSWORD], timeout=30)
            con.send_raw((args.password or "").encode() + b"\r")

        # prep() 会处理 auto-config（Ctrl+C 打断）和 "Press ENTER to get started."
        con.prep(timeout=args.prompt_timeout)
        print(f"--- 控制台原始回显（尾 800 字符，已剥离 telnet 控制序列）---")
        print(con.text[-800:])
        print()

        for cmd in READ_ONLY_CMDS:
            print(f"--- $ {cmd} ---")
            try:
                print(con.command(cmd, timeout=60))
            except ConsoleTimeout as exc:
                print(f"[超时] 已收到：{exc.transcript[-300:]!r}")
            print()

        print("=" * 68)
        print(f"真实设备验证成功：{args.host}:{con.port}")
        print(f"  打断 auto-config {con.auto_config_breaks} 次；"
              f"自动翻页 {con.answered_more} 次")
        return 0
    except ConsoleError as exc:
        print(f"连接失败：{type(exc).__name__}: {exc}")
        return 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
