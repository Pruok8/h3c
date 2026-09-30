---
name: h3c-lab-automation
description: Use when configuring, verifying, or troubleshooting H3C devices running inside HCL (H3C Cloud Lab) — driving device consoles over telnet, turning a topology diagram plus requirements into per-device configs, pushing those configs, reading back evidence, or automating SecureCRT on this machine. Covers port discovery, the Comware console quirks, batch apply/verify, and the SecureCRT scripting channels that actually work here.
---

# H3C lab automation (HCL + SecureCRT)

你把 HCL 里的 H3C 设备当**普通可编程设备**用：每台设备在 `127.0.0.1:3000N` 上暴露一个 telnet 控制台，可以直接下发配置、读回显、做验证。不需要点 GUI。

## 环境硬事实

| 项 | 值 |
|---|---|
| HCL | `D:\HCL\H3C Cloud Lab.exe`（5.10.3，PyQt5 冻结程序，**无 API**） |
| 设备控制台 | `127.0.0.1:30001` 起，每台一个；端口顺序**不由你决定** |
| HCL 日志 | `D:\HCL\Log\HCLLog\HCL.log`（`create_telnet_server ... telnet_port:3000N` 是权威来源） |
| HCL 虚拟机 | VirtualBox；`VBoxManage list runningvms` 能看到 `topo1-deviceN` |
| SecureCRT | `G:\Study\H3C\crt\SecureCRT.exe`（8.7.2，配置在 `%APPDATA%\VanDyke\Config`，脚本引擎 Python **2.7**） |
| Python | 3.13 —— **没有 `telnetlib`**，所以本 skill 自带 telnet 层 |
| 拓扑与设计 | `D:\DSH\topology\`（`lab1_ts.png/svg`、`links.md`、`h3c-lab-design.md`） |

**开始前先做三步检查**（缺一步后面全是白费）：

1. HCL 在跑吗：`Get-Process | ? ProcessName -match 'H3C|Simware'`
2. 设备起来了吗：`python <skill>/scripts/hcl_ports.py`
3. 如果扫不到端口 → HCL 没启动或拓扑没点"启动"，**这一步只能人工点**（HCL 无 API）。停下告诉用户，别猜。

## 铁律（先读这一节，四条都不可省）

### 1. 省 token 的默认工作方式

**默认：证据落文件，只把结论读进上下文。** 逐项手敲 `display` 再把全量回显拉进上下文，
是这类工作最烧 token 的做法（实测一次完整实验能差一个数量级）。

1. **验证一律用 `scripts/verify.py`**（见下），它每项只输出一行 `PASS/FAIL`，全量回显写进报告文件。
   需要细节时再 `grep` / `read offset,limit` 取那几行 —— **绝不把全量回显拉进上下文**。
2. **下发与回读全部走 `hcl_lab.py --out <目录>`**，回读原文进证据文件；
   分析时读文件，不重跑命令。
3. **能在设备侧过滤就在设备侧过滤**：一律用 `display xxx | include <关键词>`，
   过滤在设备上完成，回显只有匹配行。（注意 `| include` 对多行表格要选对关键词）
4. **状态落盘**：实验根目录维护一份 `state.json`，记
   `端口→设备/型号`、地址规划表、VLAN/接口映射、以及验证矩阵每项的最近结果。
   **新一轮先读它**，不要重新推导、也不要重复全量拉取。
5. **同一症状失败 2 次就换路子**（查官方文档 / 搜案例 / 问用户），不要试第 3 种变体。
6. **汇报短**：每轮只说「改了什么 / 结果 / 下一步」；明细放文档文件，不在对话里铺大表格。
7. **按「输出预算」选命令**（本条最有效）。同一件事，永远用输出最小的那个工具：

| 我要知道 | 别这么做 | 这么做 | 典型输出 |
|---|---|---|---|
| 20 项验证过没过 | 逐项手敲 `display` 再看回显 | `verify.py <清单> --quiet` | **2 行**（全过时） |
| 哪一项挂了、为什么 | 重跑一遍全部 | `verify.py --only <id>`，或 `grep` 报告文件 | 3~5 行 |
| 某台某状态 | `display xxx` 全量 | `ask.py <port> "display xxx \| include yyy"` | 5~15 行 |
| 设备起没起 | 逐个 telnet 试 | `hcl_ports.py` | ~10 行 |
| 某台上联到哪 | 猜 / 通读 `.net` | `net2map.py <lab>.net --port-of <设备>` | 4~8 行 |
| 这坑踩过吗 | 直接搜网页 | `remember.py <关键词> --max 2` | ≤20 行 |
| 记忆里都有啥 | 通读 `gotchas.md` | `remember.py --list` + `grep` | ~75 行 |
| 大文件（PDF/日志/目录清单） | 整篇读进来 | 下载到磁盘后 `grep`，或 `read offset,limit` | 按需 |

**判据：任何一步预期输出超过 ~30 行，先问自己"能不能过滤 / 截断 / 落文件"。**

### 2. 排障优先级：**记忆 → 官方文档 → 外部搜索**（顺序不可颠倒）

出问题的第一动作是**查记忆**（本 skill 里已经踩过的坑），不是猜、不是试、不是直接搜博客。

```bash
python <skill>/scripts/remember.py --list              # 先扫一眼有哪些条目
python <skill>/scripts/remember.py m-lag DOWN          # 多个关键词 = 全部命中（更准）
python <skill>/scripts/remember.py vpn-instance --any  # 任一命中即可（更宽）
```

命中就照做（记忆里有**根因 + 可直接复制的命令 + 验证方式**）。
**没命中再查厂商官方文档**；官方文档也没解决，最后才外部搜索 —— 并在结论里写明
"官方文档未覆盖 / 未能解决"。

| 厂商 | 官方来源 |
|---|---|
| H3C | 官网「配置指导」+「命令参考」（按版本：如 *Comware 7 / S6850*）、`kms.h3c.com` 知识库、`zhiliao.h3c.com` 问答 |
| 华为 | `support.huawei.com` 产品文档（配置指南 / 命令参考） |
| 思科 | `cisco.com` Configuration Guides / Command Reference |
| 锐捷 | `ruijie.com.cn` 文档中心（配置指南 / 命令参考） |

**顺序**：厂商官方文档 → 官方知识库/问答 → 同厂商其他平台案例 → 第三方博客/论坛。
官方文档没解决再往下走，并**在结论里写明"官方文档未覆盖/未能解决"**。

设备侧还有一份"现场文档"：`display xxx ?` 逐层问出**本机实际支持的命令与参数** ——
这比任何文档都权威（同一命令在不同版本可能不同）。**不确定语法就先 `?`**。

### 3. 适配性优先：**能落地才算完成**，不追最新版

同一功能常有多种写法，**老写法/经典写法往往适配性更好**。目标是**最终落地可验证**，不是用上最新特性。

- 一种写法跑不通 → **立刻换另一种等价写法**（而不是在同一个写法上反复调参数）。
  本项目实例：DHCPv6 地址池 `address range` 不生效 → 换 `network` + `gateway-list`；
  业务网段发布 `import-route direct` 与 `silent-interface` 冲突 → 换 `silent-interface` + 区域网段发布；
  两个 VPN 实例能跑但复杂 → 合并成单一 `vpn1`。
- **适配性差的特性不要硬啃**：若某特性在本环境/本版本反复不成立，
  就换等价方案（或降级到经典方案）并在交付里写明取舍 —— **不要为了"用上新版特性"卡住整个交付**。
- 反之，**参考解法/官方示例的写法优先照抄**（它是在该版本上验证过的），
  不要"我觉得更优雅"地自创。

### 4. 交付判定

一项功能只有同时满足才算完成：**配置下发无报错 + 回读有可观测断言通过 + 已 `save`**。
做不到的要么换等价方案（第 3 条），要么写清"官方文档未覆盖 + 已尝试的替代方案 + 需要什么环境才能验证"。

### 5. ★ 收工前必须**写回记忆**（不写等于这次白踩）

**每次配置作业结束前，必须把本次新出现的问题追加进记忆**，否则下次还会从头再踩一遍：

1. 打开 `references/cases.md`，在**末尾追加一个新案例**（`C-00N`），并更新文件顶部的**索引表**。
   每条至少写：**症状（报错原文/你的原话）→ 根因 → 可复制的命令 → 怎么验证 → 版本/环境**。
2. 把其中**可复用的**提炼进 `references/gotchas.md` 对应章节（按症状索引，一条一小节）。
3. 若踩坑涉及"官方文档怎么说 vs 实际怎么表现"，**两边都写**，并标明结论以哪边为准。
4. 记得把更新后的 skill **同步到安装目录**（见"迁移"一节）。

> 判定标准：**下一个终端上的我，只看记忆能不能复现这次的解法。** 不能就说明写少了。

## 工具速查（按"什么时候用"排）

| 时机 | 命令 | 作用 |
|---|---|---|
| 拿到拓扑 | `net2map.py <lab>.net --md topology.md` | 设备表/连线表/Mermaid；**连线唯一权威** |
| 开工 / 换终端 | `doctor.py` | 自检：文件齐全 / 可编译 / 无绝对路径 / 记忆可查 |
| 记状态 | `lab_state.py set <k> <v>` | 落盘 `state.json`，**下一轮先读它** |
| 下发前 | `hcl_lab.py plan.json`（预演）；`--apply --only SW1` | 逐条下发 + 报错检出 + 证据落文件 |
| 对称性回归 | `cfgdiff.py spec.json` | 多设备差异，**能发现"两台都缺"** |
| 改坏了 | `restore.py <snapshot>.txt --section "interface X"` | 从快照回滚单节 |
| 链路掉线 | `linkwatch.py watch.json --fix` | 巡检并按已知手法复位（两端一起） |
| 验收 | `verify.py checklist.json` | 一项一行 PASS/FAIL，全量回显落文件 |
| 交付前 | `cleanup.py cleanup.json` | 审计临时/旁路对象：保留还是清除 |
| 交付 | `report.py spec.json` | 合成交付 Markdown（表格化 + 证据索引） |
| **出问题** | `remember.py <关键词>` | **查记忆（第一动作，见铁律 2）** |
| 收工 | `newcase.py` + `lintmem.py` | 写回记忆 + 记忆体检（见铁律 5） |

## 排错卷（TS）怎么打

先读 `references/ts-playbook.md`：**分层定位树**（物理 → 二层 → 邻居 → 路由 → 标签 → VPN/策略 → 终端设备）
+ 本实验验证过的 **15 个注入点**及其定位命令/判据。
纪律：**一次只改一处；改前 `--snapshot`；每步留证据；定位到根因再动手**（回滚用 `restore.py`）。

## 姊妹技能（多厂商）

本技能是「骨架 + H3C 实现」。要用华为/思科/锐捷时**不要往这里塞**，而是生成姊妹技能：

```bash
python scripts/make_sister_skill.py list
python scripts/make_sister_skill.py huawei --dest <skills 目录>    # 或 all
```

姊妹技能共享同一套骨架：五条铁律 + 记忆机制（`cases.md`/`gotchas.md`/`aliases.md`
+ `remember`/`newcase`/`lintmem`）+ 工具链（`verify`/`lab_state`/`report`/`cfgdiff`/`cleanup`/`doctor`）
+ 通用 telnet 驱动 `hcldrv.py` + 下发引擎 `lab.py`；只留厂商 TODO（`scripts/ports.py`、CLI 配方、平台坑）。
生成后跑 `doctor.py`，它会明确告诉你还缺什么。

## 迁移：整个目录复制即可

本 skill 是**自包含**的：脚本、知识库、案例记忆、环境说明都在同一个目录里，
**复制整个 `h3c-lab-automation/` 目录到新终端即可继续用**，不需要改代码。

```bash
# 新终端上：把整个目录拷进该终端的 skills 目录即可
# DSH 的技能发现路径是 ${DSH_HOME:-~/.dsh}/skills/<名字>/SKILL.md
mkdir -p "$HOME/.dsh/skills"                    # Windows: %USERPROFILE%\.dsh\skills
cp -r h3c-lab-automation "$HOME/.dsh/skills/"   # 拷完目录名即技能名
python "$HOME/.dsh/skills/h3c-lab-automation/scripts/hcl_ports.py"   # 先看能不能发现设备
```

**迁移时需要一起带走的东西（都在这个目录里，别无依赖）**：`SKILL.md`、`scripts/`、
`references/`（含 `cases.md` 案例记忆与 `gotchas.md` 知识库）。
**别只拷 `SKILL.md`** —— 记忆和脚本才是这个技能的价值所在。

已做的可迁移性保证：
- 脚本**不含硬编码绝对路径**；输出文件默认写在脚本所在目录或其上级
  （`crt_probe.py` 可用环境变量 `CRT_PROBE_OUT` 覆盖）。
- 所有脚本只用标准库；入口都是 `python <skill>/scripts/xxx.py <参数>`，端口/设备名一律走参数。
- 知识库与记忆都是纯 Markdown，任何编辑器/终端都能读。

**换终端后需要确认的几件事**（都是环境差异，代码不用改）：

| 要确认的 | 怎么查 | 不一致时 |
|---|---|---|
| HCL 装在哪、装没装 | `Get-Process | ? ProcessName -match 'H3C\|Simware'` | 把新路径补进 `references/environment.md` |
| 设备控制台端口 | `python scripts/hcl_ports.py` | 端口公式仍是 `30000 + device_id`；以 `.net` 文件为准 |
| Python 版本与依赖 | `python -V`；本 skill **只用标准库**，无第三方依赖 | 3.8+ 均可（脚本用 `from __future__ import annotations`） |
| SecureCRT 路径与脚本引擎 | 见 `references/securecrt-automation.md` | 版本不同时按该文档自查通道 |
| 拓扑/实验文件位置 | 读 `<lab>.net` 的绝对路径 | 更新 `references/environment.md` 里的示例路径 |

**记忆/知识库与"具体某台机器"的边界**：`gotchas.md`、`cases.md` 里的技术结论（设备行为、命令写法、
平台限制）是**跨终端通用**的，照抄即可；只有路径、端口顺序、SecureCRT 版本这类会变，
所以环境相关内容都集中在 `references/environment.md`，迁移时**只需改那一个文件**。

## 工作流：拓扑图 + 要求 → 配置

### 1. 读懂输入，先产出两张表

拿到拓扑图（图片/`.svg`/`links.md`）和要求后，**先写出来**再动手：

- **设备表**：设备名 / 型号 / 角色（核心/汇聚/接入/出口）/ 每台的关键接口连接关系
- **规划表**：VLAN、网段、网关、互联地址、LoopBack、路由协议域、策略（ACL/IPsec/VRRP/MSTP 角色）

有歧义或需求自相矛盾时**先问**，别自己选一种默默做（例：设备既做三层网关又要"单臂路由"就是冲突的，正确做法是摆出取舍让用户选）。

### 2. 端口发现（必做，别跳过）

```bash
python <skill>/scripts/hcl_ports.py --json ports.json
```

输出 `端口 → 主机名 → 型号`。**把它和拓扑逐条核对**：拓扑说要 10 台，只扫到 3 台就是有设备没起来。空配设备的主机名都是 `H3C`，重名很正常 —— 那就用型号 + 接口数 + 逐个 `display interface brief` 去认。

### 3. 写配置计划 `plan.json`

每台设备一段。`hostname` 用于定位（推荐），也可直接写 `port`：

```json
{
  "devices": [
    {
      "name": "SW1",
      "hostname": "SW1",
      "commands": [
        "sysname SW1",
        "vlan 10",
        "quit",
        "interface GigabitEthernet 1/0/1",
        "port link-type trunk",
        "port trunk permit vlan 10 20",
        "quit"
      ],
      "verify": ["display link-aggregation verbose Bridge-Aggregation 1"]
    }
  ]
}
```

规则：
- `commands` **只写配置正文**，不要写开头的 `system-view`（脚本自己进；写了也会被跳过）。
- 命令里的 `quit` 可以保留 —— 脚本会在每条命令后看提示符，被踢回用户视图会自动重新 `system-view`。
- `verify` 跑在用户视图，写成你要**回读**的 `display ...`。

### 4. 预演 → 下发

```bash
python <skill>/scripts/hcl_lab.py plan.json                      # 预演，不碰设备
python <skill>/scripts/hcl_lab.py plan.json --apply --save        # 下发并保存
python <skill>/scripts/hcl_lab.py plan.json --apply --only SW1    # 只做一台，便于排错
```

脚本逐条下发并**检出 Comware 报错**（`% Unrecognized command`、`% Wrong parameter`、`% Incomplete command` …），把全过程写进 `lab-evidence-<时间>/<设备>.txt`。退出码非 0 表示有报错，**必须逐条处理**，不许"看起来跑完了"就收工。

**先拿一台做样板**：只对 SW1 `--apply`，确认无误再铺开。配置类实验最忌讳一次全推然后不知道哪儿错了。

### 5. 保存

`--save` 会执行 `save force`（免交互）。**没 save 的配置重启就没了**，实验交付必须 save。

### 6. 验证：按"要求"逐条写验证，而不是只看配置没报错

**把每条要求写成 `checklist.json` 里的一条断言，用 `verify.py` 一次跑完**（输出只有 N 行）：

```bash
python <skill>/scripts/verify.py checklist.json --out verify-report.txt
# PASS  MLAG-SW1   SW1 M-LAG 三组 UP            | BAGG2       2            UP
# FAIL  E2E-A     PC -> Server VM1 通            | 命令超时(45s)
# 合计 19 项：PASS 18，FAIL 1
```

清单写法（模板见 `scripts/verify-example.json`）：
- `expect`: 正则**全部命中**才算 PASS；多行表格要选对关键词（如 `BAGG2\s+2\s+UP`）。
- `expect_not`: **负向验证**，要求一个都不命中（"不该出现的东西不在"）。
- `timeout`: 单独放大某项超时 —— `ping`、`save force`、`undo interface Bridge-Aggregation`
  这类天生慢，默认 15s 会误报 FAIL。
- 同一台设备的多项会**复用同一条连接**（按 `port` 分组），别为每项各连一次。

要求里每一条都要有对应的可观测断言，常见对应关系：

| 需求 | 验证命令 | 期望 |
|---|---|---|
| 聚合/跨设备聚合 | `display link-aggregation verbose Bridge-Aggregation N` | 成员口 **S(Selected)**，无 `I` |
| OSPF 邻居 | `display ospf peer` | 期望的邻居 Full |
| 路由隔离 | `display ospf routing` / `display ip routing-table` | 该出现的网段在，**不该出现的不在** |
| VRRP | `display vrrp` | Master/Backup 与设计一致 |
| MSTP | `display stp instance N brief` | 根桥与设计一致 |
| IPsec | `display ipsec sa` / `display ike sa` | SA 建立 |
| 连通性 | `ping -c 5 <目标>` | 要求"通"的必须通 |

**隔离/负向验证同样重要**：要求"两个网段不互通"，就必须在终端设备上真的 `ping` 一次并记录**失败**结果。只验证正向等于没验证。

### 7. 报告

交付时给：设备表 → 端口映射 → 每台的下发结果（含报错）→ 验证矩阵（命令/实际输出/是否达标）→ 未验证项与原因。证据文件路径要写出来（`present` 出来）。

## 安全规则（别越线）

1. **不猜**。主机名、接口名、端口、VLAN 号一律先读后写（`display interface brief`、`display current-configuration`）。
2. **默认不发**。`hcl_lab.py` 不带 `--apply` 只预演，这是刻意的。
3. **破坏性操作先确认**：`reset saved-configuration`、`undo` 大批配置、`shutdown` 关键链路、`reboot` —— 先说明后果并取得同意。
4. **改前有底**：动已有配置前先 `--snapshot` 抓一份 `display current-configuration` 存档。
5. **删你自己的垃圾**：实验产生的临时文件、写进别人配置目录的会话文件，收工时要清理或明确告知。
6. **不碰 shipped preset 安装目录**（`...@deepseek-ai/dsh-agent-presets\`）；那是部署的，升级会覆盖。

## 两条通道，怎么选

| 场景 | 用什么 |
|---|---|
| 下发配置、读回显、跑验证（**默认**） | `scripts/hcldrv.py` + `hcl_ports.py` + `hcl_lab.py` |
| 需要"在 SecureCRT 里留下痕迹"、抓终端日志、给用户看界面 | SecureCRT 会话 + 登录脚本（见 `references/securecrt-automation.md`） |
| 需要读某个 GUI 程序的对话框文字 / 点按钮 / 截图 | `scripts/click_dialog.py`、`list_windows.py`、`shot.ps1` |
| HCL 拖拓扑、点启动、加设备 | ❌ 无 API，只能人工 |

HCL 控制台**允许并发客户端**：SecureCRT 连着的设备，你的脚本照样能连，不用担心抢占。

## 文件导航

```
scripts/
  hcldrv.py            telnet 控制台驱动（IAC/翻页/auto-config）—— 一切的基础
  hcl_ports.py         端口 → 主机名/型号 发现
  hcl_lab.py           按 plan.json 批量下发 + 报错检出 + 验证 + 证据
  net2map.py         ★ 解析 .net → 设备表 / 连线表 / Mermaid（连线唯一权威）
  verify.py          ★ 一键验证矩阵：每项只打印一行 PASS/FAIL，全量回显写报告文件
  verify-example.json  verify.py 的清单模板（正向/负向/超时/单设备复用连接）
  remember.py        ★ 查记忆：检索 cases.md / gotchas.md / aliases.md
  newcase.py         ★ 写回记忆：生成案例骨架并自动插索引表
  lintmem.py         ★ 记忆体检：索引一致、必备小节、同义词表
  lab_state.py       ★ 状态落盘 state.json（端口/地址规划/验证结果）
  linkwatch.py       ★ 链路巡检：按名单查 UP，掉线可两端自动复位
  cfgdiff.py         ★ 多设备配置差异（**能发现"两台都缺"**）
  restore.py         ★ 从 `--snapshot` 快照回滚单节配置
  cleanup.py         ★ 交付前审计临时/旁路对象（保留还是清除）
  report.py          ★ 把 verify 报告+拓扑+证据合成交付 Markdown
  doctor.py          ★ 自检：迁移后一条命令确认健康
  make_sister_skill.py ★ 生成姊妹技能（华为/思科/锐捷共用同一骨架）
  probe_hcl.py         单台设备最小验证（诊断"到底通不通"）
  selftest_console.py + mock_device.py   不依赖 HCL 的自测（改驱动后必跑）
  diag_console.py      原始字节诊断（怀疑协议层出问题时）
  crt_probe.py         SecureCRT 侧脚本（Python 2 语法）
  make_crt_session.py  生成带登录脚本的 SecureCRT 会话
  click_dialog.py      按文字找对话框、读控件、点按钮
  list_windows.py      枚举窗口与对话框
  shot.ps1             只截目标窗口（PrintWindow，不怕被遮挡）
