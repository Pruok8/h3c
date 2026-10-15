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
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hcldrv as H  # noqa: E402
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


def test_silent_refuse_detection() -> None:
    """设备说"命令没生效"但**不带 `%`** 的提示，必须报成失败。

    实测：IRF 建栈时 `port group interface FortyGigE1/0/53` 只回一句
    "Please shutdown the current interface first." —— 工具报 **0 报错**，
    但 `display irf configuration` 里 IRF-Port 还是 `disable`，三组堆叠全白干。
    这类"静默失效"比报错难查得多，必须当成失败。
    """
    cases = [
        ("Please shutdown the current interface first.", True),
        ("Please configure the port as a trunk port before adding it.", True),
        # `save` 的正常输出绝不能被误判
        ("Validating file. Please wait...\n"
         "Saved the current configuration to mainboard device successfully.", False),
        ("System View: return to User View with Ctrl+Z.", False),
        ("", False),
        # 本来就该报错的，仍然要报
        ("% Unrecognized command found at '^' position.", True),
        ("% Too many parameters found at '^' position.", True),
    ]
    for text, want in cases:
        got = S._error_line(text) is not None
        check("静默拒绝检测 %-48r -> %s" % (text[:46].replace("\n", "\\n"),
                                            "报错" if want else "不报错"), got == want)


def test_verify_changed_offline() -> None:
    """回读校验（verify_changed）的离线判定：配置 diff / "无变化"命令 / 未保存。

    这块要灭掉本次实验里最贵的一类问题：**工具报 0 报错，命令其实没生效**。
    其中两类（属性不一致只回不带 % 的 warning、`undo shutdown` 后没 save）
    无法靠认提示文本解决，只能靠回读比对。
    """
    before = (
        "display current-configuration\n"
        "#\n"
        " version 7.1.070, Release 6555P01\n"
        "#\n"
        " sysname SW1\n"
        "#\n"
        "interface GigabitEthernet1/0/1\n"
        " port link-type trunk\n"
        " shutdown\n"
        "#\n"
        "interface GigabitEthernet1/0/2\n"
        " port access vlan 301\n"
        "#\n"
        "return\n")
    after = (
        "display current-configuration\n"
        "#\n"
        " version 7.1.070, Release 6555P01\n"
        "#\n"
        " sysname SW1\n"
        "#\n"
        "interface GigabitEthernet1/0/1\n"
        " port link-type trunk\n"
        "#\n"
        "interface GigabitEthernet1/0/2\n"
        " port access vlan 301\n"
        "#\n"
        " vlan 301\n"
        "#\n"
        "return\n")
    added, removed = S._cfg_diff(before, after)
    check("cfg_diff：新增行里能找到 vlan 301", any("vlan 301" in x for x in added))
    check("cfg_diff：删除行里能找到 shutdown",
          any(x.strip() == "shutdown" for x in removed))
    check("cfg_diff：提示符/#/命令回显都不算配置行",
          all(not x.strip().startswith(("#", "<", "[", "display")) for x in added + removed))

    cmds = ["undo shutdown", "vlan 301", "interface GigabitEthernet1/0/1",
            "ip address 10.0.31.1 24", "quit", "save force", "display vlan 301"]
    nochange = S._commands_without_change(cmds, added + removed)
    check("无变化判定：只报真正没生效的那一条（%r）" % (nochange,),
          nochange == ["ip address 10.0.31.1 24"])
    check("无变化判定：undo shutdown 靠'删除行'算已生效",
          "undo shutdown" not in nochange)
    check("无变化判定：视图切换/读命令/保存不进清单（不制造噪音）",
          not any(c in nochange for c in ("quit", "save force", "display vlan 301",
                                          "interface GigabitEthernet1/0/1")))

    # 接口缩写 / 掩码写法差异绝不能把**已生效**的命令误报成失败
    check("特征匹配：命令写 GE1/0/1，配置里是 GigabitEthernet1/0/1 -> 算生效",
          S._line_covers("interface GigabitEthernet1/0/1",
                         *S._cmd_signature("interface GE1/0/1")))
    check("特征匹配：接口编号不同 -> 不算生效",
          not S._line_covers("interface GigabitEthernet1/0/2",
                             *S._cmd_signature("interface GE1/0/1")))
    check("特征匹配：掩码写成 24 / 255.255.255.0 都算同一条",
          S._line_covers("ip address 10.0.0.1 255.255.255.0",
                         *S._cmd_signature("ip address 10.0.0.1 24")))
    check("特征匹配：IP 不同 -> 不算生效",
          not S._line_covers("ip address 10.0.0.2 255.255.255.0",
                             *S._cmd_signature("ip address 10.0.0.1 24")))
    check("特征匹配：vlan 10 不会被 vlan 100 顶替",
          not S._line_covers("vlan 100", *S._cmd_signature("vlan 10")))

    check("未保存判定：startup 里没有新增行 -> 报出来",
          S._startup_missing(["vlan 301"], "sysname SW1\n") == ["vlan 301"])
    check("未保存判定：startup 里已有该行 -> 不报",
          S._startup_missing(["vlan 301"], "sysname SW1\n vlan 301\n") == [])

    # _read_cfg 的失败路径：绝不能抛异常，只能返回一句人话
    class FakeSess:
        def __init__(self, out: str) -> None:
            self.con = self
            self.out = out
            self.cmds: list[str] = []

        def to_user(self, timeout: float = 5) -> None:      # noqa: ARG002
            pass

        def command(self, cmd: str, timeout: float = 5) -> str:   # noqa: ARG002
            self.cmds.append(cmd)
            return self.out

    text, why = S._read_cfg(FakeSess(" error: % Unrecognized command found at '^' position."),
                            "display current-configuration", 5)
    check("read_cfg：命令不识别 -> 不抛异常、给出原因", text == "" and bool(why))
    text, why = S._read_cfg(FakeSess(""), "display current-configuration", 5)
    check("read_cfg：空回显 -> 不抛异常、给出原因", text == "" and bool(why))
    text, why = S._read_cfg(FakeSess("The saved configuration file is not existed."),
                            "display saved-configuration", 5, "saved")
    check("read_cfg：saved-configuration 不存在 -> 给原因而不是当配置",
          text == "" and bool(why))
    text, why = S._read_cfg(FakeSess(before), "display current-configuration", 5)
    check("read_cfg：正常配置 -> 返回文本且没有原因", bool(text) and why is None)


