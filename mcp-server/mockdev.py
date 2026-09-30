#!/usr/bin/env python3
"""mockdev.py - 假 HCL 设备控制台（TCP，只用标准库），给 selftest 的 `--mock` 模式用。

它不是 `hcldrv.py` 的一部分，也**不是给真机用的**：只是在 HCL 没启动时，
让 h3c-lab-mcp 的工具逻辑（提示符识别、命令回显、`display` 输出解析、
报错检出、接口 brief 解析）能被真实验证一遍。真机行为仍以 HCL 实测为准。

用法::

    from mockdev import MockDevice, MockFleet
    fleet = MockFleet([("SW1", 30008, "S6850"), ("SW2", 30009, "S6850")])
    fleet.start()

每个假设备只认这几个命令（够覆盖工具的关键路径）：

* 任意行 -> 回显该行（如果没有专门输出）
* `screen-length disable` / `undo terminal monitor` / `system-view` / `quit` / `return`
* `display version` / `display clock` / `display device` / `display interface brief`
* `display vlan <n>`、`ping ...`
* 以 `bad-command` 或 `_err` 结尾的命令 -> 回 `% Unrecognized command found at '^' position.`
"""

from __future__ import annotations

import socket
import threading
import time

BRIEF_HEAD = (
    "Brief information on interfaces in route mode:\n"
    "Interface            Link Protocol Primary IP      Description\n"
)

BRIEF_UP = (
    "Brief information on interfaces in bridge mode:\n"
    "Interface            Link Protocol Primary IP      Description\n"
    "GE1/0/20             UP   1G      F(a)   Access 10\n"
    "GE1/0/21             UP   1G      F(a)   Access 10\n"
    "GE1/0/22             DOWN auto    A      Access 10\n"
    "GE1/0/23             ADM  auto    A      Access 10\n"
)


