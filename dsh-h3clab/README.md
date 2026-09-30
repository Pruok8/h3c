# dsh-h3clab

DeepSeek Harness (DSH) 的 **H3C 实验自动化工具桥**：用 stdio 与一个 MCP 服务器通信，
把它的 MCP 工具转成 **8 个 DSH 原生工具**（`h3c_*`）。

本包**不实现任何设备驱动**——telnet 控制台、Comware 提示符处理、拓扑解析、记忆检索
全部由 MCP 服务器（`scripts/server.py`）负责；插件只负责启动/握手/转发/错误转译。

- 包类型：DSH Profile Bundle（`package.json` 里声明 `dsh.bundle.patch`）。
- 与 `dsh-doc` 同构：`type: module`、`main: ./lib/index.js`、`cordis.patch.yml` 用 `- insert:` 插一行。
- 运行时依赖：只有 Node 内置模块；`@deepseek-ai/*` 为 peerDependencies（由 DSH 宿主提供）。
- 参考实现：`C:\Users\30358\.dsh\profiles\web\node_modules\dsh-doc`（包元数据、patch、`defineTool` 用法照抄其形状）。

## 架构

```
DSH / Cordis
└── dsh-h3clab (lib/index.js: export name / inject=['tools'] / apply(ctx, config))
    ├── McpStdioClient           一个长生命周期实例，首次调用时懒启动
    │   └── spawn(pythonCommand, [serverPath], { stdio: ['pipe','pipe','pipe'] })
    │        · JSON-RPC over stdio，**一行一个 JSON**（不做 Content-Length 分帧）
    │        · initialize -> notifications/initialized -> tools/call
    │        · stdout 只解析 JSON；stderr 转发到 ctx.logger('dsh-h3clab').debug/info
    │        · 超时/取消/子进程退出 -> kill + 让在途请求可读失败 -> 下次调用重新 spawn+握手
    └── 8 × defineTool(...)      显式声明参数（不从 MCP 动态拉 schema）
```

## 目录

```
dsh-h3clab/
  package.json          name/version/type=module/main + dsh.bundle.patch + peerDependencies
  cordis.patch.yml      - insert: [ - id/name: dsh-h3clab / config: {...} ]
  lib/index.js          Cordis 入口：name / inject / apply
  lib/config.js         Config(schemastery) + resolveConfig（默认值、相对路径基准）
  lib/mcp-client.js     ★ stdio MCP 客户端（换行分隔 JSON-RPC、握手、超时、懒重启）
  lib/tools.js          8 个工具定义 + DSH 名 -> MCP 名映射
  scripts/server.py     随包发布的 MCP 服务器副本（node scripts/sync-server.mjs 生成）
  scripts/hcldrv.py     server.py 依赖的驱动模块（同上）
  scripts/sync-server.mjs  从 D:\DSH\NET\h3c-lab-mcp 同步 server.py 及其本地 import
  scripts/probe-server.ps1 真实服务器协议冒烟探针（Windows）
  test/mock-mcp-server.mjs 自测用 mock MCP 服务器（真实子进程 + stdio）
  test/mock-protocol.mjs   mock 的协议实现（与内存流 child 共用）
  test/in-process-child.mjs 受限沙箱下的内存流 child（自测回退传输）
  test/fixtures/lab.net    hcl_topology 探针用的最小 .net 拓扑
  selftest.mjs          自测（22 项断言）
```

## 8 个工具

| DSH 工具 | 参数 | 副作用 | MCP 原名 |
|---|---|---|---|
| `h3c_devices` | `ports?`(int[]) | 只读 | `hcl_list_devices` |
| `h3c_topology` | `net_file?`(string) | 只读 | `hcl_topology` |
| `h3c_run` | `port`(int,必填)、`command`(string,必填)、`timeout?`(number)、`max_chars?`(int) | 只读 | `hcl_run_command` |
| `h3c_facts` | `port`(int,必填) | 只读 | `hcl_get_facts` |
| `h3c_verify` | `checklist_json`(string,必填，**清单文件路径**)、`only?`(逗号分隔 id 前缀) | 只读 | `hcl_verify` |
| `h3c_link_watch` | `links`(必填，元素 `{name?, port, intf, peer_port?, peer_intf?}`) | 只读 | `hcl_link_watch` |
| `h3c_memory_search` | `keywords`(string[],必填)、`any?`(bool)、`max?`(int) | 只读 | `hcl_search_memory` |
| `h3c_apply_plan` | `plan_json`(string,必填，**计划文件路径**)、`only?`(逗号分隔设备名)、`save?`、`dry_run?`(**默认 true**) | **写** | `hcl_apply_plan` |

