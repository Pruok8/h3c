# h3c-lab-mcp

> 本目录现在是 **`dsh-h3c-lab` git 仓库的一部分**（2026-09-30 从 `D:\DSH\NET\h3c-lab-mcp`
> 迁入，为的是让 `server.py` 的真源也有版本历史）。历史证据文件仍在 `evidence\`（已被
> `.gitignore` 排除）。
>
> 它是 `dsh-h3clab` 插件里 `scripts\server.py` 的**真源**——两份副本由
> `node ..\dsh-h3clab\scripts\sync-all.mjs` 同步，插件自测里有一条断言直接比对两者。

把 H3C Cloud Lab（HCL）里的实验设备暴露成 **MCP（Model Context Protocol）工具**：一个纯 Python 3 标准库实现的 stdio MCP 服务器，让 Claude Desktop / Cursor / 其它 MCP 客户端可以直接“看设备、读回显、下发配置、跑验证、查踩坑记忆”。

> **声明：这是社区互操作工具，与 H3C（新华三）官方无关，也不受其支持。** 它只是在本机 `127.0.0.1` 上连 HCL 暴露的 telnet 控制台，用 `display` / `ping` 这类命令做只读取证（写操作需显式开启）。H3C、H3C Cloud Lab、Comware 是新华三的商标/产品，本项目与新华三无任何隶属或背书关系。

---

## 1. 工具清单

| 工具名 | 参数 | 只读 | 说明 |
|---|---|---|---|
| `hcl_list_devices` | `ports`(int[]，可选)、`model`(bool=true)、`prompt_timeout`(num=25)、`workers`(int≤10) | ✅ | 并发探测各控制台，每台一行 `端口 主机名 型号 UP\|DOWN`，失败的端口单独列出；单台失败不影响其他。**不传 `ports` 时端口来源**：显式配置的 `ports` > 从拓扑 `.net` 的 `device_id` 推导 > 内置兜底 `30001..30010`；输出末尾会写明用的是哪一种 |
| `hcl_topology` | `net_file`(path，可选) | ✅ | 解析 HCL 的 `.net` 拓扑：设备表（名称/型号/device_id/控制台端口=30000+device_id）与连线表；**不传且没配置 `net_file` 时直接报错，不会去猜**（同一个 `D:\NET` 下有十几个不同实验的 `.net`） |
| `hcl_run_command` | `port`(int，必填)、`command`(str，必填)、`timeout`(num=20)、`max_chars`(int=8000) | ✅ | 只读白名单：命令必须以 `display`/`show`/`ping`/`tracert`/`traceroute` 开头，否则拒绝；返回回显（超长截断并标注） |
| `hcl_get_facts` | `port`(int，必填)、`timeout`(num=30) | ✅ | 一次取 `display version` + `display clock` + `display device`，返回 ≤25 行的提炼摘要（型号/软件版本/运行时间/设备时间/单板） |
| `hcl_apply_plan` | `plan_json`(path，必填)、`only`(str)、`save`(bool=false)、`dry_run`(bool=**true**)、`timeout`(num=60)、`workers`(int≤5) | ⚠️ **写** | 按 plan.json 逐条下发并检出 Comware 报错；**默认 dry_run 只预演**，必须显式 `dry_run:false` 才碰设备；完整回显写 `<evidence_root>\<时间戳>\<设备>.txt` |
| `hcl_verify` | `checklist_json`(path，必填)、`only`(str，逗号分隔 id 前缀)、`timeout`(num=15) | ✅ | 验证矩阵：`expect` 正则全命中才 PASS、`expect_not` 一个都不许命中、每项可带 `timeout`、同 `port` 复用连接；每项一行 `PASS/FAIL id desc \| 证据片段` + 合计行 |
| `hcl_search_memory` | `keywords`(str \| str[]，必填)、`any`(bool=false)、`max`(int=5)、`max_lines`(int=20) | ✅ | 在 `references/cases.md`、`gotchas.md`、`aliases.md` 里检索：同义词扩展（`aliases.md` 每行 `\|` 分隔的词互为同义词，组内任一命中即算命中，组间 AND），返回小节标题 + 命中行 + 文件行号；`references_dir` 不存在时给出可读错误 |
| `hcl_link_watch` | `links`(array，必填)、`timeout`(num=25) | ✅ | 用 `display interface brief` 判断每条链路两端第 2 列状态，每行 `name intf 本端 UP\|DOWN\|ADM \| 对端 ...` + 合计；**对端名字以实测提示符为准**，配置名只作兜底并标注「配置名，非实测」，两者不一致时列告警；`ADM` = 人工 shutdown 不算故障 |

`links` 元素形如：

```json
{"name": "SW1", "port": 30008, "intf": "GE1/0/20", "peer_port": 30001, "peer_intf": "GE0/0"}
```

`port` / `peer_port` 可省略：会先查配置里的 `devices` 映射，再按提示符主机名扫描端口。
**重名时报错并列出全部候选，绝不"随便挑一台"**——HCL 出厂配置下所有设备提示符都是
默认的 `H3C`，老实现"命中即返回第一台"等于随机挑一台设备下发配置。

---

## 2. 文件构成

| 文件 | 行数 | 作用 |
|---|---|---|
| `server.py` | ~2200 | MCP stdio 服务器 + 8 个工具的实现（手写 JSON-RPC，无第三方库） |
| `hcldrv.py` | ~240 | telnet 控制台驱动（IAC 协商、`---- More ----` 翻页、提示符识别、auto-config 打断），**从 h3c-lab-automation 技能复制而来**，除文件头说明外与来源一致 |
| `mockdev.py` | ~200 | 假 HCL 设备（纯标准库 TCP），给 `selftest.py --mock` 用；**只用于自测，不参与真机流程** |
| `test_config.py` | ~330 | **配置层回归 44 项**（离线）：BOM、坏配置必须硬失败、不猜拓扑、端口从拓扑推导、重名报错 |
| `test_session.py` | ~120 | **视图状态机回归 28 项**（离线）：视图嵌套、受控确认、破坏性拒答 |
| `selftest.py` | ~450 | 自测：把 `server.py` 当子进程，喂 JSON-RPC 验证协议 + 真实调用工具 |
| `verify_calls.py` | ~150 | 把 8 个工具逐个真实调用并把原始返回存档到 `evidence\selftest-calls-<时间戳>\` |
| `examples\claude_desktop_config.json` | 14 | Claude Desktop 配置示例 |
| `examples\cursor_mcp.json` | 14 | Cursor 配置示例 |
| `examples\plan_example.json` | 32 | `hcl_apply_plan` 的计划文件示例 |
| `examples\checklist_example.json` | 24 | `hcl_verify` 的清单示例（正/负向、单项超时） |

---

## 3. 安装与运行

**依赖：只有 Python 3.8+ 标准库**（本项目在 Windows + Python 3.13.13 上实测）。不装任何 pip 包，不需要 `mcp` SDK。

```powershell
# 直接跑（stdio 传输；stdout 只输出 JSON-RPC，日志走 stderr）
python D:\DSH\NET\dsh-h3c-lab\mcp-server\server.py

