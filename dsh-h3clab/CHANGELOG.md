# 变更记录

本文件记录 `dsh-h3clab` 的版本变更。版本号同时写在 `package.json` 的 `version` 里
（自测有一条断言盯着两者一致，防止漂移）。

## [0.9.0] - 2026-10-15

**主题：把 20 设备实验的教训**全部**固化回插件 —— 从"事后写文档"变成"下发前就能拦住"。**

上一版（0.8.0）记下了实验踩的坑，这一版把它们**做成了能力**：能自动发现端口、能给控制台加锁、
能在下发前预检、能在下发后回读、能引用平台已知回显。测试 **291 → 409**。

### 新增：下发前预检（`h3c_apply_plan`，dry-run 也跑）

> 实测过：拿一份**故意埋雷**的计划跑 dry-run，5 类问题**全部命中**。

| 规则 | 拦住的真实事故 |
|---|---|
| `reboot` / `reset saved-configuration` 之前没有 `save` | **IRF 死循环 40 分钟**（合并重启后备机重载 startup，端口又变 ADM） |
| 同设备多个 `ospf <n>` 进程号 | 跨 VRF 重号 → `OSPF Process ID already exists in another instance` → 之后该 VRF 所有命令 `% Unrecognized` |
| 黑名单 `undo port m-lag group` / `undo port access vlan` / `undo port link-aggregation group` **带参数** | 三次 `% Too many parameters` |
| 跨设备同网段两个不同 IP | VLAN301 撞车 → 对端 ARP 解析到错 MAC → **丢包 100%** |
| 同命令重复 / 同一接口反复进入 | 计划拼接错误 |

只提示不阻断；破坏性命令仍按原逻辑拒绝。

### 新增：下发后回读校验（`h3c_apply_plan` 的 `verify_changed`）

下发前后各读一次 `display current-configuration` 做多重集 diff（归一化：大小写/空白/
`GE1/0/1`↔`GigabitEthernet1/0/1`/`ip address x 24`↔`x 255.255.255.0`），逐条列出
**"命令发了、配置没变、也没报错"**；另读 `display saved-configuration` 报**"改了但没保存"**。

为什么要它：本次"工具报 0 报错但命令没生效"出现 **4 次**，其中两类（**属性不一致只回不带 `%` 的 warning**、
**`undo shutdown` 后没 save**）**无法靠认提示文本解决**，只能靠回读。
★ 这个校验**绝不拖垮下发**：快照失败/超时只落一行说明，`applied`/`errors` 不变（有专门断言）。

### 新增：HCL 工程与控制台端口自动发现

> 实测：`h3c_devices {project:"mylab20#1"}` → `222ms` 发现 `mylab20=31001-31020` → 探 31001 → `BR-PE` → `[verified]`

HCL 给**每个工程窗口分配一个不同的端口基址**（先开的 30001+，后开的 **31001+**）。
按惯例的 `30000 + device_id` 会连到**另一个工程**的设备上 —— 而且**连得上**，看起来像成功。

- **分层置信度**：显式 ports(100) > `netstat` 实测(80) > 拓扑 `30000+device_id` 推算(40) > 兜底(10)
- **反验**：`h3c_devices` 回显强制带 `[verified]` / `[suspect]` / `[unverified]` / `[unreachable]`；
  期望主机名按**实际选中端口**从 `.net` 反推 → `[suspect] 疑似连错工程：31001 实测 HQ-CORE1 ≠ 拓扑 BR-PE`
- 配了 `netFile` 时**只认工程路径匹配**的窗口，匹配不上就不用 netstat（避免"拿着 lab4 的 netFile 连到 mylab20"）
- 非 Windows / 探测失败只**回退不报错**
- ★ **"连上了"不等于"连对了"** —— 端口发现必须有反验环节

### 新增：控制台 per-port 互斥

并行作业时**没有任何机制阻止两个计划同时往同一个端口下发**（之前全靠人工在任务书里写"只碰这几个端口"）。
现在同端口串行、异端口并行；锁键从**最终参数**算（`apply_plan` 读计划文件取 `devices[].port`，
`verify` 读清单 `checks[].port`）；多键排序申请防死锁；等锁超时给可读错误（含端口、占用者、已等待时长）。

### 新增：内置已知平台回显（`lib/platform-notes.js` + `references/platform-echo.md`）

`O_INTRA`/`IS_L2`/`O_ASE2`、`display vrrp`（`display vrrp brief` 报 Too many parameters）、
三条不能带编号的 `undo`、`vlan-type dot1q vid <n>`、M-LAG/IRF 字段取舍 —— 全部带**证据来源**。
附断言正则生成器 `routeProtoRegex()`（并修掉了"整组写成可选导致**永真断言**"的坑）。

### 修复：对齐 netmiko 的 Comware 驱动（7 条逐条核查）

参照 `netmiko/hp/hp_comware.py`（★4306）里**带源码注释的实测结论**逐条核查。3 条缺口已修：

