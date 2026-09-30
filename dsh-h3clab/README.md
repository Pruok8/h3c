# dsh-h3clab

> 版本 **0.2.0**。[CHANGELOG](./CHANGELOG.md)

DeepSeek Harness (DSH) 的 **H3C 实验自动化工具桥**：用 stdio 与一个 MCP 服务器通信，
把它的 MCP 工具转成 **13 个 DSH 原生工具**（`h3c_*`），并带一个浏览器里的 **H3CLab 面板**。

本包**不实现任何设备驱动**——telnet 控制台、Comware 提示符处理、拓扑解析、记忆检索
全部由 MCP 服务器（`scripts/server.py`）负责；插件只负责启动/握手/转发/错误转译，
以及**把配置变成服务器能读的 JSON**。

- 包类型：DSH Profile Bundle（`package.json` 里声明 `dsh.bundle.patch`）。
- 运行时依赖：只有 Node 内置模块；`@deepseek-ai/*` 为 peerDependencies（由 DSH 宿主提供）。
- 参考实现：`dsh-doc`（包元数据、patch、`defineTool` 用法照抄其形状）。

## 架构

```
DSH / Cordis
└── dsh-h3clab (lib/index.js: export name / inject=['tools'] / apply(ctx, config))
    ├── lib/config.js           桥接层 + 实验层的配置解析与校验
    │   └── writeServerConfig() 实验层 -> <DSH_HOME>/h3clab/server-config.json（内容不变不重写）
    ├── McpStdioClient          一个长生命周期实例，首次调用时懒启动
    │   └── spawn(pythonCommand, [serverPath], { stdio: ['pipe','pipe','pipe'],
    │             env: {...process.env, H3C_MCP_CONFIG: <上一步的 JSON>, PYTHONUTF8: '1'} })
    │        · JSON-RPC over stdio，**一行一个 JSON**（不做 Content-Length 分帧）
    │        · initialize -> notifications/initialized -> tools/call
    │        · stdout 只解析 JSON；stderr 转发到日志，并保留最后 20 行
    │          附在启动失败/子进程退出的错误里（不然只看到一句"退出 code=2"）
    │        · 超时/取消/子进程退出 -> kill + 让在途请求可读失败 -> 下次调用重新 spawn+握手
    └── 13 × defineTool(...)     显式声明参数（不从 MCP 动态拉 schema）
```

**配置只有一条通路**：插件 config → `server-config.json` → 环境变量 `H3C_MCP_CONFIG`
→ `server.py` 的 `load_config()`。不需要再手工维护 `h3c_lab_mcp.json`（但仍兼容）。

## 目录

```
dsh-h3clab/
  package.json             name/version/type=module/main + dsh.bundle.patch + dsh.client + peerDependencies
  cordis.patch.yml         - insert: [ - id/name: dsh-h3clab / config: {...} ]（含全部配置项注释）
  lib/index.js             Cordis host 入口：name / inject / apply / writeServerConfig / settings 覆盖
  lib/config.js            Config(schemastery) + resolveConfig + buildServerConfig
  lib/panel.js             ★ 面板 host 侧：/h3clab/api 路由（把面板动作转成 ctx.tools.execute）
  lib/client.js            ★ 面板 client 半边：手写 lazy-CJS bundle，注册进 settings.section
  lib/mcp-client.js        ★ stdio MCP 客户端（换行分隔 JSON-RPC、握手、超时、懒重启、stderr 留痕、脚本变更重载）
  lib/tools.js             13 个工具定义 + DSH 名 -> MCP 名映射
  scripts/server.py        随包发布的 MCP 服务器快照（node scripts/sync-all.mjs 生成）
  scripts/hcldrv.py        server.py 依赖的控制台驱动（同上）
  scripts/sync-all.mjs     ★ 三副本同步 + 一致性校验（--check）
  scripts/sync-server.mjs  只同步 server.py 的旧脚本（已废弃，转发给 sync-all）
  scripts/probe-server.ps1 真实服务器协议冒烟探针（Windows）
  test/mock-mcp-server.mjs 自测用 mock MCP 服务器（真实子进程 + stdio）
  test/mock-protocol.mjs   mock 的协议实现（与内存流 child 共用）
  test/in-process-child.mjs 受限沙箱下的内存流 child（自测回退传输）
  test/fixtures/lab.net    hcl_topology 探针用的最小 .net 拓扑
  test/panel-host.test.mjs   面板 host 路由测试（假 req/res，含安全边界）
  test/panel-client.test.mjs 面板 client 半边测试（假 ModuleLoader + 假 React）
  selftest.mjs             插件自测（53 项断言）
```