def test_precheck_offline() -> None:
    """计划预检：5 类本次真金白银踩过的坑，dry_run 也要跑。"""
    devs = [
        {"name": "SW1", "commands": [
            "vlan 301",
            "vlan 301",                                     # 1a 重复命令
            "interface GigabitEthernet1/0/1",
            "interface GigabitEthernet1/0/1",               # 1b 同一接口反复进
            "undo port access vlan 301",                    # 4 后缀不收参数
            "undo port m-lag group",                        # 4 的正确写法：不报
            "ip address 10.0.31.1 30",                      # 5 同网段双 IP
            "ospf 1",
            "ospf 2",                                       # 3 多个 OSPF 进程号
        ]},
        {"name": "SW2", "commands": ["ip address 10.0.31.2 30"]},
        {"name": "SW3", "commands": ["reset saved-configuration"]},   # 2 reboot 前没 save
    ]
    warns = S._precheck_devices(devs)
    joined = "\n".join(warns)

    check("预检1：重复命令被抓到", "重复" in joined and "vlan 301" in joined)
    check("预检1：同一接口反复进入被抓到", "反复进入" in joined and "ge1/0/1" in joined)
    check("预检2：reboot/清配置之前没有 save 被抓到",
          "之前没有 `save`" in joined and "reset saved-configuration" in joined)
    check("预检3：同设备多个 OSPF 进程号被抓到",
          "OSPF 进程号" in joined and "1、2" in joined)
    check("预检4：黑名单后缀带参数被抓到并点出 Too many parameters",
          "后缀不收参数" in joined and "Too many parameters" in joined
          and "undo port access vlan 301" in joined)
    check("预检4：不带编号的正确写法不报", "undo port m-lag group`" not in joined)
    check("预检5：同网段双 IP 被抓到（10.0.31.0/30）",
          "同一网段 10.0.31.0/30" in joined and "SW1=10.0.31.1" in joined
          and "SW2=10.0.31.2" in joined)

    # 干净的（且 save 在 reboot 之前的）计划必须一条都不报
    clean = [{"name": "SW9", "commands": [
        "save force", "reboot force", "ospf 1", "ospf 1 area 0.0.0.0",
        "vlan 10", "ip address 10.9.9.2 30"]}]
    check("预检：干净计划零提示", S._precheck_devices(clean) == [])
    check("预检：`ospf 1 area 0.0.0.0`（接口级命令）不算第二个进程号",
          not any("OSPF 进程号" in w for w in S._precheck_devices(
              [{"name": "SW9", "commands": ["ospf 1", "ospf 1 area 0.0.0.0"]}])))
    check("预检：空计划/坏输入不炸", S._precheck_devices([]) == []
          and S._precheck_devices([{}, "x", None]) == [])


