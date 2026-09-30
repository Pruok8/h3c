# SecureCRT 自动化（本机 8.7.2）

## 三条通道与结论

| 通道 | 结论 | 说明 |
|---|---|---|
| 外部 COM / ActiveX | ❌ 不存在 | 扫注册表无 ProgID，`Dispatch` 全部"无效的类字符串" |
| 命令行 `/SCRIPT` | ❌ 不支持 | 报"命令行上出现意外的参数 '/SCRIPT'"，尽管 CHM 里写了 |
| 命令行 `/F` `/S` `/T` `/TELNET` `/L` `/PASSWORD` `/LOG` `/ARG` | ✅ 可用 | 能开会话、能连设备 |
| **会话登录脚本（Logon script）** | ✅ **可用，是唯一的脚本注入方式** | 会话连上即执行 |
| GUI 读取/点击/截图 | ✅ 可用 | 枚举对话框控件文字、点按钮、PrintWindow 截图 |

## 可用的会话配置（照抄）

```
S:"Protocol Name"=Telnet
S:"Hostname"=127.0.0.1
D:"Port"=00007531                  ← 非 SSH 协议就是朴素的 D:"Port"（30001）
D:"Use Login Script"=00000000      ← Automate logon 必须【关】
D:"Use Script File"=00000001       ← Logon script【开】
S:"Script Filename V2"=<脚本绝对路径>
```

三个坑都别再踩：端口键写法 / Automate logon 必须关 / 两键不能都置 1。

## 生成并启动

```bash
# 从 SecureCRT 自己的 Default.ini 生成（按字节改，不碰字体等二进制字段）
python scripts/make_crt_session.py \
  "%APPDATA%\VanDyke\Config\Sessions\Default.ini" \
  D:\DSH\NET\hcl\crt_cfg  hcl-console

# 启动：/F 指定独立配置目录（该目录必须是已有配置的副本，否则会弹密码短语对话框）
Start-Process 'G:\Study\H3C\crt\SecureCRT\SecureCRT.exe' `
  -ArgumentList '/F','D:\DSH\NET\hcl\crt_cfg','/S','hcl-console'
```

要让**用户那个正在运行的实例**执行脚本，会话文件必须位于**它使用的配置目录**里
（默认 `%APPDATA%\VanDyke\Config\Sessions\`，需要一次写授权）。

## 脚本骨架（Python 2.7 —— 注意语法）

```python
#$language = "python"
#$interface = "1.0"
# 头两行是 SecureCRT 的指令，Python 2 的编码声明放不进去 —— 所以脚本【只写 ASCII】

import codecs, time, traceback

try:
    unicode                      # Python 2
except NameError:
    unicode = str

OUT = r"D:\DSH\NET\hcl\crt_output.txt"   # 产物写工作区（沙箱）
LOG = []

def U(value):
    """API 返回值类型不统一：tab.Index 是 int，Connected 是 bool。"""
    if isinstance(value, unicode):
        return value
    if not isinstance(value, str):
        return unicode(value)
    try:
        return value.decode("utf-8")
    except Exception:
        return value.decode("latin-1")

def flush():
    h = codecs.open(OUT, "wb+", "utf-8")
    try:
        h.write(U("\n".join(LOG)))
    finally:
        h.close()

def main():
    LOG.append("script started"); flush()      # 先落盘：证明脚本真的被拉起来了
    tab = crt.GetScriptTab()
    if not tab.Session.Connected:
        tab.Session.Connect("/TELNET 127.0.0.1 30001")
    tab.Screen.IgnoreEscape = True
    tab.Screen.Synchronous = True
    tab.Screen.WaitForStrings(["<H3C>", "Press ENTER to get started",
                               "CTRL_C or CTRL_D to break"], 15)
    tab.Screen.Send("display version\r")
    LOG.append(U(tab.Screen.ReadString("<H3C>", 60))); flush()

try:
    main()
except Exception:
    LOG.append(traceback.format_exc())
finally:
    try: flush()
    except Exception: pass
```

**必须写 `flush()` 到 `finally`**：登录脚本静默失败时，产物文件是唯一的证据。
`crt_probe.py` 就是这么写的，实测能跑通并抓到设备回显。

## 怎么验证它真的跑了

1. **产物文件**（最可靠）：脚本把回显写到工作区，读文件即可。
2. **窗口列举**：`python scripts/list_windows.py` 打印每个窗口的标题和子控件文字 —— SecureCRT 的报错对话框文字能直接读出来。
3. **截图**：`pwsh -File scripts/shot.ps1 -TitleMatch hcl-console -ProcessName SecureCRT -Out shot.png`，然后用图像查看。PrintWindow 不受遮挡影响。

`crt_hello.py`（最小验证脚本）已验证过"登录脚本会跑 + 能写哪些目录"。

## 事实备忘

- 脚本引擎是 **Python 2.7**：没有 f-string，`print` 是语句，`unicode` 存在。
- 内建示例在 `G:\Study\H3C\crt\SecureCRT\Scripts\`（`.py` 和 `.vbs` 各一套），遇到 API 不确定时**先读示例**。
- 官方帮助是 CHM：`hh.exe -decompile <目标目录> SecureCRT.chm` 可解出 HTML，比网上搜的版本参数表准。