- `h3c_apply_plan` 的描述里写明"默认 dry_run 只预演"；`execute` 里把缺省的 `dry_run` 归一为 `true`，
  **只有显式 `dry_run: false` 才真下发**。
- 参数 schema 用 `@deepseek-ai/dsh-tools` 的 schema DSL 写（`required: true`、`items`、`enum`…），
  输出统一为 `{ text: string }`，`output.render` 渲染成 text 块。
- 每个工具 `execute` 都有 try/catch，任何异常都转成 `h3c_xxx 调用失败：…` 的可读失败，不抛崩插件。
- 工具级参数是**静态声明**：改动 `scripts/server.py` 的参数时需要同步 `lib/tools.js`。

## 安装（由调用方执行）

本包构建时**未执行安装**，也**未修改 `~/.dsh` 下任何文件**：

```powershell
# dsh plugin 会把参数转发给 profile 目录里的 pnpm；安装后 dsh.profile.bundles 会按
# package.json 的 dsh.bundle.patch 声明自动把本包加入层栈。
dsh plugin --profile web add D:\DSH\NET\dsh-h3clab
# 然后重启该 profile（例如 dsh web）
```

手工兜底（等价于 pnpm 安装的那一步）：把本目录放到
`%USERPROFILE%\.dsh\profiles\web\node_modules\dsh-h3clab`，并把 `"dsh-h3clab"` 追加到该
profile `package.json` 的 `dsh.profile.bundles` 数组。

安装前先打包服务器副本：

```powershell
node scripts/sync-server.mjs      # npm run sync-server
node scripts/sync-server.mjs --check   # 只校验副本与源是否一致（不一致退非 0）
```

`scripts/server.py` + `scripts/hcldrv.py` 是从 `D:\DSH\NET\h3c-lab-mcp` 复制进来的**快照**
（本次构建时的版本，74,758 / 11,620 字节）。上游还在改动时，重新跑一次 sync 即可刷新。

## 配置

`cordis.patch.yml` 已给出默认值；可在 profile 的 `cordis.patch.yml` 里按 id 覆盖
（与 dsh-doc 完全一样的写法）：

```yaml
- id: dsh-h3clab
  config:
    pythonCommand: python                # 默认 'python'；也可以是 'py' 或绝对路径
    serverPath: ''                       # 留空 => 包内 scripts/server.py；相对路径按包根目录解析
    toolCallTimeoutMs: 240000            # 单次 MCP 调用超时；超时会 kill python 子进程，下次调用重启
    serverName: h3clab                   # 只用于日志
```

## 自测

```powershell
node selftest.mjs                        # npm run selftest
powershell -File scripts/probe-server.ps1   # 真实 server.py 的协议冒烟（Windows）
```

自测会：必要时把本机 DSH 自带的 `@deepseek-ai` 目录以 junction 挂到 `node_modules/@deepseek-ai`
（正常安装过依赖的副本直接命中），然后跑协议层、超时/懒重启、插件注册、端到端、
包元数据与 Config 校验共 22 项断言。

## 验证结果

### 1) `node selftest.mjs`（本会话真实输出）