MCP 服务器的**真源**在同一个 git 仓库的 `mcp-server\` 目录（2026-09-30 从
`D:\DSH\NET\h3c-lab-mcp` 迁入，为的是让 `server.py` 也有版本历史），含 `server.py` 与
`test_config.py`（配置层回归，44 项）、`test_session.py`（视图状态机，28 项）、
`selftest.py`、`mockdev.py`。

## 13 个工具

| DSH 工具 | 参数 | 副作用 | MCP 原名 |
|---|---|---|---|
| `h3c_devices` | `ports?`(int[])、`model?`(bool,默认true)、`prompt_timeout?`(秒,默认25)、`workers?`(1-10,默认10) | 只读 | `hcl_list_devices` |
| `h3c_topology` | `net_file?`(string) | 只读 | `hcl_topology` |
| `h3c_run` | `port`、`command`、`timeout?`(秒,默认20)、`max_chars?`(默认8000) | 只读 | `hcl_run_command` |
| `h3c_facts` | `port`、`timeout?`(秒,默认30) | 只读 | `hcl_get_facts` |
| `h3c_verify` | `checklist_json`(**文件路径**)、`only?`、`timeout?`(秒,默认15) | 只读 | `hcl_verify` |
| `h3c_link_watch` | `links`(必填数组)、`timeout?`(秒,默认25) | 只读 | `hcl_link_watch` |
| `h3c_memory_search` | `keywords`、`any?`、`max?`(默认5)、`max_lines?`(默认20) | 只读 | `hcl_search_memory` |
| `h3c_apply_plan` | `plan_json`(**文件路径**)、`only?`、`save?`、`dry_run?`(**默认 true**)、`timeout?`(秒,默认60)、`workers?`(1-5) | **改设备配置** | `hcl_apply_plan` |
| `h3c_doctor` | `ports?`、`net_file?`、`probe?`(默认true)、`prompt_timeout?`(默认8)、`workers?` | 只读 | `hcl_doctor` |
| `h3c_cfgdiff` | `action`(snapshot/diff/list,默认diff)、`port?`、`name?`、`against?`、`timeout?`、`max_chars?` | 设备只读；本地写快照 | `hcl_cfgdiff` |
| `h3c_lab_state` | `action`(get/set/merge/delete/history)、`path?`、`data?`、`key?`、`note?` | 写本地状态文件 | `hcl_lab_state` |
| `h3c_report` | `title?`、`out?`、`sections?`、`net_file?`、`ports?`、`links?`、`checklist_json?`、`only?`、`state?`、`model?`、`prompt_timeout?`、`timeout?` | 设备只读；本地写报告 | `hcl_report` |
| `h3c_memory_write` | `kind`(case/gotcha)、`title`、`body`、`id?`、`date?`、`tags?`、`one_line?`、`section?`、`file?`、`dry_run?`(**默认 true**) | **写记忆库** | `hcl_memory_write` |

**只有 `h3c_apply_plan` 会改设备配置。** 另外两个写工具只碰本地文件，且其中
`h3c_memory_write` 默认 `dry_run=true`（和 `h3c_apply_plan` 同一个安全口径）：

- `h3c_apply_plan` 的描述里写明"默认 dry_run 只预演"；`execute` 把缺省的 `dry_run` 归一为
  `true`，**只有显式 `dry_run: false` 才真下发**。
- `h3c_memory_write` 同理：默认只打印"将要追加什么"；真写必须 `dry_run: false`，
  且写入前会把原文件备份成 `<文件>.bak-<时间戳>`。

### 新工具解决什么

- **`h3c_doctor`** —— "为什么连不上 / 为什么改了没生效"一条命令定位：配置到底来自哪个文件、
  端口是怎么定的（配置 / 拓扑推导 / 兜底）、拓扑能不能解析、记忆库与证据/状态目录在不在、
  哪些控制台可达。实测输出见下面「实测结果」。
- **`h3c_cfgdiff`** —— 下发前后对比：`snapshot` 存一份 `display current-configuration` 基线，
  `diff` 与基线做逐行统一 diff。这是"我到底改动了什么"的唯一可信来源。
- **`h3c_lab_state`** —— 跨调用记住进度（做到第几步、哪台设备还没配），每次写入自动备份。
- **`h3c_report`** —— 把拓扑 / 设备可达性 / 链路 / 验证矩阵 / lab 状态汇成一份 Markdown 报告。
- **`h3c_memory_write`** —— 把这次踩的坑写回 `cases.md` / `gotchas.md`（自动分配 `C-00N`、
  自动往索引表插一行），形成"查记忆 → 解决 → 写回记忆"的闭环。

实现约定：

- 参数 schema 用 `@deepseek-ai/dsh-tools` 的 schema DSL 写（`required: true`、`items`、
  `enum`、`oneOf`、`default`…），输出统一为 `{ text: string }`，`output.render` 渲染成 text 块。
- 每个工具 `execute` 都有 try/catch，任何异常都转成 `h3c_xxx 调用失败：…` 的可读失败。
- 参数是**静态声明**：改动 `server.py` 的参数时必须同步 `lib/tools.js`
  （selftest 里有一条断言专门盯这个——13 个工具的参数名逐个核对，漏了就红）。

### 端口从拓扑推导（默认行为）

`h3c_devices` 不传 `ports` 时，端口**从拓扑 `.net` 的 `device_id` 推导**：控制台端口 = `30000 + device_id`。
优先级：**显式 `ports` 配置 > 拓扑推导 > 内置兜底 30001-30010**。

这不是锦上添花：实测本机 HCL 有 22 台设备、端口一直到 30022，而旧的固定默认范围
30001-30010 **只覆盖 13 台里的 8 台**，会静默漏报 R2/R3/JR/JR3/模拟终端/FTP服务器。
每次 `h3c_devices` 的输出末尾都会打印一行 `端口来源：…`，说清这次用的是哪一种。

## GUI 面板（客户端半边）

插件带一个**浏览器里的面板**：设置页 → **H3CLab**。5 个页签：

| 页签 | 做什么 |
|---|---|
| 设备 | 调 `h3c_devices`，渲染成表格（端口 / 主机名 / 型号 / 状态），显示「N/M 台可达」与**端口来源**；可临时填端口、可开关读型号 |
| 链路 | 填本端/对端端口与接口，调 `h3c_link_watch`，渲染成表（本端状态、对端、phy/proto） |
| 计划下发 | 选计划文件 → **预演（dry-run）**；确认无误后**手输 `REAL`** 才能点「真下发」 |
| 记忆库 | 调 `h3c_memory_search`，带 `any` 与 `max` |
| 配置 | 编辑 `netFile` / `ports` / `devices` / `evidenceRoot` / `referencesDir` / `stateDir`，保存即生效 |

### 为什么是这个形状

浏览器里的 cordis Context **没有 `ctx.tools`**，面板不能直接调工具。官方一等公民
（Typert / API Gateway）需要改 DSH 自身装配并跑代码生成器，profile bundle 插件做不到。
所以走 host 的 `webServer`：host 注册一条同源 prefix 路由，面板 `fetch` 它，
host 再用 `ctx.tools.execute` 去跑 `h3c_*` 工具（本机已装的 `dsh-super-injector` 同款做法）。

```
lib/client.js  --fetch /h3clab/api/call-->  lib/panel.js  --ctx.tools.execute-->  h3c_* 工具
```

安全边界（都有断言盯着）：

- 面板**只能调本插件的 `h3c_*` 工具**，不做通用工具代理（白名单之外的请求 403）。
- `h3c_apply_plan` 真下发必须带 `confirm: "REAL"`，且界面上要求**手输 REAL** 才解锁按钮。
- 请求体上限 1 MB；工具抛异常返回结构化失败，不会 500。

### 配置编辑的优先级

面板保存的是**面板覆盖**，写在 `<生成的 server-config.json 同目录>/settings.json`：

```
面板设置 > profile 的 cordis.patch.yml 配置 > 内置默认值
```

保存后会重写服务器配置 JSON 并**重启 python 子进程**，下一次工具调用生效——**不必重启 DSH**。
点「清除面板覆盖」即回到 profile 配置。

### 生效条件（重要）

| 改动 | 生效方式 |
|---|---|
| `lib/client.js` | 客户端 HMR 会热替换（**不用重启、不用刷新页面**） |
| `package.json`（含 `dsh.client`）、`lib/index.js`、`cordis.patch.yml` | **必须重启 DSH**（pkgMeta 与层栈在启动时缓存） |

也就是说：**第一次装上面板要重启一次 DSH**；之后只改面板界面就即时生效。

## 配置

全部配置项都在插件 config 里；`cordis.patch.yml` 已给默认值，**在 profile 的
`cordis.patch.yml` 里按 id 覆盖即可**（与 dsh-doc 完全一样的写法），
或者直接在 GUI 面板的「配置」页签里改（面板覆盖优先级更高）。

```yaml
- id: dsh-h3clab
  config:
    # ---- 桥接层 ----
    pythonCommand: python
    serverPath: ''
    toolCallTimeoutMs: 240000
    serverName: h3clab
    # ---- 实验层（转交 server.py）----
    host: 127.0.0.1
    ports: []
    devices: {}
    netFile: ''
    evidenceRoot: ''
    referencesDir: ''
    serverConfigPath: ''
    stateDir: ''
    env: {}