- ★★ **`find_prompt()` 之后清缓冲** —— 真缺口。`Console.command()` 的 `_PROMPT` 是行尾锚定的，
  通道里排着残留 `<HOST>` 碎片时提示符**立刻命中**，返回的正文其实是**上一条命令的输出**，
  本条输出串到下一条。netmiko 通用层靠"读到回显"防失步，而 Comware 关了 `cmd_verify` 所以**专门补一次清缓冲** ——
  我们等价于"永远 cmd_verify=False"却没有那次补偿，正好落在需要补偿的分支上。
  修法：`clear_buffer()`（select 非阻塞）+ `_wait_response()`（提示符命中后**还要求本条命令自己的内容出现过**，
  2s 宽限后 fail-open）+ `_strip_echo()` 文本层兜底。有**精确复现竞态的假 telnet 服务端**测试。
  → 附带收益：`tracert` 卡住后的**"偶发回显错位"变成"明确超时"**。
- **`RBM_.` HA 前缀**未归一化 → `[RBM_.F1000]` 与 `[F1000]` 被当成"名字变了"，
  `_Session.host` 学到的名字对不上，**子视图判定整批错位**。已按 netmiko 同款剥掉。
- **启动横幅** `Press Y or ENTER to continue, N to exit.` 改为精确识别（原来靠 else 分支盲发 `\r` 兜住）。

已等价不必改的：`save force` 本就免交互、`screen-length disable` 本就用对、无硬回显校验。
`Telnet 行尾` 按**真机实证**保持 `\r`（HCL 三种都认，改 CRLF 反而多送空行），但已参数化。

### 修复：三份 `hcldrv.py` 漂移 + `sync-all` 覆盖面

`skills/h3c-lab-automation/scripts/hcldrv.py` 与 `hcl/skill-src/...` 曾是旧版（缺上面那些修复）——
**skill 才是 agent 会话实际加载的东西**，它落后意味着"插件修好了、skill 还是旧的"。
已对齐，并**把这两个 skill 路径纳入 `scripts/sync-all.mjs` 的同步与 `--check` 校验范围**
（33 → 35 个文件），以后漂移会被自检报出来而不是靠人偶然发现。

### 修复：`mcp-server/selftest.py` 的 3 条过期断言

原来写死"8 个工具 / 1.0.0"，早已过时（现为 13 个 / 1.1.0）→ **静默变红，把真有问题的红淹没掉**。
改为从 `TOOLS` 与 `__version__` **动态取**。

### 文档：README 新增三条作业纪律

6. **测试先行** —— 先写验收清单再派单。本次是"配完才写清单"，而清单一写出来就**立刻暴露 3 个阻塞级设计缺口**，
   本该在派单当天发现。
7. **任务书必须显式写"每条链路的两端各归谁"** —— 本次两个缺口（站点 PE 的 MPLS、ISP 客户侧 EBGP）
   都源于"只写了谁负责哪几台，没写这条链路两端归谁"。
8. **设计事实要有唯一来源** —— 3 处互相矛盾的设计全因"同一事实写在 DESIGN / 任务书 / 验收清单三处"。
   并强调 ★**按名字断言，不要按数量断言**（本次"多线对 == 3"被另外 3 对凑够数，
   于是**整对缺失的堆叠链路照样通过自检**）。

### 测试

291 → **409**：session 125→148、labtools 97→111、config 45、selftest 61→105。

## [0.8.0] - 2026-10-14

**主题：自己设计并实做一套 20 台设备的实验，把暴露的问题和作业纪律固化回插件。**

自己出的题：企业三站点 + 运营商 MPLS L3VPN，20 台设备，覆盖
**堆叠 IRF ×3 组 / M-LAG / VRRP ×2 组 / OSPF / BGP / VPN 双实例**。
工程由 `gen_project.py` 生成（含几何自检），经 HCL 打开后由插件配置。

### 修复：不带 `%` 的"命令没生效"提示被当成成功

Comware 有一类提示**不带 `%`**，引擎以前一律当成功：

```
$ port group interface FortyGigE1/0/53
Please shutdown the current interface first.
```

实际 `display irf configuration` 里 IRF-Port 仍是 `disable` —— 工具报 **0 报错**，
**三组堆叠全部白干**。这种静默失效比报错难查一个量级。
新增 `SILENT_REFUSE_RE`：命中 `Please ... first/before` 这类带顺序条件的整句就报失败，
同时排除 `save` 的正常输出 `Please wait...`。

### 新增：大型实验的并行作业纪律（写进 README）

20 台规模串行做会把时间全耗在等设备重启上。本次实测 IRF 连卡 4 轮
（`port group interface` 静默失败 → 口停在 ADM → 重新激活 → 仍未合并），
串行等的话这 40 分钟里其它 14 台一个字都配不了。固化成五条：