```
依赖解析：node_modules/@deepseek-ai -> 已存在（复用）
node v24.19.0 / mock 服务器 D:\DSH\NET\dsh-h3clab\test\mock-mcp-server.mjs
python: 无法探测（EPERM）
spawn pipe stdio: EPERM
[WARN] 本会话沙箱禁止创建命名管道，自测改用内存流 child（test/in-process-child.mjs）。
[WARN] 协议/超时/懒重启断言照常执行；"真实子进程 + pipe stdio" 这一条在本会话无法验证，
[WARN] 请在普通终端（非受限沙箱）重跑 node selftest.mjs 以覆盖它。
---
[PASS] 握手 + tools/list 能拉到 8 个 MCP 工具（内存流 child（沙箱回退））
[PASS] callTool 能拿到 content[].text
[PASS] callToolText 透传参数（port/command）
[PASS] stderr 被转发到日志出口
[PASS] isError 结果 -> 可读失败
[PASS] MCP error 响应 -> 可读失败
[PASS] 超时按 timeoutMs 报错并结束子进程
[PASS] 超时后下一次调用能懒重启（重新 spawn + 重新握手）
[PASS] 真实 spawn 抛 EPERM 时给出可读失败（不崩插件）
[PASS] lib/index.js 导出 name / inject / apply
[PASS] 假 ctx 下 apply 注册 8 个工具
[PASS] 每个工具都有 name/description/parameters/output.schema/output.render/execute
[PASS] 工具名 -> MCP 工具名映射正确
[PASS] presentCall.kind 使用 dsh-tools 声明的合法枚举值
[PASS] h3c_devices.execute 端到端返回文本 + render 成 content（内存流 child（沙箱回退））
[PASS] h3c_apply_plan 默认 dry_run=true（只预演），显式 false 才真下发
[PASS] 工具内部异常 -> 带工具名的可读失败
[PASS] 插件卸载时通过 ctx.effect 回收子进程
[PASS] package.json 是合法的 dsh bundle
[PASS] cordis.patch.yml 是 insert 形式的 bundle patch
[PASS] resolveConfig 补全默认值（serverPath 默认指向包内 scripts/server.py）
[PASS] schemastery Config 能被 StandardSchema 校验并补默认值
---
总计：22 通过 / 0 失败（传输：内存流 child（沙箱回退））
```

**为什么是"内存流 child"**：本会话运行在 DSH 的 Windows 受限文件沙箱里，
被托管进程**不能创建命名管道**，Node 的 `spawn(..., { stdio: ['pipe','pipe','pipe'] })`
会**同步抛 EPERM**（实测：任意含 `'pipe'` 的 stdio 组合都抛）。这不是插件的问题，
DSH 宿主进程自身不受此限（例如 dsh-doc 就是从宿主里 spawn python 的）。
自测因此自动改用 `test/in-process-child.mjs` 的内存流 child（同一个 mock 协议实现），
协议/超时/重启/错误断言全部真实执行；**只有"真实子进程 + pipe stdio"这一条在本会话无法覆盖**，
请在普通终端重跑一次以补齐。

### 2) 真实 MCP 服务器协议探针（`powershell -File scripts/probe-server.ps1`）

用 PowerShell 自己的管道把 6 条请求（含 1 条通知）喂给**包内** `scripts/server.py`：

```
服务器：D:\DSH\NET\dsh-h3clab\scripts\server.py
响应行数：5（6 个请求，其中 1 个是通知，应得 5 行）
id=1  serverInfo=h3c-hcl-mcp protocolVersion=2024-11-05
id=2  tools=hcl_list_devices,hcl_topology,hcl_run_command,hcl_get_facts,hcl_apply_plan,hcl_verify,hcl_search_memory,hcl_link_watch
id=3  isError=False 首行=拓扑文件: D:\DSH\NET\dsh-h3clab\scripts\..\test\fixtures\lab.net
id=4  isError=False 首行=关键词: console  （任一命中 ANY）
id=5  isError=False 首行=30001  -  -  DOWN
```

这证明：真实服务器确实是**一行一个 JSON** 分帧、`initialize` 返回
`protocolVersion=2024-11-05`（与客户端握手一致）、通知不产生响应、`tools/list` 给出 8 个工具且
名字与本包映射表完全一致、本地文件类只读工具（`hcl_topology` 用 `test/fixtures/lab.net`、
`hcl_search_memory`）返回真实结果；HCL 未启动时 `hcl_list_devices` 返回可读的 DOWN 列表而不是崩溃。

## 安全声明

- **只有 `h3c_apply_plan` 有副作用**，且默认 `dry_run: true`（只预演）；真下发必须显式 `dry_run: false`。
  其余 7 个工具在 `server.py` 侧是只读的（`h3c_run` 有只读命令白名单，由 server.py 实现，本桥不重复实现）。
- 子进程只执行你配置的 `pythonCommand` + `serverPath`；环境变量继承当前进程并强制
  `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1`（避免 Windows cp936 乱码）。
- 子进程 stdout 只用于 JSON-RPC 解析；stderr 逐行进 DSH 日志（debug）。
- **超时、调用被取消、子进程退出都会 kill python 子进程**：正在进行的设备命令会被中断，
  设备侧可能停在命令中途——这是"宁可断连也不静默"的取舍，重建连接由下一次调用负责。
- 本插件不做鉴权、不做沙箱、不校验目标设备：工具的能力边界 = `server.py` 本身。
  **不要把 `serverPath` 指向不可信的脚本。**
