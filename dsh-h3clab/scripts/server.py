#!/usr/bin/env python3
"""server.py - H3C HCL 实验自动化工具的 MCP（Model Context Protocol）stdio 服务器。

把已有的 H3C/HCL 自动化能力（telnet 控制台驱动、.net 拓扑解析、下发引擎、
验证矩阵、记忆检索、链路巡检）包装成 MCP 工具，让支持 MCP 的客户端
（Claude Desktop / Cursor / 其它 agent）直接调用。

设计约束（重要）
----------------
* **只用 Python 3 标准库**，不依赖 mcp / pydantic / requests 等第三方包，
  MCP 协议本身是手写的。
* **stdout 只输出 JSON-RPC 消息**（一行一个 JSON 对象）；所有日志走 stderr。
  客户端按行解析 stdout，多打一个字符都会让它解析失败。
* 兼容两种分帧：一行一个 JSON（本服务器只发这种）；收到 Content-Length 头
  模式的消息时也不崩（会被忽略并给出可读提示），但不去实现它。
* 每个工具调用都在 try/except 里，任何异常都变成 ``isError: true`` 的文本，
  **绝不让服务器进程退出**。
* 只连 127.0.0.1 上的 HCL 控制台，不扫描局域网。

用法::

    python server.py                 # 以 stdio 方式跑 MCP 服务器
    python selftest.py               # 子进程自测（协议 + 真实工具调用）

配置覆盖：见 CONFIG 段（``h3c_lab_mcp.json`` 或环境变量 H3C_MCP_CONFIG）。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hcldrv import Console, ConsoleError, ConsoleTimeout, _PROMPT  # noqa: E402

# --------------------------------------------------------------------------
# 常量 / 配置
# --------------------------------------------------------------------------

SERVER_NAME = "h3c-hcl-mcp"
SERVER_VERSION = "1.1.0"
DEFAULT_PROTOCOL = "2024-11-05"
SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")

HERE = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"          # 只连本机，绝不扫局域网

#: 兜底探测端口。**只有在既没配 ports、也没配 net_file 时才会用到**：
#: 正常情况下端口由拓扑 .net 的 device_id 推导（控制台端口 = 30000 + device_id）。
DEFAULT_PORTS = list(range(30001, 30011))


def dsh_home() -> Path:
    """DSH 主目录：优先 `$DSH_HOME`，其次 `~/.dsh`。

    刻意不再写死 `C:\\Users\\<某个人>\\.dsh` —— 换机器/换用户名就会静默失效。
    """
    env = (os.environ.get("DSH_HOME") or "").strip()
    return Path(env) if env else (Path.home() / ".dsh")


#: 记忆（skill 里的知识库）默认位置；可被配置项 references_dir 覆盖。
DEFAULT_REFERENCES_DIR = dsh_home() / "skills" / "h3c-lab-automation" / "references"

#: 证据文件根目录默认值（每次调用一个时间戳子目录）；可被配置项 evidence_root 覆盖。
#: 刻意**不放在包内**：插件安装目录是发行物，不该被运行时写脏，
#: 卸载/升级插件也不该把证据一起带走。
DEFAULT_EVIDENCE_ROOT = dsh_home() / "h3clab" / "evidence"


class ConfigError(Exception):
    """配置错误。必须让用户看见，绝不静默回落到默认值。"""


def log(msg: str) -> None:
    """日志一律走 stderr —— stdout 是 JSON-RPC 的专用通道。

    ★ 必须定义在 ``CONFIG = load_config()`` **之前**：load_config 内部会调它，
      否则一旦存在配置文件，就会在 import 期 NameError 崩掉整个服务器。
    """
    sys.stderr.write("[h3c-hcl-mcp] %s\n" % msg)
    sys.stderr.flush()


#: 只读白名单：只有这些开头的命令允许通过 hcl_run_command。
READONLY_PREFIXES = ("display", "show", "ping", "tracert", "traceroute")

#: Comware 报错长相（与 skill 的 hcl_lab.py 对齐；要求里点名的 4 类一定在内）。
ERROR_PATTERNS = [
    re.compile(r"%\s*Unrecognized command", re.I),
    re.compile(r"%\s*Wrong parameter", re.I),
    re.compile(r"%\s*Incomplete command", re.I),
    re.compile(r"%\s*Too many parameters", re.I),
    re.compile(r"%\s*Ambiguous command", re.I),
    re.compile(r"%\s*Unmatched", re.I),
    re.compile(r"%\s*Not enough", re.I),
    re.compile(r"%\s*Please input", re.I),
    re.compile(r"^\s*Error:", re.I | re.M),
    re.compile(r"^\s*Can't\b", re.I | re.M),
    re.compile(r"^\s*Failed\b", re.I | re.M),
    re.compile(r"^\s*Invalid\b", re.I | re.M),
    re.compile(r"Permission denied", re.I),
    re.compile(r"\bis not allowed\b", re.I),
    re.compile(r"\bdoes not exist\b", re.I),
    re.compile(r"\bnot supported\b", re.I),
]

CONFIRM_RE = re.compile(r"\[Y/N\]|\(y/n\)|\[yes/no\]", re.I)
#: 交互确认提示（Comware 形如 "Continue? [Y/N]:"，句尾不一定有冒号）。
_CONFIRM_TAIL_RE = re.compile(r"(?:Continue|Are you sure|confirm)[^\r\n]*\[[Yy]\s*/\s*[Nn]\]\s*:?\s*$",
                              re.I | re.M)
SKIP_AS_ENTER = {"system-view", "system view"}
_ERR_LINE = re.compile(r"^\s*%\s*\S+", re.MULTILINE)

#: ★ 严格报错判定：只认"确定的失败"，用于**视图状态决策**。
#: 宽松的 ERROR_PATTERNS 仍用于**生成给人看的问题清单**——里面有
#: `does not exist` / `is not allowed` 这类正常回显也会出现的词，拿它做状态决策会误判。
STRICT_ERROR_RE = re.compile(
    r"%\s*(?:Unrecognized|Wrong|Incomplete|Ambiguous|Too many|Invalid|Not supported|"
    r"Unmatched|Not enough|Please input)\b"
    r"|\bError:"
    r"|This subnet overlaps"
    r"|\bnot enough memory\b",
    re.I)

#: 可自动应答 Y 的命令前缀（白名单！）。Comware 很多子视图确认都不带 `%`，
#: 不答就静默不生效；但 reboot / reset saved-configuration 这类绝不能盲答 Y。
CONFIRM_SAFE_PREFIXES = (
    "save", "port link-mode", "undo interface", "undo port", "undo link-aggregation",
    "undo port-security", "interface ", "vlan ", "undo vlan", "shutdown", "undo shutdown",
    "port access", "port trunk", "port hybrid", "port default", "undo stp", "stp ",
    "reset counters", "reset arp", "undo nqa", "nqa ", "undo mad", "mad ",
)
#: 命中这些字样时**拒绝下发**并明确报错，不猜、不盲答。
DESTRUCTIVE_RE = re.compile(
    r"\b(reboot|reset\s+saved-configuration|reset\s+saved|format|delete\s+/unreserved"
    r"|restore\s+factory|factory-reset)\b", re.I)

#: 打开新配置子视图的命令。
#: ★ 为什么不用简单的 startswith 前缀表：`ospf 1`（进进程视图）与
#: `ospf timer hello 3`（接口级命令，留在原视图）前两个单词完全相同；
#: `nqa entry a b`（进视图）与 `nqa schedule a b`（留在原视图）同理。
#: 这里用"视图名 + 精确头部"匹配，并对已知的**子命令词**做否定。
ENTRY_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    # (视图关键字, 可以进入的头部, 明确不是进入块的头部)
    ("interface", ("interface ",), ()),
    ("ospf", ("ospf ",), ("ospf timer", "ospf bfd", "ospf cost", "ospf network-type",
                          "ospf authentication-mode", "ospf dr-priority",
                          "ospf mtu-enable", "ospf trans-delay", "ospf peer",
                          "ospf fast-reroute", "ospf hello")),
    ("area", ("area ",), ()),
    ("acl", ("acl ",), ()),
    ("vlan", ("vlan ",), ()),
    ("nqa", ("nqa entry",), ("nqa schedule", "nqa statistics", "nqa reaction")),
    ("line", ("line ",), ()),
    ("policy-based-route", ("policy-based-route ",), ()),
    ("route-policy", ("route-policy ",), ("route-policy ",)),  # permit/deny node 即视图
    ("time-range", ("time-range ",), ()),
    ("rip", ("rip ",), ()),
    ("local-user", ("local-user ", "local-user"), ()),
    ("dhcp", ("dhcp server ip-pool", "dhcp server pool"), ()),
    ("ip-pool", ("ip pool ",), ()),
    ("domain", ("domain ",), ()),
    ("radius", ("radius ",), ()),
    ("hwtacacs", ("hwtacacs",), ()),
    ("irf-port", ("irf-port ",), ()),
    ("wlan", ("wlan ",), ()),
    ("ap", ("ap ",), ()),
    ("ipsec", ("ipsec ",), ()),
    ("ike", ("ike ",), ()),
    ("virtual-template", ("virtual-template ",), ()),
    ("control-plane", ("control-plane",), ()),
    ("attack-defense", ("attack-defense",), ()),
    ("aaa", ("aaa",), ()),
)


def _is_entry_command(cmd: str) -> bool:
    """该命令是否打开一个新的配置子视图（见上方 ENTRY_RULES 的说明）。"""
    s = cmd.strip()
    for _key, heads, negs in ENTRY_RULES:
        for head in heads:
            if s.startswith(head):
                if any(s == n or s.startswith(n + " ") for n in negs):
                    return False
                return True
    return False


def _error_line(out: str, strict: bool = False) -> str | None:
    """从回显里挑出第一条真报错（strict=True 时用严格判定，供状态决策）。"""
    for line in (out or "").splitlines():
        if strict:
            if STRICT_ERROR_RE.search(line):
                return line.strip()
        elif any(p.search(line) for p in ERROR_PATTERNS):
            return line.strip()
    # 不带 % 的整段类报错（例如 "This subnet overlaps with another interface!"）
    if strict:
        m = STRICT_ERROR_RE.search(out or "")
        if m:
            return m.group(0).strip()
    return None

MODEL_RE = re.compile(r"(?m)^\s*H3C\s+(\S+)\s+uptime is")
VERSION_RE = re.compile(r"Version\s+([0-9][0-9A-Za-z.\-]*)")
UPTIME_RE = re.compile(r"(?m)^\s*\S+\s+uptime is\s+(.+?)\s*$")
_DEVICE_LINE_RE = re.compile(r"^\s*(\d+)\s+(\S+)\s+(.+?)\s*$")
_DATE_RE = re.compile(r"(?m)^\s*(\d{1,2}:\d{2}:\d{2}\s+\S+\s+\S+\s+\S+\s+\d{4})")
# display interface brief 的一行：接口 | Link | Protocol | IP（L2 口第 3 列是 Speed）
_IF_LINE_RE = re.compile(
    r"^(?P<intf>[A-Za-z][A-Za-z0-9.\-/]*\d(?:/\d+)*(?:\.\d+)?)\s+"
    r"(?P<phy>UP|DOWN|ADM|DOWN\(ADM\))\s+"
    r"(?P<link>UP|DOWN|ADM|UP\([a-z]+\)|DOWN\([a-z]+\)|ADM\([a-z]+\))"
    r"(?:\s+(?P<ip>\S+))?\s*$")
# 有些设备的 brief 只给两列（接口 + Link），协议列缺省；另有 Speed/Duplex 两列
_IF_LINE_2COL_RE = re.compile(
    r"^(?P<intf>[A-Za-z][A-Za-z0-9.\-/]*\d(?:/\d+)*(?:\.\d+)?)\s+"
    r"(?P<phy>UP|DOWN|ADM|DOWN\(ADM\))\s*$")
_IF_LINE_SPEED_RE = re.compile(
    r"^(?P<intf>[A-Za-z][A-Za-z0-9.\-/]*\d(?:/\d+)*(?:\.\d+)?)\s+"
    r"(?P<phy>UP|DOWN|ADM|DOWN\(ADM\))\s+"
    r"(?P<link>\S+)\s+(?P<speed>\S+)")

#: 设备清单默认值（本机 HCL 实例实测；**只作默认，可被配置项 devices 覆盖**）。
DEFAULT_DEVICES: dict[str, int] = {
    "PE1": 30001, "PE2": 30002, "ASBR": 30003, "PE3": 30004, "PC": 30005,
    "SW3-IRF1": 30006, "SW3-IRF2": 30007, "SW1": 30008, "SW2": 30009,
    "Server": 30010,
}

#: 配置文件里允许出现的键。出现别的键只告警，不报错（向前兼容）。
CONFIG_KEYS = ("host", "ports", "devices", "net_file", "evidence_root", "references_dir")


def _check_port(value) -> int:
    """校验一个控制台端口号；不合法抛 ValueError（由调用方转成 ConfigError）。"""
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError("不是整数: %r" % (value,))
    if not 1 <= port <= 65535:
        raise ValueError("超出合法范围 1-65535: %r" % (value,))
    return port


def _read_config_file(path: Path, strict: bool) -> dict:
    """读一个 JSON 配置文件。

    宽容 BOM：Windows 记事本 / ``Set-Content -Encoding UTF8`` / ``Out-File`` 都会写
    UTF-8 BOM，而 ``encoding="utf-8"`` 读它会直接 JSONDecodeError（实测踩过）。
    ``utf-8-sig`` 两种都能读。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        if strict:
            raise ConfigError("H3C_MCP_CONFIG 指向的配置文件无法解析：%s -> %s" % (path, exc))
        log("配置读取失败（忽略）：%s -> %s" % (path, exc))
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ConfigError("H3C_MCP_CONFIG 指向的文件顶层必须是 JSON 对象：%s" % path)
        log("配置顶层不是 JSON 对象（忽略）：%s" % path)
        return {}
    log("已加载配置：%s" % path)
    return data


