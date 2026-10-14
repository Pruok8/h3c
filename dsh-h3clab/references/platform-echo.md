# 本平台（Microsoft HCL + Comware 7.1.064）已知回显特性

> **写任何验收断言、`display` 命令或"能不能用"的判断之前，先读这一页。**
>
> 为什么要有它：这些结论**每一条都是真金白银试错换来的**——`display vrrp brief` 报
> `Too many parameters`、OSPF 域内路由的 `Proto` 列是 `O_INTRA` 而不是 `OSPF`、
> `undo port m-lag group` 不能带编号……两个人各自重复踩了同一批坑。把它们固化下来，
> 并且**在代码里也有对应常量**：`lib/platform-notes.js`（可 `import`、可生成断言正则）。
>
> 机器可读版本：`import { ROUTE_PROTO, routeProtoRegex, DISPLAY_COMMANDS } from 'dsh-h3clab/lib/platform-notes.js'`。
> 想直接看渲染结果：`platformEchoNotes()`（整篇）或 `platformEchoNotes('mlag')`（单话题）。

表格里 **✅ = 本平台实测**，**— = 未在本平台实测**（Comware 通用取值，别当结论用）。

---

## 1. 路由表 `Proto` 列的取值

| Proto | 含义 | 实测 | 证据 |
|---|---|---|---|
| `O_INTRA` | OSPF 域内（intra-area） | ✅ | `lab20/t2-report.md`、`lab20/verify-final.json`（A 段整表实跑） |
| `O_INTER` | OSPF 域间（inter-area） | — | Comware 通用取值 |
| `O_ASE` | OSPF 外部（Type-1） | — | 本平台实测到的是 `O_ASE2` |
| `O_ASE2` | OSPF 外部（Type-2） | ✅ | `lab20/t2-report.md`：`172.17.10.0/24  O_ASE2  150 1` |
| `IS_L1` | IS-IS Level-1 | — | Comware 通用取值 |
| `IS_L2` | IS-IS Level-2 | ✅ | `lab20/t3-report.md`：`1.1.1.4/32  IS_L2  15  20` |
| `BGP` | BGP 学到的路由 | ✅ | `lab20/t2-report.md` |
| `Direct` | 直连 | ✅ | 所有设备的 `display ip routing-table` |
| `Static` | 静态 | ✅ | 所有设备的 `display ip routing-table` |

**写断言时最容易犯的错**：写死 `OSPF`。本平台根本没有这个取值。用工具生成正则：

```js
import { routeProtoRegex } from 'dsh-h3clab/lib/platform-notes.js'

routeProtoRegex('ospf')          // 'O(?:_INTRA|_INTER|_ASE2?|_SPF)'
routeProtoRegex('ospf', 'isis')  // 'O(?:_INTRA|_INTER|_ASE2?|_SPF)|IS_L[12]'
```

> ⚠️ 这个正则**不能**把整组写成可选（`(?:…)?`）——那样光秃秃一个字母 `O` 也会命中，
> 断言会变成"永远通过"。（第一版就写错成那样，selftest 里有专门的断言盯着。）

真机样本（本次实测，可直接拿去写 `expect`）：

```text
1.1.1.4/32         IS_L2   15  10          100.1.11.2      GE0/1
172.16.100.0/24    O_INTRA 10  2           10.0.31.1       RAGG1.301
172.17.10.0/24     O_ASE2  150 1           10.0.31.2       Vlan301
```

---

## 2. `display` 命令

| 用途 | 命令 |
|---|---|
| VRRP 状态 | `display vrrp`（**不是** `display vrrp brief`） |
| 路由表 | `display ip routing-table` |
| VRF 内路由表 | `display ip routing-table vpn-instance <vrf> <prefix>` |
| M-LAG 概览 | `display m-lag summary` |
| M-LAG 详情 | `display m-lag verbose` |
| IRF 成员 | `display irf` |
| IRF 拓扑 | `display irf topology` |
| IRF 预期配置 | `display irf configuration` |
| BGP peer | `display bgp peer <af>`（`vpnv4` / `ipv4` / `vpn-instance X`） |
| IS-IS / LDP peer | `display isis peer` / `display mpls ldp peer` |

### 要避开的命令 / 写法

| 命令 | 为什么 |
|---|---|
| `display vrrp brief` | 报 `% Too many parameters found at '^' position.` → 用 `display vrrp` |
| `tracert` | 会把控制台卡住（连跑两次后提示符都认不出）→ 验证选路改用 `display ip routing-table`。卡住之后还会**偶发回显错位**（把 tracert 的剩余输出记成下一条命令的输出）；驱动已修，后果变成**明确超时**，见 §8 |
| `ping <VPN 内地址>`（不带 `-vpn-instance`） | 默认查**公网**表，VPN 内地址一律 timeout，会被误判成"单向不通" |
| `peer label-route-capability` | 地址族 `vpnv4` 下报 `% Wrong parameter`（Option C 用不到，Option B 不需要它） |

