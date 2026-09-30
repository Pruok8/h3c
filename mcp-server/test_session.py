#!/usr/bin/env python3
"""test_session.py - 视图状态机 / 受控确认 / 严格报错判定的离线自测。

不需要 HCL：用假 Console 复现 Comware 的嵌套视图与确认提示行为。
用法：python test_session.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as S  # noqa: E402

RESULTS: list[bool] = []


def check(name: str, cond: bool) -> None:
    RESULTS.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + name)


class FakeConsole:
    """极简假控制台：模拟 Comware 的提示符与嵌套视图。"""

    def __init__(self) -> None:
        self.text = "<SW1>"
        self.sent: list[str] = []

    def command(self, line: str, timeout: float = 20) -> str:
        self.sent.append(line)
        body = self._default(line)
        self.text += "\n" + body
        return body

    def send_raw(self, data: bytes) -> None:
        self.sent.append(data.decode("ascii", "replace"))
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


class ConfirmConsole(FakeConsole):
    """模拟带 [Y/N] 的交互确认（真实设备上这类确认不带 %）。"""

    def __init__(self) -> None:
        super().__init__()
        self.confirmed = False

    def _default(self, line: str) -> str:
        if line == "port link-mode route":
            return ("The configuration of the interface will be restored to the default. "
                    "Continue? [Y/N]:")
        if line == "Y":
            self.confirmed = True
            return "[SW1-GigabitEthernet10/1]"
        return super()._default(line)


def main() -> int:
    # 1) interface 子视图
    c = FakeConsole()
    s = S._Session(c)
    s.host, s.sub, s.auto_confirm = "SW1", 2, True
    s.run("interface GigabitEthernet 1/0/1", 5)
    check("interface 打开子视图（sub 2→3）", s.sub == 3)
    s.run("port link-type trunk", 5)
    check("子视图内普通命令不退出视图", s.sub == 3)

    # 2) 嵌套 ospf -> area -> network
    s2 = S._Session(FakeConsole())
    s2.host, s2.sub, s2.auto_confirm = "SW1", 2, True
    s2.run("ospf 1", 5)
    a = s2.sub
    s2.run("area 1", 5)
    b = s2.sub
    s2.run("network 10.0.0.0 0.0.0.255", 5)
    d = s2.sub
    check("ospf/area 依次入子视图（sub 2→3→4）", a == 3 and b == 4)
    check("area 内普通命令仍在 area（sub=4）", d == 4)
    s2.run("quit", 5)
    check("quit 只退一级（sub=3）", s2.sub == 3)
    s2.run("quit", 5)
    check("再 quit 回到系统视图（sub=2）", s2.sub == 2)

    # 3) 受控确认
    cc = ConfirmConsole()
    s3 = S._Session(cc)
    s3.host, s3.sub, s3.auto_confirm = "SW1", 2, True
    _out, err = s3.run("port link-mode route", 5)
    check("白名单命令自动应答 Y 且无报错", cc.confirmed and err is None)

    c4 = ConfirmConsole()
    s4 = S._Session(c4)
    s4.host, s4.sub, s4.auto_confirm = "SW1", 2, False
    _out, err = s4.run("port link-mode route", 5)
    check("未开自动应答时报出未生效", (not c4.confirmed) and err is not None)

    # 4) 破坏性命令拒绝
    s5 = S._Session(FakeConsole())
    s5.host, s5.sub = "SW1", 2
    for bad in ("reboot force", "reset saved-configuration", "format flash:"):
        _out, err = s5.run(bad, 5)
        check("拒绝破坏性命令：%s" % bad, err is not None and "破坏性" in err)

    # 5) 严格 vs 宽松报错判定
    check("严格判定认得 This subnet overlaps（不带 %）",
          S._error_line("This subnet overlaps with another interface!", strict=True) is not None)
    check("严格判定不误判正常回显",
          S._error_line("Aggregation Mode: Dynamic\nPort Status: S -- Selected",
                        strict=True) is None)
    check("严格判定认得 % Unrecognized",
          S._error_line(" % Unrecognized command found at '^' position.", strict=True) is not None)
    check("宽松判定仍列出 does not exist（给人看）",
          S._error_line("The link aggregation group does not exist.") is not None)
    check("严格判定不把 does not exist 当状态错误",
          S._error_line("The link aggregation group does not exist.", strict=True) is None)

    # 6) 进入块前缀表
    for bad in ("track 1 nqa entry a b reaction 1",
                "nqa schedule a b start-time now lifetime forever",
                "ospf timer hello 3", "port link-aggregation group 1", "display irf"):
        check("非进入块：%s" % bad[:30], not S._is_entry_command(bad))
    for good in ("interface GigabitEthernet 1/0/1", "ospf 1", "area 0.0.0.1", "vlan 10",
                 "acl advanced 3001", "dhcp server ip-pool p1", "route-policy RP permit node 10"):
        check("是进入块：%s" % good[:30], S._is_entry_command(good))

    passed = sum(1 for x in RESULTS if x)
    print("\n合计：%d 通过 / %d 失败" % (passed, len(RESULTS) - passed))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