def _ports_source_label(conf: dict) -> str:
    """用一句人话说明"端口是从哪来的"。

    刻意不调用 ``_ports_spec``：那个函数定义在本文件后面，而 load_config 在
    import 期就执行，调用它会再现一次 NameError。
    """
    if conf.get("ports_explicit"):
        shown = "、".join(str(p) for p in conf["ports"][:12])
        more = "" if len(conf["ports"]) <= 12 else " …共 %d 个" % len(conf["ports"])
        return "配置项 ports（%s%s）" % (shown, more)
    if conf.get("net_file"):
        return "拓扑 %s 的 device_id（控制台端口 = 30000 + device_id）" % conf["net_file"]
    return "内置兜底范围（既没配 ports、也没配 net_file）"


def load_config() -> dict:
    """读配置。优先级：

    1. 环境变量 ``H3C_MCP_CONFIG`` 指向的文件 —— 由 dsh-h3clab 插件自动生成并传入。
       **显式指定却读不到/读不懂 = 直接报错**，绝不静默回落：否则"改了配置没生效"
       会变成最难查的一类问题。（旧实现在这里既会静默回落，又会因调用未定义的
       ``log()`` 直接 NameError 崩掉。）
    2. ``<本目录>/h3c_lab_mcp.json``，其次 ``<cwd>/h3c_lab_mcp.json`` —— 手工配置兜底；
       这类文件坏了只告警，不拖垮服务器。

    返回的 dict 额外带两个诊断字段：``ports_explicit``（ports 是否来自显式配置）
    与 ``config_source``（配置实际来自哪里）。
    """
    explicit = (os.environ.get("H3C_MCP_CONFIG") or "").strip()
    cfg: dict = {}
    source = "(内置默认值)"
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(
                "H3C_MCP_CONFIG 指向的配置文件不存在：%s\n"
                "  （该变量由 dsh-h3clab 插件自动设置；若你手工设过，请改正或取消它。）" % path)
        cfg = _read_config_file(path, strict=True)
        source = str(path)
    else:
        for path in (HERE / "h3c_lab_mcp.json", Path.cwd() / "h3c_lab_mcp.json"):
            if path.is_file():
                cfg = _read_config_file(path, strict=False)
                source = str(path)
                break

    conf: dict = {
        "host": DEFAULT_HOST,
        "ports": list(DEFAULT_PORTS),
        "ports_explicit": False,
        "devices": dict(DEFAULT_DEVICES),
        "references_dir": str(DEFAULT_REFERENCES_DIR),
        "evidence_root": str(DEFAULT_EVIDENCE_ROOT),
        "config_source": source,
    }

    unknown = sorted(set(cfg) - set(CONFIG_KEYS))
    if unknown:
        log("配置里有无法识别的键（已忽略）：%s" % ", ".join(unknown))

    if "devices" in cfg:
        if not isinstance(cfg["devices"], dict):
            raise ConfigError("devices 必须是 {设备名: 端口} 形式的 JSON 对象")
        devices: dict[str, int] = {}
        for key, value in cfg["devices"].items():
            try:
                devices[str(key)] = _check_port(value)
            except ValueError as exc:
                raise ConfigError("devices[%s] 不合法：%s" % (key, exc))
        conf["devices"] = devices

    if cfg.get("ports"):
        if not isinstance(cfg["ports"], list):
            raise ConfigError("ports 必须是端口号数组，例如 [30001, 30002]")
        ports: list[int] = []
        for value in cfg["ports"]:
            try:
                ports.append(_check_port(value))
            except ValueError as exc:
                raise ConfigError("ports 里有不合法项：%s" % exc)
        conf["ports"] = sorted(set(ports))
        conf["ports_explicit"] = True

    if cfg.get("host"):
        conf["host"] = str(cfg["host"]).strip()

    for key in ("references_dir", "evidence_root", "net_file"):
        value = cfg.get(key)
        if isinstance(value, str) and value.strip():
            conf[key] = value.strip()

    log("配置来源：%s；host=%s；端口来源：%s"
        % (source, conf["host"], _ports_source_label(conf)))
    return conf


try:
    CONFIG = load_config()
except ConfigError as exc:
    # 配置错了就不启动：让插件把这条消息原样带回给调用方，
    # 好过带着错配置去连设备、再让人猜"为什么改了没生效"。
    sys.stderr.write("[h3c-hcl-mcp] 配置错误：%s\n" % exc)
    raise SystemExit(2)


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------

def _text(value) -> str:
    return str(value)