# 看看有哪些工具
python D:\DSH\NET\dsh-h3c-lab\mcp-server\server.py --list-tools

# 看 python 侧最终读到的配置（含"配置到底来自哪个文件"，排查"改了没生效"）
python D:\DSH\NET\dsh-h3c-lab\mcp-server\server.py --show-config

# 自测
python D:\DSH\NET\dsh-h3c-lab\mcp-server\test_config.py         # 配置层回归（44 项，不需要 HCL）
python D:\DSH\NET\dsh-h3c-lab\mcp-server\test_session.py        # 视图状态机（28 项，不需要 HCL）
python D:\DSH\NET\dsh-h3c-lab\mcp-server\selftest.py            # 协议 + 真实端口探测
python D:\DSH\NET\dsh-h3c-lab\mcp-server\selftest.py --mock     # 额外用假设备把 8 个工具全跑一遍
```

前置条件：HCL 正在运行，且拓扑已经点过“启动”（HCL 没有 API，这一步只能人工点）。
设备控制台端口 = `30000 + device_id`；本机实测 `hcl_2015` 那套拓扑有 22 台设备、端口到 `30022`。

### 配置

配置来源优先级：

1. 环境变量 `H3C_MCP_CONFIG` 指向的 JSON —— **由 `dsh-h3clab` 插件自动生成并传入**
   （`<DSH_HOME>\h3clab\server-config.json`）。显式指定却读不到/读不懂 ⇒ **报错退出（code 2）**，
   绝不静默回落。这条通道以前是坏的（`load_config` 里调用了尚未定义的 `log()`，
   一有配置文件就在 import 期 `NameError`），现在有 44 项回归测试盯着。
2. 本目录的 `h3c_lab_mcp.json`，其次当前目录的 `h3c_lab_mcp.json`（手工兜底；坏了只告警）。
3. 内置默认值。

内置默认值（注意：**没有** `net_file`，**没有** `devices`）：

```json
{
  "host": "127.0.0.1",
  "ports": [],
  "devices": {},
  "references_dir": "<DSH_HOME>\\skills\\h3c-lab-automation\\references",
  "evidence_root": "<DSH_HOME>\\h3clab\\evidence"
}
```

- `<DSH_HOME>` = `$DSH_HOME`，没设就是 `~/.dsh`。以前这里写死了 `C:\Users\30358\...`，
  换机器就静默失效。
- `ports: []` 表示"没显式配置"：此时端口从 `net_file` 拓扑的 `device_id` 推导，
  连 `net_file` 也没有才回落到内置的 `30001..30010`。
- `devices: {}` 表示"不替用户认定某套 lab 的设备名"。以前这里写死了另一套 lab 的名字表
  （PE1/SW1/SW3-IRF1…），在 `hcl_2015` 上会把端口 30006 标成 `SW3-IRF1`，而那台设备实测叫 `H3C`。
- `net_file` 没有默认值：`D:\NET` 下有十几个不同实验的 `.net`，**猜错比报错危险**。
- `evidence_root` 不再指向包内目录：插件安装目录是发行物，不该被运行时写脏。

读配置文件用 `utf-8-sig`：Windows 记事本 / `Set-Content -Encoding UTF8` / `Out-File`
都会写 UTF-8 BOM，用 `utf-8` 读会直接抛 `JSONDecodeError`（实测踩过）。同样的坑还在
计划文件、清单文件和 `.net` 拓扑上（`.net` 那个更阴：`\ufeff` 不是空白字符，会静默少解析一台设备）。

---

## 4. 客户端接入

### Claude Desktop

把 `examples\claude_desktop_config.json` 的内容合并进
`%APPDATA%\Claude\claude_desktop_config.json`（macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "h3c-hcl-lab": {
      "command": "python",
      "args": ["D:\\DSH\\NET\\dsh-h3c-lab\\mcp-server\\server.py"],
      "env": { "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

改完**完全退出并重启 Claude Desktop**（不是关窗口）。`command` 用 `python`，`args` 必须是 `server.py` 的**绝对路径**。

### Cursor

把 `examples\cursor_mcp.json` 的内容合并进项目里的 `.cursor\mcp.json`（或全局的 `%USERPROFILE%\.cursor\mcp.json`），结构同上：

```json
{
  "mcpServers": {
    "h3c-hcl-lab": {
      "command": "python",
      "args": ["D:\\DSH\\NET\\dsh-h3c-lab\\mcp-server\\server.py"],
      "env": { "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8" }
    }
  }
}
```

在 Cursor 的 MCP 面板里应能看到 8 个工具；`h3c-hcl-lab` 显示为绿色即已连接。

---

## 5. 安全说明

1. **默认只读。** `hcl_run_command` 有硬白名单，只有 `display` / `show` / `ping` / `tracert` / `traceroute` 开头的命令能过；`hcl_verify` 只跑清单里的断言命令；`hcl_link_watch` 只做巡检，**不会自动复位链路**。
2. **写操作要显式二次确认。** `hcl_apply_plan` 默认 `dry_run:true`，只打印将要下发什么；必须显式传 `dry_run:false` 才会真的碰设备。改完是否落盘也要显式 `save:true`（执行 `save force`）。
3. **只连 `127.0.0.1`。** 服务器不提供 host 参数，不扫描局域网，不发任何外部网络请求。
4. **证据落盘可回溯。** 下发与验证的完整回显都写进 `evidence\<时间戳>\`，工具只把摘要回给模型（省 token，也便于事后核对）。
5. **不吞异常但也不崩。** 任何工具异常都会变成 `isError:true` 的可读文本；连接失败会被捕获成 `ConnectionRefusedError: [WinError 10061] ...` 这类原始错误，绝不伪造设备输出。
6. **`.net` / 计划 / 清单文件都是调用方给的本地路径**，服务器只读它们，不回写。

---

## 6. 验证结果

> 全部数据都是**本机实测**，没有任何编造。唯一没验证成的部分是“真机设备侧的 5 个调用”，原因见 6.3：
> **验证时 HCL 完全没启动**（无 H3C/Simware 进程，`127.0.0.1:30001-30010` 全无监听），
> 因此那 5 个调用拿到的是真实的连接拒绝错误，**不是**设备输出。

### 6.1 环境事实

| 项 | 实测值 |
|---|---|
| 机器 / 系统 | Windows，工作目录 `D:\DSH\NET` |
| Python | `C:\Program Files\Python313\python.exe`，3.13.13 |
| 第三方依赖 | 无（`import` 仅 `argparse concurrent.futures json os re sys threading time traceback datetime pathlib socket`） |
| HCL 状态（验证时） | **未运行**：`Get-Process` 无 `H3C`/`Simware`/`VirtualBox`；`127.0.0.1:30001..30010` 全部 `ConnectionRefused` |
| 拓扑文件（真实存在） | `D:\NET\ie\e\kongpei\lab4_ts.net` |

### 6.2 协议层自测（`python selftest.py --mock`）

命令：

```powershell
$env:PYTHONUTF8='1'; python D:\DSH\NET\dsh-h3c-lab\mcp-server\selftest.py --mock
```

结果：**29 项 PASS，0 FAIL**。逐项原文摘录：

```
== h3c-lab-mcp selftest ==
server : D:\DSH\NET\dsh-h3c-lab\mcp-server\server.py
python : C:\Program Files\Python313\python.exe

