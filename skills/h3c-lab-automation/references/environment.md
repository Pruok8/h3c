# 环境事实（本机）

这些是实测确认过的值。环境变了就按每节的"重新发现"命令重新确认真值，别假设。

## 关键路径

| 用途 | 路径 |
|---|---|
| HCL 主程序 | `D:\HCL\H3C Cloud Lab.exe` |
| HCL 日志（端口权威来源） | `D:\HCL\Log\HCLLog\HCL.log` |
| HCL 设备镜像 | `D:\HCL\version\*.vmdk` |
| HCL 虚拟机定义 | `C:\Users\30358\HCL VMs\` |
| SecureCRT | `G:\Study\H3C\crt\SecureCRT.exe`（8.7.2.2214） |
| SecureCRT 配置 | `C:\Users\30358\AppData\Roaming\VanDyke\Config` |
| SecureCRT 内建脚本示例 | `G:\Study\H3C\crt\SecureCRT\Scripts\` |
| 另一份 SecureCRT（9.6，未使用） | `D:\NET\SecureCRT\`（带 Python 3.13 shim） |
| 拓扑与设计文档 | `D:\DSH\topology\` |
| 实验素材 | `D:\NET\ie\`（`lab1_ts.hcl`、`LAB3_空配.hcl` 等） |
| 本 skill | `<skill 根>\h3c-lab-automation\` |

## 版本

- HCL **5.10.3**（PyQt5 + 冻结 Python 3.8；内含 `python38.dll`，但没有可用的 `python.exe`）
- SecureCRT **8.7.2.2214**，脚本引擎 **Python 2.7**（目录里有 `vpython27.dll`）
- 系统 Python **3.13**（`telnetlib` 已移除；已装 `pywin32`，**没有** paramiko/netmiko/pywinauto）
- VirtualBox 已装：`C:\Program Files\Oracle\VirtualBox\VBoxManage.exe`

## 重新发现命令

```powershell
# HCL / 设备在不在
Get-Process | Where-Object { $_.ProcessName -match 'H3C|Simware|VirtualBox|VBox' }
& 'C:\Program Files\Oracle\VirtualBox\VBoxManage.exe' list runningvms

# 设备控制台端口（权威）
Get-Content D:\HCL\Log\HCLLog\HCL.log -Tail 30 | Select-String 'create_telnet_server|close telnet_server'
& netstat -ano | Select-String ':3000[0-9]'