def _one_line(text: str, limit: int = 70) -> str:
    """把多行回显压成一行，便于单行输出。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return "(空输出)"


def _check_errors(text: str) -> list[str]:
    """检出 Comware 报错行。"""
    hits: list[str] = []
    for line in (text or "").splitlines():
        for pattern in ERROR_PATTERNS:
            if pattern.search(line):
                hits.append(line.strip())
                break
    return hits


def _to_user_view(con: Console, timeout: float) -> None:
    """控制台视图状态跨连接保持；先归位用户视图，避免"命令不识别"其实是视图不对。"""
    match = _PROMPT.search(con.text)
    if match and match.group(0).strip().startswith("["):
        try:
            con.command("return", timeout=timeout)
        except (ConsoleError, ConsoleTimeout):
            pass


def _silence_terminal(con: Console, timeout: float = 15.0) -> None:
    """关掉本控制台的日志输出。

    LACP 超时、接口 up/down 这类 ``%日志`` 会被插进回显里把提示符冲散，
    导致 ``command()`` 等不到提示符而超时。
    """
    try:
        in_sys = _PROMPT.search(con.text) and _PROMPT.search(con.text).group(0).strip().startswith("[")
        if in_sys:
            con.command("return", timeout=timeout)
        con.command("undo terminal monitor", timeout=timeout)
    except (ConsoleError, ConsoleTimeout):
        pass


def _truthy(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _text(value).strip().lower() in ("1", "true", "yes", "y", "on")


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if limit is None or limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _connect(port: int, timeout: float = 25.0, connect_timeout: float = 3.0,
             prep_timeout: float = 60.0) -> Console:
    """连上一台 HCL 控制台并等到可用提示符；失败时保证 socket 已关闭。"""
    con = Console(host=CONFIG["host"], port=int(port), timeout=timeout)
    try:
        con.connect(timeout=connect_timeout)
        con.prep(timeout=prep_timeout)
    except Exception:
        con.close()
        raise
    return con


def _prompt_name(con: Console) -> str | None:
    match = _PROMPT.search(con.text)
    return match.group(1) if match else None


_PROMPT_END_RE = re.compile(r"([<\[](?:[^<>\[\]\r\n]{0,64})[>\]])[ \t]*$")


class _Session:
    """带**视图状态机**的控制台会话。

    为什么需要它（实测教训）：HCL 控制台 (1) 跨 telnet 会话保持 CLI 视图、
    (2) 子视图里 `quit` 只退一级，(3) 很多确认提示不带 `%` 且不应答就静默不生效。
    只按"每条命令后等提示符"下发，会在 `ospf→area→network`、`interface→属性`
    这类嵌套上整批错位（命令全报 `% Unrecognized`，其实一条没错）。
    """

    def __init__(self, con: Console):
        self.con = con
        self.sub = 0                      # 2 = 系统视图；>=3 = 配置子视图
        self.host: str | None = None
        self.auto_confirm = False          # 是否白名单式自动应答 [Y/N]
        self.refused: list[str] = []       # 因破坏性被拒绝下发的命令

    # ---- 连接与归位 --------------------------------------------------
    def _wake(self, rounds: int = 4) -> bool:
        """确保看到的是真提示符（不是登录横幅）。连上后/退到用户视图后调用。"""
        for _ in range(rounds):
            p = _prompt_name(self.con)
            if p and p not in ("H3C",):
                return True
            self.con.send_raw(b"\r")
            time.sleep(0.6)
            try:
                self.con.read_until([_PROMPT], timeout=4)
            except Exception:
                pass
        return bool(_prompt_name(self.con))

    def _in_subview(self) -> bool:
        p = _prompt_name(self.con)
        return bool(self.host and p and p.startswith(self.host + "-"))

    def to_system(self, timeout: float = 30.0) -> None:
        """从任意视图回到系统视图（必要时连退多级子视图）。"""
        for _ in range(12):
            p = _prompt_name(self.con)
            if p is None:
                self._wake()
                continue
            if self._in_subview():
                try:
                    self.con.command("quit", timeout=timeout)
                except (ConsoleError, ConsoleTimeout):
                    pass
                self.sub = max(2, self.sub - 1)
                continue
            if p == self.host:                      # 已在系统视图
                self.sub = 2
                return
            if not self.host:                        # 首次：记住主机名
                self.host = p
                self.sub = 2
                return
            # 用户视图 <HOST> —— 进系统视图
            try:
                self.con.command("system-view", timeout=timeout)
            except (ConsoleError, ConsoleTimeout):
                pass
            self._wake()
            if _prompt_name(self.con) == self.host:
                self.sub = 2
                return
            break
        # 兜底：至少保证不进错视图
        self.sub = 2 if _prompt_name(self.con) == self.host else 0

    def to_user(self, timeout: float = 30.0) -> None:
        """回到用户视图（跑 verify / 交给下一条命令前用）。"""
        p = _prompt_name(self.con)
        if not self.host and p and not p.startswith("H3C"):
            self.host = p
        for _ in range(14):
            p = _prompt_name(self.con)
            if p is None:
                self._wake()
                continue
            if p.startswith("<") and p.endswith(">"):
                self.sub = 0
                return
            try:
                self.con.command("quit", timeout=timeout)
            except (ConsoleError, ConsoleTimeout):
                pass
            self._wake()
        self.sub = 0

    def silence_logs(self, timeout: float = 15.0) -> None:
        """关掉本控制台的日志输出（LACP/接口 up-down 日志会把提示符冲散）。"""
        self.to_user(timeout)
        try:
            self.con.command("undo terminal monitor", timeout=timeout)
        except (ConsoleError, ConsoleTimeout):
            pass

    def prepare(self, timeout: float = 30.0) -> None:
        """连上后的标准动作：归位到系统视图 + 记住主机名 + 关终端日志。"""
        self.silence_logs(timeout)
        p = _prompt_name(self.con)
        if p and not p.startswith("H3C"):
            self.host = p
        self.to_system(timeout)

    # ---- 下发 --------------------------------------------------------
    def _say(self, cmd: str, timeout: float) -> str:
        try:
            return self.con.command(cmd, timeout=timeout)
        except ConsoleTimeout as exc:
            return (exc.transcript or "")[-300:]

    #: Comware 允许"在父视图里直接进子视图"的嵌套命令。例如：
    #:   [HX-ospf-1] 下 `area 0.0.0.1`  → [HX-ospf-1-area-0.0.0.1]
    #:   这两个提示符都以 `-ospf-` 开头，所以"同族"时**不要**先退回系统视图，
    #:   否则会把 OSPF 进程视图弹掉，后面 network/import-route 全部落错地方（实测踩过）。
    NEST_KINDS = ("ospf", "bgp", "isis", "rip", "mpls", "vlan", "acl", "ipsec")

    def _same_family(self, cmd: str) -> bool:
        """下一条命令是否要把当前子视图再往深一层（而不是换一个视图块）。"""
        p = _prompt_name(self.con) or ""
        return any(("-" + k) in p for k in self.NEST_KINDS)

    def run(self, cmd: str, timeout: float = 60.0) -> tuple[str, str | None]:
        """下发一条配置命令，返回 (回显, 真报错或 None)。

        视图规则：
          * 普通配置命令 → **留在当前视图**（`ospf→area→network` 必须如此）；
          * 打开新子视图的命令 → 若是同一父视图下的嵌套则原地进，否则先退回系统视图；
          * `quit` → 只退一级。
        """
        line = cmd.strip()
        if DESTRUCTIVE_RE.search(line):
            self.refused.append(line)
            return "", "拒绝下发破坏性命令（需人工确认）：%s" % line

        if line.lower() in SKIP_AS_ENTER:
            self.to_system(timeout)
            return "（已确保处于系统视图）", None

        if line == "quit":
            if self.sub > 2:
                self._say("quit", timeout)
                self.sub -= 1
            return "", None

        if _is_entry_command(line):
            if self._in_subview() and line.startswith(("area ", "network ")) and self._same_family(line):
                pass                     # 同族嵌套：留在当前子视图里继续进
            else:
                self.to_system(timeout)
        elif _prompt_name(self.con) is None or (_prompt_name(self.con) or "").startswith("<"):
            self.to_system(timeout)

        out = self._say(line, timeout)
        err = _error_line(out, strict=True)

        # 受控自动应答：只对白名单命令答 Y
        if CONFIRM_RE.search(out) or _CONFIRM_TAIL_RE.search(out):
            if self.auto_confirm and line.startswith(CONFIRM_SAFE_PREFIXES):
                out += "\n" + self._say("Y", timeout)
                err = _error_line(out, strict=True)
            elif not self.auto_confirm:
                err = err or ("需要 [Y/N] 确认且未开启自动应答，可能未生效：%s" % line)

        if _is_entry_command(line) and not err:
            self.sub += 1
        return out, err


def _ports_from_topology() -> list[int]:
    """从已配置的拓扑 .net 推导控制台端口：``30000 + device_id``。

    这才是"默认端口"的正解：既不用猜、也不用把某一个 lab 的端口表写死在内置默认值里。
    找不到拓扑或解析不出设备时返回空列表，由调用方回落。
    """
    path = _find_net_file(None)
    if path is None:
        return []
    try:
        devs = parse_net(path)
    except Exception as exc:                            # 拓扑坏了不该拖垮探测
        log("从拓扑推导端口失败（忽略）：%s -> %s" % (path, exc))
        return []
    return sorted({int(d.port) for d in devs.values() if getattr(d, "port", None)})


def effective_ports() -> list[int]:
    """本次进程实际使用/扫描的端口列表。

    优先级：**显式配置的 ports** > **从拓扑 net_file 推导** > **内置兜底范围**。
    """
    if CONFIG.get("ports_explicit"):
        return list(CONFIG["ports"])
    derived = _ports_from_topology()
    return derived if derived else list(CONFIG["ports"])


def _ports_origin() -> str:
    """给工具输出用的"这些端口是从哪来的"一句话说明。"""
    if CONFIG.get("ports_explicit"):
        return "配置项 ports"
    path = _find_net_file(None)
    if path is not None and _ports_from_topology():
        return "拓扑 %s 的 device_id" % path
    return "内置兜底范围（未配置 netFile，只有 %s）" % _ports_spec(list(CONFIG["ports"]))


def _default_ports(raw) -> list[int]:
    """解析工具入参 ``ports``；没给（或给的值全非法）就回落到 effective_ports()。"""
    if raw is not None:
        if isinstance(raw, (int, str)):
            raw = [raw]
        out: list[int] = []
        for item in raw:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        if out:
            return out
    return effective_ports()


def _resolve_port(entry: dict) -> tuple[int | None, str | None]:
    """把一条链路/设备描述解析成端口：优先 port，其次按 devices 映射，最后扫端口找主机名。"""
    if entry.get("port") is not None:
        try:
            return int(entry["port"]), None
        except (TypeError, ValueError):
            return None, "port 不是整数: %r" % entry.get("port")
    name = _text(entry.get("name") or "").strip()
    for dev, port in (CONFIG.get("devices") or {}).items():
        if dev.lower() == name.lower():
            return int(port), None
    if name:
        found = _scan_for_hostname(name)
        if found:
            return found, None
        return None, "找不到主机名为 %r 的设备控制台（已扫描 %s）" % (
            name, _ports_spec(effective_ports()))
    return None, "既没有 port 也没给出可识别的设备名"


def _ports_spec(ports: list[int]) -> str:
    if not ports:
        return "(空)"
    return "%d-%d" % (min(ports), max(ports)) if len(ports) > 1 else str(ports[0])


_scan_cache: dict[str, int] = {}
_scan_lock = threading.Lock()


def _scan_for_hostname(name: str) -> int | None:
    """按提示符主机名扫端口找设备（并发；结果缓存）。"""
    key = name.lower()
    with _scan_lock:
        if key in _scan_cache:
            return _scan_cache[key]
    ports = effective_ports()
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, max(1, len(ports)))) as pool:
        futures = {pool.submit(_probe_identity, p, False): p for p in ports}
        for fut in concurrent.futures.as_completed(futures):
            try:
                info = fut.result()
            except Exception:
                continue
            if info and (info.get("hostname") or "").lower() == key:
                with _scan_lock:
                    _scan_cache[key] = info["port"]
                return info["port"]
    return None


# --------------------------------------------------------------------------
# 设备探测（hcl_list_devices / hcl_get_facts 的底座）
# --------------------------------------------------------------------------

def _probe_identity(port: int, want_model: bool = True,
                    prompt_timeout: float = 25.0) -> dict:
    """探测一个端口的身份；连不上返回 reachable=False 的记录，不抛异常。"""
    info = {"port": int(port), "hostname": None, "model": None, "state": "DOWN",
            "error": None, "auto_config_breaks": 0}
    con = Console(host=CONFIG["host"], port=int(port))
    try:
        con.connect(timeout=3.0)
    except OSError as exc:
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
        con.close()
        return info
    except Exception as exc:
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
        con.close()
        return info
    try:
        con.prep(timeout=prompt_timeout)
        info["hostname"] = _prompt_name(con)
        info["auto_config_breaks"] = con.auto_config_breaks
        info["state"] = "UP"
        if want_model:
            try:
                out = con.command("display version", timeout=30)
                match = MODEL_RE.search(out)
                info["model"] = match.group(1) if match else None
            except ConsoleTimeout:
                info["error"] = "display version 超时（控制台可用，型号未知）"
    except (ConsoleError, ConsoleTimeout) as exc:
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
    except Exception as exc:
        info["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        con.close()
    return info


def tool_hcl_list_devices(args: dict) -> str:
    ports = _default_ports(args.get("ports"))
    want_model = _truthy(args.get("model"), True)
    prompt_timeout = float(args.get("prompt_timeout") or 25.0)
    workers = max(1, min(10, int(args.get("workers") or 10), len(ports)))

    results: dict[int, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_probe_identity, p, want_model, prompt_timeout): p for p in ports}
        for fut in concurrent.futures.as_completed(futures):
            port = futures[fut]
            try:
                results[port] = fut.result()
            except Exception as exc:            # 单台失败不影响其他
                results[port] = {"port": port, "hostname": None, "model": None,
                                 "state": "DOWN", "error": "%s: %s" % (type(exc).__name__, exc)}
    lines: list[str] = []
    up = 0
    for port in sorted(results):
        info = results[port]
        if info.get("state") == "UP":
            up += 1
        lines.append("%d  %s  %s  %s" % (
            port,
            info.get("hostname") or "-",
            info.get("model") or "-",
            info.get("state") or "DOWN",
        ))
    failed = [p for p in sorted(results) if results[p].get("state") != "UP"]
    out = list(lines)
    if failed:
        out.append("")
        out.append("连不上的端口（%d 个）：" % len(failed))
        for port in failed:
            out.append("  %d: %s" % (port, results[port].get("error") or "连接失败"))
    out.append("")
    out.append("合计 %d 台可达 / 探测 %d 个端口（%s）" % (up, len(results), _ports_spec(ports)))
    out.append("端口来源：%s" % _ports_origin())
    if up == 0:
        out.append("提示：HCL 没启动，或拓扑还没点『启动』（HCL 无 API，这一步只能人工点）。")
    return "\n".join(out)


# --------------------------------------------------------------------------
# .net 拓扑解析（net2map 的解析逻辑，内联为 hcl_topology 助手）
# --------------------------------------------------------------------------

_SECTION = re.compile(r"^\[\[(.+?)\]\]\s*$")
_LINKKEY = re.compile(r"^[A-Za-z][A-Za-z-]*_\d+/\d+$")
_SKIP_TYPE = {"NOTE", "SHAPE"}


class NetDev:
    __slots__ = ("name", "dtype", "did", "slot", "links")

    def __init__(self, name: str, dtype: str) -> None:
        self.name = name
        self.dtype = dtype
        self.did: int | None = None
        self.slot = ""
        self.links: list[tuple[str, str, str]] = []   # (本端端口, 对端设备, 对端端口)

    @property
    def port(self) -> int | None:
        return None if self.did is None else 30000 + self.did


def parse_net(path: Path) -> dict[str, NetDev]:
    """解析 HCL 的 .net 拓扑（INI 风格）。"""
    devs: dict[str, NetDev] = {}
    cur: NetDev | None = None
    # utf-8-sig：HCL 导出的 .net 可能带 BOM，而 \ufeff 不是空白字符，
    # 会让第一台设备的 [[型号 名字]] 匹配失败、静默少一台设备。
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        m = _SECTION.match(line)
        if m:
            dtype, _, name = m.group(1).partition(" ")
            cur = None
            if dtype.upper() not in _SKIP_TYPE and name:
                cur = NetDev(name, dtype)
                devs[name] = cur
            continue
        if cur is None or "=" not in line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key == "device_id":
            try:
                cur.did = int(val)
            except ValueError:
                pass
        elif key == "slot0":
            cur.slot = val
        elif _LINKKEY.match(key):
            parts = val.split()
            if len(parts) >= 2:
                cur.links.append((key, parts[0], parts[-1]))
            else:
                cur.links.append((key, val or "?", "?"))
    return devs


def _find_net_file(explicit) -> Path | None:
    """定位拓扑文件：**显式传参 > 配置 net_file > 没有**。

    ★ 刻意删掉了旧的"去 ``D:\\NET`` 里挑 mtime 最新的 .net"兜底：
      那个目录下有十几个**不同实验**的拓扑，按时间猜会把 A 套的实验设备表
      贴到 B 套的验证结论上 —— 猜错比报错危险得多。
    """
    # ★ _text(None) 会得到字符串 "None" —— 那是个**真值**，会把"没传参"误判成
    #   "传了一个叫 None 的文件"。必须先 `or ""` 再转字符串。
    explicit = _text(explicit or "").strip()
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    configured = _text(CONFIG.get("net_file") or "").strip()
    if configured:
        p = Path(configured)
        return p if p.is_file() else None
    return None


def _net_file_error(explicit) -> str:
    """拓扑缺失时给出的、能照着做的错误说明。"""
    explicit = _text(explicit or "").strip()
    configured = _text(CONFIG.get("net_file") or "").strip()
    lines = ["错误：找不到 .net 拓扑文件。（不会去猜：猜错拓扑比报错危险。）"]
    if explicit:
        lines.append("  本次传入的 net_file 不存在：%s" % explicit)
    if configured:
        lines.append("  配置的 net_file 不存在：%s" % configured)
    if not explicit and not configured:
        lines.append("  既没有传 net_file，配置里也没有 netFile。")
    lines += [
        "  HCL 把拓扑存在 <HCL安装目录>\\sessions\\*.net（INI 风格，形如 [[型号 名字]]）。",
        "  两种修法（任选其一）：",
        "    1) 调用时传绝对路径：h3c_topology(net_file=\"D:\\\\...\\\\xxx.net\")",
        "    2) 长期配置：在 profile 的 cordis.patch.yml 里给 dsh-h3clab 设置",
        "       netFile: D:\\...\\xxx.net    （或写 %s\\h3c_lab_mcp.json 的 \"net_file\"）" % HERE,
    ]
    return "\n".join(lines)


def tool_hcl_topology(args: dict) -> str:
    explicit = args.get("net_file")
    path = _find_net_file(explicit)
    if path is None:
        return _net_file_error(explicit)
    devs = parse_net(path)
    if not devs:
        return ("错误：没从 %s 解析出设备。确认这是 HCL 的 .net（形如 [[型号 名字]] / "
                "GE_0/0 = PEER PORT）。" % path)
    out = ["拓扑文件: %s" % path, ""]
    out.append("== 设备表（控制台端口 = 30000 + device_id）==")
    out.append("名称            型号            device_id  控制台端口")
    for dev in devs.values():
        out.append("%-15s %-14s %-10s %s" % (dev.name, dev.dtype,
                                              dev.did if dev.did is not None else "-",
                                              dev.port if dev.port else "-"))
    out += ["", "== 连线表 =="]
    seen: set[frozenset] = set()
    for dev in devs.values():
        for lport, peer, pport in dev.links:
            key = frozenset({(dev.name, lport), (peer, pport)})
            if key in seen:
                continue
            seen.add(key)
            out.append("%-14s %-12s <-> %-14s %s" % (dev.name, lport, peer, pport))
    out.append("")
    out.append("合计 %d 台设备、%d 条连线" % (len(devs), len(seen)))
    return "\n".join(out)


# --------------------------------------------------------------------------
# hcl_run_command
# --------------------------------------------------------------------------

def _check_readonly(command: str) -> str | None:
    head = command.strip().split()[0].lower() if command.strip() else ""
    if not head:
        return "命令为空。"
    if not any(head.startswith(p) or head == p for p in READONLY_PREFIXES):
        return ("拒绝执行：`%s` 不在只读白名单内。\n"
                "只允许以 %s 开头的命令（大小写不敏感）。\n"
                "配置类下发请用 hcl_apply_plan（需显式 dry_run=false）。"
                % (command.strip().split()[0], " / ".join(READONLY_PREFIXES)))
    return None


def tool_hcl_run_command(args: dict) -> str:
    if args.get("port") is None:
        return "错误：缺少必填参数 port（HCL 控制台端口，例如 30008）。"
    if not _text(args.get("command") or "").strip():
        return "错误：缺少必填参数 command。"
    try:
        port = int(args["port"])
    except (TypeError, ValueError):
        return "错误：port 必须是整数，收到 %r。" % args.get("port")
    command = _text(args["command"]).strip()
    timeout = float(args.get("timeout") or 20.0)
    max_chars = int(args.get("max_chars") or 8000)

    bad = _check_readonly(command)
    if bad:
        return bad

    con = None
    try:
        con = _connect(port, timeout=max(timeout, 25.0))
        _to_user_view(con, timeout)
        out = con.command(command, timeout=timeout)
    finally:
        if con is not None:
            con.close()

    body, cut = _truncate(out, max_chars)
    head = "端口 %d  $ %s" % (port, command)
    tail = "[已截断：仅显示前 %d 字符，完整 %d 字符]" % (max_chars, len(out)) if cut else "[完整回显 %d 字符]" % len(out)
    return "%s\n%s\n%s" % (head, body if body.strip() else "(无输出)", tail)


# --------------------------------------------------------------------------
# hcl_get_facts
# --------------------------------------------------------------------------

def _parse_facts(version: str, clock: str, device: str) -> list[str]:
    lines: list[str] = []
    m = MODEL_RE.search(version)
    model = m.group(1) if m else None
    m2 = VERSION_RE.search(version)
    ver = m2.group(1).rstrip(",") if m2 else None
    m3 = UPTIME_RE.search(version)
    uptime = m3.group(1).strip() if m3 else None
    lines.append("型号: %s" % (model or "未知"))
    lines.append("软件版本: Comware %s" % (ver or "未知"))
    if uptime:
        lines.append("运行时间: %s" % uptime[:80])
    first = next((ln.strip() for ln in version.splitlines() if ln.strip()), "")
    if first and (not model or model not in first):
        lines.append("版本行: %s" % first[:80])
    md = _DATE_RE.search(clock)
    if md:
        lines.append("设备时间: %s" % md.group(1))
    else:
        first_clock = next((ln.strip() for ln in clock.splitlines() if ln.strip()), "")
        if first_clock:
            lines.append("设备时间: %s" % first_clock[:70])
    for ln in clock.splitlines():
        if "Time Zone" in ln or "Zone" in ln:
            lines.append("时区: %s" % ln.strip()[:70])
            break

    dl = [ln.rstrip() for ln in device.splitlines() if _DEVICE_LINE_RE.match(ln)]
    if dl:
        lines.append("单板/子卡 (%d 行):" % min(len(dl), 6))
        for ln in dl[:6]:
            lines.append("  %s" % ln.strip()[:90])
    hits = _check_errors(device)
    if hits:
        lines.append("display device 报错: %s" % hits[0][:70])
    return lines[:25]


def tool_hcl_get_facts(args: dict) -> str:
    if args.get("port") is None:
        return "错误：缺少必填参数 port。"
    try:
        port = int(args["port"])
    except (TypeError, ValueError):
        return "错误：port 必须是整数，收到 %r。" % args.get("port")
    base_to = float(args.get("timeout") or 30.0)

    con = None
    try:
        con = _connect(port, timeout=max(base_to, 25.0))
        _to_user_view(con, base_to)
        version = con.command("display version", timeout=base_to)
        clock = con.command("display clock", timeout=base_to)
        device = con.command("display device", timeout=base_to)
        name = _prompt_name(con)
    finally:
        if con is not None:
            con.close()

    head = "端口 %d  主机名 %s" % (port, name or "?")
    return "\n".join([head] + _parse_facts(version, clock, device))


# --------------------------------------------------------------------------
# hcl_apply_plan
# --------------------------------------------------------------------------

def _evidence_dir() -> Path:
    """本次调用的证据目录（按时间戳分目录）。

    创建失败（配置的 evidence_root 指向只读盘/无权限路径等）要当场说清楚是配置问题，
    而不是让后面写证据时抛一个难懂的 OSError。
    """
    d = Path(CONFIG["evidence_root"]) / _stamp()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            "无法创建证据目录 %s：%s\n"
            "  请检查配置项 evidence_root（插件侧是 evidenceRoot）是否指向一个可写目录。" % (d, exc))
    return d


def _apply_one_device(dev: dict, apply: bool, do_save: bool, timeout: float,
                      out_dir: Path) -> dict:
    """下发一台设备的计划；证据无条件落盘。"""
    name = _text(dev.get("name") or dev.get("hostname") or "device")
    result = {"name": name, "port": None, "applied": 0, "errors": [],
              "warnings": [], "verify_ok": 0, "evidence": None}
    port_raw = dev.get("port")
    if port_raw is None:
        port, err = _resolve_port({"name": dev.get("hostname") or name})
        if port is None:
            result["errors"].append(err or "找不到设备端口")
            return result
    else:
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            result["errors"].append("port 不是整数: %r" % port_raw)
            return result
    result["port"] = port
    commands = [_text(c).strip() for c in (dev.get("commands") or []) if _text(c).strip()]
    verify = [_text(c).strip() for c in (dev.get("verify") or []) if _text(c).strip()]

    con = None
    transcript: list[str] = []
    try:
        con = _connect(port, timeout=max(timeout, 25.0))
        sess = _Session(con)
        sess.auto_confirm = _truthy(dev.get("auto_confirm"), True)
        sess.prepare(timeout=max(30.0, timeout))
        result["prompt_before"] = _prompt_name(con)
        result["view_ready"] = "system" if sess.sub == 2 else "unknown"

        if apply and commands:
            for line in commands:
                out, err = sess.run(line, timeout=timeout)
                transcript.append("$ %s" % line)
                if out:
                    transcript.append(out)
                result["applied"] += 1
                if err:
                    result["errors"].append("%s -> %s" % (line, err[:160]))
                # 报告用的问题清单仍走宽松正则（人看）
                for hit in _check_errors(out):
                    if not err and hit not in (err or ""):
                        result["warnings"].append("%s -> %s" % (line, hit[:120]))
            if sess.refused:
                result["errors"].extend(
                    ["拒绝下发破坏性命令（需人工确认）：%s" % c for c in sess.refused])
            sess.to_user(max(30.0, timeout))
            transcript.append("$ return")

        if apply and do_save:
            sess.to_system(max(30.0, timeout))
            out, err = sess.run("save force", timeout=max(180.0, timeout))
            transcript.append("$ save force")
            transcript.append(out)
            if err:
                result["errors"].append("save force -> %s" % err[:160])
            result["saved"] = True

        for cmd in verify:
            try:
                out = con.command(cmd, timeout=max(180.0, timeout))
            except (ConsoleError, ConsoleTimeout) as exc:
                out = "[超时/失败] %s" % exc
                result["warnings"].append("%s 超时" % cmd)
            transcript.append("$ %s" % cmd)
            transcript.append(out)
            result["verify_ok"] += 1
    except (ConsoleError, ConsoleTimeout) as exc:
        result["errors"].append("%s: %s" % (type(exc).__name__, exc))
        transcript.append("[连接/会话错误] %s: %s" % (type(exc).__name__, exc))
    except Exception as exc:
        result["errors"].append("%s: %s" % (type(exc).__name__, exc))
        transcript.append("[异常] %s" % traceback.format_exc())
    finally:
        if con is not None:
            con.close()
        # 证据必须无条件落盘 —— 中途失败时才是最需要它的时候
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.\-]+", "_", result["name"]) or "device"
            path = out_dir / ("%s.txt" % safe)
            body = ["### %s  port=%s" % (result["name"], result["port"]),
                    "### 模式: %s%s" % ("下发" if apply else "预演(dry-run)",
                                        " + save force" if (apply and do_save) else ""),
                    "### 下发前提示符: %s" % result.get("prompt_before"),
                    ""] + transcript + [
                    "--- 结论 ---",
                    "下发命令 %d 条" % result["applied"],
                    "验证命令 %d 条" % result["verify_ok"],
                    "报错 %d 处" % len(result["errors"])]
            body += ["  " + e for e in result["errors"]]
            body += ["警告 %d 处" % len(result["warnings"])]
            body += ["  " + w for w in result["warnings"]]
            path.write_text("\n".join(body), encoding="utf-8")
            result["evidence"] = str(path)
        except Exception as exc:
            result["warnings"].append("证据落盘失败: %s" % exc)
    return result


def tool_hcl_apply_plan(args: dict) -> str:
    plan_path = args.get("plan_json")
    if not plan_path:
        return "错误：缺少必填参数 plan_json（指向计划文件）。"
    p = Path(_text(plan_path))
    if not p.is_file():
        return "错误：找不到计划文件 %s。" % p
    try:
        # utf-8-sig：Windows 上用记事本/PowerShell 存的计划文件带 BOM，用 utf-8 会直接报错。
        plan = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return "错误：计划文件不是合法 JSON：%s -> %s" % (p, exc)

    devices = plan.get("devices") or []
    if not isinstance(devices, list) or not devices:
        return "错误：计划文件里没有 devices 数组（格式见 README）。"
    only = args.get("only")
    if only:
        wanted = {s.strip().lower() for s in _text(only).split(",") if s.strip()}
        devices = [d for d in devices
                   if _text(d.get("name") or d.get("hostname") or "").lower() in wanted]
        if not devices:
            return "错误：--only %s 没有匹配到任何设备。" % only

    # 默认 dry_run=true：只有显式传 dry_run=false 才真正下发
    dry_run = True
    if "dry_run" in args and args.get("dry_run") is not None:
        dry_run = _truthy(args.get("dry_run"), True)
    save = _truthy(args.get("save"), False)
    timeout = float(args.get("timeout") or 60.0)

    if dry_run:
        out = ["[预演 dry-run] 不会碰设备。真要下发请显式传 dry_run:false。",
               "计划文件: %s" % p, ""]
        total = 0
        for dev in devices:
            name = _text(dev.get("name") or dev.get("hostname") or "device")
            cmds = [_text(c).strip() for c in (dev.get("commands") or []) if _text(c).strip()]
            ver = [_text(c).strip() for c in (dev.get("verify") or []) if _text(c).strip()]
            total += len(cmds)
            out.append("%s (port=%s) 将下发 %d 条，验证 %d 条"
                       % (name, dev.get("port", "?"), len(cmds), len(ver)))
            for c in cmds:
                out.append("    %s" % c)
            for c in ver:
                out.append("    (verify) %s" % c)
        out.append("")
        out.append("合计将下发 %d 条命令（%d 台设备）" % (total, len(devices)))
        return "\n".join(out)

    out_dir = _evidence_dir()
    results: list[dict] = []
    workers = max(1, min(5, int(args.get("workers") or 4), len(devices)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_apply_one_device, d, True, save, timeout, out_dir)
                   for d in devices]
        for fut in concurrent.futures.as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append({"name": "?", "applied": 0,
                                "errors": ["%s: %s" % (type(exc).__name__, exc)],
                                "warnings": [], "verify_ok": 0, "evidence": None})
    results.sort(key=lambda r: _text(r.get("name")))

    lines: list[str] = []
    total_errors = 0
    for r in results:
        total_errors += len(r["errors"])
        tags = []
        if r.get("saved"):
            tags.append("已保存")
        if r.get("verify_ok"):
            tags.append("验证 %d 条" % r["verify_ok"])
        lines.append("%s 下发 %d 条 报错 %d%s"
                     % (r["name"], r["applied"], len(r["errors"]),
                        (" [%s]" % ",".join(tags)) if tags else ""))
        for e in r["errors"][:5]:
            lines.append("    ! %s" % e[:150])
        if len(r["errors"]) > 5:
            lines.append("    ! …另有 %d 处" % (len(r["errors"]) - 5))
        for w in r["warnings"][:3]:
            lines.append("    ~ %s" % w[:150])
        if r.get("evidence"):
            lines.append("    证据: %s" % r["evidence"])
    lines.append("合计报错 %d 处" % total_errors)
    lines.append("证据目录: %s" % out_dir)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# hcl_verify
# --------------------------------------------------------------------------

def _run_check(con: Console, check: dict, timeout: float) -> tuple[bool, str, str]:
    """跑一项断言，返回 (是否通过, 证据片段, 完整回显)。语义与 skill 的 verify.py 一致。"""
    cmd = _text(check.get("cmd") or "")
    per = float(check.get("timeout") or timeout)
    try:
        out = con.command(cmd, timeout=per)
    except ConsoleTimeout as exc:
        return False, "命令超时(%.0fs)" % per, "ConsoleTimeout: %s\n%s" % (exc, exc.transcript[-2000:])
    except ConsoleError as exc:
        return False, "命令失败", "%s: %s" % (type(exc).__name__, exc)

    expect = check.get("expect") or []
    forbid = check.get("expect_not") or []
    ok = True
    snippet = ""
    if expect:
        for pat in expect:
            try:
                m = re.search(pat, out, re.MULTILINE)
            except re.error as exc:
                ok, snippet = False, "正则非法: %s (%s)" % (pat, exc)
                break
            if not m:
                ok = False
                snippet = "未匹配: %s" % pat
                break
            if not snippet:
                snippet = m.group(0).strip().replace("\n", " ")[:70]
    if ok and forbid:
        for pat in forbid:
            try:
                m = re.search(pat, out, re.MULTILINE)
            except re.error as exc:
                ok, snippet = False, "正则非法: %s (%s)" % (pat, exc)
                break
            if m:
                ok = False
                snippet = "不该出现: %s" % m.group(0).strip()[:70]
                break
    if ok and not expect and not forbid:
        bad = _ERR_LINE.search(out)
        if bad:
            ok, snippet = False, bad.group(0).strip()
        else:
            snippet = _one_line(out)
    if not snippet:
        snippet = _one_line(out)
    return ok, snippet, out


def tool_hcl_verify(args: dict) -> str:
    path_raw = args.get("checklist_json")
    if not path_raw:
        return "错误：缺少必填参数 checklist_json。"
    p = Path(_text(path_raw))
    if not p.is_file():
        return "错误：找不到清单文件 %s。" % p
    try:
        # utf-8-sig：清单文件常由 Windows 工具生成并带 BOM，用 utf-8 读会直接报错。
        spec = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return "错误：清单不是合法 JSON：%s -> %s" % (p, exc)
    checks = spec.get("checks") or []
    if not checks:
        return "错误：清单里没有 checks 数组。"
    only = args.get("only")
    if only:
        wanted = [s.strip() for s in _text(only).split(",") if s.strip()]
        checks = [c for c in checks
                  if any(_text(c.get("id", "")).startswith(w) for w in wanted)]
        if not checks:
            return "错误：--only %s 没有匹配到任何检查项。" % only
    timeout = float(args.get("timeout") or 15.0)

    by_port: dict[int, list[dict]] = {}
    for c in checks:
        try:
            by_port.setdefault(int(c["port"]), []).append(c)
        except (KeyError, TypeError, ValueError):
            by_port.setdefault(-1, []).append(c)

    out_dir = _evidence_dir()
    report: list[str] = ["# hcl_verify 报告  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "checklist: %s" % p, ""]
    lines: list[str] = []
    passed = failed = unreachable = 0

    for port in sorted(by_port):
        items = by_port[port]
        tag = _text(items[0].get("name") or port)
        if port < 0:
            failed += len(items)
            for c in items:
                lines.append("FAIL  %-10s %-30s | 检查项缺少 port"
                             % (_text(c.get("id", "?")), _text(c.get("desc", ""))))
            continue
        con = None
        try:
            con = _connect(port, timeout=max(25.0, timeout))
            _to_user_view(con, timeout)
        except (ConsoleError, ConsoleTimeout, OSError) as exc:
            unreachable += 1
            failed += len(items)
            lines.append("FAIL  %-10s %-30s | 连不上设备 %s: %s"
                         % (tag, "（%d 项）" % len(items), port, exc))
            report.append("## %s (port %s) 连接失败: %s\n" % (tag, port, exc))
            for c in items:
                report.append("- FAIL %s %s | 连接失败" % (c.get("id"), c.get("desc", "")))
            if con is not None:
                con.close()
            continue
        report.append("## %s (port %s)\n" % (tag, port))
        try:
            for c in items:
                cid = _text(c.get("id", "?"))
                desc = _text(c.get("desc", ""))
                ok, snippet, detail = _run_check(con, c, timeout)
                passed, failed = (passed + 1, failed) if ok else (passed, failed + 1)
                lines.append("%s  %-10s %-30s | %s"
                             % ("PASS" if ok else "FAIL", cid, desc, snippet))
                report.append("- %s %s  %s\n  cmd: %s\n  证据: %s\n  --- 回显 ---\n%s\n"
                              % ("PASS" if ok else "FAIL", cid, desc,
                                 c.get("cmd", ""), snippet, detail))
        finally:
            con.close()

    total = passed + failed
    lines.append("合计 %d 项：PASS %d，FAIL %d%s"
                 % (total, passed, failed, "，%d 台连不上" % unreachable if unreachable else ""))
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        ev = out_dir / "verify-report.txt"
        report.append("\n".join(lines[-1:]))
        ev.write_text("\n".join(report), encoding="utf-8")
        lines.append("证据: %s" % ev)
    except Exception as exc:
        lines.append("（证据落盘失败: %s）" % exc)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# hcl_search_memory
# --------------------------------------------------------------------------

def _references_dir() -> Path:
    """记忆库（skill references）目录；配置项 references_dir 优先，默认在 DSH_HOME 下。"""
    return Path(CONFIG["references_dir"])


def _load_aliases(path: Path) -> list[list[str]]:
    """读 aliases.md：每一行用 | 分隔的词互为同义词。"""
    groups: list[list[str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return groups
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "|" not in s:
            continue
        terms = [t.strip() for t in s.split("|") if t.strip()]
        if len(terms) >= 2:
            groups.append(terms)
    return groups


def _expand_keywords(keys: list[str], groups: list[list[str]]) -> list[list[str]]:
    """每个查询词扩展成一个同义词组；组内任一命中即算该词命中（组间 AND）。"""
    out: list[list[str]] = []
    for k in keys:
        hit = next((g for g in groups
                    if any(k.lower() == t.lower() or k.lower() in t.lower() or t.lower() in k.lower()
                           for t in g)), None)
        out.append(sorted({k} | set(hit or [])))
    return out


class _Block:
    __slots__ = ("file", "line", "section", "heading", "body")

    def __init__(self, file: str, line: int, section: str, heading: str) -> None:
        self.file = file
        self.line = line
        self.section = section
        self.heading = heading
        self.body: list[str] = []

    @property
    def title(self) -> str:
        return ("%s / " % self.section if self.section else "") + self.heading


def _parse_memory_file(path: Path, key: str) -> list[_Block]:
    """把 Markdown 按 ## / ### / #### 切成小节。"""
    blocks: list[_Block] = []
    section = ""
    cur: _Block | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return blocks
    for i, raw in enumerate(lines, 1):
        line = raw.rstrip()
        m2 = re.match(r"^##\s+(.*)$", line)
        m = re.match(r"^(#{3,4})\s+(.*)$", line)
        if m:
            cur = _Block(key, i, section, m.group(2).strip())
            blocks.append(cur)
            continue
        if m2:
            section = m2.group(1).strip()
            cur = _Block(key, i, "", section)
            blocks.append(cur)
            continue
        if cur is not None:
            cur.body.append(line)
    return blocks