```

| 键 | 默认 | 说明 |
|---|---|---|
| `pythonCommand` | `python` | 也可以是 `py` 或绝对路径 |
| `serverPath` | 包内 `scripts/server.py` | 相对路径按**包根目录**解析 |
| `toolCallTimeoutMs` | `240000` | 单次 MCP 调用超时；超时会 kill python 子进程 |
| `serverName` | `h3clab` | 只用于日志 |
| `host` | `127.0.0.1` | 设备控制台主机；**默认只连本机，绝不扫局域网** |
| `ports` | `[]` | 留空 => 从 `netFile` 推导；连 `netFile` 也没配才用内置兜底 |
| `devices` | `{}` | `{设备名: 端口}`；给按主机名寻址的工具用 |
| `netFile` | `''` | HCL 拓扑 `.net` 路径。**刻意不给默认值**，见下 |
| `evidenceRoot` | `<DSH_HOME>\h3clab\evidence` | 证据落盘根目录，**不再写进插件安装目录** |
| `referencesDir` | `<DSH_HOME>\skills\h3c-lab-automation\references` | `h3c_memory_search` 的检索目录 |
| `serverConfigPath` | `<DSH_HOME>\h3clab\server-config.json` | 上面几项被写成 JSON 放在哪 |
| `stateDir` | `<DSH_HOME>\h3clab` | 配置快照（`h3c_cfgdiff`）与 lab 状态文件放哪；面板覆盖写在同目录的 `settings.json` |
| `env` | `{}` | 额外注入 python 子进程的环境变量（如 `H3C_MCP_DEBUG_DUMP`） |

`<DSH_HOME>` = `$DSH_HOME`，没设就是 `~/.dsh`。相对路径一律按**包根目录**解析。

### 为什么不给 `netFile` 默认值

`D:\NET` 下有**十几个不同实验**的 `.net`。旧实现会"取 mtime 最新的那个"——
换了实验就会把 A 套的设备表贴到 B 套的验证结论上，**猜错比报错危险得多**。
现在未配置时 `h3c_topology` 直接报错，并在错误里给出两种修法（传参 / 配置）。

### 排查"改了配置没生效"

```powershell
# 看 python 侧最终读到的配置（含 config_source：来自哪个文件还是内置默认值）
python scripts\server.py --show-config
python scripts\server.py --version        # 版本
python scripts\server.py --list-tools     # 工具清单
```

约定：`H3C_MCP_CONFIG` 指向的文件**不存在或解析失败 = 直接报错退出（code 2）**，
绝不静默回落到别的配置。手工兜底的 `<本目录>\h3c_lab_mcp.json` 坏了则只告警。

## 安装

```powershell
# 1) 先打包服务器快照（把上游 server.py 同步进来）
node scripts/sync-all.mjs