PASS  initialize: protocolVersion 回显 2024-11-05  | got '2024-11-05'
PASS  initialize: capabilities.tools 存在  | {"tools": {}}
PASS  initialize: serverInfo = h3c-hcl-mcp 1.0.0  | {"name": "h3c-hcl-mcp", "version": "1.0.0"}
PASS  notifications/initialized 无响应  | 收到 0 条意外消息
PASS  tools/list 返回 8 个工具  | 实际 8 个: hcl_list_devices, hcl_topology, hcl_run_command,
      hcl_get_facts, hcl_apply_plan, hcl_verify, hcl_search_memory, hcl_link_watch
PASS  tools/list 工具名与要求一致
PASS  每个工具都有 description + inputSchema
PASS  ping 返回空结果  | {"jsonrpc": "2.0", "id": 3, "result": {}}
PASS  未知方法返回 -32601  | {"code": -32601, "message": "Method not found: no/such/method"}
PASS  hcl_list_devices: 返回 content/isError 结构
PASS  hcl_list_devices: 有可读文本结果
PASS  stdout 每一行都是合法 JSON
PASS  服务器进程仍然存活（异常没有让它退出）  | exit=None

---- --mock：假设备 [39101, 39102, 39103] 上的全工具验证 ----
PASS  mock hcl_list_devices: 紧凑列出主机名/型号/状态，失败的端口单独列出  | 39101  SW1  S6850  UP
PASS  mock hcl_run_command: 正常回显只读命令  | H3C Comware Software, Version 7.1.070, Release 6555P01
PASS  mock hcl_run_command: 非白名单命令被拒绝  | 拒绝执行：`reboot` 不在只读白名单内。
PASS  mock hcl_run_command: max_chars 触达时标注被截断  | [已截断：仅显示前 80 字符，完整 314 字符]
PASS  mock hcl_get_facts: 提炼摘要（≤25 行，含型号/软件版本/设备时间）  | 型号: S6850 / 软件版本: Comware 7.1.070
PASS  mock hcl_topology: 设备表 + 连线表（端口=30000+device_id）  | 合计 3 台设备、2 条连线
PASS  mock hcl_topology: 文件不存在时返回可读错误  | 错误：找不到 .net 拓扑文件。
PASS  mock hcl_apply_plan: 默认 dry_run 不碰设备（设备未收到任何命令）  | 命令数 10 -> 10
PASS  mock hcl_apply_plan: 真下发并检出 Comware 报错  | SW1 下发 3 条 报错 1 [验证 1 条]
PASS  mock hcl_apply_plan: 完整回显写到 evidence\<时间戳>\SW1.txt  | ...\evidence\20260922-130446\SW1.txt
PASS  mock hcl_apply_plan: save=true 会在设备上执行 save force  | SW1 下发 1 条 报错 0 [已保存]
PASS  mock hcl_verify: expect/expect_not 语义正确（4 项全 PASS）  | 合计 4 项：PASS 4，FAIL 0
PASS  mock hcl_search_memory: 同义词扩展 + 命中行 + 文件行号  | 同义词扩展: B 标志->DOWN B/DOWN(B)/No peer M-LAG；...
PASS  mock hcl_search_memory: 无命中时给出下一步指引
PASS  mock hcl_link_watch: UP/DOWN/ADM 判定与合计正确  | 合计 3 条链路：本端 UP 1，DOWN 1，ADM 1，未知 0
PASS  mock 连接失败: isError=True 且是真实错误文本（进程不崩）