def tool_hcl_search_memory(args: dict) -> str:
    keys_raw = args.get("keywords")
    if isinstance(keys_raw, str):
        keys = [k.strip() for k in re.split(r"[\s,]+", keys_raw) if k.strip()]
    elif isinstance(keys_raw, (list, tuple)):
        keys = [_text(k).strip() for k in keys_raw if _text(k).strip()]
    else:
        keys = []
    if not keys:
        return "错误：缺少参数 keywords（字符串或字符串数组）。"
    any_mode = _truthy(args.get("any"), False)
    limit = int(args.get("max") or 5)
    per_section = int(args.get("max_lines") or 20)

    refs = _references_dir()
    if not refs.is_dir():
        return ("错误：记忆库目录不存在：%s\n"
                "  配置项 references_dir（插件侧是 referencesDir）应指向 h3c-lab-automation 的\n"
                "  references 目录，里面应有 cases.md / gotchas.md / aliases.md。\n"
                "  默认值按 $DSH_HOME（其次 ~/.dsh）推导；把 skill 的那三个文件放到该目录即可。"
                % refs)
    files = {"cases": refs / "cases.md", "gotchas": refs / "gotchas.md",
             "aliases": refs / "aliases.md"}
    aliases = _load_aliases(files["aliases"])
    groups = _expand_keywords(keys, aliases)
    terms = sorted({t for g in groups for t in g})

    index: list[_Block] = []
    for key in ("cases", "gotchas"):
        if files[key].is_file():
            index += _parse_memory_file(files[key], key)

    scored: list[tuple[int, _Block, list[tuple[int, str]]]] = []
    for b in index:
        text = b.title + "\n" + "\n".join(b.body)
        low = text.lower()
        hit_groups = [g for g in groups if any(t.lower() in low for t in g)]
        if any_mode:
            if not hit_groups:
                continue
        elif len(hit_groups) != len(groups):
            continue
        lines_all = (b.title + "|||" + "\n".join(b.body)).splitlines()
        hit_lines = [(i, ln) for i, ln in enumerate(lines_all)
                     if any(t.lower() in ln.lower() for t in terms)]
        scored.append((len(hit_groups), b, hit_lines))
    scored.sort(key=lambda t: (-t[0], t[1].file, t[1].line))

    out: list[str] = []
    out.append("关键词: %s  （%s）" % (", ".join(keys), "任一命中 ANY" if any_mode else "全部命中 AND"))
    multi = [g for g in groups if len(g) > 1]
    if multi:
        out.append("同义词扩展: %s" % "；".join(
            "%s->%s" % (g[0], "/".join(g[1:4])) for g in multi[:6]))
    out.append("检索文件: %s" % " / ".join(str(files[k]) for k in ("cases", "gotchas", "aliases")))
    if not scored:
        out.append("")
        out.append("记忆里没有命中。按铁律 2 的顺序继续：")
        out.append("  1) 换关键词再查一次（英文报错原文、协议名、设备名往往更有效）")
        out.append("  2) 查厂商官方文档（H3C 配置指导/命令参考；设备上 `display xxx ?` 最权威）")
        out.append("  3) 官方文档没解决再搜同厂商案例/外部资料")
        out.append("  ★ 问题解决后把经验写回 references/cases.md（可复用的提炼进 gotchas.md）")
        return "\n".join(out)

    out.append("命中 %d 个小节，显示前 %d 个：" % (len(scored), min(limit, len(scored))))
    for _score, b, hit_lines in scored[:limit]:
        out.append("")
        out.append("### [%s] %s:%d  %s" % (b.file, files[b.file].name, b.line, b.title))
        shown = 0
        for i, ln in hit_lines:
            if shown >= per_section:
                out.append("    …（本节命中行已截断，全文见 %s 第 %d 行）"
                           % (files[b.file].name, b.line))
                break
            src_line = b.line + i
            out.append("    L%d: %s" % (src_line, ln.strip()[:160]))
            shown += 1
        if not hit_lines:
            out.append("    （命中标题行，正文无单独命中行）")
    if len(scored) > limit:
        out.append("")
        out.append("（另有 %d 个命中未显示，可用 max 参数放大）" % (len(scored) - limit))
    return "\n".join(out)