def test_apply_one_device_verify_changed() -> None:
    """端到端：`_apply_one_device(verify_changed=True)` 在假设备上真的回读比对。

    重点验证三件事：
    * 静默失效的命令（这里 `ip address` 刻意不生效）会被列进"配置无变化"；
    * running 变了但 startup 没变 -> 提示"改了但没保存"；
    * 快照读失败**绝不影响下发**（命令照发、报错数为 0，只多一行说明）。
    """
    import tempfile

    class CfgDevice(FakeConsole):
        """维护 running / startup 两份配置的假设备。"""

        def __init__(self, host: str = "SW1", broken_cfg: bool = False) -> None:
            super().__init__(host)
            self.running = ["sysname SW1", "vlan 301"]
            self.startup = ["sysname SW1"]
            self.broken_cfg = broken_cfg
            self.closed = False

        def close(self) -> None:
            self.closed = True

        def _render(self, lines: list) -> str:
            return "\n#\n" + "\n".join(lines) + "\n#"

        def _default(self, line: str) -> str:
            if line == "display current-configuration":
                if self.broken_cfg:
                    return " % Unrecognized command found at '^' position."
                return self._render(self.running)
            if line == "display saved-configuration":
                return self._render(self.startup)
            if line == "save force":
                self.startup = list(self.running)
                return "Validating file. Please wait...\nSaved successfully."
            if line == "vlan 400":
                if "vlan 400" not in self.running:
                    self.running.append("vlan 400")
                return ""
            if line == "ip address 10.0.31.1 30":
                return ""                  # ★ 静默失效：回显干净，配置没动
            return super()._default(line)

    def run_dev(cmds: list, **kw) -> dict:
        dev = CfgDevice(**kw)
        orig = S._connect
        S._connect = lambda port, **kwargs: dev          # noqa: ARG005
        try:
            out_dir = Path(tempfile.mkdtemp(prefix="vchg-"))
            return S._apply_one_device({"name": "SW1", "port": 39999, "commands": cmds},
                                       apply=True, do_save=False, timeout=3.0,
                                       out_dir=out_dir, allow_destructive=False,
                                       verify_changed=True)
        finally:
            S._connect = orig

    res = run_dev(["vlan 400", "ip address 10.0.31.1 30"])
    ch = res.get("change") or {}
    check("端到端：回读校验确实跑了", ch.get("checked") is True)
    check("端到端：静默失效的命令被列出来（%r）" % (ch.get("no_change"),),
          ch.get("no_change") == ["ip address 10.0.31.1 30"])
    check("端到端：生效的命令不被误报（vlan 400 在新增行里）",
          "vlan 400" not in (ch.get("no_change") or []))
    check("端到端：running 变了但 startup 没有 -> 报「重启会丢」",
          bool(ch.get("unsaved"))
          and any("startup" in w for w in res.get("change_report", [])))
    check("端到端：回读校验不制造报错（报错数仍是 %d）" % len(res["errors"]),
          res["errors"] == [] and res["applied"] == 2)
    check("端到端：证据文件里写了回读校验小节",
          bool(res.get("evidence")) and "回读校验" in Path(res["evidence"]).read_text(
              encoding="utf-8"))

    res2 = run_dev(["vlan 400", "save force"])
    ch2 = res2.get("change") or {}
    check("端到端：`save force` 之后不再报未保存", ch2.get("unsaved") == [])
    check("端到端：没有假警报时 change_report 里没有「无变化」",
          not any("无变化" in w for w in res2.get("change_report", [])))

    res3 = run_dev(["vlan 400", "ip address 10.0.31.1 30"], broken_cfg=True)
    check("端到端：快照读失败时下发照做、报错仍为 0（校验绝不能拖垮下发）",
          res3["errors"] == [] and res3["applied"] == 2)
    check("端到端：快照读失败给出可读说明",
          any("快照失败" in w for w in res3.get("change_report", [])))