- 不实现 Content-Length 分帧（只发/只按换行分隔解析）；服务器反向请求统一回 `-32601`；
  不提供 MCP resources / prompts。

## 已知限制

1. **受限沙箱下无法验证真实 pipe spawn**（见上）；生产是普通进程，不受此限。
2. **HCL 未启动**：需要设备的工具（`h3c_devices`/`h3c_run`/`h3c_facts`/`h3c_verify`/
   `h3c_link_watch`/`h3c_apply_plan`）本次**没有**对真实设备验证过，只验证了协议与
   `hcl_topology`/`hcl_search_memory` 两个纯本地只读工具。
3. 参数是静态声明：`server.py` 参数变化需手工同步 `lib/tools.js`；
   `tools/list` 只用于诊断（`client.listTools()`），不参与 schema 生成。
4. 单实例、单子进程：所有工具调用共用一个 python 进程，服务器串行处理；
   并发调用会在客户端各自排队（每个请求有独立 id 与超时）。
5. `server.py` 现在同时容忍 Content-Length 输入，但本客户端只用换行分隔。
6. `h3c_run` 的 `timeout` 语义是"秒"（server.py 的定义），与 `toolCallTimeoutMs`（毫秒，客户端）不同。
7. 参数名 `plan_json` / `checklist_json` 虽然叫 `*_json`，但 **server.py 读的是文件路径**（不是内联 JSON 文本）；
   工具描述里已写明"PATH"。构造参数时请传绝对路径。
8. 为保持描述简短，只暴露了每个工具的主参数：`hcl_list_devices` 的
   `model`/`prompt_timeout`/`workers`、`hcl_get_facts`/`hcl_verify`/`hcl_link_watch` 的 `timeout`、
   `hcl_apply_plan` 的 `timeout`/`workers` 未暴露（走服务器默认值）。
   需要时可在 `lib/tools.js` 里补声明。

## 待调用方安装时验证（本会话无法确认，未谎报通过）

1. **peerDependencies 版本口径（已按宿主实际版本校正）**：
   - 本机宿主统一为 **DSH 0.1.5-rc.2**：`@deepseek-ai/cordis 4.0.2`、`@deepseek-ai/dsh-tools 0.1.5-rc.2`、
     `@deepseek-ai/schemastery 3.18.2`。
   - 原声明 `@deepseek-ai/dsh-tools: ^0.1.0-rc.6` 在严格 semver 下**不满足** 0.1.5-rc.2
     （prerelease 同 tuple 规则），已改为 **`^0.1.5-rc.2`**；三项 peer 现全部 `satisfies` 通过。
   - 同时按参考插件 `dsh-doc` 的口径补了 `devDependencies`。
   - 运行时 API 已逐项核对 `dsh-tools@0.1.5-rc.2` 的真实导出：`defineTool` 在；
     `ToolCallKind = 'read' | 'edit' | 'delete' | 'move' | 'search' | 'execute' | 'fetch' | 'other'`
     含本插件用到的 `read`/`search`/`edit`。自测 22 项在该版本下全部通过。
2. **Cordis 挂载与回收**：`ctx.effect(() => () => cleanup)` 是按本机 `@deepseek-ai/cordis@4.0.2`
   的 `fiber.d.ts`（`effect(execute: () => Effect)`，effect body 返回 disposer）写的；
   `ctx.on('dispose', …)` 在 cordis 4 中**不是公开事件**（只有 `internal/*`），因此只作
   `ctx.effect` 不存在时的兜底。真机上 fiber 卸载是否确实 kill 掉 python 子进程，需装完确认。
3. **加载器接受本 patch/config**：`Config` 已在本机用 StandardSchema（`Config['~standard']`）实测
   能校验并补默认值，但"profile 层栈里 id 不冲突、row 能 activate、`inject: ['tools']` 能解析"
   只有装进 profile 才知道。
4. **真实 spawn + pipe stdio 路径**（生产路径）：受限沙箱会话未覆盖，
   请在普通终端重跑 `node selftest.mjs`（此时会自动走真实子进程 + mock 服务器）。