references/
  environment.md       环境事实与"环境变了怎么重新发现"（★ 换终端只需改这一个文件）
  cases.md           ★ 案例记忆（逐次作业台账：症状原文→根因→命令→证据→版本）
  gotchas.md         ★ 知识库：所有已知坑，按症状索引（症状 → 原因 → 对策）
  aliases.md         ★ 记忆检索同义词表（中英文/报错原文互为同义词）
  ts-playbook.md     ★ 排错卷手册：分层定位树 + 15 个已验证注入点
  securecrt-automation.md  SecureCRT 可用通道、限制、正确的会话配置
  comware-recipes.md   常用 H3C 配置片段与对应验证命令
```

## 改驱动后必须自测

```bash
python <skill>/scripts/selftest_console.py     # 12 项检查，全过才继续
```

它用 `mock_device.py` 假设备覆盖 IAC 协商、提示符识别、回显剥离、`---- More ----` 自动翻页、超时诊断。**假设备测不出**真机才有的 auto-config 行为（见 gotchas），所以改完还要对真机跑一次 `probe_hcl.py`。

## 出问题先看 `references/gotchas.md`

那里按"症状"索引了所有踩过的坑（连上被关闭、设备卡在自动配置、`/SCRIPT` 不支持、端口键写错导致会话"未连接"、登录脚本静默不跑、沙箱写入被拒 …）。遇到反直觉的失败，先查它，别从头试。
