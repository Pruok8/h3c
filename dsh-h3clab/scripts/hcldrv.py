"""hcldrv.py - 极简 telnet 控制台驱动，目标：HCL 设备控制台 (127.0.0.1:30001+)。

本文件是 h3c-lab-automation 技能里 ``scripts/hcldrv.py`` 的**只读副本**，
为了让 h3c-lab-mcp 这个 MCP 包自包含（不依赖 skill 目录）。除本段说明外内容
与来源一致 —— 驱动逻辑要改请回到源头改，再重新复制一份过来。

Python 3.13 已移除 telnetlib，这里只实现 RFC854 中够用的部分：

  * 拒绝一切选项协商（收到 WILL -> 回 DONT，收到 DO -> 回 WONT）
  * 跳过子协商 SB..SE，处理 IAC IAC 转义，能容忍序列被 recv 边界截断
  * 剥离 ANSI 转义
  * 遇到 ``---- More ----`` 自动补空格，长输出不会被卡住
  * ``command()`` 下发一行并读到"新的"提示符，返回干净回显

典型用法::

    with Console(port=30001) as c:
        c.prep()                       # 等提示符 + 关分页
        print(c.command("display version"))
"""

from __future__ import annotations

import re
import socket
import time

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07]*\x07")
_MORE = re.compile(r"-{2,}\s*More\s*-{2,}", re.I)
# Comware 提示符：<H3C>  [H3C]  [H3C-GigabitEthernet1/0/1]
# 必须排除 Y/N 交互确认（如 "Continue? [Y/N]"），否则会被当成提示符提前返回，
# 后续命令就被当成确认答案吃掉了。
_PROMPT = re.compile(r"[<\[]((?![Yy]\s*/\s*[Nn])[^<>\[\]\r\n]{1,64})[>\]]\s*$")
# 交互确认提示
_CONFIRM = re.compile(r"\[[Yy]\s*/\s*[Nn]\]|\([Yy]\s*/\s*[Nn]\)|\[[Yy]es\s*/\s*[Nn]o\]")
# 空配设备连上时经常正卡在自动配置里，必须 Ctrl+C 打断
_AUTOCONFIG = re.compile(
    r"(?i)(automatic configuration is running"
    r"|press ctrl_?c|automatic configuration attempt"
    r"|not ready for automatic configuration)")
# 打断之后设备停在 "Press ENTER to get started."，要敲回车
_NEED_ENTER = re.compile(r"(?i)press enter to get started")



class ConsoleError(RuntimeError):
    """连接层错误（对端关闭、socket 异常）。"""


class ConsoleTimeout(ConsoleError):
    """等待超时；transcript 里带着已经收到的内容，方便排错。"""

    def __init__(self, msg: str, transcript: str = ""):
        super().__init__(msg)
        self.transcript = transcript