合计 29 项：PASS 29，FAIL 0
```

`--mock` 里那条 `hcl_list_devices` 的真实输出（假设备）：

```
30008  -  -  DOWN
30009  -  -  DOWN

连不上的端口（2 个）：
  30008: ConnectionRefusedError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。
  30009: ConnectionRefusedError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。

合计 0 台可达 / 探测 2 个端口（30008-30009）
提示：HCL 没启动，或拓扑还没点『启动』（HCL 无 API，这一步只能人工点）。
```

> 自测里踩到并已修掉的一个真问题：本机 Windows 上**对匿名管道做阻塞的 `sys.stdin.buffer.read()` 会一直不返回**，MCP 客户端的第一条 `initialize` 因此永远处理不到（表现是客户端超时、服务器日志却已打印“已启动”）。`server.py` 的 stdin 读取改成 **`os.set_blocking(fd, False)` + 非阻塞 `os.read` 轮询** 之后稳定通过。这是本次自测最有价值的一条发现。

### 6.3 真实设备调用的 5 个调用（**因 HCL 未启动而未验证**）

> **状态：未验证（环境不具备）。** 下面每一段都是**原始返回**，请按“HCL 没启动时的错误路径验证”来读它，而不是设备事实。
> 恢复方式：人工在 HCL 里打开拓扑并点“启动”，确认 `python ...\hcl_ports.py` 能扫到设备后重跑 `python verify_calls.py`。

存档命令（真实执行过，原始 JSON 存在 `evidence\selftest-calls-20260922-130602\` 等目录）：

```powershell
$env:PYTHONUTF8='1'; python D:\DSH\NET\dsh-h3c-lab\mcp-server\verify_calls.py
```

**(1) `hcl_list_devices`（ports = 30001..30010）** — 10 个端口全部连接被拒：

```
30001  -  -  DOWN
30002  -  -  DOWN
...（30003..30009 同）
30010  -  -  DOWN