---

## 3. 不能带编号的 `undo`

以下三条**带编号一律 `% Too many parameters`**（本次撞了 3 次）：

| ✅ 正确 | ❌ 错误 |
|---|---|
| `undo port m-lag group` | `undo port m-lag group 1` |
| `undo port access vlan` | `undo port access vlan 10` |
| `undo port link-aggregation group` | `undo port link-aggregation group 1` |

代码里是 `UNDO_WITHOUT_NUMBER`（含 `wrong` 字段，可以直接拿来做预检）。
运行期预检见 `h3c_apply_plan` 的 pre-flight（destructive / `[Y/N]` / 无端口设备）。

---

## 4. 子接口封装

```text
interface GigabitEthernet0/0.110
 vlan-type dot1q vid 110        ← 必须有 vid
```

- ✅ `vlan-type dot1q vid 110`
- ❌ `vlan-type dot1q 110` → `% Unrecognized command`

代码：`subinterfaceEncap(110) === 'vlan-type dot1q vid 110'`（会做 1..4094 校验）。

---

## 5. M-LAG：哪些字段可信

**结论：M-LAG 在本平台不可用。** 三个组全部 `DOWN (B) = No peer M-LAG interface configured`：
DRCP 通道是通的、配置一致、角色选举正常，**只缺 "peer M-LAG interface" TLV**
（仿真器不在 DRCPDU 里携带它）。已排除：配置不一致（diff 干净）、状态卡死（干净重启）、
加载顺序（同时加组）。验收断言不要写"组 UP"。

`display m-lag summary`：

| 字段 | 可用 | 说明 |
|---|---|---|
| `Peer-link interface` | ✅ | 本端 peer-link 聚合口名（如 `BAGG10`） |
| `Peer-link interface state (cause)` | ✅ | UP/DOWN 与原因 |
| `Keepalive link state (cause)` | ✅ | UP/DOWN 与原因 |
| `Flags`（A/B/C） | ✅ | `B` 是本平台常态，**不是配置错误** |

`display m-lag verbose`：

| 字段 | 可用 | 说明 |
|---|---|---|
| `Local M-LAG interface LACP MAC: Effective` | ✅ | 本机算得对（`0001-0001-0001`） |
| `Peer M-LAG interface LACP MAC: Effective` | ❌ | **N/A** —— M-LAG 组永远起不来的根因 |

两个附带坑（设计时必须写进方案代价里）：

- **keepalive 要起来必须先 `undo stp enable`**：统一系统 MAC 后两端指定桥 ID 完全相同，
  MSTP 平局决胜失效 → 双双 `DESCARDING` → 各发 7 包收 0 包。
- **不要开 MAD**：控制台响应中位从 0.01 s 涨到 0.41 s（41×）且持续恶化；
  还有自锁：keepalive 没起来时 MAD 会把成员口连 keepalive 口一起 shutdown。

`display m-lag summary` 真机样本（本次实测，端口 31018）：

```text
Flags: A -- Aggregate interface down, B -- No peer M-LAG interface configured
       C -- Configuration consistency check failed

Peer-link interface: BAGG10
Peer-link interface state (cause): UP
Keepalive link state (cause): UP
```

证据：`lab20/CAPABILITIES.md` A 节。

---

## 6. IRF：三个命令怎么选

| 要回答的问题 | 用哪条 |
|---|---|
| 合并成功没有？谁是 Master？ | `display irf`（看 `*+1 Master` / `+2 Standby`） |
| 堆叠口 up 没有？邻居对不对？ | `display irf topology`（`IRF-Port1/2` 的 `Link` 与 `neighbor`） |
| 预期配置与当前是否一致、要不要 renumber？ | `display irf configuration`（`MemberID` / `NewID` / 端口成员） |

**取舍**：先 `display irf` 确认成员角色，再看 `display irf topology` 确认堆叠口与邻居；
只有在准备 `renumber` / 对账"预期配置"时才需要 `display irf configuration`。

容易忽略的事实：

- 成员合并成功的标志是 `*+1 Master` / `+2 Standby`（`*` = master，`+` = 当前登录的成员）。
- `display irf topology` 里 `Link` 有 `UP` / `DIS` 两种（`DIS` = 该 IRF-Port 没有连邻居）。
- **`undo shutdown` 堆叠口后必须 `save force`**：否则死循环——端口 un-shut → 链路 UP →
  合并重启备机 → 备机重载 startup-config → 端口又变 ADM。工具全程报 0 报错，
  `display current-configuration` 干净，**只有 `display saved-configuration` 看得出**。

真机样本（本次实测，端口 31010，2 成员 IRF）：