class MockDevice:
    """一个假的 Comware 控制台。"""

    def __init__(self, name: str = "SW1", port: int = 39101, model: str = "S6850",
                 brief: str | None = None, host: str = "127.0.0.1") -> None:
        self.name, self.port, self.model, self.host = name, port, model, host
        self.brief = brief if brief is not None else BRIEF_UP
        self.view = "user"          # user | system
        self.lock = threading.Lock()
        self.connections = 0
        self.commands: list[str] = []
        self._srv: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---------------- 生命周期 ----------------
    def start(self) -> "MockDevice":
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((self.host, self.port))
        self._srv.listen(8)
        self._srv.settimeout(0.3)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def __enter__(self) -> "MockDevice":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------------- 服务端 ----------------
    def _serve(self) -> None:
        assert self._srv is not None
        while not self._stop.is_set():
            try:
                conn, _addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self.lock:
                self.connections += 1
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn: socket.socket) -> None:
        conn.settimeout(30.0)
        try:
            conn.sendall(self._banner())
            buf = b""
            while not self._stop.is_set():
                try:
                    data = conn.recv(4096)
                except (socket.timeout, OSError):
                    break
                if not data:
                    break
                # 真 HCL 控制台接受 CR / LF / CRLF 三种行结束；hcldrv 发的是 CR
                buf += data
                while True:
                    idx = min([i for i in (buf.find(b"\r"), buf.find(b"\n")) if i >= 0]
                              or [-1])
                    if idx < 0:
                        break
                    raw, buf = buf[:idx], buf[idx + 1:]
                    if buf.startswith(b"\n"):        # 吃掉 CRLF 的第二个字节
                        buf = buf[1:]
                    line = raw.decode("utf-8", "replace").strip()
                    self._handle(conn, line)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _banner(self) -> bytes:
        return ("\r\nPress ENTER to get started.\r\n" + self._prompt()).encode("utf-8")

    def _prompt(self) -> str:
        return "<%s>" % self.name if self.view == "user" else "[%s]" % self.name

    def _handle(self, conn: socket.socket, line: str) -> None:
        with self.lock:
            self.commands.append(line)
        low = line.lower()
        out = ""
        if low in ("", "\x03"):
            out = ""
        elif low == "screen-length disable" or low == "undo terminal monitor":
            out = ""
        elif line and not line.startswith("display") and not line.startswith("ping") \
                and low not in ("system-view", "quit", "return", "y", "n") \
                and not line.startswith("sysname") and not line.startswith("vlan") \
                and not line.startswith("interface") and not line.startswith("description"):
            out = ""
        if low == "system-view":
            self.view = "system"
        elif line in ("quit", "return"):
            self.view = "user"
        elif line.startswith("display version"):
            out = (
                "H3C Comware Software, Version 7.1.070, Release 6555P01\r\n"
                "Copyright (c) 2004-2024 New H3C Technologies Co., Ltd.\r\n"
                "H3C %s uptime is 0 weeks, 3 days, 4 hours, 12 minutes\r\n"
                "Last reboot reason : User reboot\r\n" % self.model)
        elif line.startswith("display clock"):
            out = ("09:41:23 UTC Mon 09/22/2026\r\n"
                   "Time Zone : UTC add 08:00:00\r\n")
        elif line.startswith("display device"):
            out = ("Slot 1 CPU information:\r\n"
                   "Slot  Type                   Status\r\n"
                   "1     S6850 48XG+8QSFP+      Normal\r\n")
        elif line.startswith("display interface brief"):
            out = self.brief
        elif line.startswith("display vlan "):
            vlan = line.split()[-1]
            out = ("Total VLANs: 2\r\n"
                   "The VLANs include:\r\n"
                   "1(default), %s\r\n" % vlan)
        elif line.startswith("display m-lag summary"):
            out = ("Flags: A--Suspended  B--No peer M-LAG interface configured\r\n"
                   "       C--Isolated because of consistency check failure\r\n"
                   "M-LAG group   State    Local  Peer\r\n"
                   "BAGG2         UP       2      2\r\n")
        elif line.startswith("ping"):
            out = ("Ping 10.0.0.2 (10.0.0.2): 56 data bytes, press CTRL_C to break\r\n"
                   "56 bytes from 10.0.0.2: icmp_seq=0 ttl=255 time=1.000 ms\r\n"
                   "56 bytes from 10.0.0.2: icmp_seq=1 ttl=255 time=1.000 ms\r\n"
                   "--- Ping statistics for 10.0.0.2 ---\r\n"
                   "2 packet(s) transmitted, 2 packet(s) received, 0.0% packet loss\r\n")
        if low.endswith("bad-command") or low.endswith("_err"):
            out = "% Unrecognized command found at '^' position.\r\n"
        if line.startswith("display") and not out:
            out = "% Unrecognized command found at '^' position.\r\n"
        payload = "%s\r\n%s" % (line, out) if out else line
        time.sleep(0.01)
        try:
            conn.sendall(("%s\r\n%s" % (payload, self._prompt())).encode("utf-8"))
        except OSError:
            pass


class MockFleet:
    """一组假设备，端口用 39xxx，避免和真 HCL 的 3000x 撞。"""

    def __init__(self, spec: list[tuple[str, int, str]] | None = None) -> None:
        self.devices = [MockDevice(name, port, model) for name, port, model in
                        (spec or [("SW1", 39101, "S6850"), ("SW2", 39102, "S6850"),
                                  ("PE1", 39103, "MSR36-20")])]

    def start(self) -> "MockFleet":
        for d in self.devices:
            d.start()
        return self

    def stop(self) -> None:
        for d in self.devices:
            d.stop()

    def __enter__(self) -> "MockFleet":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def ports(self) -> list[int]:
        return [d.port for d in self.devices]

    def commands_of(self, name: str) -> list[str]:
        for d in self.devices:
            if d.name == name:
                with d.lock:
                    return list(d.commands)
        return []