# 2) 装进 profile（web profile 用 CLI；desktop profile 只能手工，见下）
dsh plugin --profile web add D:\DSH\NET\dsh-h3clab
```

## 三副本同步

`server.py` 在同一工作区里存在三份，`sync-all` 是唯一的同步与校验入口：

```powershell
node scripts/sync-all.mjs              # 同步（只写有变化的文件）
node scripts/sync-all.mjs --check      # 只校验；有漂移退 2（适合接进 CI / 提交前跑）
```

| 副本 | 路径 | 角色 |
|---|---|---|
| ① 源 | `..\dsh-h3c-lab\mcp-server\server.py` | 真源，带 `test_config.py` / `test_session.py`，在 git 仓库里 |
| ② 插件快照 | `<插件包>\scripts\server.py` | 随包发布、DSH 实际运行的那份（活副本在 `D:\DSH\NET\dsh-h3clab`） |
| ③ 仓库副本 | `D:\DSH\NET\dsh-h3c-lab\dsh-h3clab\` | git 里发布出去的那份 |

`sync-all.mjs` 在**两种**位置都能跑：插件活副本里（做 ①②③），或仓库内的插件副本里
（此时跳过 ②→③，因为它自己就是 ③）。`selftest.mjs` 里还有一条断言直接比对 ① 与 ②，
漂移了测试就红。

## 自测与验证

```powershell
node selftest.mjs                        # 插件自测：53 项（含面板 host + client）
cd ..\dsh-h3c-lab\mcp-server
python test_config.py                    # 配置层回归：45 项（BOM、坏配置、不猜拓扑、端口推导、重名报错）
python test_labtools.py                  # 新增 5 个工具：37 项（doctor/state/memory_write/report/cfgdiff，离线）
python test_session.py                   # 视图状态机：28 项
python selftest.py                       # 上游服务器自测
powershell -File ..\..\dsh-h3clab\scripts\probe-server.ps1   # 真实 server.py 的协议冒烟
```

### 实测结果（2026-09-30，本机）

```
node selftest.mjs                -> 53 通过 / 0 失败（真实子进程 + pipe stdio；含 20 项面板断言）
python test_config.py            -> 45 通过 / 0 失败
python test_labtools.py          -> 37 通过 / 0 失败
python test_session.py           -> 44 通过 / 0 失败
node scripts/sync-all.mjs --check-> 一致（0 漂移）
```

**真机闭环验证**（`verify_plan_loop.py`，会改配置但自动还原）：

```
步骤 1  基线快照             R3 249 行 / 2177 字节，sha1=da36fa9f5a12
步骤 2  dry-run 预演         明确说明"不会碰设备"
步骤 3  真下发（dry_run=false）R3 下发 3 条 报错 0
步骤 4  与基线 diff          +2 行：description H3CLAB-VERIFY-DO-NOT-KEEP
步骤 5  反向命令还原          R3 下发 3 条 报错 0
步骤 6  再 diff              ✅ 0 差异（完全恢复原状）
判定                         4/4 PASS
```

> 面板的 20 项断言是在 Node 里用**假 ModuleLoader + 假 React** 跑的：能验证外壳格式、
> 导出契约、注册到哪个 slot、5 个页签能否渲染、切页、以及设备页真的去 fetch 了 host 路由；
> **不能**替代"重启 DSH 看真身"。首次装上面板请重启一次 DSH 确认。

真机（HCL 已启动，拓扑 `hcl_2015.net`）：

```
h3c_devices(22 个推导端口) -> 13 台 UP：30001/2/6/7/8 (S6850)、30009/13/17 (S5820V2-54QS-GE)、
                              30010/14/15/16/22 (MSR36-20)
                              9 台 DOWN：30003/4/5/11/12/18/19/20/21（AP/PC/Phone/AC 模拟器，HCL 不开控制台）
