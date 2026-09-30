"""diag_console.py - 对 HCL 设备控制台做原始字节诊断。

连上后"被对端关闭"时，需要分清是 HCL 的 telnet 服务端不肯说话、
还是握手中途断掉、还是要求客户端先开口。这个脚本用几种策略分别试一遍，
把收到的原始字节按 hex + 文本两种形式打出来。

用法::

    python diag_console.py                 # 默认打 30001
    python diag_console.py 30001 30002
"""

from __future__ import annotations

import socket
import sys
import time

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240

IAC_NAME = {DO: "DO", DONT: "DONT", WILL: "WILL", WONT: "WONT", SB: "SB", SE: "SE"}


def describe(data: bytes) -> str:
    """把字节流里的 telnet 控制序列翻译成人话。"""
    out, i, n = [], 0, len(data)
    while i < n:
        b = data[i]
        if b != IAC:
            out.append(repr(bytes([b]))[2:-1])
            i += 1
            continue
        if i + 1 >= n:
            out.append("<IAC(截断)>")
            break
        cmd = data[i + 1]
        if cmd == IAC:
            out.append("<0xFF>")
            i += 2
        elif cmd in (DO, DONT, WILL, WONT):
            opt = data[i + 2] if i + 2 < n else -1
            out.append(f"<{IAC_NAME.get(cmd, cmd)} {opt}>")
            i += 3
        elif cmd == SB:
            end = data.find(bytes((IAC, SE)), i + 2)
            if end < 0:
                out.append("<SB...截断>")
                break
            out.append(f"<SB {data[i+2:end].hex()}>")
            i = end + 2
        else:
            out.append(f"<IAC {cmd}>")
            i += 2
    return "".join(out)


def attempt(port: int, label: str, send_first: bytes | None, read_secs: float = 6.0) -> None:
    print(f"\n--- {label}  (port {port}) ---")
    s = socket.socket()
    s.settimeout(2.0)
    try:
        s.connect(("127.0.0.1", port))
    except OSError as exc:
        print(f"  connect 失败: {exc}")
        s.close()
        return

    if send_first is not None:
        try:
            s.sendall(send_first)
            print(f"  先发了 {len(send_first)} 字节: {send_first!r}")
        except OSError as exc:
            print(f"  发送失败: {exc}")

    s.settimeout(0.5)
    got = b""
    deadline = time.monotonic() + read_secs
    closed = False
    while time.monotonic() < deadline:
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            continue
        except OSError as exc:
            print(f"  recv 异常: {exc}")
            break
        if not chunk:
            closed = True
            break
        got += chunk
    s.close()

    print(f"  收到 {len(got)} 字节；对端主动关闭={closed}")
    if got:
        print(f"  hex : {got[:120].hex(' ')}")
        print(f"  翻译: {describe(got[:120])}")
        print(f"  文本: {got[:120]!r}")


def attempt_seq(port: int, label: str, script: list[tuple[float, bytes]],
                read_secs: float = 12.0) -> None:
    """按 (延迟秒, 要发的字节) 依次动作，全程读回显。用于复现驱动行为。"""
    print(f"\n--- {label}  (port {port}) ---")
    s = socket.socket()
    s.settimeout(2.0)
    try:
        s.connect(("127.0.0.1", port))
    except OSError as exc:
        print(f"  connect 失败: {exc}")
        s.close()
        return
    s.settimeout(0.5)

    got = b""
    closed = False
    t0 = time.monotonic()
    pending = list(script)
    deadline = t0 + read_secs
    while time.monotonic() < deadline:
        elapsed = time.monotonic() - t0
        while pending and pending[0][0] <= elapsed:
            _, payload = pending.pop(0)
            try:
                s.sendall(payload)
                print(f"  [{elapsed:4.1f}s] 发送 {payload!r}")
            except OSError as exc:
                print(f"  [{elapsed:4.1f}s] 发送失败: {exc}")
        try:
            chunk = s.recv(4096)
        except socket.timeout:
            continue
        except OSError as exc:
            print(f"  recv 异常: {exc}")
            break
        if not chunk:
            closed = True
            print(f"  [{elapsed:4.1f}s] 对端关闭了连接")
            break
        got += chunk
    s.close()

    print(f"  共收到 {len(got)} 字节")
    if got:
        print(f"  翻译: {describe(got[-400:])}")
        print(f"  文本: {got[-400:]!r}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only = None
    for a in sys.argv[1:]:
        if a.startswith("--only"):
            only = a.split("=", 1)[1].upper() if "=" in a else None
    ports = [int(a) for a in args] or [30001]

    # 驱动当前发的协商应答：对每个 WILL 回 DONT，对 DO 回 WONT
    driver_replies = bytes((IAC, DONT, 1, IAC, DONT, 3, IAC, DONT, 0, IAC, WONT, 0))

    for port in ports:
        if only in (None, "A"):
            attempt(port, "策略A: 连上什么都不发，纯读", None)
            time.sleep(0.5)
        if only in (None, "B"):
            attempt(port, "策略B: 连上先发 CRLF", b"\r\n")
            time.sleep(0.5)
        if only in (None, "C"):
            attempt(port, "策略C: 连上先发 telnet 协商 IAC DO ECHO", bytes((IAC, DO, 1)))
            time.sleep(0.5)
        if only in (None, "D"):
            attempt_seq(port, "策略D: 复现驱动——回 DONT/WONT 拒绝协商", [(0.5, driver_replies)], 8.0)
            time.sleep(0.5)
        if only in (None, "E"):
            attempt_seq(port, "策略E: 读 3s 后发 Ctrl+C 打断 auto-config", [(3.0, b"\x03")], 20.0)
            time.sleep(0.5)
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
