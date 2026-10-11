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

import re
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
    #: `Y`/`N` 是答 [Y/N] 确认的**原始回答**，不是命令 —— 真机上驱动直接发一行文本。
    USER_VIEW_OK = ("display", "system-view", "quit", "return", "undo terminal monitor",
                    "screen-length", "ping", "tracert", "save", "reset", "reboot",
                    "schedule", "startup", "format", "dir", "copy", "tftp",
                    "Y", "y", "N", "n")

    def __init__(self, host: str = "SW1") -> None:
        self.hostname = host
        self.stack: list[str] = []                 # 空 = 用户视图
        self.text = self._prompt()
        self.sent: list[str] = []
        #: 每条命令**发送时所在的提示符**。视图链错了就是靠这个抓出来的：
        #: 命令本身可能"没报错"，但它落在了错误的视图里，配置就静默失效了。
        self.sent_views: list[tuple[str, str]] = []
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
        self.sent_views.append((line, self._prompt()))
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
        # ★ `return` 与 `quit` 不同：它从**任意深度**直回用户视图（真机行为）。
        #   假 Console 以前没实现它，于是 `_to_user_view` 发了 return 却仍停在子视图，
        #   用户视图专有命令的测试就永远看不到 `<host>` 提示符。
        if line == "return":
            self.stack = []
            return ""
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
        # ---- 2026-10-10 lab4_ts 补：这四个也是"进视图"的命令 ----
        if line.startswith("bgp "):
            self.stack.append("-bgp-default")
            return ""
        if line.startswith("ip vpn-instance "):
            # 系统视图下进 VRF；bgp 视图下进 BGP-VPN 实例（提示符变成 H-bgp-default-vpn1）
            self.stack.append("-" + line.split(" ", 2)[2])
            return ""
        if line.startswith("address-family "):
            self.stack.append("-" + line.split(" ", 1)[1].replace(" ", "-"))
            return ""
        if re.fullmatch(r"isis\s+\d+", line):
            self.stack.append("-isis-" + line.split()[1])
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
        # ★ 这个提示是**用户视图**下才会出现的（重做实验要清空设备时实测踩到）
        if line == "reset saved-configuration":
            return "This will delete the configuration in the flash memory. Continue? [Y/N]:"
        if line == "Y":
            self.confirmed = True
            return ""
        return super()._default(line)


def test_lab4_regressions() -> None:
    """lab4_ts 真机实验暴露的三个缺陷 —— 共同特征是"配置看起来下发成功、其实没生效"。"""
    # 1) `ospf 1 area 0.0.0.0` 是**接口视图**下"在本接口使能 OSPF"的命令。
    #    被当成"进入 ospf 进程视图"时，引擎会先 to_system() 退回系统视图再发 ——
    #    命令报错、接口上根本没使能 OSPF，邻居永远起不来。
    check("ospf <n> area <x> 不算进视图（接口上使能 OSPF）",
          S._is_entry_command("ospf 1 area 0.0.0.0") is False)
    check("ospf <n> router-id ... 仍算进视图",
          S._is_entry_command("ospf 1 router-id 1.1.1.1") is True)
    check("ospf <n> router-id ... vpn-instance ... 仍算进视图",
          S._is_entry_command("ospf 1 router-id 1.1.1.1 vpn-instance vpn1") is True)
    check("ospf timer hello 仍不算进视图",
          S._is_entry_command("ospf timer hello 4") is False)

    # 2) 要求 [Y/N] 确认、又不在白名单里的命令会被**静默丢弃**（既不答 Y 也不报错）。
    #    实测就是这条把 `m-lag system-mac` 丢掉的：两台交换机的 M-LAG 系统 MAC 不一致，
    #    peer-link 永远收不到正确的 DRCPDU，整个 M-LAG 半个系统起不来。
    for cmd in ("m-lag system-mac 1-1-1", "m-lag system-number 1",
                "m-lag system-priority 100", "m-lag keepalive ip destination 1.1.1.1 source 1.1.1.2",
                "irf member 1 renumber 2", "irf-port-configuration active"):
        check("确认白名单覆盖 %r" % cmd, cmd.startswith(S.CONFIRM_SAFE_PREFIXES))
    check("reboot 绝不在自动应答白名单里", not "reboot".startswith(S.CONFIRM_SAFE_PREFIXES))
    check("reset saved-configuration 也不在", not "reset".startswith(S.CONFIRM_SAFE_PREFIXES))

    # 3) Session.auto_confirm 与 Console.auto_confirm 是两个标志。只设前者时，
    #    驱动层（read_until 里的 _answer_confirm）不会就地答 Y，wait_prompt 又等不到
    #    提示符（提示符正则刻意排除 [Y/N]），只能干等到超时 —— 实测每条 60 秒，
    #    确认结果全靠超时尾巴上的正则，极不可靠。
    c = FakeConsole("SW1")
    s = S._Session(c)
    s.auto_confirm = True
    check("auto_confirm=True 同步到 Console（驱动层就地答 Y）",
          c.auto_confirm is True and s.auto_confirm is True)
    s.auto_confirm = False
    check("auto_confirm=False 也同步回去", c.auto_confirm is False and s.auto_confirm is False)


