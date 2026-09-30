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
import difflib
import hashlib
import json
import os
import re
import shutil
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

#: 状态目录默认值：配置快照（hcl_cfgdiff）与 lab 状态文件（hcl_lab_state）都放这里。
#: 可被配置项 state_dir 覆盖。
DEFAULT_STATE_DIR = dsh_home() / "h3clab"


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

#: 设备清单默认值。
#:
#: ★ 刻意留空。过去这里写死了**另一套 lab** 的名字表（PE1/SW1/SW3-IRF1/…）：
#: 在 hcl_2015 那套拓扑上，它会把端口 30006 标成 `SW3-IRF1`，而该设备实测主机名是
#: `H3C` —— 一个"配置名"被摆在"实测名"的位置上展示，比不显示更危险。
#: 设备名天然属于某一个具体 lab，只能由配置项 `devices` 提供，
#: 或在调用/计划里显式写 `port`。
DEFAULT_DEVICES: dict[str, int] = {}

#: 配置文件里允许出现的键。出现别的键只告警，不报错（向前兼容）。
CONFIG_KEYS = ("host", "ports", "devices", "net_file", "evidence_root",
               "references_dir", "state_dir")


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
        "state_dir": str(DEFAULT_STATE_DIR),
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

    for key in ("references_dir", "evidence_root", "state_dir", "net_file"):
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


#: 带括号的完整提示符。**判定视图必须看括号**：`<...>` 是用户视图，`[...]` 是系统视图
#: 或配置子视图。不能比名字——HCL 出厂配置下所有设备都叫 H3C，而且用户视图与系统视图的
#: 名字完全一样（`<H3C>` vs `[H3C]`），只比名字必然判错。
_PROMPT_FULL_RE = re.compile(r"([<\[])((?![Yy]\s*/\s*[Nn])[^<>\[\]\r\n]{1,64})([>\]])[ \t]*$")


def _prompt_info(con: Console) -> tuple[str | None, str | None]:
    """返回 (提示符里的名字, 视图类型)。

    视图类型：``'user'``（`<...>` 用户视图）/ ``'system'``（`[...]` 系统视图或子视图）
    / ``None``（当前回显里没有提示符，例如正卡在登录横幅）。
    """
    match = _PROMPT_FULL_RE.search(con.text)
    if not match:
        return None, None
    return match.group(2), ("user" if match.group(1) == "<" else "system")


def _prompt_name(con: Console) -> str | None:
    """提示符里的名字（不含括号）。"""
    return _prompt_info(con)[0]


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
        """确保看到的是真提示符（而不是登录横幅 / "Press ENTER to get started"）。

        ★ 不再要求"名字不是 H3C"：出厂配置下设备就叫 H3C，那时看到的是**真提示符**；
          用名字去筛会永远等不到，反而把后续视图判断带偏。
        """
        for _ in range(rounds):
            if _prompt_info(self.con)[1] is not None:
                return True
            self.con.send_raw(b"\r")
            time.sleep(0.6)
            try:
                self.con.read_until([_PROMPT], timeout=4)
            except Exception:
                pass
        return _prompt_info(self.con)[1] is not None

    def _in_subview(self) -> bool:
        name, kind = _prompt_info(self.con)
        return bool(kind == "system" and self.host and name and name.startswith(self.host + "-"))

    def to_system(self, timeout: float = 30.0) -> None:
        """从任意视图回到系统视图（必要时连退多级子视图）。

        ★ 实测踩过的坑（2026-09-30，hcl_2015 拓扑）：旧实现是"第一次看到提示符就认为
          已经在系统视图"，于是用户视图 `<H3C>` 被当成系统视图 `[H3C]`，`system-view`
          一次都没发；整批配置命令在用户视图下发，全部 `% Unrecognized command`。
          HCL 出厂配置下**所有**设备都叫 H3C，所以这个坑是必踩的。
          现在只用提示符括号判定视图，并显式发 `system-view`。
        """
        for _ in range(12):
            name, kind = _prompt_info(self.con)
            if kind is None:
                self._wake()
                continue
            if name and not self.host:
                self.host = name
            if kind == "user":
                try:
                    self.con.command("system-view", timeout=timeout)
                except (ConsoleError, ConsoleTimeout):
                    pass
                self._wake()
                new_name, new_kind = _prompt_info(self.con)
                if new_kind == "system":
                    if new_name:
                        self.host = new_name
                    self.sub = 2
                    return
                continue
            # kind == "system"：可能是系统视图，也可能是某个配置子视图
            if self.host and name and name.startswith(self.host + "-"):
                try:
                    self.con.command("quit", timeout=timeout)
                except (ConsoleError, ConsoleTimeout):
                    pass
                self.sub = max(2, self.sub - 1)
                continue
            self.sub = 2
            return
        # 兜底：至少如实反映当前视图，不谎报"已在系统视图"
        self.sub = 2 if _prompt_info(self.con)[1] == "system" else 0

    def to_user(self, timeout: float = 30.0) -> None:
        """回到用户视图（跑 verify / 交给下一条命令前用）。

        ★ 旧实现判断 `p.startswith("<")`，但 `_prompt_name` 返回的是**裸名字**（不含括号），
          这个条件永远为假 —— 于是它会连发 14 次 `quit`，把控制台一路登出到
          "Press ENTER to get started."（实测证据里就留下了这段登录横幅）。
          现在同样只看提示符括号。
        """
        for _ in range(14):
            name, kind = _prompt_info(self.con)
            if kind is None:
                self._wake()
                continue
            if kind == "user":
                if name and not self.host:
                    self.host = name
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
        """连上后的标准动作：关终端日志 → 归位到系统视图（顺带记住主机名）。"""
        self.silence_logs(timeout)
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
        elif _prompt_info(self.con)[1] != "system":
            # 不在系统视图（用户视图 / 看不到提示符）→ 先归位再发。
            # ★ 旧实现这里判断 `_prompt_name(...).startswith("<")`，而 `_prompt_name` 返回的是
            #   裸名字，条件永远为假 —— 于是用户视图下的普通配置命令**不会**先归位。
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
    return "内置兜底范围 %s —— 既没配 ports 也没配 netFile" % _ports_spec(list(CONFIG["ports"]))


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
    if name:
        for dev, port in (CONFIG.get("devices") or {}).items():
            if dev.lower() == name.lower():
                return int(port), None
        found = _scan_for_hostname(name)
        if len(found) == 1:
            return found[0], None
        if len(found) > 1:
            # ★ 绝不"随便挑一台"：重名时挑第一台 = 随机给一台设备下发配置。
            return None, ("主机名 %r 在 %d 个端口上同时存在（%s），无法确定是哪一台。\n"
                          "  请改传 port，或在配置的 devices 里给它一个明确端口。\n"
                          "  提示：HCL 出厂配置下**所有**设备提示符都是默认的 H3C，重名很常见。"
                          % (name, len(found), _ports_spec(found)))
        return None, "找不到主机名为 %r 的设备控制台（已扫描 %s）" % (
            name, _ports_spec(effective_ports()))
    return None, "既没有 port 也没给出可识别的设备名"