1. **按"设备组"切，不按"阶段"切** —— 写作用域必须互不重叠，否则同一控制台被两人踩
2. **卡住就地交接，不要死等** —— 交给一个作业者继续排查，其它人立刻推进别的组
3. **每步都要回读真实状态**，不要信"0 报错"（附已踩到的静默失效对照表）
4. **计划要幂等可重跑**，`reboot` 前必须已 `save force`
5. **端口基址先探测，别硬编码**（多工程时 HCL 每个窗口一个基址）

### 记录进 lab 记忆库

- IRF 建栈的三条静默失败 + 正确顺序（renumber → **save** → reboot；shutdown → bind → undo shutdown → activate）
- HCL 多工程的控制台端口分配规律与探测方法（`Get-Process SimwareClient` + `netstat -ano`）

### 测试

284 → **291 项**：新增 7 项静默拒绝检测（含 `Please wait` 不得误判）。

## [0.7.0] - 2026-10-11

**主题：把 lab4_ts 从零重做一遍，看还有哪些坑没修。** 结果是——又找出两个，
而且都是"只有真的要重做实验时才会撞上"的那一类。

### 修复

- **用户视图专有命令被送进系统视图（严重）**。
  引擎以前的规则是"非进视图命令一律先 `to_system()`"。但 `reboot`、
  `reset saved-configuration`、`save`、`schedule reboot`、`format`、文件操作
  这些命令**只在用户视图存在**，进系统视图就 `% Unrecognized`。
  实测：重做实验第一步要把 8 台设备恢复出厂，两条命令全部报
  `% Unrecognized command` —— **设备根本清不掉**，只能人工去 HCL 里点。
  新增 `USER_VIEW_ONLY` 前缀表：命中就先 `_to_user_view()` 再下发。
  （`display`/`show` 故意**不**放进去：`display this` 之类必须在当前视图执行，
  强行归位反而弄坏它。）

- **授权了破坏性命令，却答不了它的 `[Y/N]`（死结）**。
  `reset saved-configuration` 被 `DESTRUCTIVE_RE` 挡住要授权；授权之后它又问
  `[Y/N]`，而它不在自动应答白名单里 → 答不了 Y → 命令静默失效（只报"未生效"）。
  现在 `allow_destructive=true` 时，**本次授权过的**破坏性命令也允许自动答 Y。
  授权是调用方显式给的，就该一路放行到确认。

### 真机重做结果（lab4_ts，9 台设备）

```
恢复出厂 : 8 台 reset saved-configuration + reboot force  →  0 报错
清 SW3   : 定向 undo（**保 IRF**，不做 reset）            →  0 报错
全量下发 : 663 条命令 / 9 台 / 0 报错 / 全部 save force
          ★ 计划里一条 `system-view` 归位标记都没有
```

对比第一次做同一套实验：当时靠**手工插 30 多处 `system-view` 标记** + **10 个补丁计划**
才把配置推上去。这次一次过。

### 重做中新暴露的（HCL 环境）

- **M-LAG 的 keepalive 口必须关 STP**。M-LAG 会把两台的系统 MAC 统一成
  `0001-0001-0001`，于是直连的 keepalive 口上**两端的指定桥 ID 完全相同**
  （`Desg.bridge/port : 32768.0001-0001-0001`），MSTP 靠桥 MAC 做的平局决胜失效 →
  **两端都停在 DISCARDING** → 各发 7 个包收 0 个，keepalive 永远 DOWN。
  M-LAG keepalive 是点对点链路，本来就不需要 STP。`undo stp enable` 后立刻 UP。
- **`undo port access vlan` 不能带编号**（`undo port access vlan 4094` 报
  `% Too many parameters`），与 `undo port m-lag group` 同一个坑。
- **MAD 还 enable 的时候删 `Vlan-interface4094` 会把设备卡死**
  （15 分钟超时、控制台无响应）。必须先 `undo mad bfd enable` 再删接口。
- **`tracert` 会把 PC 的控制台卡住**（连跑两次后提示符都认不出），
  改用 `display ip routing-table vpn-instance vpn1 <prefix>` 从控制平面验证选路，更硬。

### 计划固化

`build_plan.py` v2 把之前每个补丁都写回了主计划（keepalive 用 VLAN 100、
keepalive 口关 STP、`acl advanced 3000`、SW3 的 VRRP 网关、去掉 BGP 自环 peer、
`pc_bad_community` 与 server 侧团体拆成两个变量）。**A/B 选路的团体属性写错过一次**：
`route-policy server` 该打 `65000:1`/`65000:2`，我误用了给 `b2o` 团体表准备的
`65001:x`，导致 PE3 的 `community-list c1/c2` 对不上、两条业务都从同一条链路进来
（路由表里两条都是 `100.1.0.13`）。修正后 A→GE0/0、B→GE6/0。

### 测试

275 → **284 项**：新增用户视图命令的视图归位、默认拒绝、授权后放行且能答 Y；
假 Console 补上 `return`（真机上从任意深度直回用户视图）与 `Y`/`N` 应答。

## [0.6.0] - 2026-10-11

**主题：把面板搬到侧边栏，并加一个"实时操作"视图。**