h3c_topology(net_file=…)   -> 22 台设备、25 条连线
h3c_run(30001, display version) -> 759 字符真实回显
h3c_link_watch(30001 GE1/0/1 <-> 30006 GE1/0/1) -> 本端 UP / 对端 UP
h3c_facts(30001)           -> 主机名 H3C、型号 S6850、Comware 7.1.070
h3c_doctor(probe=true)     -> 13/22 台可达；主动报出"设备名映射为空"与"所有主机名都是 H3C"
h3c_cfgdiff(snapshot,30001)-> 633 行 / 6111 字节，sha1=adc3c4818116
h3c_cfgdiff(diff,30001)    -> 与刚存的基线 0 差异（✅ 配置一致）
h3c_report(topology,state) -> 写出 3139 字节的 Markdown 报告
```

**两个必须知道的实测现象**：

1. **所有设备的主机名都是默认值 `H3C`**（S6850/MSR36-20 都一样）。因此
   *按主机名寻址*（`_scan_for_hostname`）在实机上基本不可用——需要用 `port` 或配置 `devices`。
2. `h3c_link_watch` 输出里的"对端名字"来自**配置的 `devices` 映射**，不是实测值。
   内置默认映射是**另一套 lab**（PE1/SW1/SW3-IRF1/…），所以在 `hcl_2015` 上会把
   端口 30006 标成 `SW3-IRF1`，而该设备实测主机名是 `H3C`。**不要把这一列当实测值用**，
   除非你在 `devices` 里配了与当前 lab 一致的名字。（这条正在修，见"已知限制"）

## 安全声明

- **只有 `h3c_apply_plan` 会改设备配置**，且默认 `dry_run: true`（只预演）；
  真下发必须显式 `dry_run: false`。其余 12 个工具在 `server.py` 侧对设备都是只读的
  （`h3c_run` 有只读命令白名单，由 `server.py` 实现）。
- 两个工具写**本地**文件：`h3c_lab_state`（状态 JSON）与 `h3c_memory_write`（记忆库）。
  两者写入前都会备份成 `<文件>.bak-<时间戳>`；`h3c_memory_write` 默认 `dry_run: true`。
  `h3c_cfgdiff` 会往 `<stateDir>/snapshots/` 写快照文件，`h3c_report` 会往证据目录写报告。
- 面板 API（`/h3clab/api`）是**同源**路由，依赖页面自带的会话 Cookie；它只代理本插件的
  `h3c_*` 工具（白名单之外的请求 403），真下发还要求 `confirm: "REAL"`。请求体上限 1 MB。
- 命中 `DESTRUCTIVE_RE` 的命令（`reboot`、`reset saved-configuration`、`format`、
  `restore factory`）**拒绝下发并明确报错**，绝不自动答 `Y`。
- 子进程只执行你配置的 `pythonCommand` + `serverPath`；环境变量继承当前进程并强制
  `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1`（避免 Windows cp936 乱码）。
- 子进程 stdout 只用于 JSON-RPC 解析；stderr 逐行进 DSH 日志（debug），并保留最后 20 行
  附在失败信息里。
- **超时、调用被取消、子进程退出都会 kill python 子进程**：正在进行的设备命令会被中断，
  设备侧可能停在命令中途——这是"宁可断连也不静默"的取舍，重建连接由下一次调用负责。
- 本插件不做鉴权、不做沙箱、不校验目标设备：工具的能力边界 = `server.py` 本身。
  **不要把 `serverPath` 指向不可信的脚本。**
- 不实现 Content-Length 分帧（只发/只按换行分隔解析）；服务器反向请求统一回 `-32601`；
  不提供 MCP resources / prompts。

## 已知限制

1. **插件有两份副本**：活副本 `D:\DSH\NET\dsh-h3clab`（profile 里的 junction 指向它）与
   仓库副本 `D:\DSH\NET\dsh-h3c-lab\dsh-h3clab`。改完必须跑 `node scripts/sync-all.mjs`，
   否则仓库里是旧的。
2. 参数是静态声明：`server.py` 参数变化需手工同步 `lib/tools.js`（selftest 有断言兜底）。
3. 单实例、单子进程：所有工具调用共用一个 python 进程，服务器串行处理；
   并发调用在客户端各自排队（每个请求有独立 id 与超时）。
4. `h3c_run` 的 `timeout` 语义是"秒"，与 `toolCallTimeoutMs`（毫秒，客户端）不同。
5. 参数名 `plan_json` / `checklist_json` 虽然叫 `*_json`，但 `server.py` 读的是**文件路径**
   （不是内联 JSON 文本）；工具描述里已写明"PATH"，请传绝对路径。
6. 为保持描述简短，只暴露了每个工具的主参数；`server.py` 若有新增参数需同步 `lib/tools.js`。
7. **真机刚启动时主机名/型号可能是瞬态值**：HCL 加载 startup-config 之前，设备提示符是默认的
   `H3C`。刚点"启动"就去 `h3c_devices`，拿到的身份信息可能不准，等配置加载完再探一次。
8. **面板只验证到"离线可跑"**：20 项断言用假 ModuleLoader + 假 React 跑通了外壳格式、
   导出契约、slot 注册、5 个页签渲染、切页与 fetch 路径；真实 React 渲染与真实 Slot 挂载
   需要重启 DSH 后才能确认。
9. `h3c_memory_write` 只**追加**到已有文件，不会新建知识库；`gotcha` 不指定 `section`
   时会落在文件末尾最后那个 `## 分类` 之下（会提示是哪个分类）。