class Console:
    def __init__(self, host: str = "127.0.0.1", port: int = 30001,
                 timeout: float = 10.0, encoding: str = "utf-8",
                 more_key: bytes = b" ", debug: bool = False,
                 auto_confirm: bool = False):
        self.host, self.port, self.timeout = host, port, timeout
        self.encoding, self.more_key, self.debug = encoding, more_key, debug
        self.auto_confirm = auto_confirm
        self.sock: socket.socket | None = None
        self._raw = b""        # 跨 recv 边界残留的未完成 IAC 序列
        self.text = ""         # 已剥离控制字符的可见文本
        self.answered_more = 0
        self.auto_config_breaks = 0
        self.confirm_answers = 0

    # ---------------- 连接生命周期 ----------------
    def connect(self, timeout: float | None = None) -> "Console":
        """connect 的超时可以和后续读写超时不同（扫描端口时用短的）。"""
        self.sock = socket.create_connection((self.host, self.port),
                                             self.timeout if timeout is None else timeout)
        self.sock.settimeout(0.3)
        return self

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self) -> "Console":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self.sock is not None

    # ---------------- telnet 协议处理 ----------------
    def _strip_telnet(self, data: bytes) -> bytes:
        """剥离 IAC 序列；对 WILL/DO 回以 DONT/WONT。返回可见字节。"""
        buf = self._raw + data
        out, reply = bytearray(), bytearray()
        i, n = 0, len(buf)
        while i < n:
            b = buf[i]
            if b != IAC:
                out.append(b)
                i += 1
                continue
            if i + 1 >= n:          # 序列被截断，留到下一次 recv
                break
            cmd = buf[i + 1]
            if cmd == IAC:          # 转义的 0xFF
                out.append(IAC)
                i += 2
            elif cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= n:
                    break
                opt = buf[i + 2]
                if cmd == DO:
                    reply += bytes((IAC, WONT, opt))
                elif cmd == WILL:
                    reply += bytes((IAC, DONT, opt))
                i += 3
            elif cmd == SB:         # 子协商，整段跳过
                end = buf.find(bytes((IAC, SE)), i + 2)
                if end < 0:
                    break
                i = end + 2
            else:
                i += 2
        self._raw = buf[i:]
        if reply and self.sock is not None:
            self.sock.sendall(bytes(reply))
        return bytes(out)

    def _pump(self) -> bool:
        """非阻塞式收一轮数据；返回是否收到东西。"""
        if self.sock is None:
            raise ConsoleError("未连接")
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return False
        except OSError as exc:
            raise ConsoleError(f"recv 失败: {exc}") from exc
        if not data:
            raise ConsoleError("对端关闭了连接")
        clean = self._strip_telnet(data)
        if clean:
            text = clean.decode(self.encoding, "replace")
            text = _ANSI_OSC.sub("", _ANSI.sub("", text))
            self.text += text
            if self.debug:
                print(f"[recv] {text!r}")
        return True

    def _answer_more(self) -> bool:
        """命中分页标记就补一个空格，并把它从可见文本里摘掉。"""
        if _MORE.search(self.text):
            self.text = _MORE.sub("", self.text, count=1)
            assert self.sock is not None
            self.sock.sendall(self.more_key)
            self.answered_more += 1
            if self.debug:
                print("[more] -> space")
            return True
        return False

    def _answer_confirm(self) -> bool:
        """命中 [Y/N] 交互确认时自动答 Y（仅在 auto_confirm 打开时）。

        Comware 很多命令会先问一句再执行，例如
            Changing the system MAC address might flap the peer link ...
            Continue? [Y/N]:
        不答它，命令就不生效、提示符也不回来 —— M-LAG 那几条就是这么静默丢的。
        """
        if not self.auto_confirm:
            return False
        match = _CONFIRM.search(self.text)
        if not match:
            return False
        self.text = self.text[:match.start()] + self.text[match.end():]
        assert self.sock is not None
        self.sock.sendall(b"Y\r")
        self.confirm_answers += 1
        if self.debug:
            print("[confirm] -> Y")
        return True

    # ---------------- 读 ----------------
    def read_until(self, patterns, timeout: float | None = None,
                   start: int | None = None, answer_more: bool = True):
        """读到任一 pattern 命中为止，返回命中的 pattern。

        ``start`` 之前的文本不参与匹配 —— 这是避免误命中"上一个提示符"的关键。
        """
        pats = [re.compile(p) if isinstance(p, str) else p for p in patterns]
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        base = 0 if start is None else start
        while True:
            for pat in pats:
                if pat.search(self.text, base):
                    return pat
            if answer_more and self._answer_more():
                continue
            if self._answer_confirm():
                continue
            if time.monotonic() >= deadline:
                raise ConsoleTimeout("等待超时", self.text[-2000:])
            self._pump()

    def wait_prompt(self, timeout: float | None = None, start: int | None = None):
        return self.read_until([_PROMPT], timeout=timeout, start=start)

    # ---------------- 写 ----------------
    def send_raw(self, data: bytes) -> None:
        if self.sock is None:
            raise ConsoleError("未连接")
        if self.debug:
            print(f"[send] {data!r}")
        self.sock.sendall(data)

    def command(self, line: str, timeout: float | None = None) -> str:
        """下发一行命令，读到新的提示符，返回去掉回显和提示符的正文。"""
        if self.sock is None:
            raise ConsoleError("未连接")
        base = len(self.text)
        self.send_raw(line.encode(self.encoding) + b"\r")
        self.wait_prompt(timeout=timeout, start=base)

        chunk = self.text[base:].replace("\r\n", "\n").replace("\r", "\n")
        lines = chunk.split("\n")
        if lines and lines[0].strip() == line.strip():   # 去掉命令回显
            lines = lines[1:]
        if lines and _PROMPT.search(lines[-1]):          # 去掉结尾提示符
            lines = lines[:-1]
        return "\n".join(lines).strip("\n")

    # ---------------- 便捷入口 ----------------
    def prep(self, timeout: float | None = None) -> "Console":
        """把控制台弄到可用提示符，然后关掉分页。

        真实 HCL 设备（空配 S6850）连上时往往正卡在自动配置：

            Automatic configuration is running, press CTRL_C or CTRL_D to break.

        这时要发 Ctrl+C 打断；打断后设备停在 ``Press ENTER to get started.``，
        再敲回车才出 ``<H3C>``。这是假设备不会暴露、只有真机才有的行为。
        """
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ConsoleTimeout("等待提示符超时", self.text[-2000:])
            try:
                self.wait_prompt(timeout=min(2.0, left))
                break
            except ConsoleTimeout:
                pass
            tail = self.text[-400:]
            if _NEED_ENTER.search(tail):
                self.send_raw(b"\r")            # "Press ENTER to get started."
            elif _AUTOCONFIG.search(tail):
                self.send_raw(b"\x03")          # 打断自动配置
                self.auto_config_breaks += 1
            else:
                self.send_raw(b"\r")            # 唤醒可能睡着的控制台
            time.sleep(0.4)

        try:
            self.command("screen-length disable", timeout=timeout)
        except ConsoleTimeout:
            pass          # 有的设备/视图不支持，不影响后续
        return self

    def transcript(self) -> str:
        return self.text


def open_device(port: int, host: str = "127.0.0.1", prompt_timeout: float = 15.0,
                debug: bool = False) -> Console:
    """连上 HCL 设备控制台并等待可用提示符。"""
    con = Console(host=host, port=port, debug=debug).connect()
    try:
        con.prep(timeout=prompt_timeout)
    except Exception:
        con.close()
        raise
    return con
