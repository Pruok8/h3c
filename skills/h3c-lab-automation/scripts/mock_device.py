"""mock_device.py - 模拟一台 Comware 设备（telnet 服务），用于在没有 HCL 的自测。

只实现自测需要的行为：
  * 连接后发 telnet 选项协商 (WILL ECHO / WILL SGA)，验证客户端会不会正确拒绝
  * 打 banner 和 ``<H3C>`` 提示符
  * 逐字符回显、按 \\r 执行命令
  * 超过 ``paging`` 行时输出 ``  ---- More ----``，收到空格才继续

用法::

    python mock_device.py            # 监听随机端口并打印端口号
    python mock_device.py 30001
"""

from __future__ import annotations

import socket
import sys
import threading

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240

VERSION = [
    "H3C Comware Software, Version 7.1.070, Release 6555P01",
    "Copyright (c) 2004-2023 New H3C Technologies Co., Ltd. All rights reserved.",
    "H3C S5820V2-54QS-GE uptime is 0 weeks, 0 days, 1 hour, 12 minutes",
    "Last reboot reason : User reboot",
    "",
    "Boot image: flash:/s5820v2_54qs-ge-cmw710-boot-r6555p01.bin",
    "System image: flash:/s5820v2_54qs-ge-cmw710-system-r6555p01.bin",
    "",
    "CPU type            : ARM Cortex-A9 1200MHz",
    "512M bytes DDR3 SDRAM Memory",
    "1024M bytes Flash Memory",
]


def _iface_config(n: int = 60) -> list[str]:
    lines = ["#", " sysname H3C", "#"]
    for i in range(1, n):
        lines += [
            f"interface GigabitEthernet1/0/{i}",
            " port link-mode bridge",
            f" description link-to-device-{i}",
            "#",
        ]
    lines += ["return"]
    return lines


class MockComware(threading.Thread):
    """单连接的假设备；start() 后监听，端口为 0 时由系统分配。"""

    daemon = True

    def __init__(self, host: str = "127.0.0.1", port: int = 0, paging: int = 24,
                 name: str = "H3C"):
        super().__init__(name="MockComware")
        self.name_ = name
        self.paging = paging
        self.prompt = f"<{name}>"
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((host, port))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.commands: list[str] = []

    # ---------------- 服务端 ----------------
    def run(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        with conn:
            try:
                self._serve_conn(conn)
            except OSError:
                pass
            except BaseException:                      # 测试替身必须把错误喊出来
                import traceback
                print("[mock] handler crashed:", file=sys.stderr, flush=True)
                traceback.print_exc()

    def stop(self) -> None:
        try:
            self._srv.close()
        except OSError:
            pass

    def _serve_conn(self, conn: socket.socket) -> None:
        """注意：不要把这个方法叫 _handle —— Thread 自己有个 _handle 属性，会被遮蔽。"""
        conn.settimeout(10)
        conn.sendall(bytes((IAC, WILL, 1)) + bytes((IAC, WILL, 3)))  # ECHO, SGA
        conn.sendall(b"\r\n")
        conn.sendall(self.prompt.encode())

        buf = b""
        pending: list[str] | None = None
        while True:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                return
            if not data:
                return
            i = 0
            while i < len(data):
                ch = data[i:i + 1]
                if ch == b"\xff":                     # 客户端的协商应答，整条跳过
                    i += 3
                    continue
                if pending is not None:               # 分页中，只等一个空格
                    i += 1
                    if ch != b" ":
                        continue
                    out, pending = self._page(pending)
                    conn.sendall(out.encode())
                    if pending is None:
                        conn.sendall(self.prompt.encode())
                    continue
                if ch == b"\r":
                    i += 1
                    conn.sendall(b"\r\n")
                    line = buf.decode("utf-8", "replace").strip()
                    buf = b""
                    if not line:
                        conn.sendall(self.prompt.encode())
                        continue
                    self.commands.append(line)
                    result = self._exec(line)
                    if isinstance(result, list):
                        out, pending = self._page(result)
                    else:
                        out, pending = result, None
                    conn.sendall(out.encode())
                    if pending is None:
                        conn.sendall(self.prompt.encode())
                    continue
                if ch in (b"\n", b"\x00"):
                    i += 1
                    continue
                buf += ch
                conn.sendall(ch)                      # 回显
                i += 1

    # ---------------- 假 CLI ----------------
    def _page(self, lines: list[str]):
        head, rest = lines[:self.paging], lines[self.paging:]
        out = "".join(l + "\r\n" for l in head)
        if rest:
            return out + "  ---- More ----", rest
        return out, None

    def _exec(self, line: str):
        low = line.lower()
        if low in ("screen-length disable", "undo terminal monitor", ""):
            return ""
        if low.startswith("display version"):
            return "\r\n".join(VERSION) + "\r\n"
        if low.startswith("display current-configuration") or low.startswith("display current"):
            return _iface_config()
        if low.startswith("display interface brief"):
            return ("Interface            Link Speed   Duplex Type PVID Description\r\n"
                    "GE1/0/1              UP   1G      full   access 1\r\n"
                    "GE1/0/2              DOWN auto    auto   access 1\r\n")
        if low.startswith("quit"):
            return ""
        return "% Unrecognized command found at '^' position.\r\n"


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    dev = MockComware(port=port)
    dev.start()
    print(f"mock Comware device on 127.0.0.1:{dev.port} (Ctrl+C to stop)", flush=True)
    try:
        while dev.is_alive():
            dev.join(1)
    except KeyboardInterrupt:
        pass