## 本机安装现状（2026-09-30）

桌面 GUI 用的是 **`desktop` profile**，而 `dsh plugin --profile desktop` 会被 CLI 拒绝
（*"profile desktop is managed exclusively by the Electron application"*），只能手工安装：

```powershell
# 1) 链接插件（junction，等价于 pnpm 的 link: 依赖）
cmd /c mklink /J "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\dsh-h3clab" "D:\DSH\NET\dsh-h3clab"
# 2) desktop 的 package.json：dependencies 加 "dsh-h3clab": "link:D:/DSH/NET/dsh-h3clab"，
#    dsh.profile.bundles 追加 "dsh-h3clab"
# 3) 完全退出并重启桌面应用（插件按 profile 层栈在启动时加载，刷新页面不够）
```

`web` profile 里也装了一份，供 `dsh web --profile web` 使用。两处都是 junction，
改 `D:\DSH\NET\dsh-h3clab` 源码即同时生效。

### `hcl_apply_plan` 的视图与确认行为

`server.py` 的下发路径用**显式视图状态机**（`_Session`）处理 HCL 上的三类坑：

| 之前的坑 | 现象 | 现在的行为 |
|---|---|---|
| 无子视图深度跟踪 | `ospf→area→network`、`interface→属性` 整批错位，命令全报 `% Unrecognized`（其实一条没错） | 进块命令先归位系统视图；`ospf→area` 这类**同族嵌套**原地进（`NEST_KINDS`）；普通配置命令留在当前视图；`quit` 只退一级 |
| `[Y/N]` 只报警告不应答 | `port link-mode route`、部分 `undo` 静默不生效 | **白名单式自动应答**：命令前缀命中 `CONFIRM_SAFE_PREFIXES` 才答 `Y`；可用设备级 `"auto_confirm": false` 关闭 |
| 错误正则过宽 | `does not exist` / `is not allowed` 等正常回显被当真报错 | 分两套：`STRICT_ERROR_RE` 只用于**视图状态决策**，宽松 `ERROR_PATTERNS` 仍用于**给人看的报告** |