class _FakeTelnet:
    """最小假控制台（socket 服务端），用来测**驱动层**的收发时序。

    `script(srv, conn)` 自己控制收发 —— 只有这样才能精确复现"残留提示符碎片"
    这种竞态（发送时机比内容更重要）。
    """

    def __init__(self, script) -> None:            # noqa: ANN001
        self.script = script
        self.raw = b""
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.srv.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn = None
        try:
            self.srv.settimeout(10.0)
            conn, _peer = self.srv.accept()
            conn.settimeout(10.0)
            self.script(self, conn)
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def recv(self, conn, timeout: float = 3.0) -> bytes:     # noqa: ANN001
        conn.settimeout(timeout)
        try:
            data = conn.recv(4096)
        except socket.timeout:
            return b""
        self.raw += data
        return data

    def send(self, conn, data: bytes) -> None:               # noqa: ANN001
        conn.sendall(data)

    def stop(self) -> None:
        try:
            self.srv.close()
        except Exception:
            pass
        self.thread.join(timeout=2.0)


def test_hcldrv_console() -> None:
    """netmiko ``hp_comware.py`` 里那几条实测坑，对应到我们的驱动逐条钉死。

    ★ 第 1 条最重要：netmiko 原文
      ``# Comware can leave a <HOSTNAME> prompt fragment queued after find_prompt().``
      ``# Clear the residual prompt so send_command() reads the real command output.``
      不清残留提示符，下一条命令"等提示符"会立刻命中它 -> 读到上一条命令的输出。
    """
    # ---- ①-a 残留提示符碎片：本条命令必须读到自己那条的输出 ----
    def script_stale(srv, conn):                    # noqa: ANN001
        srv.send(conn, b"Welcome\r\n<SW1>")
        srv.recv(conn)                              # 收到我们的命令
        srv.send(conn, b"<SW1>")                    # ★ 残留提示符碎片
        time.sleep(0.3)
        srv.send(conn, b"display version\r\n"
                       b"H3C Comware Software, Version 7.1.070\r\n<SW1>")

    srv = _FakeTelnet(script_stale)
    try:
        con = H.Console(port=srv.port, timeout=4.0).connect()
        con.wait_prompt(timeout=3.0)                # == netmiko find_prompt
        out = con.command("display version", timeout=4.0)
        check("hcldrv①：残留提示符碎片时读到的仍是本条命令的输出（%r）" % out[:50],
              "H3C Comware Software" in out and "<SW1>" not in out)
        check("hcldrv①：记录了「光秃秃提示符又等一拍」的指纹",
              con.stale_prompt_waits >= 1)
        con.close()
    finally:
        srv.stop()

    # ---- ①-b clear_buffer：残留字节读掉、空闲时不阻塞 ----
    def script_residue(srv, conn):                  # noqa: ANN001
        srv.send(conn, b"<SW1>")
        time.sleep(0.25)
        srv.send(conn, b"stale-junk\r\n<SW1>")

    srv = _FakeTelnet(script_residue)
    try:
        con = H.Console(port=srv.port, timeout=3.0).connect()
        con.wait_prompt(timeout=3.0)
        time.sleep(0.6)                             # 让残留字节到达
        n = con.clear_buffer()
        check("hcldrv①：clear_buffer 把残留字节读掉（%d 字节）" % n,
              n > 0 and "stale-junk" in con.text)
        check("hcldrv①：没有残留时 clear_buffer 返回 0", con.clear_buffer() == 0)
        t0 = time.monotonic()
        con.clear_buffer()
        dt = time.monotonic() - t0
        check("hcldrv①：clear_buffer 空闲时立刻返回（%.3fs）" % dt, dt < 0.2)
        con.close()
    finally:
        srv.stop()

    # ---- ①-c / ⑥ 文本层面的剥离规则（折行回显、残留提示符行、不吃真输出）----
    wrapped = ["display current-configuration all verbose-with-a-very-lon",
               "g-argument", "out1", "[SW1]"]
    cmd = "display current-configuration all verbose-with-a-very-long-argument"
    check("hcldrv⑥：折行的命令回显被完整吃掉（netmiko 对 Comware 关掉 cmd_verify 的原因）",
          H.Console._strip_echo(wrapped, cmd) == ["out1", "[SW1]"])
    check("hcldrv⑥：回显对不上时**不吃**真输出",
          H.Console._strip_echo(["H3C Comware Software"], "display version")
          == ["H3C Comware Software"])
    check("hcldrv①：开头的空行 / 残留提示符行被吃掉",
          H.Console._strip_echo(["", "<SW1>", "display version", "ok", "<SW1>"],
                                "display version") == ["ok", "<SW1>"])
    check("hcldrv⑤：空行命令不会把输出吃光",
          H.Console._strip_echo(["", "", "out"], "") == ["out"])

    # ---- ⑥ 设备不回显 -> 宽限窗口后照常返回，绝不卡死 ----
    def script_noecho(srv, conn):                   # noqa: ANN001
        srv.send(conn, b"<SW1>")
        srv.recv(conn)
        srv.send(conn, b"H3C Comware Software\r\n<SW1>")

    srv = _FakeTelnet(script_noecho)
    try:
        con = H.Console(port=srv.port, timeout=6.0, echo_grace=0.5).connect()
        con.wait_prompt(timeout=3.0)
        t0 = time.monotonic()
        out = con.command("display version", timeout=6.0)
        dt = time.monotonic() - t0
        check("hcldrv⑥：设备不回显也照常返回（%.2fs，不等到超时）" % dt,
              "H3C Comware Software" in out and dt < 3.0)
        con.close()
    finally:
        srv.stop()

    # ---- ⑤ 行尾：默认发 CR（HCL 实测可用），可按需切成 CRLF ----
    def script_echo(srv, conn):                     # noqa: ANN001
        srv.send(conn, b"<SW1>")
        buf = b""
        while not buf.endswith((b"\r", b"\n")):
            data = srv.recv(conn)
            if not data:
                return
            buf += data
        srv.send(conn, b"ok\r\n<SW1>")

    for le, want in ((b"\r", b"x\r"), (b"\r\n", b"x\r\n")):
        srv = _FakeTelnet(script_echo)
        try:
            con = H.Console(port=srv.port, timeout=3.0, line_end=le).connect()
            con.wait_prompt(timeout=3.0)
            out = con.command("x", timeout=3.0)
            check("hcldrv⑤：line_end=%r 时行尾就是它（收到 %r，输出 %r）" % (le, srv.raw, out),
                  srv.raw == want and out == "ok")
            con.close()
        finally:
            srv.stop()

    # ---- ④ 启动横幅 "Press Y or ENTER to continue, N to exit." ----
    def script_banner(srv, conn):                   # noqa: ANN001
        srv.send(conn, b"Press Y or ENTER to continue, N to exit.")
        srv.recv(conn)                              # 应该收到一个回车
        srv.send(conn, b"\r\n<SW1>")
        while True:                                 # 之后正常应答
            data = srv.recv(conn)
            if not data:
                return
            line = data.decode("utf-8", "replace").strip()
            srv.send(conn, (line + "\r\n<SW1>").encode("utf-8"))

    srv = _FakeTelnet(script_banner)
    try:
        con = H.Console(port=srv.port, timeout=4.0).connect()
        con.prep(timeout=4.0)
        check("hcldrv④：续行横幅被回车放行（banner_continues=%d）" % con.banner_continues,
              con.banner_continues >= 1 and H._PROMPT.search(con.text) is not None)
        check("hcldrv③：关分页用的就是 screen-length disable（会话级）",
              b"screen-length disable" in srv.raw)
        con.close()
    finally:
        srv.stop()

    # ---- ② save force 免交互；裸 save 才问 [Y/N] ----
    check("hcldrv②：`save force` 的输出不会命中 [Y/N] 逻辑（它免交互）",
          H._CONFIRM.search("Validating file. Please wait...\n"
                            "Saved the current configuration to flash successfully.") is None)
    check("hcldrv②：裸 `save` 的问句仍认得（白名单兜底）",
          H._CONFIRM.search("Are you sure? [Y/N]:") is not None)

    # ---- 连接失败要给人话（对齐 skill 源里已有的修复）----
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    try:
        H.Console(port=dead_port, timeout=1.0).connect()
        check("hcldrv：连不上时抛 ConsoleError（可读）", False)
    except H.ConsoleError as exc:
        check("hcldrv：连不上时抛 ConsoleError 且带人话（%s）" % str(exc)[:44],
              "连不上" in str(exc))
    except OSError:
        check("hcldrv：连不上时抛 ConsoleError 且带人话", False)