连不上的端口（10 个）：
  30001: ConnectionRefusedError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。
  ...（每个端口一行，同样错误）

合计 0 台可达 / 探测 10 个端口（30001-30010）
提示：HCL 没启动，或拓扑还没点『启动』（HCL 无 API，这一步只能人工点）。
```

**(2) `hcl_get_facts`（port=30008）** — `isError: true`，工具没有崩，错误文本直接来自 socket：

```
工具 hcl_get_facts 执行失败：ConnectionRefusedError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。
```

**(3) `hcl_run_command`（port=30008, `display m-lag summary`, timeout=30）** — 同上，未验证：

```
工具 hcl_run_command 执行失败：ConnectionRefusedError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。
```

**(4) `hcl_link_watch`（两条链路：30008/GE1/0/20 ↔ 30001/GE0/0；30009/GE1/0/21 ↔ 30010/GE1/0/18）** — 端口→设备名映射生效（PE1 / Server 是从 `devices` 解出来的），状态未知：

```
SW1 GE1/0/20 本端 ? | 对端 30001/GE0/0 PE1 ?  [phy=? proto=?]
SW2 GE1/0/21 本端 ? | 对端 30010/GE1/0/18 Server ?  [phy=? proto=?]

端口 30001 查询失败: ConnectionRefusedError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。
端口 30008 查询失败: ConnectionRefusedError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。
端口 30009 查询失败: ConnectionRefusedError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。
端口 30010 查询失败: ConnectionRefusedError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。
合计 2 条链路：本端 UP 0，DOWN 0，ADM 0，未知 2
```

**(5) `hcl_search_memory`（keywords=["m-lag","consistency-check"]）** — **这条不依赖设备，真实通过**，命中 5 个小节：

```
关键词: m-lag, consistency-check  （全部命中 AND）
同义词扩展: B 标志->DOWN B/DOWN(B)/No peer M-LAG；Strict->consistency-check/type1/type2
命中 5 个小节，显示前 5 个：