进块判定用 `ENTRY_RULES`（视图名 + 精确头部 + 否定词）而不是简单前缀表，因为
`ospf 1`（进进程视图）与 `ospf timer hello 3`（留在原视图）前两个单词相同。

#### 真机抓到的严重缺陷（2026-09-30 修复）

破坏性测试（真下发 → 验证 → 还原）在 R3 上第一次跑就翻车：`interface GigabitEthernet0/0`
和 `description …` 全部 `% Unrecognized command`。证据文件显示：

- `### 下发前提示符: H3C` —— 状态机认为**已经在系统视图**，于是 `system-view` 一次都没发；
  可设备其实在用户视图 `<H3C>`。
- 证据里还有一整段 `Press ENTER to get started.` 登录横幅 —— `to_user()` 把控制台
  **一路 `quit` 到登出**了。

两个 bug 同一个根因：**用提示符的名字判视图**。HCL 出厂配置下所有设备都叫 `H3C`，
而 `<H3C>`（用户视图）与 `[H3C]`（系统视图）**名字完全一样**；再加上
`_prompt_name()` 返回的是裸名字（不含括号），`to_user()` 里 `p.startswith("<")`
这个判断永远为假、`to_system()` 又"第一次看到提示符就假定已在系统视图"。

现在**只看提示符的括号**（`_prompt_info()` 返回 `(名字, 'user'|'system')`），
并显式发 `system-view`。修复后同一台设备的闭环验证全部 PASS。