5. **`presentCall.kind` 的合法值**：**不是猜的**——从本机 `@deepseek-ai/dsh-tools@0.1.5-rc.2` 的
   `lib/types/presentation.d.ts` 读到 `ToolCallKind = 'read' | 'edit' | 'delete' | 'move' | 'search' | 'execute' | 'fetch' | 'other'`；
   本包只读工具用 `'read'`（`h3c_memory_search` 用 `'search'`），写工具 `h3c_apply_plan` 用 `'edit'`，
   并已断言这些值都在枚举内。若调用方安装的是更早/更晚的 dsh-tools，请复核该枚举。

## 本机安装现状（2026-09-30 已落地）

桌面 GUI 用的是 **`desktop` profile**，而 `dsh plugin --profile desktop` 会被 CLI 拒绝
（*"profile desktop is managed exclusively by the Electron application"*），
因此只能手工安装（与 web profile 同构）：

```powershell
# 1) 链接插件（junction，等价于 pnpm 的 link: 依赖）
New-Item -ItemType Directory -Force "$env:USERPROFILE\.dsh\profiles\desktop\node_modules" | Out-Null
cmd /c mklink /J "$env:USERPROFILE\.dsh\profiles\desktop\node_modules\dsh-h3clab" "D:\DSH\NET\dsh-h3clab"

# 2) 在 desktop 的 package.json 里登记
#    dependencies        : { "dsh-h3clab": "link:D:/DSH/NET/dsh-h3clab" }
#    dsh.profile.bundles : 追加 "dsh-h3clab"

# 3) 完全退出并重启桌面应用（插件按 profile 层栈在启动时加载，刷新页面不够）
```

`web` profile 里也装了一份（`dsh plugin --profile web add`），供 `dsh web --profile web` 使用。
两处都是 junction，改 `D:\DSH\NET\dsh-h3clab` 源码即同时生效。

安装后自检（都已实测通过）：`node selftest.mjs` → 22/22；
`powershell -File scripts/probe-server.ps1` → 5 条响应、8 个工具名；
从 `desktop\node_modules\dsh-h3clab\lib\index.js` 动态 import → `name=dsh-h3clab`、`TOOL_NAMES.length=8`。

## hcl_apply_plan 的视图与确认行为（2026-09-30 修复）

`server.py` 的下发路径过去只做"用户视图 → system-view"一次补位，实测在 HCL 上会踩三类坑，
现已用**显式视图状态机**（`_Session`）修掉，随 `scripts/server.py` 一起发布：

| 之前的坑 | 现象 | 现在的行为 |
|---|---|---|
| 无子视图深度跟踪 | `ospf→area→network`、`interface→属性` 整批错位，命令全报 `% Unrecognized`（其实一条没错） | 进块命令先归位系统视图；`ospf→area` 这类**同族嵌套**原地进（`NEST_KINDS`）；普通配置命令留在当前视图；`quit` 只退一级 |
| `[Y/N]` 只报警告不应答 | `port link-mode route`、部分 `undo` 静默不生效 | **白名单式自动应答**：命令前缀命中 `CONFIRM_SAFE_PREFIXES` 才答 `Y`；可用设备级 `"auto_confirm": false` 关闭 |
| 错误正则过宽 | `does not exist` / `is not allowed` 等正常回显被当真报错 | 分两套：`STRICT_ERROR_RE` 只用于**视图状态决策**，宽松 `ERROR_PATTERNS` 仍用于**给人看的报告**；`This subnet overlaps ...`（不带 `%`）现在也能抓到 |

**安全护栏**：命中 `DESTRUCTIVE_RE` 的命令（`reboot`、`reset saved-configuration`、`format`、
`restore factory`）**拒绝下发并明确报错**，绝不自动答 `Y`；这类操作请人工在控制台上做。

进块判定用 `ENTRY_RULES`（视图名 + 精确头部 + 否定词）而不是简单前缀表，因为
`ospf 1`（进进程视图）与 `ospf timer hello 3`（留在原视图）前两个单词相同，
`nqa entry a b` 与 `nqa schedule a b` 同理。

离线自测（不需要 HCL）：上游 `h3c-lab-mcp` 目录里 `python test_session.py`，28 项覆盖
视图嵌套、受控确认、破坏性拒答、严格/宽松报错判定、进块判定。

## 相关

DSH 自带通用 MCP 桥 `@deepseek-ai/dsh-mcp-client`（工具名形如 `mcp__<serverName>__<tool>`，
按 YAML 行配置即可）。本包与它的区别：固定 `h3c_*` 工具名、显式静态参数 schema、
中文错误转译与超时策略。若调用方更想用通用桥接，同一份 `scripts/server.py` 可以直接接过去。