def test_prompt_ha_prefix() -> None:
    """netmiko 剥掉防火墙 HA 的 `RBM_.` 前缀 —— 我们的提示符解析也要归一化。"""
    class TextConsole:
        def __init__(self, text: str) -> None:
            self.text = text

    check("提示符：`RBM_.` 前缀被剥掉（防火墙 HA）",
          S._prompt_info(TextConsole("[RBM_.F1000]")) == ("F1000", "system"))
    check("提示符：用户视图 + 子视图形态也归一化",
          S._prompt_info(TextConsole("<RBM_.F1000-ospf-1>")) == ("F1000-ospf-1", "user"))
    check("提示符：没有前缀时不受影响",
          S._prompt_info(TextConsole("[H3C-ospf-1]")) == ("H3C-ospf-1", "system"))

    class HaConsole(FakeConsole):
        """提示符带 `RBM_.` 前缀的假设备（双机热备形态）。"""

        def __init__(self) -> None:
            super().__init__("F1000")

        def _prompt(self) -> str:
            if not self.stack:
                return "<RBM_.F1000>"
            return "[RBM_.F1000%s]" % "".join(self.stack)

    c = HaConsole()
    s = S._Session(c)
    s.auto_confirm = True
    s.prepare(5)
    check("提示符：HA 设备学到的 host 不含 RBM_.（%r）" % s.host,
          s.host == "F1000" and s.sub == 2)
    s.run("ospf 1", 5)
    check("提示符：HA 设备进子视图后 sub=3（子视图判定没被前缀带偏）", s.sub == 3)
    s.to_system(5)
    check("提示符：HA 设备能从子视图退回系统视图（sub=2）", s.sub == 2 and not c.unrecognized)