起因：用户一直在侧边栏找插件，找不到 —— 因为我之前只注册了 `settings.section`
（藏在「设置 → H3CLab」三层里），从没占过侧边栏的座位。DSH 的 Slot 拓扑里其实有现成的：

| 座位 | 官方说明 |
|---|---|
| `sidebar.panellist`（list） | "Global panel icons" |
| `main`（keyed） | "Central panel selected by sidebar entry id" |

### 新增

- **侧边栏图标 + 中央面板**。注册 `sidebar.panellist`（id `h3clab`）与 `main`
  （key `h3clab`，必须与侧边栏条目 id 一致，否则点图标打不开）。图标是内联 SVG，
  跟随主题色，不引外部资源。

- **实时操作面板**（就是主面内容）。左栏是 **agent 每次工具调用的流水**
  （工具名 / 参数 / 耗时 / 结果摘要，错误标红），右栏是**最新一次下发的逐条命令 +
  设备回显**（`$ 命令` 高亮、`% Unrecognized` 之类标红）。1.5 s 轮询、自动滚到底、
  可暂停/立即刷新。

### 数据来源

MCP 服务器（`server.py`）现在为**每次工具调用**追加一行 JSON 到
`<stateDir>/activity.jsonl`（`call` / `done` / `error`，含耗时与结果摘要，2 MB 自动轮转）。
面板的 host 路由 `GET /h3clab/api/activity` 读它的尾部，外加 `<evidenceRoot>` 下
最新证据文件的尾部。任何一步失败都返回空数组，**不让只读展示把面板搞成 5xx**。

### 说明

- 客户端 bundle 的改动**需要刷新页面或重启 DSH** 才生效（`patchReload: live` 管
  host 半边；客户端 bundle 只在 `pnpm run dev:web` 时热更）。
- 顺带修了测试进程不退出：实时面板用 `setInterval`，而自测的假 React 不跑
  effect cleanup，定时器把 Node 事件循环吊住了 —— 测试里换成定时器桩。

### 测试

269 → **275 项**：新增"注册三个座位"（并断言 `main.key === sidebar.panellist.id`）、
实时面板拉 `/activity` 并渲染、`/activity` 路由读流水 + 证据、
`/activity` 目录不存在时返回空而不是 5xx。

## [0.5.0] - 2026-10-11

**主题：lab4_ts 真机实验暴露的"静默失效"与安全缺口。** 用插件做了一套 10 设备
MPLS Option B 实验（`D:\NET\ie\e\kongpei\lab4_ts.net`），过程中踩到的每一个坑都
在这里修掉——**它们有一半的共同特征：命令没报错、看着下发成功、其实没生效。**

### 修复

- **`ENTRY_RULES` 缺 `bgp` / `isis` / `address-family` / `ip vpn-instance`**（严重）。
  这四个都是"打开新子视图"的命令，但表里一直没有。后果：它们出现在某个子视图
  （如接口视图）之后时，引擎**不会先退回系统视图**，命令发到了错视图里 ——
  报 `% Unrecognized`、或更糟的**整段静默失效**（BGP 邻居、ISIS 进程）。
  当时是靠手工在计划里插 30 多处 `system-view` 归位标记绕过去的。现在不用了。
  同时给 `isis` 加了接口级子命令的否定表（`isis enable` / `isis cost` / `isis timer` …），
  免得把"接口上使能 ISIS"误判成"进 ISIS 进程视图"。

- **同族嵌套豁免扩展**：`bgp → ip vpn-instance → address-family` 这条链必须一路原地进。
  旧实现只豁免 `area`/`network`，于是 `ip vpn-instance` 会被弹回系统视图，
  `import-route ospf 1` 落进了系统级 VRF 而不是 BGP 里 —— BGP 表空的经典静默失效。
  现在豁免表是 `NESTED_OPENERS = (area, network, address-family, ip vpn-instance)`。

- **批量写工具被 4 分钟超时砍断**。所有工具共用 `toolCallTimeoutMs`（默认 4 分钟），
  而真机上一次 9 台 714 条命令要好几分钟 —— 下发到一半客户端把 python 子进程杀了，
  **设备停在半配置状态、控制台留在某个子视图、结果全丢**。现在工具可以声明
  `minTimeoutMs`：`h3c_apply_plan` 15 分钟、`h3c_verify` 10 分钟，读命令不变。

- **读命令时控制台未归位，只回一个提示符**。上一次调用被超时打断会把会话留在子视图
  （实测 `[sw2-Bridge-Aggregation3]`），此时命令**不会执行**，回显里只剩提示符，
  看起来像"设备没输出"，极难排查。现在显式报错并说明"本命令没有执行 + 怎么恢复"。

### 新增

- **`h3c_apply_plan` 的 `allow_destructive`（默认 false）**。
  以前 `reboot` / `reset saved-configuration` / `format` 一律被拒绝，且**没有任何
  正规途径**在获得授权后执行 —— 这次要给 SW1/SW2 重启破 M-LAG 僵死态时，我只能写一个
  直连脚本绕过插件保护，很不干净。现在可以显式传 `allow_destructive=true`，
  且**放行的每一条都会在结果里回执**（`!! 已放行破坏性命令: …`），不会悄悄做。
  预演（dry-run）的预检也会提前把这类命令标出来。