# --------------------------------------------------------------------------
# hcl_link_watch
# --------------------------------------------------------------------------

def _norm_intf(name: str) -> str:
    s = _text(name).strip()
    s = re.sub(r"(?i)^ten-gigabitethernet", "xge", s)
    s = re.sub(r"(?i)^gigabitethernet", "ge", s)
    s = re.sub(r"(?i)^fortygige", "fge", s)
    s = re.sub(r"(?i)^twenty-fivegige", "tge", s)
    s = re.sub(r"(?i)^m-gigabitethernet", "mge", s)
    s = re.sub(r"(?i)^bridge-aggregation", "bagg", s)
    return s.replace(" ", "").lower()


def _parse_if_brief(text: str) -> dict[str, tuple[str, str]]:
    """解析 display interface brief，返回 {归一化接口名: (物理状态, 协议状态)}。

    不同版本/接口类型的列数不一样（L2 口第 3 列是 Speed、L3 口是 Protocol、
    有的设备只有两列），所以依次用三个模式尝试，物理状态永远是第 2 列。
    """
    table: dict[str, tuple[str, str]] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or set(line) <= set("-=") or line.lower().startswith("interface"):
            continue
        for pattern, has_link in ((_IF_LINE_RE, True), (_IF_LINE_SPEED_RE, True),
                                  (_IF_LINE_2COL_RE, False)):
            m = pattern.match(line)
            if not m:
                continue
            phy = m.group("phy").upper()
            link = (m.groupdict().get("link") or phy).upper() if has_link else phy
            table[_norm_intf(m.group("intf"))] = (phy, link)
            break
    return table