def _ports_spec(ports: list[int]) -> str:
    """把端口列表写成人读的形式。

    刻意区分「连续区间」与「稀疏列表」：不可达端口常常是 30003/30004/30005/30011…
    这种稀疏集合，折叠成 `30003-30021（共 9 个）` 会让人以为中间那些也在探测范围里。
    """
    if not ports:
        return "(空)"
    ordered = sorted(ports)
    if len(ordered) == 1:
        return str(ordered[0])
    contiguous = ordered[-1] - ordered[0] + 1 == len(ordered)
    if contiguous:
        return "%d-%d" % (ordered[0], ordered[-1]) if len(ordered) <= 8 \
            else "%d-%d（共 %d 个）" % (ordered[0], ordered[-1], len(ordered))
    if len(ordered) <= 8:
        return "、".join(str(p) for p in ordered)
    return "%s …（共 %d 个）" % ("、".join(str(p) for p in ordered[:6]), len(ordered))


_scan_cache: dict[str, list[int]] = {}
_scan_lock = threading.Lock()


def _scan_for_hostname(name: str) -> list[int]:
    """按提示符主机名扫端口，返回**全部**同名端口（升序）。

    返回列表而不是单个端口，是因为实机上真的会重名：HCL 出厂配置下所有设备的
    提示符都是默认的 ``H3C``。老实现是"命中即返回第一台"，等于随机挑一台设备
    去下发配置——这类错误在实验室里极难排查。
    """
    key = name.strip().lower()
    if not key:
        return []
    with _scan_lock:
        if key in _scan_cache:
            return list(_scan_cache[key])
    ports = effective_ports()
    found: list[int] = []
    if ports:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, max(1, len(ports)))) as pool:
            futures = {pool.submit(_probe_identity, p, False): p for p in ports}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    info = fut.result()
                except Exception:
                    continue
                if info and (info.get("hostname") or "").strip().lower() == key:
                    found.append(int(info["port"]))
    found.sort()
    with _scan_lock:
        _scan_cache[key] = list(found)
    return found


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


def _if_brief_of(port: int, timeout: float) -> tuple[dict[str, tuple[str, str]], str | None, str | None]:
    """连一台设备读 ``display interface brief``，顺带带回**实测主机名**。

    为什么要多带一个主机名：``hcl_link_watch`` 过去显示的对端名字取自配置表，
    结果在 hcl_2015 拓扑上把端口 30006 标成 ``SW3-IRF1``，而那台设备实测叫 ``H3C``。
    同一次连接里多读一次提示符，不增加任何连接开销。
    """
    con = None
    try:
        con = _connect(port, timeout=max(25.0, timeout))
        _to_user_view(con, timeout)
        hostname = _prompt_name(con)
        out = con.command("display interface brief", timeout=max(30.0, timeout))
        return _parse_if_brief(out), None, hostname
    except (ConsoleError, ConsoleTimeout, OSError) as exc:
        return {}, "%s: %s" % (type(exc).__name__, exc), None
    except Exception as exc:
        return {}, "%s: %s" % (type(exc).__name__, exc), None
    finally:
        if con is not None:
            con.close()


def _configured_name_of_port(port: int) -> str | None:
    """端口 -> **配置表里的**设备名。

    ★ 这是"配置名"，不是实测主机名。展示时一定要标明来源，别让调用方
      把配置当成事实（内置默认表清空之后，这里只可能来自用户配置的 devices）。
    """
    for dev, p in (CONFIG.get("devices") or {}).items():
        try:
            if int(p) == int(port):
                return dev
        except (TypeError, ValueError):
            continue
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
        # 只有调用方**显式**给了 peer_name 才当成一个"名字"；否则名字留给实测。
        peer: dict = {"port": entry.get("peer_port"), "explicit": entry.get("peer_name")}
        if peer["port"] is None:
            # 允许只给对端设备名
            pp, perr = _resolve_port({"name": peer["explicit"] or entry.get("peer") or ""})
            peer["port"] = pp
            if pp is None:
                peer["err"] = perr
        # 配置表里的名字只作兜底，展示时会明确标注"配置名"。
        peer["configured"] = _configured_name_of_port(int(peer["port"])) if peer["port"] else None
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
    hostnames: dict[int, str] = {}
    if ports:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(10, len(ports))) as pool:
            futures = {pool.submit(_if_brief_of, p, timeout): p for p in sorted(ports)}
            for fut in concurrent.futures.as_completed(futures):
                p = futures[fut]
                try:
                    table, err, hostname = fut.result()
                except Exception as exc:
                    table, err, hostname = {}, "%s: %s" % (type(exc).__name__, exc), None
                tables[p] = table
                if err:
                    errs[p] = err
                if hostname:
                    hostnames[p] = hostname

    def label(state: tuple[str, str]) -> str:
        phy = state[0]
        if phy in ("UP", "DOWN", "ADM"):
            return phy
        if phy.startswith("DOWN"):
            return "ADM" if "ADM" in phy else "DOWN"
        return "?"

    def peer_label(peer: dict) -> tuple[str, str | None]:
        """对端显示名 + 名字来源告警。

        优先级：**实测主机名** > 调用方显式给的 peer_name > 配置表里的名字。
        后两者都会明确标注来源，避免"配置名"被误当成"实测名"。
        """
        measured = hostnames.get(int(peer["port"])) if peer.get("port") else None
        explicit = peer.get("explicit")
        configured = peer.get("configured")
        if measured:
            if explicit and explicit != measured:
                return measured, ("对端名字不一致：调用方给的 %r，实测 %r（按实测显示）"
                                  % (explicit, measured))
            if configured and configured != measured:
                return measured, ("对端名字不一致：配置名 %r，实测 %r（按实测显示；"
                                  "配置的 devices 可能是另一套 lab 的表）" % (configured, measured))
            return measured, None
        if explicit:
            return "%s（调用方给的）" % explicit, None
        if configured:
            return "%s（配置名，非实测）" % configured, None
        return "?", None

    lines: list[str] = []
    warnings: list[str] = []
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
            shown, warn = peer_label(peer)
            if warn:
                warnings.append("%s <-> %s/%s: %s" % (name, peer["port"], shown, warn))
            peer_txt = "%s/%s %s %s" % (peer["port"], _text(entry.get("peer_intf") or "?"),
                                        shown, plocal)
        else:
            peer_txt = "%s ? (对端未解析: %s)" % (peer.get("explicit") or "?", peer.get("err") or "-")
        lines.append("%s %s 本端 %s | 对端 %s  [phy=%s proto=%s]"
                     % (name, intf, local, peer_txt, st[0], st[1]))
    lines.append("")
    for p in sorted(errs):
        lines.append("端口 %d 查询失败: %s" % (p, errs[p]))
    lines.append("合计 %d 条链路：本端 UP %d，DOWN %d，ADM %d，未知 %d"
                 % (len(resolved), up, down, adm, unknown))
    if warnings:
        lines.append("")
        lines.append("⚠ 名字来源不一致（%d 条）：" % len(warnings))
        for item in warnings:
            lines.append("  " + item)
    if adm:
        lines.append("（ADM = 人工 shutdown，不算故障；本工具只做只读巡检，不自动复位）")
    return "\n".join(lines)