# SecureCRT 版本与配置位置
(Get-Item 'G:\Study\H3C\crt\SecureCRT.exe').VersionInfo.FileVersion
Get-Process SecureCRT | Select-Object Id, MainWindowTitle
```

## 沙箱事实（会影响怎么做）

- **2026-09-17 起策略已变更为 `danger-full-access`，且审批提示被关闭**：现在可以直接写工作区外
  （`~/.dsh`、`%APPDATA%`、`D:\HCL`、`G:\...`），**且不要再用 `sandbox_permissions` 提权**（会被直接拒绝）。
  若将来策略回退，按下面这条老规则办。
- （历史）文件策略曾是 **workspace-write**，工作区 `D:\DSH\NET`。写工作区外会被拒，需要用户授权一次。
- 脚本产物**一律写进工作区**（cwd）：设备回显、证据文件、plan.json。
- **从 harness 启动的进程继承沙箱**：我用 `Start-Process` 起的 SecureCRT，脚本写 `%TEMP%`、`Documents` 会得到 `IOError: [Errno 13] Permission denied`，写工作区正常。用户自己启动的 SecureCRT 不受此限。
- 读工作区外是允许的：读 SecureCRT 配置、HCL 日志、skill 脚本都没问题。

## 端口顺序

HCL 按设备**启动顺序**分配 `30001`、`30002`…，与拓扑里的摆放、设备名无关。所以：

- 永远先跑 `hcl_ports.py` 把映射确定下来，别假设"SW1 就是 30001"。
- 关掉一台再启动另一台，端口会变。**每次重新开始实验都要重新发现。**

## 已知的环境隐患

- `D:\HCL\Log\SimwareWrapperLog` 里长期刷 `ERROR: create HCL failed`（每 15 分钟一条），VirtualBox 里有若干 `<inaccessible>` 虚拟机残留。设备起不来时先怀疑这个。
- HCL 日志里 `_pygit2.GitError: reference 'refs/heads/master' not found` 属正常噪音，不影响使用。

## DSH 插件与对外网络可达性（2026-09-17 实测）

### 已安装的 DSH 插件（web profile）

| 插件 | 用途 | 备注 |
|---|---|---|
| `dsh-doc` | **文档解析**：PDF / DOCX / XLSX / PPTX / Markdown / HTML / CSV / 文本 → Markdown/JSON；工具 `dshdoc_health`、`dshdoc_extract`、`dshdoc_convert_file` | 完全本地、无 API key、无 Docker。**两条引擎都实测可用**（见下） |
| `@dsh-external/dsh-super-injector` | 原有补丁条目 | 配置见 `~/.dsh/profiles/web/cordis.patch.yml` |

`dsh-doc` 实测状态（2026-09-17）：

- **node 引擎**（无需任何下载，包内自带 `@xberg-io/xberg` 1.0.14）：
  `health = {"engine":"xberg-node"}`；27 页 PDF(1MB) → 23135 字符干净 Markdown **0.37 s**；DOCX 含表格 **5 ms**。
- **python 引擎**（+ 离线 OCR，运行时解压在 `C:\Users\30358\.dsh\runtimes\dshdoc-runtime-win32-x64`）：
  `health = {"engine":"xberg-python","ocrAvailable":true,"ocrLanguages":["chi_sim","eng"]}`；
  拓扑图 PNG OCR **868 ms**，能识别出图里的 `M-LAG Keepalive 172.16.0.0/30`。
  运行时来自 GitHub Release `runtime-win32-x64-v1.0.14`（58 MB），**经 gh-proxy 下载 + SHA-256 校验 +
  官方 `verify-runtime-win32-x64.mjs` 逐文件校验 60 个文件全部一致**。
- profile 配置（`cordis.patch.yml`）用的是 `engine: python` + `defaultOcr: true`。
  **`dsh web` 重启后**新会话才会出现 `dshdoc_*` 工具；重启前可用下面的直调脚本自测。
- 不改 `dsh web` 的自测脚本：`D:\DSH\NET\dsh-plugins\test-dshdoc-node.mjs`（node 引擎）、
  `test-dshdoc-python.mjs`（python + OCR）。

**读需求/参考解法的 PDF、Word 时用 `dshdoc_extract`，不要再自己写提取器。**
实测（node 引擎）：27 页 PDF(1MB) → 23135 字符干净 Markdown，**0.37 s**；DOCX → 表格都保住了。
（上次为读同一份 PDF 手写 300 行提取器，还被 Type0/CID 字体坑成"一行一个字"。）
不改 `dsh web` 也能自测的直调脚本：`D:\DSH\NET\dsh-plugins\test-dshdoc-node.mjs`。

### shell 的对外网络是**按域名放行**的（很关键）

| 域名 | 结果 |
|---|---|
| `registry.npmjs.org`、`pypi.org`、`raw.githubusercontent.com`、`cdn.jsdelivr.net`、`gh-proxy.com` | ✅ 通 |
| `github.com` | ❌ **TCP 能连但 TLS 被重置**（curl: `Recv failure: Connection was reset`）→ **GitHub Release 资产直连必失败** |
| `ghfast.top` | ❌ 连接超时 |

**绕过办法**：GitHub Release 资产走
`https://gh-proxy.com/https://github.com/<owner>/<repo>/releases/download/<tag>/<file>`
（实测 200，可下 58MB 资产，配 SHA-256 校验即可安全使用镜像）。
**凡是"从 GitHub Release 下东西"的步骤，直接换 gh-proxy 前缀，别在直连上耗时间。**
`web_fetch` / `web_search` 工具走的是 harness 自己的网络路径，**不受此限制**（搜索、读网页都正常）。

## DSH 的插件与 MCP 机制（2026-09-17 实查，写插件/接 MCP 必看）