def _intf_state(table: dict[str, tuple[str, str]], intf: str) -> tuple[str, str]:
    """查一个接口的物理/协议状态；查不到返回 ('?', '?')。"""
    key = _norm_intf(intf)
    if key in table:
        return table[key]
    # 容错：允许 1/0/20 与 GigabitEthernet1/0/20 混写、允许大小写不同
    tail = re.search(r"(\d+(?:/\d+)*)$", key)
    if tail:
        for k, v in table.items():
            if k.endswith(tail.group(1)):
                return v
    return "?", "?"


def _if_brief_of(port: int, timeout: float) -> tuple[dict[str, tuple[str, str]], str | None]:
    con = None
    try:
        con = _connect(port, timeout=max(25.0, timeout))
        _to_user_view(con, timeout)
        out = con.command("display interface brief", timeout=max(30.0, timeout))
        return _parse_if_brief(out), None
    except (ConsoleError, ConsoleTimeout, OSError) as exc:
        return {}, "%s: %s" % (type(exc).__name__, exc)
    except Exception as exc:
        return {}, "%s: %s" % (type(exc).__name__, exc)
    finally:
        if con is not None:
            con.close()


def _name_of_port(port: int) -> str | None:
    """端口 -> 设备名（来自配置的 devices 映射）。"""
    for dev, p in (CONFIG.get("devices") or {}).items():
        if int(p) == int(port):
            return dev
    return None