```text
MemberID    Role    Priority  CPU-Mac         Description
 *+1        Master  32        9a7d-a589-0a04  ---
   2        Standby 1         9aa2-0096-0b04  ---

MemberID    Link       neighbor      Link       neighbor    Belong To
 1           UP         2             DIS        ---         9a7d-a589-0a04
 2           DIS        ---           UP         1           9a7d-a589-0a04
```

证据：`lab20/CAPABILITIES.md` A/D 节 + 本次实测。

---

## 7. 控制台 / 工程相关

| 结论 | 说明 |
|---|---|
| **每个 HCL 工程窗口有自己的端口基址** | 先开的 `lab4_ts` → 30001+；后开的 `mylab20` → **31001+**。写死 `30000+device_id` 会连到**另一个工程**的设备上——而且**连得上**，所以看起来像成功。 |
| **同一端口不能两个会话** | HCL 控制台是单条 TCP 流，两个客户端会互相看到对方输出。插件已给每个端口加**进程内互斥锁**（见 README「控制台 per-port 互斥」）。 |
| **重开工程会冲掉配置** | 工程重开会按 `DeviceConfig/*.cfg` 重载起始配置——已配好的一切归零。改拓扑要在 GUI 里手工连线。 |

"端口基址 + device_id" 的正确用法与探测手段见 README
「端口从拓扑推导 / HCL 工程端口自动发现」：`h3c_devices` 支持 `project: "mylab20"`，
配了 `netFile` 时会**按工程路径自动匹配**，并在回显末尾给出 `[verified]` /
`[suspect]` 的**反验**结论（连上了 ≠ 连对了）。

---

## 8. 控制台驱动：残留提示符与"回显错位"

这一节不是 `display` 的取值，而是**驱动行为**（代码：`mcp-server/hcldrv.py`，
test：`mcp-server/test_session.py`）。结论来源是 netmiko 的 Comware 驱动源码注释
（`netmiko/hp/hp_comware.py`，★4306）+ 本地假 telnet 竞态复现，不是猜的。

| 结论 | 说明 |
|---|---|
| **HCL/Comware 会在提示符之后残留"提示符碎片"** | 通道里排着一个多余的 `<HOSTNAME>` 时，下一条命令"等提示符"会**立刻命中它** → 读到的是**上一条命令的输出**，本条的真实输出串到下一条。症状偶发、极难定位（像"设备没输出"或"回显张冠李戴"）。 |
| **判定不能只等提示符** | netmiko 的做法是 `find_prompt()` 之后清一次缓冲，源码注释原文：`# Comware can leave a <HOSTNAME> prompt fragment queued after find_prompt().` / `# Clear the residual prompt so send_command() reads the real command output.`；并且 Comware 因为无法设置终端宽度把 `cmd_verify` 关了（不能靠"回显必须原样出现"防失步），所以那次清缓冲是**必需的补偿**，不是可选优化。 |
| **本仓库的驱动已修** | `Console.clear_buffer()`（`select` 非阻塞，每条命令前清一次）+ `_wait_response()`（提示符命中后还要求"本条命令自己的内容"出现过；只看到光秃秃提示符就再等一拍，宽限 `echo_grace`=2s 后 fail-open，不会卡死）+ `_strip_echo()`（吃掉开头空行/残留提示符行，以及**粘在回显前面**的 `<SW1>display version` 这种碎片）。 |
| **`tracert` 卡住的后果变了** | 以前：`tracert` 迟迟不返回提示符，下一条命令命中它尾巴上的旧提示符 → **偶发回显错位**。现在：会等这条命令自己的回显/输出，等不到就**明确超时**（`ConsoleTimeout`）。**卡住本身仍未解决** —— 我们还没有取消/中断机制。 |

> **尚未做（需真机验证，Lead 已记入待办）**：超时路径加一次**有界的 Ctrl+C 恢复**，
> 才能真正把卡住的 `tracert` 打断。现在只能保证"后果从错位变成明确超时"，
> 不能保证"控制台立刻可用"。

复现与断言（`mcp-server/test_session.py::test_hcldrv_console`）：假 telnet 服务端**精确复现竞态**
（先发残留 `<SW1>`、再发真回显），断言命令读到的仍是本条输出、且计数到 `stale_prompt_waits`；
另有"`clear_buffer` 有残留时读到字节 / 空闲时 <0.2s 返回"、"设备不回显也不卡到超时"、
"折行回显被完整吃掉"、"`line_end` 可切 CRLF"、"启动横幅 `Press Y or ENTER to continue, N to exit.` 被回车放行"。

---

## 维护约定

1. 新发现的能力/回显特性 → **同时**更新本文件与 `lib/platform-notes.js` 的常量；
2. 每条都要写**证据来源**（哪个报告 / 哪次实测、端口号），不要只写结论；
3. 没在本平台实测过的，标 `—` / `verified: false`，别把"通用取值"写成"本平台结论"。
