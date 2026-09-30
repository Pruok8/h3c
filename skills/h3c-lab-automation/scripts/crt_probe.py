#$language = "python"
#$interface = "1.0"

# crt_probe.py -- drive a real HCL device console from inside SecureCRT.
#
# Called as this session's logon script (Session Options -> Logon Actions), or
# run manually from the Script menu. It:
#   1. connects the tab to /TELNET 127.0.0.1 30001 if not already connected
#      (30001 is HCL's console for topo1-device1)
#   2. copes with both console states: sitting at <H3C>, or stuck in
#      auto-configuration ("press CTRL_C or CTRL_D to break")
#   3. runs read-only commands and captures output with ReadString()
#   4. writes everything to crt_output.txt next to this script
#      (override with the CRT_PROBE_OUT environment variable)
#
# Design note: the result file is (re)written from the finally block, and every
# failure lands in it as a traceback. A logon script that dies silently is
# undebuggable, so the script must always leave evidence behind.
#
# ASCII only: Python 2 wants an encoding cookie in the first two lines, and
# those are taken by the #$language / #$interface headers.

import codecs
import os
import time
import traceback

try:
    unicode                      # noqa: F821  (Python 2)
except NameError:                # pragma: no cover
    unicode = str

HOST = "127.0.0.1"
PORT = 30001
CONNECT_STRING = "/TELNET %s %d" % (HOST, PORT)
PROMPT = "<H3C>"
# 输出路径必须可迁移：默认写到本脚本所在目录；换终端后不用改代码。
# 需要换位置时设环境变量 CRT_PROBE_OUT。
try:
    _HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:                # 交互式粘贴执行时没有 __file__
    _HERE = os.getcwd()
OUT_PATH = os.environ.get("CRT_PROBE_OUT") or os.path.join(_HERE, "crt_output.txt")

COMMANDS = [
    "display version",
    "display clock",
    "display ip interface brief",
]

LOG = []


def U(value):
    """Coerce whatever the script API hands back into unicode text.

    Not everything is a string: tab.Index returns an int, Session.Connected a
    bool. Calling .decode() on those raises AttributeError, which is exactly
    how this script died the first time (line 49).
    """
    if isinstance(value, unicode):
        return value
    if not isinstance(value, str):
        return unicode(value)
    try:
        return value.decode("utf-8")
    except Exception:
        return value.decode("latin-1")


def flush():
    handle = codecs.open(OUT_PATH, "wb+", "utf-8")
    try:
        handle.write(U("\n".join(LOG)))
    finally:
        handle.close()


def safe(label, func, *args):
    """Run something optional; never let it kill the run."""
    try:
        return func(*args)
    except Exception:
        LOG.append("  %s unavailable: %s" % (label, traceback.format_exc().strip().splitlines()[-1]))
        return None


def wait_until_usable(tab):
    for attempt in range(6):
        idx = tab.Screen.WaitForStrings(
            [PROMPT,
             "Press ENTER to get started",
             "CTRL_C or CTRL_D to break"],
            15)
        LOG.append("  wait attempt %d -> matched index %d" % (attempt + 1, idx))
        if idx == 1:
            return True
        if idx == 2:
            tab.Screen.Send("\r")
        elif idx == 3:
            tab.Screen.Send("\x03")
            LOG.append("  sent Ctrl+C to break auto-configuration")
        else:
            tab.Screen.Send("\r")
    return False


def main():
    LOG.append("script started")
    flush()                                   # 先落盘：证明脚本确实被拉起来了

    tab = crt.GetScriptTab()                  # noqa: F821
    LOG.append("SecureCRT version : %s" % U(safe("crt.Version", lambda: crt.Version)))  # noqa: F821
    LOG.append("tab index         : %s" % U(safe("tab.Index", lambda: tab.Index)))
    LOG.append("already connected : %s" % U(tab.Session.Connected))

    if not tab.Session.Connected:
        LOG.append("connecting        : %s" % CONNECT_STRING)
        tab.Session.Connect(CONNECT_STRING)
        time.sleep(1.0)

    tab.Screen.IgnoreEscape = True
    tab.Screen.Synchronous = True

    ok = wait_until_usable(tab)
    LOG.append("reached <H3C>     : %s" % ok)
    flush()

    tab.Screen.Send("screen-length disable\r")
    tab.Screen.WaitForString(PROMPT, 10)

    for command in COMMANDS:
        tab.Screen.Send(command + "\r")
        result = tab.Screen.ReadString(PROMPT, 60)
        LOG.append("")
        LOG.append("--- $ %s ---" % command)
        LOG.append(U(result).strip())
        LOG.append("--- end %s ---" % command)
        flush()                               # 每条命令都落盘

    LOG.append("")
    LOG.append("securecrt script finished OK")


try:
    main()
except Exception:
    LOG.append("")
    LOG.append("!!! script failed !!!")
    LOG.append(traceback.format_exc())
finally:
    try:
        flush()
    except Exception:
        pass