def tool_hcl_link_watch(args: dict) -> str:
    links = args.get("links")
    if not isinstance(links, list) or not links:
        return "错误：缺少必填参数 links（数组，元素形如 {\"name\":..,\"port\":30008,\"intf\":\"GE1/0/20\",\"peer_port\":30001,\"peer_intf\":\"GE0/0\"}）。"
    timeout = float(args.get("timeout") or 25.0)

    # 先解析每条链路的端口
    resolved: list[dict] = []
    for item in links:
        if not isinstance(item, dict):
            resolved.append({"bad": "链路元素不是对象: %r" % (item,)})
            continue
        entry = dict(item)
        port, err = _resolve_port(entry)
        if port is None:
            resolved.append({"bad": "%s: %s" % (entry.get("name") or "?", err)})
            continue
        entry["port"] = port
        peer_name = entry.get("peer_name") or (_name_of_port(entry["peer_port"])
                                              if entry.get("peer_port") else None)
        peer: dict = {"port": entry.get("peer_port"), "name": peer_name}
        if peer["port"] is None:
            # 允许只给对端设备名
            pp, perr = _resolve_port({"name": peer_name or entry.get("peer") or ""})
            peer["port"] = pp
            if pp is None:
                peer["err"] = perr
        elif peer["name"] is None:
            peer["name"] = _name_of_port(int(peer["port"])) or ("port %s" % peer["port"])
        resolved.append({"entry": entry, "peer": peer})

    # 需要查询的端口集合（本端 + 对端），每次调用只查一遍
    ports: set[int] = set()
    for r in resolved:
        if "entry" in r:
            ports.add(r["entry"]["port"])
            if r["peer"].get("port"):
                ports.add(int(r["peer"]["port"]))

    tables: dict[int, dict[str, tuple[str, str]]] = {}
    errs: dict[int, str] = {}
    if ports:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(ports))) as pool:
            futures = {pool.submit(_if_brief_of, p, timeout): p for p in sorted(ports)}
            for fut in concurrent.futures.as_completed(futures):
                p = futures[fut]
                try:
                    table, err = fut.result()
                except Exception as exc:
                    table, err = {}, "%s: %s" % (type(exc).__name__, exc)
                tables[p] = table
                if err:
                    errs[p] = err

    def label(state: tuple[str, str]) -> str:
        phy = state[0]
        if phy in ("UP", "DOWN", "ADM"):
            return phy
        if phy.startswith("DOWN"):
            return "ADM" if "ADM" in phy else "DOWN"
        return "?"

    lines: list[str] = []
    up = down = adm = unknown = 0
    for r in resolved:
        if "bad" in r:
            unknown += 1
            lines.append("? %s" % r["bad"])
            continue
        entry, peer = r["entry"], r["peer"]
        name = _text(entry.get("name") or entry["port"])
        intf = _text(entry.get("intf") or "?")
        st = _intf_state(tables.get(entry["port"], {}), intf)
        local = label(st)
        if entry["port"] in errs:
            local = "?"
        if local == "UP":
            up += 1
        elif local == "ADM":
            adm += 1
        elif local == "?":
            unknown += 1
        else:
            down += 1
        if peer.get("port"):
            pst = _intf_state(tables.get(int(peer["port"]), {}),
                              _text(entry.get("peer_intf") or "?"))
            plocal = label(pst)
            if int(peer["port"]) in errs:
                plocal = "?"
            peer_txt = "%s/%s %s %s" % (peer["port"], _text(entry.get("peer_intf") or "?"),
                                        peer.get("name") or "", plocal)
        else:
            peer_txt = "%s ? (对端未解析: %s)" % (peer.get("name"), peer.get("err") or "-")
        lines.append("%s %s 本端 %s | 对端 %s  [phy=%s proto=%s]"
                     % (name, intf, local, peer_txt, st[0], st[1]))
    lines.append("")
    for p in sorted(errs):
        lines.append("端口 %d 查询失败: %s" % (p, errs[p]))
    lines.append("合计 %d 条链路：本端 UP %d，DOWN %d，ADM %d，未知 %d"
                 % (len(resolved), up, down, adm, unknown))
    if adm:
        lines.append("（ADM = 人工 shutdown，不算故障；本工具只做只读巡检，不自动复位）")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# MCP 工具定义
# --------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "hcl_list_devices",
        "description": (
            "并发探测 HCL（H3C Cloud Lab）设备的 telnet 控制台，返回每个端口的"
            "主机名 / 型号 / 是否可达。只读，无副作用。\n"
            "参数：ports（可选，整数数组，默认 30001..30010，HCL 控制台端口 = 30000 + device_id）；"
            "model（可选 bool，默认 true，是否跑 display version 取型号）；"
            "prompt_timeout（可选 float，默认 25，等提示符的超时）；workers（可选 int，最多 10）。\n"
            "返回：每台一行 `端口 主机名 型号 UP|DOWN`，随后列出连不上的端口及原始错误，"
            "最后一行是可达合计。单台失败不影响其他端口。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ports": {"type": "array", "items": {"type": "integer"},
                          "description": "要探测的控制台端口列表，默认 30001..30010"},
                "model": {"type": "boolean", "description": "是否读取型号（默认 true）"},
                "prompt_timeout": {"type": "number", "description": "等待提示符的超时秒数，默认 25"},
                "workers": {"type": "integer", "description": "并发线程数，1..10，默认 10"},
            },
        },
    },
    {
        "name": "hcl_topology",
        "description": (
            "解析 HCL 的 .net 拓扑文件（INI 风格），返回设备表与连线表。只读，无副作用。\n"
            "参数：net_file（可选，.net 文件路径；不传则用配置里的 net_file，或在本机常见位置"
            "（D:\\NET、<HCL安装目录>\\sessions）里找最新的一个）。\n"
            "返回：设备表（名称 / 型号 / device_id / 控制台端口=30000+device_id）与连线表"
            "（`本端设备 本端端口 <-> 对端设备 对端端口`）。没有 net_file 且找不到文件时返回可读错误。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"net_file": {"type": "string", "description": "HCL .net 拓扑文件路径"}},
        },
    },
    {
        "name": "hcl_run_command",
        "description": (
            "在一台 HCL 设备上执行**只读**命令并返回回显。有副作用的风险被白名单挡住："
            "命令必须以 display / show / ping / tracert / traceroute 开头（大小写不敏感），"
            "其他命令一律拒绝。\n"
            "参数：port（必填 int，控制台端口，如 30008）；command（必填 str）；"
            "timeout（可选 float，默认 20；`ping`、大表 `display` 建议 60~240）；"
            "max_chars（可选 int，默认 8000，超长回显截断）。\n"
            "返回：命令回显原文（截断时末尾标注被截断）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer", "description": "HCL 控制台端口，例如 30008"},
                "command": {"type": "string", "description": "只读命令，如 display version"},
                "timeout": {"type": "number", "description": "命令超时秒数，默认 20"},
                "max_chars": {"type": "integer", "description": "回显截断字符数，默认 8000"},
            },
            "required": ["port", "command"],
        },
    },
    {
        "name": "hcl_get_facts",
        "description": (
            "一次性读取一台设备的 display version + display clock + display device，"
            "返回**提炼后的短摘要**（≤25 行：型号、Comware 版本、运行时间、设备时间、单板状态），"
            "不把原始回显整段丢回。只读，无副作用。\n"
            "参数：port（必填 int）；timeout（可选 float，默认 30）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "port": {"type": "integer", "description": "HCL 控制台端口"},
                "timeout": {"type": "number", "description": "单条命令超时秒数，默认 30"},
            },
            "required": ["port"],
        },
    },
    {
        "name": "hcl_apply_plan",
        "description": (
            "按 plan.json 给设备批量下发配置。**默认 dry_run=true 只预演**，"
            "必须显式传 dry_run:false 才会真正碰设备（写操作）。\n"
            "参数：plan_json（必填，路径，格式 {\"devices\":[{\"name\":..,\"port\":..,"
            "\"commands\":[..],\"verify\":[..]}]}，commands 只写配置正文，不用写 system-view）；"
            "only（可选，设备名，逗号分隔）；save（可选 bool，默认 false，下发后 save force）；"
            "dry_run（可选 bool，默认 true）；timeout（可选 float，默认 60，慢命令用 180~240）。\n"
            "返回：紧凑汇总（每台一行 `SW1 下发 12 条 报错 0`）+ 报错明细；完整回显写到"
            " evidence\\<时间戳>\\<设备>.txt 并在结果里给出路径。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_json": {"type": "string", "description": "计划文件路径"},
                "only": {"type": "string", "description": "只处理这些设备名，逗号分隔"},
                "save": {"type": "boolean", "description": "下发后执行 save force，默认 false"},
                "dry_run": {"type": "boolean", "description": "默认 true 只预演；false 才真正下发"},
                "timeout": {"type": "number", "description": "单条命令超时秒数，默认 60"},
                "workers": {"type": "integer", "description": "并发设备数，1..5，默认 4"},
            },
            "required": ["plan_json"],
        },
    },
    {
        "name": "hcl_verify",
        "description": (
            "跑验证矩阵：按 checklist.json 逐项下发断言命令并给出 PASS/FAIL。只读（只跑 display/ping 类命令）。\n"
            "参数：checklist_json（必填，路径，格式 {\"checks\":[{\"id\":..,\"port\":..,\"desc\":..,"
            "\"cmd\":..,\"expect\":[正则..],\"expect_not\":[正则..],\"timeout\":可选}]}）；"
            "only（可选，逗号分隔的 id 前缀）；timeout（可选 float，默认 15）。\n"
            "语义与 skill 的 verify.py 一致：expect 里的正则**全部命中**才 PASS；expect_not 要求"
            "一个都不命中；都不给则只检查没有 Comware 报错；同一 port 的多项复用同一条连接。\n"
            "返回：每项一行 `PASS/FAIL id desc | 证据片段` + 合计行；完整回显写到证据文件（给出路径）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "checklist_json": {"type": "string", "description": "验证清单文件路径"},
                "only": {"type": "string", "description": "只跑这些 id 前缀，逗号分隔"},
                "timeout": {"type": "number", "description": "单条命令超时秒数，默认 15"},
            },
            "required": ["checklist_json"],
        },
    },
    {
        "name": "hcl_search_memory",
        "description": (
            "在 H3C 实验记忆库（skill 的 references/cases.md、gotchas.md、aliases.md）里检索"
            "踩过的坑与案例。排障第一动作。只读，无副作用。\n"
            "参数：keywords（必填，字符串或字符串数组）；any（可选 bool，默认 false = 组间 AND，"
            "true = 任一命中即可）；max（可选 int，默认 5，最多返回几个小节）。\n"
            "同义词扩展：aliases.md 每行用 | 分隔的词互为同义词，关键词命中同义词组内任一词即算命中。\n"
            "返回：命中小节标题 + 命中行（每节最多 20 行）+ 文件名与行号。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keywords": {"oneOf": [{"type": "string"},
                                       {"type": "array", "items": {"type": "string"}}],
                             "description": "关键词，单个字符串或字符串数组"},
                "any": {"type": "boolean", "description": "任一关键词命中即可（默认 false = 全部命中）"},
                "max": {"type": "integer", "description": "最多返回几个小节，默认 5"},
            },
            "required": ["keywords"],
        },
    },
    {
        "name": "hcl_link_watch",
        "description": (
            "链路只读巡检：用 display interface brief 判断若干链路两端的物理状态，"
            "返回每条一行 `name intf 本端 UP|DOWN|ADM | 对端 ...` + 合计。"
            "ADM 表示人工 shutdown，不算故障。**不做自动复位**（只读）。\n"
            "参数：links（必填数组，元素 {\"name\":..,\"port\":30008,\"intf\":\"GE1/0/20\","
            "\"peer_port\":30001,\"peer_intf\":\"GE0/0\"}；port/peer_port 可省略，改用 devices 映射或按"
            "设备名扫描）；timeout（可选 float，默认 25）。\n"
            "返回：每条链路的诊断行（含物理/协议状态），末尾给出 UP/DOWN/ADM/未知 合计。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "port": {"type": "integer"},
                            "intf": {"type": "string"},
                            "peer_port": {"type": "integer"},
                            "peer_intf": {"type": "string"},
                        },
                    },
                    "description": "链路数组",
                },
                "timeout": {"type": "number", "description": "命令超时秒数，默认 25"},
            },
            "required": ["links"],
        },
    },
]