- **结果里回显收尾视图（`prompt_after`）**。批量下发结束时落在哪个视图，
  以前完全看不出来；超时被打断时这正是最需要的信息。

### 真机验证

```
port link-mode route（S6850，需 [Y/N] 确认）
  修复前：% Unrecognized command，端口仍是二层
  修复后：证据里可见 `Continue? :Y`（驱动就地应答），port link-mode route 生效 ✅

视图链（PE1，计划里**不含任何** system-view 归位标记）
  interface G0/1 → bgp 65000 → ip vpn-instance vpn1 → address-family ipv4 unicast
    → import-route ospf 1 route-policy o2b → isis 1 → interface LoopBack0 → isis enable 1
  9 条命令 0 报错 ✅

破坏性预检：`reboot force` 被标注为"会被拒绝（需人工在控制台做）" ✅
allow_destructive 透传：false / true / false 三个方向逐一断言（进程内猴补，不碰设备）
```

### 测试

253 → **269 项**：新增视图链 13 项（逐条断言命令**发送时所在的提示符**）、
确认白名单、`auto_confirm` 双层同步、`allow_destructive` 透传、按工具超时下限。

## [0.4.0] - 2026-10-10

**主题：面向项目实战。** 上一版把"小改动不烧 token"当卖点，但**真实项目里每次配置
都是大变更**——预演整份计划、整批 diff 全是万级字符。这一版把"每次项目都会发生"的
三笔开销按项目规模重做：**默认不让模型看见它已经有的、或不需要的东西**。

### 度量：项目场景（13 台设备，`bench_tokens.py --project`）

| 场景 | 0.2.0 | 现在 | 降幅 |
|---|---|---|---|
| `hcl_apply_plan` dry-run 预演一次计划 | 7167 字符 | **1719** | **-76%** |
| `hcl_cfgdiff` 批量，每台改 60 行 | 11426 | **1681** | **-85%** |
| `hcl_cfgdiff` 批量，每台改 300 行 | 45252 | **1694** | **-96%** |
| 单台 diff，改 10000 行 | 119176 | **2460** | **-98%** |

换算：13 台每台改 300 行的整批复查，从约 **11.3k token 降到约 0.42k token**。

### 修复

- **`h3c_apply_plan` 的 `buildArguments` 静默丢弃参数**（严重）。
  插件侧只透传了 `plan_json`/`only`/`save`/`dry_run` 四个字段，于是 0.2.0 加的
  `timeout`、`workers` **在工具 schema 里声明了、调用方也传了，却到不了服务器**。
  现在改为 `{...args, dry_run: args.dry_run !== false}`，并加了回归断言。
- **批量 diff 逐台重复打印完整路径**：一条 `与基线对比: C:\...\snapshots\DEV01-….cfg`
  就 120 字符，13 台光路径 3000+ 字符，比正文还长。现在摘要模式只给文件名，
  完整 diff 目录整批只给一次。

### 变更（注意，默认行为变了）

- **`h3c_apply_plan` 预演默认不再回显整份计划。**
  计划文件本来就是调用方写的，回显是纯重复；13 台 × 40 条 = 7000+ 字符，改一次配置
  就重烧一遍。默认只给：逐台条数、每台前 3 条抽样、以及**预检**——破坏性命令、
  会命中白名单自动应答 Y 的命令、未给 `port` 的设备。要看全部传 `echo_commands=true`。
  （预检现在才是有信息量的部分；旧版只是把输入又打印了一遍。）
- **`h3c_run_command` 的 `max_chars` 默认 8000 → 4000**，但**截断时完整回显会写到
  证据文件**并给出路径。以前是"截断就丢"，所以默认只能给到 8000、仍会灌满上下文。
  另外提示优先用 Comware 自带管道（`display current-configuration | include bgp`）。
- **`h3c_cfgdiff` 新增 `detail`（auto|summary|changed|full，默认 auto）和 `grep`。**
  `auto` 下：**单台**给带上下文的统一 diff（照旧），**批量**只给逐台摘要
  （否则 13 台 × 200 行会把上下文灌爆）。`grep` 例如 `"isis|bgp"` 只看某几处改动。
  **完整 diff 一律落盘**（`diff-<设备名>.diff`），任何 detail 都不丢数据。

### 说明

- 工具 schema 体积**基本持平**（0.2.0 的 10217 → 现在的 10187 字符）。本轮又加了
  3 个参数（`echo_commands`/`detail`/`grep`），把上一版的压缩收益又吃回去了。
  **固定开销基本就是这样了**——13 个工具的文档化参数约 2.5k token/轮，再压要牺牲可用性。
  真正能省的一直是**输出**，所以这一版功夫全在输出侧。