def test_view_chain_lab4() -> None:
    """lab4_ts 的视图链：`bgp → ip vpn-instance → address-family` 必须一路原地进，
    不能被弹回系统视图；而 `bgp`/`isis` 出现在接口视图之后时必须先归位。

    这是**静默失效**类缺陷：命令都"没报错"，但落在错误的视图里，配置就没生效
    （BGP 表空、ISIS 接口级命令丢失）。
    """
    c = FakeConsole("PE1")
    s = S._Session(c)
    s.auto_confirm = True
    s.prepare(5)

    seq = [
        "ip vpn-instance vpn1",          # 系统视图 → [PE1-vpn1]
        " route-distinguisher 100:1",
        "interface GigabitEthernet0/1",  # 换视图块 → 先归位再进 [PE1-GigabitEthernet01]
        " ip address 100.1.0.1 30",
        "bgp 65000",                     # 从接口视图 → 必须先归位 [PE1] 再进 [PE1-bgp-default]
        " router-id 10.10.10.3",
        " ip vpn-instance vpn1",         # 同族嵌套 → 留在 bgp 里进 [PE1-bgp-default-vpn1]
        "  address-family ipv4 unicast",
        "   import-route ospf 1",
        "isis 1",                        # 从地址族视图 → 先归位再进 [PE1-isis-1]
        " is-level level-2",
        "interface LoopBack0",           # 换块 → 归位再进
        " isis enable 1",
    ]
    for line in seq:
        _out, err = s.run(line, 5)
        assert err is None, "命令 %r 报错: %s" % (line, err)
    assert not c.unrecognized, "有命令落在错视图: %s" % c.unrecognized

    # 注意：引擎在 _Session.run 里会 strip 后再发，且同一个命令可能出现多次，
    # 所以按**顺序**配对，不能用 dict。
    expected = [
        ("ip vpn-instance vpn1", "[PE1]"),
        ("route-distinguisher 100:1", "[PE1-vpn1]"),
        ("interface GigabitEthernet0/1", "[PE1]"),
        ("ip address 100.1.0.1 30", "[PE1-GigabitEthernet01]"),
        ("bgp 65000", "[PE1]"),
        ("router-id 10.10.10.3", "[PE1-bgp-default]"),
        ("ip vpn-instance vpn1", "[PE1-bgp-default]"),
        ("address-family ipv4 unicast", "[PE1-bgp-default-vpn1]"),
        ("import-route ospf 1", "[PE1-bgp-default-vpn1-ipv4-unicast]"),
        ("isis 1", "[PE1]"),
        ("is-level level-2", "[PE1-isis-1]"),
        ("interface LoopBack0", "[PE1]"),
        ("isis enable 1", "[PE1-LoopBack0]"),
    ]
    i = 0
    for line, prompt in c.sent_views:
        if i < len(expected) and line == expected[i][0]:
            want = expected[i][1]
            check("视图链 %-32r 发在 %s（实际 %r）" % (line, want, prompt), prompt == want)
            i += 1
    check("视图链：13 条命令全部按序匹配上（只匹配到 %d 条）" % i, i == len(expected))


def test_user_view_commands() -> None:
    """用户视图专有命令（reboot / reset saved-configuration / save …）必须在**用户视图**下发。

    实测踩到：重做实验要先恢复出厂，`reset saved-configuration` 和 `reboot` 被引擎
    "非进视图命令一律先 to_system()" 的规则塞进系统视图，两条全部 `% Unrecognized`
    —— 而它们恰恰是清空设备唯一的手段，整台设备根本回不到出厂。

    顺带验证 allow_destructive=true 时这类命令的 `[Y/N]` 能被自动应答，
    否则会出现"授权了却答不了 Y"的死结。
    """
    c = ConfirmConsole("pe1")
    s = S._Session(c)
    s.auto_confirm = True
    s.allow_destructive = True      # 这两条本身是破坏性命令，默认会被拒
    s.prepare(5)

    # 先制造"深子视图"，验证它会先退回用户视图再发
    s.run("interface GigabitEthernet0/1", 5)
    assert c._prompt().startswith("["), "应处于子视图"

    _out, err = s.run("reset saved-configuration", 5)
    check("用户视图命令：reset saved-configuration 不报错（实际 %r）" % (err,), err is None)
    check("用户视图命令：reset saved-configuration 在 <%s> 下发（实际 %r）"
          % (c.hostname, c.sent_views[-1][1]), c.sent_views[-1][1] == "<%s>" % c.hostname)

    _out, err = s.run("reboot", 5)
    check("用户视图命令：reboot 不报错（实际 %r）" % (err,), err is None)
    check("用户视图命令：reboot 也在用户视图下发（实际 %r）" % (c.sent_views[-1][1],),
          c.sent_views[-1][1] == "<%s>" % c.hostname)

    # 默认（allow_destructive=False）→ 拒绝，且**不发到设备**
    c2 = ConfirmConsole("pe1")
    s2 = S._Session(c2)
    s2.auto_confirm = True
    s2.prepare(5)
    before = len(c2.sent_views)
    _out, err = s2.run("reset saved-configuration", 5)
    check("用户视图命令：默认拒绝破坏性命令（实际 %r）" % (err,),
          err is not None and "破坏性" in err)
    check("用户视图命令：被拒绝的命令不该发到设备", len(c2.sent_views) == before)

    # allow_destructive=True → 放行，且 [Y/N] 能自动答 Y
    c3 = ConfirmConsole("pe1")
    s3 = S._Session(c3)
    s3.auto_confirm = True
    s3.allow_destructive = True
    s3.prepare(5)
    _out, err = s3.run("reset saved-configuration", 5)
    check("用户视图命令：授权后不报错（实际 %r）" % (err,), err is None)
    check("用户视图命令：授权后记入放行清单",
          "reset saved-configuration" in s3.destructive_done)
    check("用户视图命令：授权过的破坏性命令能自动答 Y（否则静默失效）",
          c3.confirmed is True)


def main() -> int:
    test_lab4_regressions()
    test_view_chain_lab4()
    test_user_view_commands()
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