TOOL_HANDLERS = {
    "hcl_list_devices": tool_hcl_list_devices,
    "hcl_topology": tool_hcl_topology,
    "hcl_run_command": tool_hcl_run_command,
    "hcl_get_facts": tool_hcl_get_facts,
    "hcl_apply_plan": tool_hcl_apply_plan,
    "hcl_verify": tool_hcl_verify,
    "hcl_search_memory": tool_hcl_search_memory,
    "hcl_link_watch": tool_hcl_link_watch,
}


# --------------------------------------------------------------------------
# JSON-RPC / MCP 协议
# --------------------------------------------------------------------------

def _result(msg_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str, data=None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _negotiate_protocol(params: dict) -> str:
    """回显客户端版本；不在支持列表里则取默认（简单起见以客户端为准）。"""
    wanted = _text((params or {}).get("protocolVersion") or "").strip()
    if not wanted:
        return DEFAULT_PROTOCOL
    return wanted if wanted in SUPPORTED_PROTOCOLS else wanted


def _content(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _content_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def handle_message(msg: dict) -> dict | None:
    """处理一条 JSON-RPC 消息；通知返回 None（不回复）。"""
    if not isinstance(msg, dict):
        return _error(None, -32600, "Invalid Request：不是 JSON 对象")
    msg_id = msg.get("id")
    is_notification = "id" not in msg
    method = _text(msg.get("method") or "")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    if is_notification:
        # 通知一律不回复（notifications/initialized 等）
        log("通知: %s" % (method or "?"))
        return None

    if method == "initialize":
        client = (params.get("clientInfo") or {})
        log("initialize: client=%s protocol=%s"
            % (client.get("name", "?"), params.get("protocolVersion")))
        return _result(msg_id, {
            "protocolVersion": _negotiate_protocol(params),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "ping":
        return _result(msg_id, {})

    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        name = _text(params.get("name") or "")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return _result(msg_id, _content_error(
                "未知工具 %r。可用工具：%s" % (name, ", ".join(sorted(TOOL_HANDLERS)))))
        started = time.monotonic()
        try:
            text = handler(arguments)
            log("tools/call %s 用时 %.1fs" % (name, time.monotonic() - started))
            return _result(msg_id, _content(_text(text)))
        except Exception as exc:                      # 任何异常都变成可读文本，绝不退出进程
            log("tools/call %s 异常: %s" % (name, traceback.format_exc()))
            return _result(msg_id, _content_error(
                "工具 %s 执行失败：%s: %s" % (name, type(exc).__name__, exc)))

    if method in ("resources/list", "prompts/list", "logging/setLevel",
                  "completions/complete"):
        # 未实现的能力：给一个空结果，避免客户端直接报错
        return _result(msg_id, {} if method != "resources/list" else {"resources": []})

    return _error(msg_id, -32601, "Method not found: %s" % (method or "(空)"))


# ---------------------------- stdio 分帧 ----------------------------------

def _write_message(obj: dict) -> None:
    """stdout 只输出一行一个 JSON 对象。

    环境变量 ``H3C_MCP_DEBUG_DUMP=<文件>`` 会额外把每条响应再写一份到该文件，
    便于在没有客户端的情况下抓原始报文（正常使用不需要）。
    """
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if os.environ.get("H3C_MCP_DEBUG_DUMP"):
        try:
            with open(os.environ["H3C_MCP_DEBUG_DUMP"], "a", encoding="utf-8") as fh:
                fh.write(data + "\n")
        except Exception:
            pass
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data.encode("utf-8") + b"\n")
        buffer.flush()
    else:
        sys.stdout.write(data + "\n")
        sys.stdout.flush()


class _FrameReader:
    """从 stdin 读消息。

    * 一行一个 JSON（本服务器唯一的输出方式，也是主要输入方式）；
    * 收到 Content-Length 头模式的输入时**不崩**：跳过头部、按长度读正文，
      但本服务器不实现该模式的输出。

    实现上刻意**不走** ``sys.stdin.buffer.read()``：在 Windows 上（尤其是被
    沙箱/重定向包裹时）对匿名管道做阻塞读可能一直不返回，MCP 客户端的第一条
    ``initialize`` 就永远处理不到。这里改成对 stdin 的 **fd 做非阻塞 os.read +
    轮询**，行为稳定，也顺便能同时兼容文本/二进制两种 stdin。
    """

    POLL = 0.02

    def __init__(self, stream=None) -> None:
        self.stream = stream if stream is not None else getattr(sys.stdin, "buffer", sys.stdin)
        self.buf = b""
        self.fd: int | None = None
        self._eof = False
        try:
            self.fd = self.stream.fileno()
            os.set_blocking(self.fd, False)
            log("stdin 就绪（fd=%s，非阻塞轮询）" % self.fd)
        except Exception as exc:
            self.fd = None
            log("stdin 无法设成非阻塞（%s），退化为阻塞读" % exc)

    def _read_more(self) -> bool:
        """读一轮；返回是否读到了东西。EOF 时置 _eof。"""
        if self.fd is not None:
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                time.sleep(self.POLL)
                return False
            except InterruptedError:
                return False
            except OSError as exc:
                log("stdin 读取失败: %s" % exc)
                self._eof = True
                return False
        else:
            try:
                chunk = self.stream.read(65536)
            except Exception as exc:
                log("stdin 读取失败: %s" % exc)
                self._eof = True
                return False
        if not chunk:
            self._eof = True
            return False
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", "replace")
        self.buf += chunk
        return True

    def next_message(self) -> dict | None:
        while True:
            nl = self.buf.find(b"\n")
            if nl < 0:
                if self._eof:
                    if self.buf.strip():
                        line, self.buf = self.buf, b""
                        obj = self._parse_line(line)
                        if obj is not None:
                            return obj
                    return None
                self._read_more()
                continue
            raw, self.buf = self.buf[:nl], self.buf[nl + 1:]
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            low = line.lower()
            if low.startswith("content-length:"):
                try:
                    size = int(line.split(":", 1)[1].strip())
                except ValueError:
                    log("忽略无法解析的 Content-Length 头: %r" % line)
                    continue
                while len(self.buf) < size and not self._eof:
                    self._read_more()
                body, self.buf = self.buf[:size], self.buf[size:]
                try:
                    return json.loads(body.decode("utf-8", "replace"))
                except Exception as exc:
                    log("Content-Length 消息解析失败: %s" % exc)
                    continue
            obj = self._parse_line(line.encode("utf-8"))
            if obj is not None:
                return obj

    def _parse_line(self, raw: bytes):
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except Exception as exc:
            log("忽略无法解析的一行（不是 JSON）: %s" % exc)
            return None

    def close(self) -> None:
        pass


def serve_stdio() -> int:
    reader = _FrameReader(getattr(sys.stdin, "buffer", sys.stdin))
    log("%s v%s 已启动（stdio）；工具 %d 个" % (SERVER_NAME, SERVER_VERSION, len(TOOLS)))
    log("配置来源：%s" % CONFIG.get("config_source"))
    log("证据目录：%s；记忆库：%s" % (CONFIG["evidence_root"], CONFIG["references_dir"]))
    while True:
        try:
            msg = reader.next_message()
        except Exception as exc:
            log("读取消息异常（继续）: %s" % exc)
            continue
        if msg is None:
            log("stdin 结束，退出。")
            return 0
        try:
            response = handle_message(msg)
        except Exception:
            log("处理消息异常:\n%s" % traceback.format_exc())
            if isinstance(msg, dict) and "id" in msg:
                response = _error(msg.get("id"), -32603, "内部错误，详见 stderr")
            else:
                response = None
        if response is not None:
            try:
                _write_message(response)
            except Exception as exc:
                log("写响应失败: %s" % exc)
                return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="H3C HCL 实验自动化 MCP stdio 服务器（纯标准库）")
    ap.add_argument("--list-tools", action="store_true", help="打印工具清单后退出（不进入 MCP 循环）")
    ap.add_argument("--version", action="store_true", help="打印版本后退出")
    ap.add_argument("--show-config", action="store_true",
                    help="打印生效配置（JSON）后退出；用于排查“改了配置没生效”")
    args = ap.parse_args(argv)
    if args.version:
        sys.stderr.write("%s %s\n" % (SERVER_NAME, SERVER_VERSION))
        return 0
    if args.show_config:
        visible = {k: v for k, v in CONFIG.items() if k != "ports_explicit"}
        sys.stdout.write(json.dumps(visible, ensure_ascii=False, indent=2) + "\n")
        sys.stdout.flush()
        return 0
    if args.list_tools:
        for t in TOOLS:
            sys.stderr.write("%s\n  %s\n" % (t["name"], t["description"].splitlines()[0]))
        return 0
    try:
        return serve_stdio()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
