# dsh-h3c-lab

DeepSeek Harness 的 **H3C / HCL 实验自动化**工具集。两部分：

| 目录 | 是什么 | 形态 |
|---|---|---|
| [`dsh-h3clab/`](dsh-h3clab/) | DSH 插件：把 H3C 设备操作包装成 8 个 `h3c_*` 原生工具 | DSH Profile Bundle（Cordis patch），内含 stdio MCP 服务器 |
| [`skills/h3c-lab-automation/`](skills/h3c-lab-automation/) | DSH 技能：驱动 HCL 设备控制台的脚本 + 踩坑知识库 | Skill（`SKILL.md` + `scripts/` + `references/`） |

两者可以独立使用：技能是"给人/给 agent 读的操作手册 + 命令行工具"，
插件是"把同一套能力变成 DSH 可调用的工具"。

## dsh-h3clab（插件）

- 8 个工具：`h3c_devices`、`h3c_topology`、`h3c_run`、`h3c_facts`、`h3c_verify`、
  `h3c_link_watch`、`h3c_memory_search`、`h3c_apply_plan`（唯一有副作用，默认 `dry_run`）。
- 架构：`lib/`（Cordis 入口 + stdio MCP 客户端 + 工具定义）→ `scripts/server.py`（手写 JSON-RPC，
  只用标准库）→ HCL 设备 telnet 控制台（`127.0.0.1:30000+device_id`）。
- 关键实现点：**视图状态机**（`ospf→area→network` 这类嵌套不错位）、**白名单式 `[Y/N]` 自动应答**、
  **严格错误判定**、破坏性命令拒发。详见插件 README。

安装（把 `D:\path\to\dsh-h3clab` 换成本地路径）：

```powershell
dsh plugin --profile <profile> add D:\path\to\dsh-h3clab
```

桌面应用用的 `desktop` profile 会被 CLI 拒绝（*managed exclusively by the Electron application*），
需在 **设置 → 插件** 里指向本地目录安装。详见 [`dsh-h3clab/README.md`](dsh-h3clab/README.md)。

## skills/h3c-lab-automation（技能）

- `scripts/`：telnet 控制台驱动、端口发现、批量下发、验证矩阵、记忆检索、报告合成等。
- `references/`：`gotchas.md`（按症状索引的坑）、`cases.md`（逐次作业台账）、
  `comware-recipes.md`（常用配置片段）、`environment.md`（环境事实）。
- 用法：放进 `<DSH_HOME>/skills/`（Windows 为 `%USERPROFILE%\.dsh\skills\`），
  目录名即技能名；`SKILL.md` 会被自动发现。

## 环境前提

- H3C Cloud Lab（HCL）已安装并**手动启动**拓扑（HCL 无 API，这一步只能人工点）。
- 设备控制台端口 = `30000 + device_id`；`.net` 拓扑文件是连线的唯一权威。
- Python 3.8+（仅标准库）；Node 22/24（插件侧，运行时依赖由 DSH 宿主提供）。

## 许可

MIT
