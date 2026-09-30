#!/usr/bin/env python3
"""dbg_session.py - 打印视图状态机每一步的决策，定位嵌套视图问题。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as S  # noqa: E402


class FakeConsole:
    def __init__(self) -> None:
        self.text = "<SW1>"

    def command(self, line: str, timeout: float = 20) -> str:
        body = self._default(line)
        self.text += "\n" + body
        return body

    def send_raw(self, data: bytes) -> None:
        if data == b"\r":
            self.text += "\n<SW1>"

    def _default(self, line: str) -> str:
        if line == "system-view":
            return "System View: return to User View with Ctrl+Z.\n[SW1]"
        if line == "quit":
            return "[SW1]"
        if line.startswith("interface "):
            return "[SW1-%s]" % line.split(" ", 1)[1].replace("/", "")
        if line == "ospf 1":
            return "[SW1-ospf-1]"
        if line == "area 1":
            return "[SW1-ospf-1-area-0.0.0.1]"
        return ""


c = FakeConsole()
s = S._Session(c)
s.host, s.sub, s.auto_confirm = "SW1", 2, True
print("init: host=%r sub=%s prompt=%r" % (s.host, s.sub, S._prompt_name(c)))

_orig_to_system = S._Session.to_system


def traced_to_system(self, timeout: float = 30.0):
    before = (S._prompt_name(self.con), self.sub)
    _orig_to_system(self, timeout)
    print("    [to_system] before=%r after=%r" % (before, (S._prompt_name(self.con), self.sub)))


S._Session.to_system = traced_to_system

for cmd in ("ospf 1", "area 1", "network 10.0.0.0 0.0.0.255", "quit", "quit"):
    entry = S._is_entry_command(cmd)
    print("\n>>> %s   (is_entry=%s)" % (cmd, entry))
    out, err = s.run(cmd, 5)
    print("    out=%r err=%r" % (out[-60:], err))
    print("    after: sub=%s prompt=%r text_tail=%r" %
          (s.sub, S._prompt_name(c), c.text[-70:]))