- 测试 209 → **239 项**：新增 detail/grep/批量摘要、run 截断落盘、dry-run 预检、
  以及 `buildArguments` 透传的回归断言。

## [0.3.0] - 2026-10-01

**主题：并发与 token。** 把"一台一台串行"改成"一次并发一批"，并把会把上下文灌爆的
输出改成"摘要 + 落盘"。**受控对比（同代码、同会话、同设备，`workers=1` vs `workers=8`）**：

| | 串行 | 并发 | 加速 |
|---|---|---|---|
| `hcl_verify`（6 台 / 12 项） | 16.09 s | **2.83 s** | **5.7×** |
| `hcl_cfgdiff snapshot`（6 台） | 16.27 s | **2.87 s** | **5.7×** |

### 新增：并发

- **`hcl_verify` 跨设备并发**：端口分组后，每个端口一个线程（`workers`，默认 8、
  上限 8），各自建连接；**同端口内部仍串行**（复用一条 telnet 连接）。输出按端口排序，
  并发完成顺序不影响结果。串行版 6 台 12 项要 16 秒，现在约等于"最慢那一台"。
- **`hcl_cfgdiff` 批量并发**：新增 `ports`（数组）+ `names`（同名数组），一次调用并发
  处理多台（`workers`，默认 8）。单台 `port` 形式与输出形状保持不变。
- 两者都保留了"单台失败不拖垮整批"：每台各自成段，末尾给合计。

### 新增：token 控制

- **`hcl_cfgdiff` 的 diff 截断 + 落盘**：`max_lines`（默认 200）限制打印的 diff 行数，
  超出部分写到证据目录并在末尾给出路径。一份几千行的配置 diff 直接进上下文能烧掉
  几万 token，而调用方通常只需要"改了什么、改了多少"。
  `max_lines=0` 可关掉截断。
- **`hcl_report` 默认只回预览**：`inline`（默认 false）时只返回路径 + 行数/字节 +
  前 `preview_lines`（默认 40）行；要全文传 `inline=true`。
- **描述压缩**：工具描述 2676 → 2107 字符（-21%），参数描述 2678 → 2276（-15%）。

**实测 token 账**（`bench_tokens.py`，换算假设 4 字符 ≈ 1 token）：

```
固定开销（每轮请求都要带的工具 schema）
  完整 schema   10217 -> 9574 字符    -6%（≈ -160 token/轮）
    ├ 工具描述   2676 -> 2107        -21%
    ├ 参数描述   2678 -> 2276        -15%
    └ 结构       4863 -> 5191         +7%   <- 新增 9 个参数撑大的

输出（随调用规模增长，这才是大头）
  hcl_cfgdiff diff   变更 <=200 行      321~2370 -> 同左        0%
  hcl_cfgdiff diff   变更 500 行        5670 -> 2454          57%
  hcl_cfgdiff diff   变更 2000 行      23173 -> 2458          89%
  hcl_cfgdiff diff   变更 10000 行    119176 -> 2462          98%
  hcl_report         topology 小节      2940 -> 1718          42%
```

**一处需要自我纠正**：0.3.0 开发中途我曾在提交信息里写"schema 缩减 2%（省约 77 token）"，
那是拿"还没加 `inline`/`preview_lines` 的中间态"当基线量的。以 0.2.0 为基线重测后，
第一次的结果其实是 **+22 字符（不降反增）**——因为新增的 9 个参数把结构撑大了。
补做参数描述压缩后才是现在的 **-6%（≈-160 token/轮）**。
真正的 token 大头始终是**输出**，所以 diff 截断与 report 预览才是关键。

### 修复

- **diff 截断的净收益门槛**：截断提示本身含完整落盘路径（一百多字符），小 diff 上截断
  反而更长。现在只有超出 `max_lines + 8` 行才截断。这是被新增的离线测试抓出来的。
- `hcl_verify` 并发后输出仍按端口排序（`as_completed` 的顺序是随机的）。

### 工程化

- 新增 `mcp-server/bench_tools.py`：真机只读基准，支持 `--repeat`，并内置
  `workers=1` 串行对照组——优化前后可以直接量化。
- 新增 `mcp-server/bench_compare.py`：**从 git 取出旧版 `server.py`**，与新版在
  **同一会话、同一批设备、几分钟内**各跑一遍。拿几天前记下的数字对照容易受设备预热
  影响，这样比才能把变量收敛到"代码"一个；状态/证据目录都指向临时目录，不污染真实目录。
- `verify_plan_loop.py` 第一步改用批量快照 API（一次调用代替 N 次）。
- 测试 190 → **209 项**（`test_labtools.py` 47 → 66：批量目标解析、批量失败路径、
  输出截断与净收益门槛、`workers` 参数）。

### 真机验证

`bench_compare.py`——旧版从 git 现场取出，6 台设备，同一会话内对比：

