#!/usr/bin/env python3
"""test_session.py - 视图状态机 / 归位 / 受控确认 / 严格报错判定的离线自测。

不需要 HCL：用假 Console 复现 Comware 的嵌套视图与确认提示行为。

★ 重要教训（2026-09-30 真机踩到）：第一版的假 Console 是**视图无关**的 ——
  任何视图都接受任何命令。于是它的 28 项断言全绿，而真机上 `hcl_apply_plan`
  在**用户视图**下把整批配置命令发出去、全部 `% Unrecognized command`：
  `to_system()` 只看提示符**名字**（`<H3C>` 与 `[H3C]` 名字一模一样），
  "第一次看到提示符"就认为已在系统视图，`system-view` 一次都没发。
  现在假 Console 会**按视图拒绝命令**，并模拟用户视图下 `quit` 会登出。

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
    """假控制台：模拟提示符、嵌套视图、**错视图拒绝**、用户视图 quit 登出。

    视图用栈表示：空栈 = 用户视图 `<HOST>`；`['']` = 系统视图 `[HOST]`；
    再往下是 `[HOST-ospf-1]`、`[HOST-ospf-1-area-0.0.0.1]` 这种。
    """

    #: 用户视图里也合法的命令前缀（其余配置命令在用户视图会被拒）
    USER_VIEW_OK = ("display", "system-view", "quit", "return", "undo terminal monitor",
                    "screen-length", "ping", "tracert", "save", "reset")

    def __init__(self, host: str = "SW1") -> None:
        self.hostname = host
        self.stack: list[str] = []                 # 空 = 用户视图
        self.text = self._prompt()
        self.sent: list[str] = []
        self.unrecognized: list[str] = []
        self.logged_out = False

    # ---- 视图与提示符 ----
    def _prompt(self) -> str:
        if not self.stack:
            return "<%s>" % self.hostname
        return "[%s%s]" % (self.hostname, "".join(self.stack))

    def _say_prompt(self, body: str) -> str:
        out = (body + "\n" if body else "") + self._prompt()
        self.text += "\n" + out
        return out

    # ---- Console 接口 ----
    def command(self, line: str, timeout: float = 20) -> str:
        self.sent.append(line)
        # 注意要拿**整行**去匹配前缀表（表里有 "undo terminal monitor" 这种多词前缀；
        # 只取首词会让它永远匹配不上，反而把合法命令误判成错视图）。
        if not self.stack and not line.strip().startswith(self.USER_VIEW_OK):
            self.unrecognized.append(line)
            return self._say_prompt(" % Unrecognized command found at '^' position.")
        if line == "quit" and not self.stack:
            self.logged_out = True
            self.text += "\nPress ENTER to get started."
            return "Press ENTER to get started."
        return self._say_prompt(self._default(line))

    def send_raw(self, data: bytes) -> None:
        self.sent.append(data.decode("ascii", "replace"))
        if data == b"\r":
            if self.logged_out:
                self.logged_out = False
                self.stack = []
            self.text += "\n" + self._prompt()

    def read_until(self, patterns, timeout: float = 20, start=None) -> str:  # noqa: ANN001
        return self.text

    def _default(self, line: str) -> str:
        if line == "system-view":
            self.stack = [""]
            return "System View: return to User View with Ctrl+Z."
        if line == "quit":
            if self.stack:
                self.stack.pop()
            return ""
        if line.startswith("interface "):
            if not self.stack:
                return " % Unrecognized command found at '^' position."
            self.stack.append("-" + line.split(" ", 1)[1].replace("/", ""))
            return ""
        if line == "ospf 1":
            self.stack.append("-ospf-1")
            return ""
        if line == "area 1":
            self.stack.append("-area-0.0.0.1")
            return ""
        if line.startswith("vlan "):
            self.stack.append("-vlan" + line.split(" ", 1)[1])
            return ""
        return ""


class ConfirmConsole(FakeConsole):
    """模拟带 [Y/N] 的交互确认（真实设备上这类确认不带 %）。"""

    def __init__(self, host: str = "SW1") -> None:
        super().__init__(host)
        self.confirmed = False

    def _default(self, line: str) -> str:
        if line == "port link-mode route":
            return "The configuration of the interface will be restored to the default. Continue? [Y/N]:"
        if line == "Y":
            self.confirmed = True
            return ""
        return super()._default(line)


def main() -> int:
    # ------------------------------------------------------------------
    # 0) 归位：这才是真机上出错的那条路径
    # ------------------------------------------------------------------
    for host in ("SW1", "H3C"):
        c = FakeConsole(host)
        s = S._Session(c)
        s.auto_confirm = True
        s.prepare(5)
        check("[%s] prepare() 真的发了 system-view 并进入系统视图" % host,
              "system-view" in c.sent and s.sub == 2)
        check("[%s] prepare() 没有把控制台 quit 到登出" % host, not c.logged_out)
        _out, err = s.run("interface GigabitEthernet 1/0/1", 5)
        check("[%s] prepare() 后能直接下配置命令且零 %% Unrecognized" % host,
              err is None and not c.unrecognized)

    # 出厂默认主机名 H3C：用户视图 <H3C> 与系统视图 [H3C] 名字完全一样，专测这一点
    c0 = FakeConsole("H3C")
    s0 = S._Session(c0)
    s0.auto_confirm = True
    s0.run("vlan 10", 5)
    check("H3C：用户视图下下普通配置命令会自动先归位（不再直接发）",
          "system-view" in c0.sent and not c0.unrecognized)

    # 从二级子视图归位
    c1 = FakeConsole("H3C")
    s1 = S._Session(c1)
    s1.auto_confirm = True
    s1.prepare(5)
    s1.run("ospf 1", 5)
    s1.run("area 1", 5)
    check("H3C：二级子视图 sub=4", s1.sub == 4)
    s1.to_system(5)
    check("H3C：to_system 能退回系统视图（sub=2）", s1.sub == 2 and not c1.unrecognized)
    s1.to_user(5)
    check("H3C：to_user 不再连发 quit 把控制台登出", not c1.logged_out)

    # ------------------------------------------------------------------
    # 1) interface 子视图
    # ------------------------------------------------------------------
    c = FakeConsole()
    s = S._Session(c)
    s.host, s.sub, s.auto_confirm = "SW1", 2, True
    s.run("interface GigabitEthernet 1/0/1", 5)
    check("interface 打开子视图（sub 2→3）", s.sub == 3)
    s.run("port link-type trunk", 5)
    check("子视图内普通命令不退出视图", s.sub == 3)

    # ------------------------------------------------------------------
    # 2) 嵌套 ospf -> area -> network
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 3) 受控确认
    # ------------------------------------------------------------------
    cc = ConfirmConsole()
    s3 = S._Session(cc)
    s3.auto_confirm = True
    s3.prepare(5)
    _out, err = s3.run("port link-mode route", 5)
    check("白名单命令自动应答 Y 且无报错", cc.confirmed and err is None)

    c4 = ConfirmConsole()
    s4 = S._Session(c4)
    s4.auto_confirm = False
    s4.prepare(5)
    _out, err = s4.run("port link-mode route", 5)
    check("未开自动应答时报出未生效", (not c4.confirmed) and err is not None)

    # ------------------------------------------------------------------
    # 4) 破坏性命令拒绝
    # ------------------------------------------------------------------
    s5 = S._Session(FakeConsole())
    s5.host, s5.sub = "SW1", 2
    for bad in ("reboot force", "reset saved-configuration", "format flash:"):
        _out, err = s5.run(bad, 5)
        check("拒绝破坏性命令：%s" % bad, err is not None and "破坏性" in err)

    # ------------------------------------------------------------------
    # 5) 严格 vs 宽松报错判定
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 6) 进入块前缀表
    # ------------------------------------------------------------------
    for bad in ("track 1 nqa entry a b reaction 1",
                "nqa schedule a b start-time now lifetime forever",
                "ospf timer hello 3", "port link-aggregation group 1", "display irf"):
        check("非进入块：%s" % bad[:30], not S._is_entry_command(bad))
    for good in ("interface GigabitEthernet 1/0/1", "ospf 1", "area 0.0.0.1", "vlan 10",
                 "acl advanced 3001", "dhcp server ip-pool p1", "route-policy RP permit node 10"):
        check("是进入块：%s" % good[:30], S._is_entry_command(good))

    # ------------------------------------------------------------------
    # 7) 提示符解析：视图必须看括号，不能看名字
    # ------------------------------------------------------------------
    class TextConsole:
        def __init__(self, text: str) -> None:
            self.text = text

    check("_prompt_name 返回裸名字", S._prompt_name(TextConsole("<H3C>")) == "H3C")
    check("_prompt_info 认出用户视图 <H3C>", S._prompt_info(TextConsole("<H3C>")) == ("H3C", "user"))
    check("_prompt_info 认出系统视图 [H3C]（名字相同但视图不同）",
          S._prompt_info(TextConsole("[H3C]")) == ("H3C", "system"))
    check("_prompt_info 认出子视图 [H3C-ospf-1]",
          S._prompt_info(TextConsole("[H3C-ospf-1]")) == ("H3C-ospf-1", "system"))
    check("_prompt_info 在登录横幅上返回 (None, None)",
          S._prompt_info(TextConsole("Press ENTER to get started.")) == (None, None))
    check("_prompt_info 不把 [Y/N] 当成提示符",
          S._prompt_info(TextConsole("Continue? [Y/N]:")) == (None, None))

    passed = sum(1 for x in RESULTS if x)
    print("\n合计：%d 通过 / %d 失败" % (passed, len(RESULTS) - passed))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