### [cases] cases.md:72  C-001 MPLS Option B 跨域 + M-LAG 总部（HCL 5.10.3） / 2) M-LAG
    L73: **2.1 ★★ 三组恒 `DOWN (B)`，配置逐行对称——真凶是"一致性检查"**
    L78: - 根因：`display m-lag consistency-check status` = **Enabled + Strict**，HCL 模拟器**误报配置不一致**
    L83: m-lag consistency-check disable          # 两台都要

### [gotchas] gotchas.md:323  F12 ★ 决定性解法：`m-lag consistency-check disable`（S6850 模拟器必做）
    L344: m-lag consistency-check disable      # 两台都要
    L357: 复位后即应看到 `display m-lag summary` 三组 `UP`，且接入侧
```

### 6.4 顺带被这条路径验证到的（不需要真机）

| 调用 | 结果 |
|---|---|
| `hcl_topology`（不带 `net_file`，自动找到 `D:\NET\ie\e\kongpei\lab4_ts.net`） | ✅ 真实解析成功：10 台设备（PE1/PE2/ASBR/PE3/PC/SW3-IRF1/SW3-IRF2/SW1/SW2/Server）、21 条连线；设备表里 `SW1 S6850 device_id=8 → 30008` 与环境事实一致 |
| `hcl_apply_plan`（`dry_run` 默认 true） | ✅ 只预演不碰设备：打印 SW1 6 条 + SW2 3 条命令后返回（`合计将下发 9 条命令（2 台设备）`），未建立任何连接 |
| `hcl_verify`（清单 4 项，port=30008） | ✅ 设备连不上时**整台记 FAIL 且只打一行**，其余项不重复刷屏：`FAIL SW1 （4 项） \| 连不上设备 30008: [WinError 10061] ...` + `合计 4 项：PASS 0，FAIL 4，1 台连不上`，并把报告写到 `evidence\20260922-130602\verify-report.txt` |
| 文件不存在 / 参数缺失 | ✅ 返回 `错误：...` 的可读文本（不是异常堆栈） |
| 非白名单命令 | ✅ `拒绝执行：\`reboot\` 不在只读白名单内。` |

### 6.5 未解决 / 不确定的点

1. **真机链路状态、`display m-lag summary` 回显、`display interface brief` 的列布局仍未经真实 S6850 验证。** 接口 brief 解析写了三种列布局（`Link+Protocol(+IP)`、`Link+Speed+Duplex`、两列）的兼容分支，但它们只被假设备覆盖过。
2. **假设备不等于真机。** `mockdev.py` 覆盖了关键交互（CR/LF 结束、提示符、回显、报错行），但真机才有的 auto-config 卡死、`---- More ----` 翻页、日志打断提示符等行为，仍以 `hcldrv.py` 在真机上的既有实测为准。
3. **`hcl_apply_plan` 的并发是“每台一个连接”（最多 5 台同时）。** 单台内部的命令仍串行；`save force` 用 180 秒超时。真机上如果某台设备提示符回收慢，仍可能触发单命令超时（会记为报错，不会静默丢）。
4. **`hcl_search_memory` 只检索 `cases`/`gotchas`/`aliases` 三个文件**（路径由 `references_dir` 配置）；命中行做了 160 字符截断、每节最多 20 行。
5. **`hcl_topology` 不传 `net_file` 时的兜底搜索**只在 `D:\NET` 与 `D:\HCL\sessions` 下递归找最新的 `.net`，并排除本工具自己的目录（自测夹具也是 `.net`，不能被误当成真拓扑）。它**不是**权威来源 —— 权威来源永远是显式传入的 `net_file`，或设备自己 `display` 出来的结果。