```
hcl_verify（6 台 / 12 项）     旧 16.20 s  ->  新   2.80 s     5.79x
hcl_cfgdiff snapshot（6 台）   旧 16.81 s  ->  新   2.82 s     5.96x  （旧版要 6 次调用）
hcl_list_devices（6 端口）     旧  2.68 s  ->  新   2.69 s     0.99x  <- 该路径未改动

新版内部对照（同一份代码，只改 workers）：
hcl_verify    串行 16.56 s -> 并发  2.80 s     5.92x
hcl_cfgdiff   串行 16.63 s -> 并发  2.82 s     5.90x
```

`hcl_list_devices` 的 0.99× 是**对照组**：没改动的路径耗时不变，说明测量方法可信，
也反证了 0.2.0 记录里那个"5.26 s → 2.65 s"是设备预热而非收益（当时已注明不记功）。

```
bench_tools.py（6 台，workers=8 vs workers=1）：
  hcl_verify      16.09 s -> 2.83 s
  hcl_cfgdiff     16.27 s -> 2.87 s

verify_plan_loop（R2/模拟终端/FTP服务器，批量快照 + 并发下发/还原）：
  步骤 1 批量快照 3 台 2.7 秒；真下发 0 报错 2.7 秒；还原 0 报错 2.8 秒；
  逐台 diff 精确、逐台还原后 0 差异 -> 12/12 PASS
```

## [0.2.0] - 2026-09-30

**主题：把"能跑"变成"可信"。** 8 → 13 个工具，新增 GUI 面板，并修掉若干个
"测试全绿但真机上就是不对"的缺陷。

### 修复（真机上实测出来的）

- **`hcl_apply_plan` 在用户视图下发整批配置命令**（严重）。
  `_Session.to_system()` 只看提示符的**名字**：`<H3C>`（用户视图）与 `[H3C]`（系统视图）
  名字完全一样，于是"第一次看到提示符"就被判定为已在系统视图，`system-view` 一次都没发。
  HCL 出厂配置下**所有**设备都叫 `H3C`，所以在 hcl_2015 上这是必踩的：整批命令全部
  `% Unrecognized command`。现在**只看提示符的括号**判定视图（`<...>` = 用户视图，
  `[...]` = 系统/子视图）。
- **`_Session.to_user()` 会把控制台一路 `quit` 到登出**。
  同一个原因：判断写成 `p.startswith("<")`，而 `_prompt_name()` 返回的是**裸名字**
  （不含括号），条件永远为假 → 连发 14 次 `quit` → 设备回到
  `Press ENTER to get started.`（真机证据文件里留下了登录横幅）。现在同样按括号判定。
- **`load_config()` 调用了尚未定义的 `log()`**：只要存在配置文件，服务器就在 import 期
  `NameError` 崩溃。也就是说 `H3C_MCP_CONFIG` / `h3c_lab_mcp.json` 这条配置通道
  **从未真正可用过**。
- **UTF-8 BOM 全线踩雷**：配置文件、计划文件、清单文件、`.net` 拓扑都用 `utf-8` 读，
  而 Windows 记事本 / `Set-Content -Encoding UTF8` / `Out-File` 都会写 BOM。`.net` 那个
  更阴——`\ufeff` 不是空白字符，会让第一台设备静默少解析。统一改 `utf-8-sig`。
- **`hcl_link_watch` 把"配置名"当"实测名"显示**：内置设备名表写死的是**另一套 lab**
  （PE1/SW1/SW3-IRF1…），在 hcl_2015 上会把端口 30006 标成 `SW3-IRF1`，而该设备实测
  主机名是 `H3C`。现在对端名字**以实测提示符为准**，配置名只作兜底并标注来源，
  不一致时列告警。内置设备名表已清空（设备名天然属于某一个具体 lab）。
- **按主机名寻址会"随便挑一台"**：`_scan_for_hostname` 命中即返回第一台。在
  "所有设备都叫 H3C" 的现实下等于随机挑一台设备下发配置。现在返回**全部**同名端口，
  多于一个就报错并列出候选。
- **`hcl_topology` 按 mtime 猜拓扑**：`D:\NET` 下有十几个**不同实验**的 `.net`，
  取最新一个会把 A 套的设备表贴到 B 套的验证上。现在不猜，未配置就报错并给出两种修法。
- **配置快照的中文设备名塌缩成同一个 key**：`_snapshot_key()` 把所有非 ASCII 都替换成
  下划线再 `strip("_")`，于是"模拟终端"变成空串、回落成 `device`。两台中文名设备会共用
  `device-*.cfg` 前缀，`h3c_cfgdiff` 不传 `against` 时就会拿**另一台设备**的快照当基线
  （静默比错——与"按 mtime 猜拓扑"同一类危险）。现在保留中文等非 ASCII，只替换文件系统
  禁忌字符，并给 Windows 保留名（CON/NUL/COM1…）加尾下划线。

### 新增

- **5 个工具**（共 13 个）：`h3c_doctor`（环境自检）、`h3c_cfgdiff`（配置快照与逐行 diff）、
  `h3c_lab_state`（跨调用记进度）、`h3c_report`（汇总 Markdown 报告）、
  `h3c_memory_write`（经验写回记忆库，默认 dry_run）。