# ==========================================================================
# 状态目录 / 通用文件助手（hcl_cfgdiff、hcl_lab_state、hcl_report 用）
# ==========================================================================

#: 状态目录下的固定名字。
STATE_FILE_NAME = "lab-state.json"
SNAPSHOT_DIR_NAME = "snapshots"


def _state_dir() -> Path:
    """状态目录（配置快照 / lab 状态文件的家）。按需创建。"""
    d = Path(CONFIG["state_dir"])
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            "无法创建状态目录 %s：%s\n  请检查配置项 state_dir（插件侧是 stateDir）是否可写。" % (d, exc))
    return d


def _snapshots_dir() -> Path:
    d = _state_dir() / SNAPSHOT_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_file(explicit=None) -> Path:
    raw = _text(explicit or "").strip()
    return Path(raw) if raw else (_state_dir() / STATE_FILE_NAME)


def _load_json_file(path: Path, default):
    """读一个 JSON 文件：不存在返回 default；坏了抛可读错误（不静默吞掉）。"""
    if not path.is_file():
        return default
    try:
        # utf-8-sig：Windows 工具写出来的 JSON 常带 BOM
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError("文件不是合法 JSON：%s -> %s" % (path, exc))


def _write_json_file(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _backup_file(path: Path) -> Path | None:
    """改写一个已有文件之前先做时间戳备份；文件不存在返回 None。"""
    if not path.is_file():
        return None
    backup = path.with_name("%s.bak-%s" % (path.name, _stamp()))
    shutil.copy2(path, backup)
    return backup


# ==========================================================================
# hcl_doctor —— 环境自检
# ==========================================================================

def tool_hcl_doctor(args: dict) -> str:
    """环境自检：一条命令定位"为什么连不上 / 为什么改了没生效"。

    只读：不碰设备配置，最多连控制台读一次提示符。
    """
    lines: list[str] = []
    problems: list[str] = []
    warnings: list[str] = []

    def emit(mark: str, label: str, detail: str = "") -> None:
        lines.append("  %s %s%s" % (mark, label, ("  —  " + detail) if detail else ""))

    def say_ok(label, detail=""):
        emit("✅", label, detail)

    def say_warn(label, detail=""):
        warnings.append(label)
        emit("⚠️ ", label, detail)

    def say_bad(label, detail=""):
        problems.append(label)
        emit("❌", label, detail)

    # ---------- 1) 运行环境 ----------
    lines.append("== 1) 运行环境 ==")
    say_ok("python %s" % sys.version.split()[0], sys.executable)
    say_ok("服务器 %s v%s" % (SERVER_NAME, SERVER_VERSION))
    if os.name == "nt":
        say_ok("PYTHONUTF8=%s  PYTHONIOENCODING=%s"
               % (os.environ.get("PYTHONUTF8") or "(未设)",
                  os.environ.get("PYTHONIOENCODING") or "(未设)"),
               "两者都由 dsh-h3clab 插件自动设置，用于避免 cp936 乱码")

    # ---------- 2) 配置 ----------
    lines.append("")
    lines.append("== 2) 配置 ==")
    source = _text(CONFIG.get("config_source") or "")
    if source == "(内置默认值)":
        say_warn("配置来源：内置默认值",
                 "没读到任何配置文件。走 dsh-h3clab 插件时这是异常——"
                 "插件应当把 H3C_MCP_CONFIG 指到 <DSH_HOME>\\h3clab\\server-config.json。"
                 "手工跑时用 `--show-config` 看生效值。")
    else:
        say_ok("配置来源：%s" % source)
    say_ok("host=%s" % CONFIG["host"], "只连本机，绝不扫局域网")

    ports = effective_ports()
    origin = _ports_origin()
    if ports:
        say_ok("探测端口 %d 个：%s" % (len(ports), _ports_spec(ports)), origin)
    else:
        say_bad("探测端口为空", origin)

    devices = CONFIG.get("devices") or {}
    if devices:
        say_ok("设备名映射 %d 条" % len(devices), "、".join(list(devices)[:8]))
    else:
        say_warn("设备名映射为空",
                 "plan/links 里只写 name 时无法解析端口。实测 HCL 出厂配置下所有设备提示符都是 H3C，"
                 "重名会被拒绝——建议直接用 port。")

    # ---------- 3) 拓扑 ----------
    lines.append("")
    lines.append("== 3) 拓扑 ==")
    try:
        net_path = _find_net_file(args.get("net_file"))
    except Exception as exc:
        net_path = None
        say_bad("读取拓扑时异常", "%s: %s" % (type(exc).__name__, exc))
    if net_path is None:
        configured_net = _text(CONFIG.get("net_file") or "").strip()
        if configured_net:
            say_bad("配置的 net_file 不存在", configured_net)
        else:
            say_warn("未配置 net_file",
                     "hcl_topology 会直接报错；端口也无法从拓扑推导，当前用的是 %s" % origin)
    else:
        devs = parse_net(net_path)
        if devs:
            net_ports = sorted({d.port for d in devs.values() if d.port})
            say_ok("拓扑 %s" % net_path,
                   "%d 台设备、控制台端口 %s" % (len(devs), _ports_spec(net_ports)))
        else:
            say_bad("拓扑解析不出设备", "%s（确认是 HCL 的 .net）" % net_path)

    # ---------- 4) 目录 ----------
    lines.append("")
    lines.append("== 4) 目录 ==")
    refs = _references_dir()
    if refs.is_dir():
        present = [name for name in ("cases.md", "gotchas.md", "aliases.md") if (refs / name).is_file()]
        if present:
            say_ok("记忆库 %s" % refs, "、".join(present))
        else:
            say_warn("记忆库目录在，但 cases/gotchas/aliases.md 一个都没有", str(refs))
    else:
        say_warn("记忆库目录不存在", "%s（hcl_search_memory 会报错；配置项 references_dir）" % refs)

    evidence = Path(CONFIG["evidence_root"])
    try:
        probe = evidence / ("_doctor-probe-%s" % _stamp())
        probe.mkdir(parents=True, exist_ok=True)
        (probe / ".keep").write_text("ok\n", encoding="utf-8")
        shutil.rmtree(probe, ignore_errors=True)
        say_ok("证据目录可写 %s" % evidence)
    except OSError as exc:
        say_bad("证据目录不可写", "%s -> %s（配置项 evidence_root）" % (evidence, exc))

    state_dir = Path(CONFIG["state_dir"])
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        say_ok("状态目录 %s" % state_dir, "配置快照与 lab 状态文件放这里")
    except OSError as exc:
        say_bad("状态目录不可用", "%s -> %s（配置项 state_dir）" % (state_dir, exc))

    # ---------- 5) 设备可达性 ----------
    lines.append("")
    lines.append("== 5) 设备可达性 ==")
    if not _truthy(args.get("probe"), True):
        say_warn("已跳过设备探测", "probe=false")
    elif not ports:
        say_bad("没有可探测的端口")
    else:
        prompt_timeout = float(args.get("prompt_timeout") or 8.0)
        workers = max(1, min(10, int(args.get("workers") or 10), len(ports)))
        results: dict[int, dict] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_probe_identity, p, False, prompt_timeout): p for p in ports}
            for fut in concurrent.futures.as_completed(futures):
                port = futures[fut]
                try:
                    results[port] = fut.result()
                except Exception as exc:
                    results[port] = {"port": port, "state": "DOWN",
                                     "error": "%s: %s" % (type(exc).__name__, exc)}
        up = sorted(p for p in results if results[p].get("state") == "UP")
        down = sorted(p for p in results if results[p].get("state") != "UP")
        if up:
            names = sorted({(results[p].get("hostname") or "-") for p in up})
            say_ok("%d/%d 台可达" % (len(up), len(results)),
                   "端口 %s；主机名 %s" % (_ports_spec(up), "、".join(names[:6])))
            if len(names) == 1 and len(up) > 1:
                say_warn("所有可达设备主机名都是 %r" % names[0],
                         "HCL 出厂配置下的默认值；按主机名寻址会因重名被拒绝，请直接用 port")
        else:
            say_bad("0/%d 台可达" % len(results),
                    "HCL 没启动，或拓扑还没点『启动』（HCL 无 API，这一步只能人工点）")
        if down:
            first = results[down[0]].get("error") or "-"
            say_warn("不可达 %d 个：%s" % (len(down), _ports_spec(down)), "例如 %s：%s" % (down[0], first))

    # ---------- 结论 ----------
    lines.append("")
    lines.append("== 结论 ==")
    if problems:
        lines.append("  ❌ %d 项失败、%d 项告警。" % (len(problems), len(warnings)))
        for item in problems:
            lines.append("     ❌ %s" % item)
        for item in warnings:
            lines.append("     ⚠️  %s" % item)
        lines.append("  建议先修第一项失败，再重跑 hcl_doctor。")
    elif warnings:
        lines.append("  ✅ 没有硬失败；%d 项告警（多数情况下仍可正常用）：" % len(warnings))
        for item in warnings:
            lines.append("     ⚠️  %s" % item)
    else:
        lines.append("  ✅ 全绿：环境、配置、拓扑、目录、设备都正常。")
    return "\n".join(lines)