def main() -> int:
    test_lab4_regressions()
    test_view_chain_lab4()
    test_user_view_commands()
    test_silent_refuse_detection()
    test_verify_changed_offline()
    test_precheck_offline()
    test_apply_one_device_verify_changed()
    test_hcldrv_console()
    test_prompt_ha_prefix()
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

    # ------------------------------------------------------------------
    # 8) ★ 2026-10-15：异步日志把提示符挤到非行尾 -> 假红
    #
    # 真实现场（20 台设备，63 条断言里 4 条假红，4 次跑共 11 条）：
    # 设备把 `%Oct 15 05:54:48:210 2026 BR-PE PING/6/...` 这类**异步日志**
    # 紧贴在提示符后面吐出来（甚至同一个分片）：
    #
    #     ...round-trip min/avg/max/std-dev = 2.0/2.2/2.4/0.1 ms\r\n<HQ-PE1>%Oct 15 ...
    #
    # 老 `_PROMPT` 用 `[>\]]\s*$` 锚定，于是**永远匹配不上**：回显早就到齐了，
    # 却一直干等到 timeout，然后被当成"断言不成立"报出去。
    # ------------------------------------------------------------------
    tail_log = ("<BR-PE>%Oct 15 05:54:48:210 2026 BR-PE PING/6/PING_VPN_STATISTICS: "
                "Ping statistics for 172.16.100.11 in VPN instance vpn-office: "
                "5 packet(s) transmitted, 0 packet(s) received, 100.0% packet loss.")
    check("异步日志：提示符后**同一行**粘着日志，提示符仍能命中",
          bool(H._PROMPT.search(tail_log)))
    check("异步日志：提示符与日志**分两行**也命中",
          bool(H._PROMPT.search("<HQ-PE1>\r\n%Oct 15 06:01:55:710 2026 HQ-PE1 PING/6/x")))
    check("异步日志：连来两条日志仍命中",
          bool(H._PROMPT.search("<SW1>\r\n%Oct 15 06:01:55:710 2026 SW1 A/1/x\r\n"
                                "%Oct 15 06:01:56:710 2026 SW1 B/1/y")))
    check("异步日志：无年份的形态（%Nov  2 09:41:23 SW1 ...）也命中",
          bool(H._PROMPT.search("<SW1>%Nov  2 09:41:23 SW1 SHELL/5/SHELL_LOGIN: x")))
    check("异步日志：子视图提示符 + 日志同样命中",
          bool(H._PROMPT.search("[SW1-ospf-1]%Oct 15 06:01:55:710 2026 SW1 A/1/x")))
    # ★ 放宽不能放宽到"吃掉真内容"：提示符之后还有正文行就不算命中。
    check("异步日志：提示符后还有**真正文**时不算命中（没放宽过头）",
          not H._PROMPT.search("<SW1>\r\nDestination/Mask   Proto\r\n"))
    check("异步日志：提示符后是 Comware 报错行时不算命中（% 开头的报错不是日志）",
          not H._PROMPT.search("<SW1>\r\n% Unrecognized command found at '^' position."))
    check("异步日志：提示符名字仍能正确取出（group(1) 没被日志吃掉）",
          H._PROMPT.search(tail_log).group(1) == "BR-PE")
    # server.py 的视图判定用的是另一份正则，必须同源 —— 否则"命令跑通了但视图判成 None"。
    check("异步日志：_PROMPT_FULL_RE（视图判定）也能认出粘日志的提示符",
          S._prompt_info(TextConsole(tail_log)) == ("BR-PE", "user"))
    check("异步日志：视图判定在系统视图粘日志时同样正确",
          S._prompt_info(TextConsole("[BR-PE]%Oct 15 06:01:55:710 2026 BR-PE A/1/x"))
          == ("BR-PE", "system"))

    # ---- 尾部清理：提示符/日志不该混进命令输出 ----
    check("异步日志：_drop_tail 摘掉「提示符+同行日志」",
          H.Console._drop_tail(["out1", tail_log]) == ["out1"])
    check("异步日志：_drop_tail 摘掉提示符与后续日志两行",
          H.Console._drop_tail(["out1", "<HQ-PE1>",
                                "%Oct 15 06:01:55:710 2026 HQ-PE1 PING/6/x"]) == ["out1"])
    check("异步日志：_drop_tail 摘掉结尾空行后再摘提示符",
          H.Console._drop_tail(["out1", "<HQ-PE1>", "", ""]) == ["out1"])
    check("异步日志：_drop_tail **不吃**输出中间的日志（那是真证据）",
          H.Console._drop_tail(["line1", "%Oct 15 06:01:55:710 2026 SW1 A/1/x",
                                "line2", "<SW1>"]) == ["line1",
                                                       "%Oct 15 06:01:55:710 2026 SW1 A/1/x",
                                                       "line2"])
    check("异步日志：_drop_tail 在没有提示符时原样返回",
          H.Console._drop_tail(["a", "b"]) == ["a", "b"])
    check("异步日志：_is_async_log 认得真日志",
          H.Console._is_async_log("%Oct 15 05:54:48:210 2026 BR-PE PING/6/x"))
    check("异步日志：_is_async_log 不认 Comware 报错行",
          not H.Console._is_async_log("% Unrecognized command found at '^' position."))

    # ------------------------------------------------------------------
    # 9) ★ 结果分类：只有 FAIL 是"断言不成立"，超时/错误/连不上是"未判定"
    # ------------------------------------------------------------------
    check("分类：PASS/FAIL 属于已判定",
          set(S.VERIFY_DECIDED) == {S.VERIFY_PASS, S.VERIFY_FAIL})
    check("分类：超时/错误/连不上/跳过都属于未判定",
          set(S.VERIFY_UNDECIDED) == {S.VERIFY_TIMEOUT, S.VERIFY_ERROR,
                                      S.VERIFY_UNREACHABLE, S.VERIFY_SKIP})
    check("分类：两类互不重叠",
          not (set(S.VERIFY_DECIDED) & set(S.VERIFY_UNDECIDED)))
    check("分类：状态常量与报告里的字面值一致（免得测试和输出各说各话）",
          (S.VERIFY_PASS, S.VERIFY_FAIL, S.VERIFY_TIMEOUT, S.VERIFY_ERROR,
           S.VERIFY_UNREACHABLE, S.VERIFY_SKIP)
          == ("PASS", "FAIL", "TIMEOUT", "ERROR", "UNREACHABLE", "SKIP"))

    # ---- _run_check 的返回值必须是字典且带 status（老的 (bool, str, str) 三元组已废）----
    class _TimeOutConsole:
        def command(self, line, timeout=None):            # noqa: ANN001
            raise H.ConsoleTimeout("等待超时", "...ms\r\n<SW1>%Oct 15 06:01:55:710 2026 SW1 A/1/x")

    r = S._run_check(_TimeOutConsole(), {"cmd": "display x"}, 5)
    check("_run_check：超时 -> TIMEOUT（**不是 FAIL**）",
          r["status"] == S.VERIFY_TIMEOUT and "命令超时" in r["snippet"])
    check("_run_check：超时时仍保留 transcript（证据不丢）",
          "Oct 15" in r["detail"])

    class _ErrConsole:
        def command(self, line, timeout=None):            # noqa: ANN001
            raise H.ConsoleError("对端关闭了连接")

    r = S._run_check(_ErrConsole(), {"cmd": "display x"}, 5)
    check("_run_check：控制台错误 -> ERROR（不是 FAIL）",
          r["status"] == S.VERIFY_ERROR)

    class _OkConsole:
        def command(self, line, timeout=None):            # noqa: ANN001
            return "Summary count : 1\n1.1.10.1/32   O_INTRA 10 2  10.0.1.1  GE0/0.101"

    r = S._run_check(_OkConsole(), {"cmd": "display x", "expect": ["O_INTRA"]}, 5)
    check("_run_check：expect 命中 -> PASS", r["status"] == S.VERIFY_PASS)
    r = S._run_check(_OkConsole(), {"cmd": "display x", "expect": ["NOT-THERE"]}, 5)
    check("_run_check：expect 未命中 -> FAIL（这才是真红）",
          r["status"] == S.VERIFY_FAIL and "未匹配" in r["snippet"])
    r = S._run_check(_OkConsole(), {"cmd": "display x", "expect_not": ["O_INTRA"]}, 5)
    check("_run_check：expect_not 命中 -> FAIL", r["status"] == S.VERIFY_FAIL)
    r = S._run_check(_OkConsole(), {"cmd": "display x"}, 5)
    check("_run_check：不给 expect/expect_not 且无报错 -> PASS",
          r["status"] == S.VERIFY_PASS)

    class _ErrLineConsole:
        def command(self, line, timeout=None):            # noqa: ANN001
            return "% Unrecognized command found at '^' position."

    r = S._run_check(_ErrLineConsole(), {"cmd": "display x"}, 5)
    check("_run_check：无 expect 但回显有 Comware 报错 -> FAIL",
          r["status"] == S.VERIFY_FAIL)

    # ---- per-port 锁：同一端口拿到的必须是同一把锁 ----
    check("per-port 锁：同一端口返回同一把锁", S._port_lock(31008) is S._port_lock(31008))
    check("per-port 锁：不同端口是不同锁", S._port_lock(31008) is not S._port_lock(31009))
    check("per-port 锁：锁是 threading.Lock 且有 acquire/release",
          hasattr(S._port_lock(31008), "acquire") and hasattr(S._port_lock(31008), "release"))
    # 同一端口的两个并发调用必须被串行化（真并发下不会交错）
    order: list[str] = []
    lk = S._port_lock(31999)

    def _worker(tag: str) -> None:
        with lk:
            order.append(tag + "-in")
            time.sleep(0.05)
            order.append(tag + "-out")

    ths = [threading.Thread(target=_worker, args=(t,)) for t in ("A", "B")]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    check("per-port 锁：两个线程真被串行化（不交错）",
          order in (["A-in", "A-out", "B-in", "B-out"],
                    ["B-in", "B-out", "A-in", "A-out"]))

    passed = sum(1 for x in RESULTS if x)
    print("\n合计：%d 通过 / %d 失败" % (passed, len(RESULTS) - passed))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