- **GUI 面板**（客户端半边）：DSH 设置页 → H3CLab，5 个页签（设备 / 链路 / 计划下发 /
  记忆库 / 配置）。host 侧注册同源路由 `/h3clab/api`，面板动作经 `ctx.tools.execute`
  落到工具上。安全边界：工具白名单、真下发需 `confirm:"REAL"`、请求体上限 1 MB。
- **配置全量外露**：`host` / `ports` / `devices` / `netFile` / `evidenceRoot` /
  `referencesDir` / `stateDir` / `serverConfigPath` / `env` 都可配，由插件生成
  `<DSH_HOME>/h3clab/server-config.json` 经 `H3C_MCP_CONFIG` 传给 python。
  面板保存的覆盖写在同目录 `settings.json`，优先级高于 profile 配置。
- **端口从拓扑推导**：不传 `ports` 时按 `.net` 的 `device_id` 推导（`30000 + id`）。
  实测本机有 22 台设备、端口到 30022，旧的固定默认范围 30001-30010 会静默漏报 6 台。
- **`watchPath` 机制**：每次调用前比对服务器脚本的 `(mtime, size)`，变了就结束旧进程，
  用新代码重启。否则"改了 server.py 却不生效"极易被误判成代码写错。
- **stderr 留痕**：python 的崩溃/配置错误不再只进 debug 日志，最后 20 行会附在失败信息里。
- `server.py` 新增 `--show-config`（看配置到底来自哪个文件）。
- `scripts/sync-all.mjs`：三副本（上游真源 / 插件快照 / 仓库副本）同步 + `--check`。

### 工程化

- MCP 服务器真源从 `D:\DSH\NET\h3c-lab-mcp` **并入本仓库** `mcp-server/`
  （此前它不在任何 git 仓库里，只有镜像副本有历史）。
- 测试从 28 项增长到 **190 项**：
  - `test_config.py` 45 项（配置层：BOM、坏配置必须硬失败、不猜拓扑、端口推导、重名报错）
  - `test_session.py` 44 项（视图状态机；**假控制台现在会按视图拒绝命令**，
    并模拟用户视图 `quit` 登出——第一版是视图无关的，所以它抓不到上面那个严重 bug）
  - `test_labtools.py` 47 项（新增 5 个工具的离线回归 + 快照文件名片段 10 项）
  - `selftest.mjs` 54 项（插件本身：协议、超时、懒重启、配置管道、面板 host + client）
- 新增 `mcp-server/verify_plan_loop.py`：真机"下发 → 验证 → 还原"闭环验证脚本，
  支持**单台**与**多台并发**两种模式（`--ports` / `--workers`）。

### 真机验证（HCL，hcl_2015 拓扑，13/22 台可达）

```
h3c_doctor(probe=true)      -> 正确报出 13/22 可达、端口由拓扑推导、
                               以及"设备名映射为空""所有主机名都是 H3C"
h3c_cfgdiff(snapshot,30001) -> 633 行 / 6111 字节，sha1=adc3c4818116
h3c_report(topology,state)  -> 写出 3139 字节 Markdown 报告
verify_plan_loop.py --port 30022 --name R3 --interface GigabitEthernet0/0
  -> 单台闭环：基线 249 行 → 真下发 0 报错 → diff +2 行（description）
     → 反向还原 → diff 0 差异 → 4/4 PASS
verify_plan_loop.py --ports 30014,30015,30016 --names R2,模拟终端,FTP服务器
  -> 多台并发闭环：3 台写进同一个 plan，并发下发 2.8 秒、并发还原 2.8 秒，
    各线程自建控制台连接 / 各自维护视图状态 / 共用同一证据目录均无冲突，
     逐台 diff 精确、逐台还原后 0 差异 → 12/12 PASS
```

### 已知限制

- GUI 面板只验证到"离线可跑"（假 ModuleLoader + 假 React）：外壳格式、导出契约、
  slot 注册、5 个页签渲染、切页、fetch 路径。真实 React 渲染与真实 Slot 挂载需重启 DSH 确认。
- `package.json` 变更（含 `dsh.client`）需要重启 DSH 才生效；之后只改 `lib/client.js`
  由客户端 HMR 热替换。
- `h3c_verify` 是**逐台串行**的（同端口复用一条连接）；需要跨设备并发请拆成多个
  清单或用 `h3c_report`。
- 未发布到 npm（本版本只准备发布物料，不执行 `publish`）。

## [0.1.0] - 初始版本

第一期：把 MCP 服务器（`server.py`）的 8 个工具桥成 DSH 原生工具
（`h3c_devices` / `h3c_topology` / `h3c_run` / `h3c_facts` / `h3c_verify` /
`h3c_link_watch` / `h3c_memory_search` / `h3c_apply_plan`），含 stdio MCP 客户端、
超时与懒重启、插件自测与安装脚本。