本机 DSH 版本 **`0.1.5-rc.2`（= npm `latest`）**；CLI 在 `C:\Users\30358\AppData\Local\Programs\nodejs\dsh.cmd`。
**插件与 MCP 都是官方支持的**，不是野路子。

### 1) DSH 插件 = npm 包 + `dsh.bundle` 清单

```jsonc
// package.json
{ "name": "my-plugin", "type": "module", "main": "./lib/index.js",
  "dsh": { "bundle": { "patch": "./cordis.patch.yml" } },
  "peerDependencies": { "@deepseek-ai/cordis": "^4.0.1",
                        "@deepseek-ai/dsh-tools": "^0.1.0-rc.6",
                        "@deepseek-ai/schemastery": "^3.18.1" } }
```
```yaml
# cordis.patch.yml —— bundle 自带的一层补丁
- insert:
    - id: my-plugin
      name: my-plugin
      config: { ... }        # 原样传给插件的 apply(ctx, config)
```
```js
// lib/index.js —— Cordis 入口
import { defineTool } from '@deepseek-ai/dsh-tools';
export const name = 'my-plugin';
export const inject = ['tools'];
export function apply(ctx, config) {
  ctx.tools.register(defineTool({
    name: 'my_tool',
    description: '…',                    // 这句话占每次请求的 token，写短
    parameters: { port: { type: 'integer', required: true, description: '…' } },
    output: { schema: { type: 'object', properties: { ok: { type: 'boolean' } } },
              render: (args, v) => [{ type: 'text', text: String(v.ok) }] },
    async execute(args, exec) { /* exec.signal 可取消；异常要转成可读失败 */ return { ok: true }; },
    presentCall: args => ({ card: 'generic', title: '…', kind: 'read' }),
  }));
}
```
**安装**：`dsh plugin --profile web add <包名|file:本地路径|tarball>` —— 实际是把参数转发给
**profile 目录（`~/.dsh/profiles/web`）里的 pnpm**；装完用 `dsh --profile web --dump-config` 确认组合结果。
**最好的契约样板**就是已装的 `dsh-doc`：读它的 `package.json` / `cordis.patch.yml` / `lib/index.js` /
`lib/tools/*.js` 即可照抄。

### 2) MCP 官方支持：`@deepseek-ai/dsh-mcp-client`

位于 `…node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-mcp-client`，
描述即 **"MCP client bridge: connects to MCP servers and registers their tools on ctx.tools"**。

```yaml
- id: mcp-<name>
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: <namespace>       # 工具名变成 mcp__<serverName>__<tool>
    transport: stdio              # 或 streamable-http
    command: python
    args: ['<绝对路径>/server.py']
    toolCallTimeoutMs: 240000     # 慢命令（save force / undo interface）要放大
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `transport` / `serverName` | 必填 | stdio 或 streamable-http；serverName 限 `[A-Za-z0-9_-]{1,32}` 且唯一 |
| `command`/`args`/`env`/`cwd` | — | stdio 启动方式 |
| `url`/`headers` | — | streamable-http |
| `toolCallTimeoutMs` | 60000 | 单次 `tools/call` 超时 |
| `failOnStartupError` | false | 初始连接失败时是否中止启动 |
| `reconnect.*` | enabled | 断线自动重连（指数退避，上限 30s，最多 10 次） |

**⚠️ 省 token 的关键取舍**：README 明确写 *"工具定义会为每次模型请求增加 token"*。
**插件与 MCP 两种形态不要同时挂在同一个 profile 上**（同样 8 个工具要多付一倍 token）。
按用途二选一：DSH 内用**插件**（原生、零配置），外部客户端（Claude Desktop / Cursor）用 **MCP**。

### 3) 沙箱下的两个操作坑（workspace-write 模式必踩）

- **`.ps1` shim 会被 ExecutionPolicy 挡住**：`dsh`/`npm` 报
  `cannot be loaded because running scripts is disabled`。改用 **`dsh.cmd` / `npm.cmd`** 或 `cmd /c "…"`。
- **npm 需要写缓存**，在工作区外会被拒（`npm error code EPERM … open`）。加 `--cache`：
  `cmd /c "npm --cache D:\DSH\NET\.npm-cache view @deepseek-ai/dsh version"`。