# ==========================================================================
# hcl_cfgdiff —— 配置快照与逐行对比
# ==========================================================================

#: 快照文件名里不允许出现的字符：Windows 保留字符 + 控制字符。
#: ★ 刻意**不**把非 ASCII 一起干掉。早先的版本用 `[^0-9A-Za-z_.\-]+` 把所有非 ASCII
#: 都替换成下划线再 strip，于是"模拟终端"变成空串、回落成 `device` —— 两台中文名设备
#: 会共用同一个快照前缀，`diff` 不传 `against` 时就会拿**另一台设备**的快照当基线。
#: 静默比错基线，比"文件名里有中文可能踩编码坑"危险得多。
_SNAPSHOT_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_SNAPSHOT_SPACE_RE = re.compile(r"\s+")
#: Windows 保留设备名（不区分大小写）——设备真叫 CON/NUL 这类名字时不能直接当文件名。
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % i for i in range(1, 10)]
    + ["LPT%d" % i for i in range(1, 10)]
)


def _snapshot_key(name: str) -> str:
    """把设备名变成安全的文件名片段（保留中文等非 ASCII 字符）。"""
    cleaned = _SNAPSHOT_UNSAFE_RE.sub("_", (name or "").strip())
    cleaned = _SNAPSHOT_SPACE_RE.sub("_", cleaned).strip(" ._")
    if cleaned.upper() in _WINDOWS_RESERVED:
        cleaned += "_"
    return cleaned[:40] or "device"


def _capture_running_config(port: int, timeout: float, max_chars: int) -> str:
    """连一台设备把 `display current-configuration` 读完整。"""
    con = _connect(port, timeout=max(25.0, timeout))
    try:
        _to_user_view(con, timeout)
        try:
            _silence_terminal(con, timeout)
        except (ConsoleError, ConsoleTimeout):
            pass
        out = con.command("display current-configuration", timeout=max(60.0, timeout))
    finally:
        con.close()
    text, _cut = _truncate(out, max_chars)
    return text