**为什么 28 项离线测试没抓到**：`test_session.py` 的假控制台是**视图无关**的
（任何视图都接受任何命令），而且测试都手动把 `host, sub` 设成 `2` 再调 `run()`，
从来没走过"进对视图"这条路。现在假控制台会按视图拒绝命令、并模拟用户视图 `quit` 登出，
断言也从 28 项增加到 44 项。

离线自测（不需要 HCL）：`cd ..\dsh-h3c-lab\mcp-server; python test_session.py`，44 项。

真机闭环（会改设备配置，但会自动还原）：

```powershell
cd ..\dsh-h3c-lab\mcp-server
python verify_plan_loop.py --port 30022 --name R3 --interface GigabitEthernet0/0
# 基线快照 → dry-run 预演 → 真下发 → diff 应出现新增行 → 反向还原 → 再 diff 应为 0 差异
```

## 变更记录

见 [CHANGELOG.md](./CHANGELOG.md)。当前版本 **0.2.0**（自测里有一条断言盯着
`package.json` 的 `version` 与 CHANGELOG 最新版本一致，防止漂移）。

## 相关

DSH 自带通用 MCP 桥 `@deepseek-ai/dsh-mcp-client`（工具名形如 `mcp__<serverName>__<tool>`，
按 YAML 行配置即可）。本包与它的区别：固定 `h3c_*` 工具名、显式静态参数 schema、
中文错误转译与超时策略。同一份 `scripts/server.py` 可以直接接过去。