def tool_hcl_cfgdiff(args: dict) -> str:
    """配置快照 + 对比：存基线、列出快照、与基线做逐行 diff。

    设备侧只读（只跑 `display current-configuration`）；快照文件写在
    `<state_dir>/snapshots/` 下。
    """
    action = _text(args.get("action") or "diff").strip().lower()
    name = _text(args.get("name") or "").strip()
    timeout = float(args.get("timeout") or 60.0)
    max_chars = int(args.get("max_chars") or 200_000)
    try:
        snap_dir = _snapshots_dir()
    except RuntimeError as exc:
        return "错误：%s" % exc

    # ---------- list ----------
    if action == "list":
        prefix = ("%s-" % _snapshot_key(name)) if name else ""
        files = sorted((p for p in snap_dir.glob("*.cfg") if p.name.startswith(prefix)),
                       key=lambda p: p.stat().st_mtime)
        if not files:
            return ("快照目录 %s 里%s没有 .cfg 快照。\n先用 action=snapshot 存一份基线。"
                    % (snap_dir, ("以 %s 开头的" % prefix) if prefix else ""))
        out = ["快照目录: %s" % snap_dir, "共 %d 个：" % len(files)]
        for p in files:
            out.append("  %-44s %8d 字节  %s" % (
                p.name, p.stat().st_size,
                datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")))
        return "\n".join(out)

    if action not in ("snapshot", "diff"):
        return "错误：action 只能是 snapshot / diff / list，收到 %r。" % action

    if args.get("port") is None:
        return "错误：action=%s 需要 port（HCL 控制台端口）。" % action
    try:
        port = int(args["port"])
    except (TypeError, ValueError):
        return "错误：port 必须是整数，收到 %r。" % args.get("port")
    key = _snapshot_key(name or ("port-%d" % port))

    # ---------- snapshot ----------
    if action == "snapshot":
        try:
            text = _capture_running_config(port, timeout, max_chars)
        except Exception as exc:
            return "错误：读取端口 %d 的 current-configuration 失败：%s: %s" % (port, type(exc).__name__, exc)
        path = snap_dir / ("%s-%s.cfg" % (key, _stamp()))
        path.write_text(text, encoding="utf-8")
        return ("已存快照: %s\n  端口 %d，%d 行 / %d 字节，sha1=%s\n"
                "  之后用 action=diff 与它对比（不传 against 时自动选该前缀下最新的一份做基线）。"
                % (path, port, text.count("\n") + 1, len(text.encode("utf-8")),
                   hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]))

    # ---------- diff ----------
    against = _text(args.get("against") or "").strip()
    if against:
        base_path = Path(against)
        if not base_path.is_file():
            base_path = snap_dir / against
        if not base_path.is_file():
            return "错误：找不到基线快照 %s（也不在 %s 里）。" % (against, snap_dir)
    else:
        candidates = sorted(snap_dir.glob("%s-*.cfg" % key), key=lambda p: p.stat().st_mtime)
        if not candidates:
            return ("错误：还没有 %s 的基线快照。\n先跑一次 action=snapshot（例如 {port:%d, name:%r}）。"
                    % (key, port, name or ("port-%d" % port)))
        base_path = candidates[-1]

    base_lines = base_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    try:
        now_text = _capture_running_config(port, timeout, max_chars)
    except Exception as exc:
        return "错误：读取端口 %d 的 current-configuration 失败：%s: %s" % (port, type(exc).__name__, exc)
    now_lines = now_text.splitlines()

    diff = list(difflib.unified_diff(base_lines, now_lines, fromfile=base_path.name,
                                     tofile="now(port %d)" % port, lineterm="", n=2))
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    head = ["与基线对比: %s" % base_path,
            "  基线 %d 行 -> 现在 %d 行；新增 %d 行，删除 %d 行" % (len(base_lines), len(now_lines), added, removed)]
    if not diff:
        head.append("  ✅ 没有差异（配置与基线一致）。")
        return "\n".join(head)
    head.append("  （统一 diff：`-` 基线里有、现在没有；`+` 是现在新增。上下文 2 行。）")
    head.append("")
    return "\n".join(head + diff)


# ==========================================================================
# hcl_lab_state —— lab 状态文件读写
# ==========================================================================

def tool_hcl_lab_state(args: dict) -> str:
    """读写 lab 状态 JSON，用来在多次调用之间记住"当前做到哪一步"。

    写操作每次都会先备份到 `<状态文件>.bak-<时间戳>`。
    """
    action = _text(args.get("action") or "get").strip().lower()
    try:
        target = _state_file(args.get("path"))
    except RuntimeError as exc:
        return "错误：%s" % exc

    if action == "history":
        backups = sorted(target.parent.glob(target.name + ".bak-*"))
        if not backups:
            return "没有历史备份。set / merge / delete 每次都会先备份到 <状态文件>.bak-<时间戳>。"
        out = ["状态文件: %s" % target, "历史备份 %d 个：" % len(backups)]
        for p in backups[-20:]:
            out.append("  %-50s %9d 字节" % (p.name, p.stat().st_size))
        return "\n".join(out)

    if action == "get":
        if not target.is_file():
            return "状态文件还不存在: %s\n（用 action=set / merge 创建；写入前会自动备份。）" % target
        try:
            data = _load_json_file(target, {})
        except RuntimeError as exc:
            return "错误：%s" % exc
        return "状态文件: %s\n%s" % (target, json.dumps(data, ensure_ascii=False, indent=2))

    if action not in ("set", "merge", "delete"):
        return "错误：action 只能是 get / set / merge / delete / history，收到 %r。" % action

    try:
        current = _load_json_file(target, {})
    except RuntimeError as exc:
        return "错误：%s" % exc
    if not isinstance(current, dict):
        return "错误：状态文件顶层不是 JSON 对象，拒绝改写：%s" % target

    if action == "delete":
        keys = args.get("key")
        if isinstance(keys, str):
            keys = [k.strip() for k in keys.split(",") if k.strip()]
        if not isinstance(keys, list) or not keys:
            return "错误：action=delete 需要 key（字符串或字符串数组）。"
        removed = [str(k) for k in keys if str(k) in current]
        for key in removed:
            current.pop(key, None)
        new_data = current
        summary = "删除 %d 个键：%s" % (len(removed), "、".join(removed) if removed else "（原本都不存在）")
    else:
        data = args.get("data")
        if not isinstance(data, dict):
            return "错误：action=%s 需要 data（JSON 对象）。" % action
        new_data = dict(current) if action == "merge" else {}
        new_data.update(data)
        summary = "%s %d 个键" % ("合并" if action == "merge" else "写入", len(data))

    note = _text(args.get("note") or "").strip()
    if note:
        new_data["_note"] = note
    new_data["_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    backup = _backup_file(target)
    try:
        _write_json_file(target, new_data)
    except OSError as exc:
        return "错误：写状态文件失败：%s -> %s" % (target, exc)
    out = ["状态文件: %s" % target,
           "%s；%s" % (summary, ("已备份到 %s" % backup.name) if backup else "（新建，无备份）")]
    out.append("")
    out.append(json.dumps(new_data, ensure_ascii=False, indent=2))
    return "\n".join(out)


# ==========================================================================
# hcl_report —— 汇总成 Markdown 报告
# ==========================================================================

#: 报告默认包含哪些小节。
REPORT_SECTIONS = ("topology", "devices", "links", "verify", "state")


def tool_hcl_report(args: dict) -> str:
    """把拓扑 / 设备可达性 / 链路 / 验证 / lab 状态汇成一份 Markdown 报告并存盘。

    设备侧只读。报告默认写到 `<evidence_root>/<时间戳>/report.md`。
    """
    title = _text(args.get("title") or "").strip()
    raw_sections = _text(args.get("sections") or "").strip()
    wanted = [s.strip().lower() for s in raw_sections.split(",") if s.strip()] or list(REPORT_SECTIONS)
    unknown = [s for s in wanted if s not in REPORT_SECTIONS]
    if unknown:
        return "错误：不认识的 sections：%s（可选 %s）。" % ("、".join(unknown), "、".join(REPORT_SECTIONS))

    out_arg = _text(args.get("out") or "").strip()
    out_path = Path(out_arg) if out_arg else (Path(CONFIG["evidence_root"]) / _stamp() / "report.md")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return "错误：无法创建报告目录 %s：%s" % (out_path.parent, exc)

    body: list[str] = []
    body.append("# %s" % (title or "H3C 实验报告"))
    body.append("")
    body.append("- 生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    body.append("- 控制台 host：%s（只连本机）" % CONFIG["host"])
    body.append("- 配置来源：%s" % CONFIG.get("config_source"))
    body.append("- 端口来源：%s" % _ports_origin())
    body.append("")

    for name in wanted:
        if name == "topology":
            body.append("## 拓扑")
            body.append("")
            body.append("```")
            body.append(tool_hcl_topology({"net_file": args.get("net_file")}))
            body.append("```")
            body.append("")
        elif name == "devices":
            body.append("## 设备可达性")
            body.append("")
            body.append("```")
            body.append(tool_hcl_list_devices({
                "ports": args.get("ports"),
                "model": _truthy(args.get("model"), False),
                "prompt_timeout": args.get("prompt_timeout") or 8.0
            }))
            body.append("```")
            body.append("")
        elif name == "links":
            links = args.get("links")
            body.append("## 链路状态")
            body.append("")
            if not isinstance(links, list) or not links:
                body.append("（未提供 `links`，跳过。链路需要显式给出本端/对端端口与接口名，"
                            "因为 .net 里的 `GE_0/1` 与 Comware 的 `GE1/0/1` 之间隔着 IRF 成员号，"
                            "自动换算会猜错。）")
                body.append("")
            else:
                body.append("```")
                body.append(tool_hcl_link_watch({"links": links, "timeout": args.get("timeout") or 25.0}))
                body.append("```")
                body.append("")
        elif name == "verify":
            checklist = _text(args.get("checklist_json") or "").strip()
            body.append("## 验证矩阵")
            body.append("")
            if not checklist:
                body.append("（未提供 `checklist_json`，跳过。）")
                body.append("")
            else:
                body.append("```")
                body.append(tool_hcl_verify({
                    "checklist_json": checklist,
                    "only": args.get("only"),
                    "timeout": args.get("timeout") or 15.0
                }))
                body.append("```")
                body.append("")
        elif name == "state":
            body.append("## Lab 状态")
            body.append("")
            try:
                state_path = _state_file(args.get("state"))
            except RuntimeError as exc:
                body.append("（%s）" % exc)
                body.append("")
                continue
            if not state_path.is_file():
                body.append("（状态文件不存在：%s）" % state_path)
                body.append("")
            else:
                try:
                    data = _load_json_file(state_path, {})
                except RuntimeError as exc:
                    body.append("（%s）" % exc)
                    body.append("")
                    continue
                body.append("来源：`%s`" % state_path)
                body.append("")
                body.append("```json")
                body.append(json.dumps(data, ensure_ascii=False, indent=2))
                body.append("```")
                body.append("")

    text = "\n".join(body).rstrip() + "\n"
    try:
        out_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return "错误：写报告失败：%s -> %s\n\n%s" % (out_path, exc, text)
    return "报告已写入: %s（%d 字节）\n包含小节：%s\n\n%s" % (
        out_path, len(text.encode("utf-8")), "、".join(wanted), text)


# ==========================================================================
# hcl_memory_write —— 把经验写回记忆库
# ==========================================================================

_CASE_ID_RE = re.compile(r"^\|\s*(C-\d+)\s*\|", re.M)
_GOTCHA_SECTION_RE = re.compile(r"^##\s+(.*)$", re.M)


def _next_case_id(text: str) -> str:
    """按现有索引表算出下一个 C-00N。"""
    numbers = [int(m.group(1)[2:]) for m in _CASE_ID_RE.finditer(text)]
    return "C-%03d" % ((max(numbers) + 1) if numbers else 1)


def _insert_index_row(text: str, row: str) -> tuple[str, bool]:
    """把一行插到 `## 索引` 表格的最后一条数据行之后。"""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == "## 索引":
            start = index
            break
    if start is None:
        return text, False
    last = None
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("## "):
            break
        if stripped.startswith("|") and not set(stripped) <= set("|-: "):
            last = index
    if last is None:
        return text, False
    lines.insert(last + 1, row)
    joined = "\n".join(lines)
    if text.endswith("\n"):
        joined += "\n"
    return joined, True


def tool_hcl_memory_write(args: dict) -> str:
    """把这次踩到的经验**追加**写回记忆库（`cases.md` 或 `gotchas.md`）。

    ★ 默认 `dry_run=true`：只打印"将要追加什么"，不碰文件。
      真要写入必须显式 `dry_run:false`。写入前会自动备份成 `<文件>.bak-<时间戳>`。
    只追到已有文件里，不会替你新建知识库（免得格式和你现有的不一致）。
    """
    kind = _text(args.get("kind") or "case").strip().lower()
    if kind not in ("case", "gotcha"):
        return "错误：kind 只能是 case / gotcha，收到 %r。" % kind
    title = _text(args.get("title") or "").strip()
    body_text = _text(args.get("body") or "").strip()
    if not title:
        return "错误：缺少 title。"
    if not body_text:
        return "错误：缺少 body（Markdown 正文）。"

    refs = _references_dir()
    explicit_file = _text(args.get("file") or "").strip()
    target = Path(explicit_file) if explicit_file else (refs / ("cases.md" if kind == "case" else "gotchas.md"))
    if not target.is_file():
        return ("错误：目标文件不存在：%s\n"
                "  记忆库目录（配置项 references_dir / referencesDir）默认是\n"
                "  <DSH_HOME>\\skills\\h3c-lab-automation\\references。\n"
                "  本工具只**追加**到已有文件，不会替你新建知识库。" % target)

    original = target.read_text(encoding="utf-8-sig", errors="replace")
    date = _text(args.get("date") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    tags = _text(args.get("tags") or "").strip()
    one_line = _text(args.get("one_line") or "").strip()
    identifier = _text(args.get("id") or "").strip()
    notes: list[str] = []

    if kind == "case":
        identifier = identifier or _next_case_id(original)
        if re.search(r"^\|\s*%s\s*\|" % re.escape(identifier), original, re.M):
            return "错误：索引表里已经有 %s 了；换一个 id，或先手工确认。" % identifier
        row = "| %s | %s | %s | %s | %s |" % (identifier, date, title, one_line or "（见正文）", tags)
        updated, inserted = _insert_index_row(original, row)
        if not inserted:
            return ("错误：没在 %s 里找到 `## 索引` 下的表格，拒绝改写（怕破坏格式）。\n"
                    "  请手工把这一行加进索引表：\n%s" % (target, row))
        section = "## %s %s\n\n%s\n" % (identifier, title, body_text)
    else:
        heading = "### %s「%s」\n\n%s\n" % (identifier, title, body_text) if identifier \
            else "### 「%s」\n\n%s\n" % (title, body_text)
        cls = _text(args.get("section") or "").strip()
        if cls:
            section = "## %s\n\n%s" % (cls, heading)
        else:
            section = heading
            found = _GOTCHA_SECTION_RE.findall(original)
            if found:
                notes.append("未指定 section，新条目会落在文件末尾、最后那个 `## %s` 小节之下。"
                             "想归到特定分类请传 section（现有分类：%s）。"
                             % (found[-1], "、".join(found[:6])))
            else:
                notes.append("目标文件里没有 `## 分类` 小节，已直接追加到文件末尾。")
        updated = original

    if not updated.endswith("\n"):
        updated += "\n"
    updated += "\n---\n\n" + section

    if _truthy(args.get("dry_run"), True):
        return ("[预演 dry_run] 没有写文件。真要写入请显式传 dry_run:false。\n"
                "目标: %s\n"
                "预览（将追加的片段）:\n%s\n%s"
                % (target, "-" * 62, section))

    backup = _backup_file(target)
    try:
        target.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return "错误：写 %s 失败：%s" % (target, exc)
    out = ["已写入 %s" % target,
           "  追加 %s%s" % (kind, (" " + identifier) if identifier else ""),
           "  备份 %s" % (backup.name if backup else "(无，原文件不存在)"),
           "  文件 %d -> %d 字节" % (len(original.encode("utf-8")), len(updated.encode("utf-8")))]
    out += ["  " + note for note in notes]
    out.append("  提示：写完后用 hcl_search_memory 搜一下新条目的关键词，确认能被检索到。")
    return "\n".join(out)


# --------------------------------------------------------------------------
# MCP 工具定义
# --------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "hcl_list_devices",
        "description": (
            "并发探测 HCL（H3C Cloud Lab）设备的 telnet 控制台，返回每个端口的"
            "主机名 / 型号 / 是否可达。只读，无副作用。\n"
            "参数：ports（可选，整数数组。不传则按【显式配置的 ports > 从拓扑 .net 的 "
            "device_id 推导（控制台端口 = 30000 + device_id）> 内置兜底 30001..30010】"
            "的顺序决定，输出末尾会写明这次用的是哪一种）；"
            "model（可选 bool，默认 true，是否跑 display version 取型号）；"
            "prompt_timeout（可选 float，默认 25，等提示符的超时）；workers（可选 int，最多 10）。\n"
            "返回：每台一行 `端口 主机名 型号 UP|DOWN`，随后列出连不上的端口及原始错误，"
            "再给出可达合计、端口来源。单台失败不影响其他端口。\n"
            "注意：HCL 出厂配置下所有设备提示符都是默认的 H3C，主机名不具区分度。"
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
            "参数：net_file（可选，.net 文件路径；不传则用配置里的 net_file。\n"
            "★ 两者都没有时**直接报错，不会去猜**：同一个 D:\\NET 下有十几个不同实验的 .net，"
            "按时间挑最新一个会把 A 套的设备表贴到 B 套的验证上）。\n"
            "返回：设备表（名称 / 型号 / device_id / 控制台端口=30000+device_id）与连线表"
            "（`本端设备 本端端口 <-> 对端设备 对端端口`）。"
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
    {
        "name": "hcl_doctor",
        "description": (
            "环境自检：一条命令定位“为什么连不上 / 为什么改了没生效”。只读。\n"
            "依次检查：python 与编码环境、配置来源（含「配置到底来自哪个文件」）、探测端口与来源、"
            "设备名映射、拓扑 .net 是否存在且可解析、记忆库与证据/状态目录、以及各控制台的可达性。\n"
            "参数：ports（可选，覆盖探测端口）；net_file（可选）；probe（可选 bool，默认 true，"
            "是否真的去连控制台）；prompt_timeout（默认 8）；workers（默认 10）。\n"
            "返回：分节报告（✅/⚠️/❌）+ 结论 + 建议先修哪一项。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ports": {"type": "array", "items": {"type": "integer"},
                          "description": "要探测的控制台端口；不传则按配置/拓扑推导"},
                "net_file": {"type": "string", "description": "HCL .net 拓扑文件路径"},
                "probe": {"type": "boolean", "description": "是否真的连控制台探测（默认 true）"},
                "prompt_timeout": {"type": "number", "description": "等待提示符的秒数，默认 8"},
                "workers": {"type": "integer", "description": "并发线程数，1..10，默认 10"},
            },
        },
    },
    {
        "name": "hcl_cfgdiff",
        "description": (
            "配置快照与对比：把 `display current-configuration` 存成基线，之后与它逐行 diff。"
            "设备侧**只读**；快照写在 <state_dir>/snapshots/ 下。\n"
            "参数：action（snapshot | diff | list，默认 diff）；port（snapshot/diff 必填）；"
            "name（设备名，用于快照文件名，默认 port-<端口>）；against（对比哪一份基线快照，"
            "默认自动选该前缀下最新的一份）；timeout（默认 60）；max_chars（默认 200000）。\n"
            "返回：list 列出快照；snapshot 给出路径/行数/字节/sha1；diff 给出统一 diff 与增删行数。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["snapshot", "diff", "list"],
                           "description": "snapshot 存基线 / diff 对比 / list 列出快照"},
                "port": {"type": "integer", "description": "HCL 控制台端口，例如 30008"},
                "name": {"type": "string", "description": "设备名，用于快照文件名"},
                "against": {"type": "string", "description": "基线快照文件名或绝对路径"},
                "timeout": {"type": "number", "description": "命令超时秒数，默认 60"},
                "max_chars": {"type": "integer", "description": "回显截断字符数，默认 200000"},
            },
        },
    },
    {
        "name": "hcl_lab_state",
        "description": (
            "读写 lab 状态 JSON（默认 <state_dir>/lab-state.json），用来在多次调用之间记住"
            "“做到哪一步了”。写操作每次先备份到 <状态文件>.bak-<时间戳>。\n"
            "参数：action（get | set | merge | delete | history，默认 get）；path（可选，换一个状态文件）；"
            "data（set/merge 用，JSON 对象）；key（delete 用，字符串或数组）；note（可选，写入 _note 字段）。\n"
            "返回：get 给出当前内容；set/merge/delete 给出改动摘要 + 备份文件名 + 新内容；history 列出备份。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["get", "set", "merge", "delete", "history"],
                           "description": "默认 get"},
                "path": {"type": "string", "description": "状态文件路径；默认 <state_dir>/lab-state.json"},
                "data": {"type": "object", "description": "set/merge 要写入的对象"},
                "key": {"description": "delete 要删的键，字符串（逗号分隔）或字符串数组"},
                "note": {"type": "string", "description": "可选备注，写入 _note"},
            },
        },
    },
    {
        "name": "hcl_report",
        "description": (
            "把拓扑 / 设备可达性 / 链路 / 验证矩阵 / lab 状态汇成一份 Markdown 报告并存盘。"
            "设备侧只读；报告默认写到 <evidence_root>/<时间戳>/report.md。\n"
            "参数：title；out（输出路径）；sections（逗号分隔，默认全选 topology,devices,links,verify,state）；"
            "net_file；ports；links（links 小节需要，不传就跳过并说明原因）；"
            "checklist_json 与 only（verify 小节需要）；state（状态文件路径）；"
            "model / prompt_timeout（设备小节）；timeout。\n"
            "返回：报告文件路径 + 完整报告正文。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "报告标题"},
                "out": {"type": "string", "description": "报告输出路径（默认 evidence 下的时间戳目录）"},
                "sections": {"type": "string",
                             "description": "逗号分隔：topology,devices,links,verify,state（默认全部）"},
                "net_file": {"type": "string", "description": ".net 拓扑路径"},
                "ports": {"type": "array", "items": {"type": "integer"}, "description": "设备探测端口"},
                "links": {"type": "array", "items": {"type": "object"}, "description": "链路数组"},
                "checklist_json": {"type": "string", "description": "验证清单文件路径"},
                "only": {"type": "string", "description": "验证项 id 前缀，逗号分隔"},
                "state": {"type": "string", "description": "lab 状态文件路径"},
                "model": {"type": "boolean", "description": "设备小节是否读型号（默认 false，更快）"},
                "prompt_timeout": {"type": "number", "description": "等提示符秒数，默认 8"},
                "timeout": {"type": "number", "description": "命令超时秒数"},
            },
        },
    },
    {
        "name": "hcl_memory_write",
        "description": (
            "把这次踩到的经验**追加**写回记忆库（cases.md 或 gotchas.md）。\n"
            "★ 默认 dry_run=true：只打印“将要追加什么”，不碰文件；真要写入必须显式 dry_run:false。"
            "写入前自动备份成 <文件>.bak-<时间戳>。只追加到已有文件，不会替你新建知识库。\n"
            "参数：kind（case | gotcha，默认 case）；title（必填）；body（必填，Markdown 正文）；"
            "id（可选；case 默认自动分配 C-00N）；date（默认今天）；tags（关键词，写进 cases 索引表）；"
            "one_line（cases 索引表的“一句话症状”）；section（gotcha 的 ## 分类标题）；"
            "file（可选，直接指定目标文件）；dry_run（默认 true）。\n"
            "返回：dry_run 给出预览；真写入给出目标路径/备份名/字节变化，并提示用 hcl_search_memory 复验。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["case", "gotcha"], "description": "默认 case"},
                "title": {"type": "string", "description": "条目标题（case 用场景，gotcha 用症状）"},
                "body": {"type": "string", "description": "Markdown 正文"},
                "id": {"type": "string", "description": "条目 id；case 默认自动分配 C-00N"},
                "date": {"type": "string", "description": "日期，默认今天"},
                "tags": {"type": "string", "description": "关键词（逗号分隔），写进 cases 索引表"},
                "one_line": {"type": "string", "description": "cases 索引表的一句话症状"},
                "section": {"type": "string", "description": "gotcha 归属的 ## 分类标题"},
                "file": {"type": "string", "description": "直接指定目标文件"},
                "dry_run": {"type": "boolean", "description": "默认 true：只预览不写"},
            },
            "required": ["title", "body"],
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
    "hcl_doctor": tool_hcl_doctor,
    "hcl_cfgdiff": tool_hcl_cfgdiff,
    "hcl_lab_state": tool_hcl_lab_state,
    "hcl_report": tool_hcl_report,
    "hcl_memory_write": tool_hcl_memory_write,
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
