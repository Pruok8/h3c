# 案例记忆（每次配置作业后追加）

> **这是"记忆"的第一层。出问题时的第一步就是查这里，不是查文档。**
>
> 顺序：**本文件/gotchas.md（记忆） → 厂商官方文档 → 外部搜索**。
> 查法：`python scripts/remember.py <关键词>`（同时搜本文件与 gotchas.md）
>      `python scripts/remember.py --list`（列出所有条目标题，先看有没有对得上的）
>
> 写法要求：**症状用报错原文或你的原话**，这样关键词才好命中；
> 每条必须给**可直接复制的命令**和**验证方式**。
> 记录完记得把可复用的提炼进 `gotchas.md`（那是按症状索引的知识库，本文件是逐次作业的台账）。

## 索引

| 编号 | 日期 | 场景 | 一句话症状 | 关键词 |
|---|---|---|---|---|
| C-001 | 2026-09-17 | MPLS Option B 跨域 + M-LAG（HCL 5.10.3 / S6850+S5820V2+MSR36） | 十台设备从零配到端到端互通，中途踩了 20 处坑 | m-lag, DOWN(B), consistency, MAD, 卡顿, vpn-instance, DHCPv6, community, LACP |
| C-002 | 2026-09-23 | 园区综合实验：IRF+BFD MAD、二层/三层动态聚合、DHCP server、PBR+NQA 双出口、OSPF/RIP 双域（HCL 5.10.3 / S6850+S5820V2+MSR36-20） | 配置命令全报 `% Unrecognized` 其实一条没错，是**下发工具从没进 system-view**；最后**明知故犯启用 MAD 把两台核心控制台拖死** | 未识别命令, system-view, 视图错位, irf-port 交叉, ISOLATE, irf-port-configuration active, MAD 卡顿, 控制台刷死, 聚合属性不一致, 子视图, quit, PBR, NQA, track, PPPoE 串口, acl advanced |

---

## C-001 MPLS Option B 跨域 + M-LAG 总部（HCL 5.10.3）

**场景**：`lab4_ts.net`，10 台设备（PE1/PE2/ASBR/PE3/PC/SW3-IRF1/IRF2/SW1/SW2/Server）。
需求：M-LAG+VRRP、IRF2.0+BFD MAD、OSPF(P2P+BFD)、ISIS L2、MP-IBGP/MP-EBGP Option B、
MPLS LDP、A/B 选路、IPv6 SLAAC/DHCPv6、SNMPv2c+scheme、业务网段无动态路由报文。
**最终结果**：全部达成，PC↔Server 双向 0% 丢包。

设备控制台：`127.0.0.1:(30000+device_id)`；SW1=30008 SW2=30009 SW3-IRF1=30006 SW3-IRF2=30007
PE1=30001 PE2=30002 ASBR=30003 PE3=30004 PC=30005 Server=30010。

### 1) 链路层

**1.1 HCL 虚拟链路会整体自行掉线（不是重启）**
- 症状：SW1/SW2 的 `GE1/0/20/21/22` 连同对端 PE1/PE2 的 `GE0/0+GE6/0`、Server 的 `GE1/0/17/18`
  **同时**变 DOWN，而 peer-link(17/18)、keepalive(19) 不受影响；`display version` 的 uptime **不变**
  （排除重启）。`display interface <口>` 的 `Last time when physical state changed to down` 能拿到掉线时刻。
- 解法：对这批端口（**两端都要**）`shutdown` → 等 15s → `undo shutdown`。
  只复位一端会"只起一半"。判据：`display interface brief` 出现 `1G(a) F(a)`。
- 关联：MAD 开启时该现象更容易出现（见 1.4）。

**1.2 成员口加入聚合口前属性必须一致**
```
[SW1-GigabitEthernet1/0/20] port link-aggregation group 3
Can't assign the port to the aggregation group because its attribute configurations
are different than the aggregate interface.
```
- 根因：端口已设 `port access vlan 30`，而聚合口还是默认（access vlan 1）。
- 解法：**加入之前**让两者属性一致。稳妥顺序：
  `undo interface Bridge-Aggregation N` → 重建 → 成员口 `port link-type trunk` +
  `port trunk permit vlan ...`（或 `port access vlan X`）→ `port link-aggregation group N`
  → 最后再配聚合口的 trunk/access/mode/`port m-lag group`。

**1.3 从聚合组里摘端口：`undo` 不带编号**
```
undo port link-aggregation group 1     → % Too many parameters found at '^' position.
undo port link-aggregation group       → ✅
```
`undo port m-lag group 2` 同理（也是不带编号）。带编号只在**配置**时用。

**1.4 ★ MAD 开启会拖死设备，并发导致聚合协商楔死**
- 症状：调过 MAD 之后 IRF **控制台卡死**（下发 0 条、`ConsoleTimeout`），
  且 IRF 面向 PC 的 `Bridge-Aggregation 2` 两侧本地标志只剩 `{A}`/`{AC}`、聚合 DOWN、
  PC 全部业务子接口 DOWN。**复位端口无效**。
- 根因：参考解法原文——*"实验环境建议关闭 mad 口，**mad 开启设备会卡顿**"*（`< shutdown 0/19 口 >`）。
- 解法（两步，缺一不可）：
  1. `interface GigabitEthernet 1/0/19` / `2/0/19` → `shutdown`（MAD 口，按参考解法指引）；
  2. **删除重建那个楔死的聚合口**：`undo interface Bridge-Aggregation 2` → 重建 →
     成员口设好属性后 `port link-aggregation group 2` → 再配聚合口。恢复后两侧 `{ACDEF}` Selected。
- **收尾必须 `save force`**，否则重启后 MAD 又启用、又卡顿。
- 官方文档没覆盖这条（参考解法与外部案例都提到），**官方文档未能解决 → 采用经验做法**。

### 2) M-LAG

**2.1 ★★ 三组恒 `DOWN (B)`，配置逐行对称——真凶是"一致性检查"**
- 症状：`display m-lag summary` 三组 `DOWN (B)`（`B` = No peer M-LAG interface configured），
  但 peer-link UP、keepalive UP、DRCP 统计正常（`*BAGG1 UP Sent 112 Received 109/0/0`）、
  `display m-lag system` 能读到对端全部系统参数；两台 `display current-configuration` 逐行 diff
  **只有主机名/环回/VLAN 地址等预期差异**。
- 根因：`display m-lag consistency-check status` = **Enabled + Strict**，HCL 模拟器**误报配置不一致**
  并据此**关闭 M-LAG 接口**（症状既可能是 `DOWN (C)`，也可能表现为 `DOWN (B)`）。
- 解法：
  ```
  system-view
  m-lag consistency-check disable          # 两台都要
  # 再复位成员聚合口触发重新协商
  interface Bridge-Aggregation 2 / shutdown / undo shutdown   # 2/3/4 各来一遍，两台都做
  ```
  之后 `display m-lag summary` 三组 `UP`，接入侧 `display link-aggregation summary`
  的 `Partner ID` 变成 M-LAG 虚 MAC（如 `0x64, 0001-0001-0001`）、`Selected Ports` 由 0 变 2。
- 证据：同平台同症状外部案例
  [H3C交换机S6850配置M-LAG基本功能](https://blog.csdn.net/gtj0617/article/details/135542399)
  （环境与本机完全一致：HCL 5.10.1 + S6850 / Version 7.1.070 Alpha 7170）——
  原文：*"可能是模拟器问题…会导致配置一致性检查而关闭 M-LAG 接口…可以暂时关闭 M-LAG 配置一致性检查"*。
- 教训：**先试重建 peer-link/重建成员聚合口/清 MAD 闩锁，全都不是根因**；
  第 10 多轮才去搜案例。**同一症状失败 2 次就该搜**。

**2.2 链路闪断会把 M-LAG 闩死在 MAD DOWN**
- 症状：配置全对但三组 `DOWN (B)`；`display m-lag summary` 的 `Local state` 与 `Peer state` 自相矛盾。
  真凶要用 `display m-lag role` 才看得到：`MAD DOWN state: Yes`（Effective role 变成 Secondary，
  与配置的 Primary 相反）。
- 解法：`m-lag mad restore` 有守卫 —— 两条链路都 UP 时会拒绝
  （`Can't execute this command when the peer link or keepalive link is up.`）。
  顺序必须是：**先关 keepalive（GE1/0/19），再关 peer-link（BAGG1）** → `m-lag mad restore`
  → 逐条 `undo shutdown`。

**2.3 M-LAG 排错的四条必查命令**
| 命令 | 看什么 |
|---|---|
| `display m-lag role` | `MAD DOWN state` 是否 Yes |
| `display m-lag drcp statistics` | peer-link 行 `Sent/Received`，非 0 即 DRCP 通 |
| `display m-lag system` | 能否读到对端 system-number/MAC/priority |
| `display m-lag consistency-check status` | 是否 Enabled + **Strict**（见 2.1） |
（`display m-lag drcp` / `display m-lag consistency` 是**不完整命令**，要接 `statistics` / `type1|type2`。）

### 3) 路由 / VPN

**3.1 ★ OSPF「邻居 Full 却一条路由都算不出来」**
- 3.1a 两端 `ospf network-type` 不一致（一端 `p2p`、一端 `broadcast`）：
  邻居 `Full/ -`，LSDB 里对端 Router-LSA 完整，但 `display ospf 101 routing` = `Total nets: 1`。
  判据：自己 Router-LSA 里那条链路是 `Link Type: P2P`（Link ID = 对端 router-id）还是
  `TransNet`。→ 两端统一 `broadcast`（本环境 p2p↔broadcast 能 Full 但不算路由；
  两端都 p2p 反而连邻居都建不起来）。
- 3.1b **Hello/Dead 不一致**：`display ospf peer` 直接**空输出**（邻居根本建不起来）。
  一端 `ospf timer hello 5 / dead 20`、另一端默认 10/40。→ 两端同值或都回默认。
- 教训：**只在一边看 `display ospf peer` 看到 Full 是不够的**，要把两端
  `display current-configuration interface <if>` 逐行对。

**3.2 同一个 VPN 实例里，两端的 OSPF 进程号可以不同**
`ospf 1 vpn-instance vpn1`（SW1/SW2/IRF）与 `ospf 200 vpn-instance vpn1`（PE1/PE2）能正常建邻 ——
只要**接口所在路由表、area、Hello/Dead 一致**。反过来，`display ospf 1 peer` 查不到内容时，
先怀疑"这个进程号属于 vpn 实例而 display 解析到了全局"，直接回读接口配置确认。

**3.3 ★ PE 侧与 SW 侧不在同一个路由表 → 总部业务网段进不了 VPN**
- 症状：PE1 RAGG1（VPN 实例内）与 SW1/SW2 的 SVI（全局表）**永远建不起 OSPF 邻接**，
  总部业务网段进不了 vpn1，端到端不通。
- 解法：把 SW1/SW2 的业务 SVI 也纳入同一个 `vpn1`（本课题做法），
  即**一条 VPN 链路的两端必须在同一实例**。参考解法把接入侧放全局、PE 侧放 vpn1，
  在 HCL 上实测建不起邻接 —— **以本环境实测为准**。

**3.4 ★ PE3 的 BGP AS 号用错，导致整个 vpn 实例没生效**
- 症状：PE3 上 `ip vpn-instance vpn1` + `import-route` 全部"下发成功"，
  但 PE1/PE2 学不到分部路由；`display current-configuration configuration bgp` 里
  **根本没有 vpn1 实例**。
- 根因：PE3 属于 **AS 65001**，我敲了 `bgp 65000` → 那些命令进了错误/不存在的进程。
- 解法：`bgp 65001` → `ip vpn-instance vpn1` → `address-family ipv4 unicast` → `import-route ...`。
- 教训：**动 BGP 前先 `display bgp peer` 看 `Local AS number`**，别凭记忆。

**3.5 跨域 eBGP peer 指向了自己的接口地址**
- 症状：ASBR 上有一条 MP-EBGP 恒 `Idle`（`100.1.0.18`），而 `100.1.0.18` 正是 ASBR 自己的接口地址。
- 根因：本拓扑里 PE3 = `100.1.0.13/100.1.0.17`、ASBR = `100.1.0.14/100.1.0.18`，我把对端写反了。
- 解法：ASBR 侧 peer 改 `100.1.0.17`，并在 PE3 侧补齐对应的 `peer 100.1.0.18`（vpnv4 AF）。
  修完 ASBR 4/4、PE3 2/2 Established。

**3.6 ★ `ip binding vpn-instance` 会清掉接口上的其它配置**
- 症状：下发时回显 `Some configurations on the interface are removed.`，
  事后 `display ipv6 interface brief` 全部 `Unassigned`，**IPv4 地址、接口级 OSPF、
  `ipv6 dhcp select server`、`ipv6 nd ...-flag` 全没了**。
- 解法：绑定 VPN 之后**把该接口的 IPv4/IPv6/OSPF/DHCP 配置整体重下一遍**并回读确认。
  顺序：`undo ip address` → `ip binding vpn-instance X` → `ip address ...` → `ipv6 ...` → `ospf ...`。
- 注意：`ip binding vpn-instance` **换绑**时也要 `undo ip binding vpn-instance` 再重绑，
  否则绑定不会变（本次吃过：IP 变了但绑定还在旧实例）。

**3.7 同进程号不能同时存在于两个 VPN 实例**
```
OSPF Process ID already exists in another instance.
```
跨实例要么换进程号（`ospf 100 vpn-instance A` / `ospf 101 vpn-instance B`），
要么按参考解法只用一个 `vpn1`（RD/RT 100:1）—— 后者才是这类 LAB 的常规答案。

### 4) A/B 选路（BGP 社区分流）

**4.1 分流不生效的两个真因（都要满足）**
1. 两条跨域链路的 **import 策略必须不同**：
   链路1 `route-policy server1 import`（惩罚 c2=VM2）、链路2 `server2 import`（惩罚 c1=VM1）；
   两条都写 `server1` = 没分流。
2. **每一条相关 eBGP 链路都要 `advertise-community`**：只配一条 → 社区只在一条链路传递 →
   另一条上 `if-match community` 匹配不到 → 仍选同一条路径。
- 验证：`display bgp routing-table vpnv4 10.10.1.0`（看 `Community`/`From`）+
  `display ip routing-table vpn-instance vpn1`（看两个网段出接口是否不同）。
- 修好后的实测：`10.10.1.0/24 BGP via 100.1.0.14 GE0/0`、`10.10.2.0/24 BGP via 100.1.0.18 GE6/0`。

### 5) IPv6 / DHCPv6

**5.1 ★★ DHCPv6 服务端在「绑定了 VPN 实例的接口」上收不到报文**
- 症状：服务端配置齐全（池、`ipv6 dhcp select server`、`ipv6 dhcp server apply pool`、
  M/O 标志、RA），客户端也确实在请求，但两边计数器对不上：
  ```
  客户端: Packets sent : 7  Solicit : 7   /  Packets received : 0  Advertise : 0
  服务端: Packets received : 0  Solicit : 0
  ```
  且 `ping ipv6 -i <该 SVI> ff02::1 / ff02::2` **100% 丢包**（同链路 IPv6 单播 0% 丢包）。
- 决定性对照实验：在**同一条链路**上另建一个**全局表** SVI（同 VLAN 或另开 VLAN 均可），
  配同样的 DHCPv6 服务端 —— 客户端**立刻拿到地址**、`State: OPEN`、
  服务端 `Ip-in-use: 1 / Packets received: 2 / Solicit: 1`。⇒ **根因就是接口上的 `ip binding vpn-instance`。**
- 本平台**没有** DHCPv6 的 VPN 写法（用 `?` 逐层确认）：
  `ipv6 dhcp server apply ?` 只有 `pool`；`ipv6 dhcp pool ?` 只接受池名。
- 解法：业务 IPv4 必须进 VPN 时，业务 SVI 保持绑 vpn1，**另开一个全局网段专门跑
  SLAAC/DHCPv6**（本次：IRF↔PC 之间加 `vlan 900` + 全局 SVI + DHCPv6 服务端，
  PC 侧加对应子接口 `ipv6 address dhcp-alloc`）。
- 注意：**SLAAC 在 VPN 绑定接口上是能用的**（只需要网关→主机方向的 RA），
  所以 SLAAC 可留在原 SVI；**只有 DHCPv6 需要主机→服务器方向的组播，才会失效**。

**5.2 DHCPv6 池用 `network` + `gateway-list`，不要用 `address range`**
`address range` 形式在本镜像上 `Total address number: 0`（不生效）。

**5.3 只配 `ipv6 dhcp select server` 不够，还要绑池**
`display ipv6 dhcp server` 若显示 `Pool: global`，客户端会一直卡在 `SOLICIT`。
接口视图补 `ipv6 dhcp server apply pool <池名>`，之后应显示 `Vlan-interfaceX <池名>`。

**5.4 SLAAC 生效三条件**
① 网关侧接口有同前缀 IPv6 地址；② 网关侧 `undo ipv6 nd ra halt`（默认 ra halt 不发 RA）；
③ 客户端 `ipv6 address auto`。成功标志：`display ipv6 interface brief` 出现
`... subnet is 1:0:0:2::/64 [AUTOCFG]`。

**5.5 判定"是配置问题还是模拟器问题"的两步法**
① 两侧计数器对账（客户端发了 N 个 / 服务端收 0 个 = 中间组播转发有问题，池和绑定都是无辜的）；
② 直接实测 `ping ipv6 -c 2 -i <出接口> ff02::1` / `ff02::2`
（**必须带 `-i <出接口>`**，否则报 `Outbound interface is required for the ping of a multicast address.`）。

### 6) 终端与周边

**6.1 Server 侧要默认网关**：Peer/PE 能 ping 通本机 SVI 但 ping 不通 Server 时，
先看 Server 的路由表有没有默认路由。补 `ip route-static 0.0.0.0 0 <VRRP VIP>`。

**6.2 PC 进 VPN 后 ping 要带 `-vpn-instance`**：业务子接口绑进 vpn1 后，
不带 `-vpn-instance vpn1` 的 ping 走全局表、无路由 → **假失败**（本次据此误判过一次"端到端断了"）。

**6.3 保存提示语因平台而异**：SW1/SW2 是 `Saved the current configuration to mainboard device
successfully.`，PE/ASBR/PC/IRF 是 `Configuration is saved to device successfully.`
检查脚本不能只匹配一句，否则会误判"未保存"。

**6.4 IRF 备机（SW3-IRF2，30007）控制台偶发超时**：其配置与主机共享，
主机 `save force` 成功即视为已保存；判断状态时可直接读主机，必要时再连备机交叉验证。

**6.5 HCL 链路通不通，以 `.net` 文件为权威**
端口 = `30000 + device_id`，逐端口连线写在 `<lab>.net`（INI 风格，形如 `GE_0/0 = SW1 GE_0/20`）。
排查"某链路为什么不通"**第一步就核这个文件**，别凭 `display interface brief` 猜。
本次实例：`D:\NET\ie\e\kongpei\lab4_ts.net`（主机上历史实验在 `D:\NET\ie\...`）。

### 版本 / 环境
- **环境**：HCL **5.10.3**（PyQt5 冻结程序，无 API）；设备控制台 `127.0.0.1:(30000+device_id)`。
- **设备与版本**：SW1/SW2 = **H3C S6850，`Comware 7.1.070, Alpha 7170`**；
  SW3-IRF1/2 与 Server = S5820V2-54QS-GE；PE1/PE2/ASBR/PE3/PC = MSR36-20。
- **外部对照案例的环境**：HCL **5.10.1** + 同款 S6850 / 同版本 `7.1.070 Alpha 7170`
  （M-LAG 症状完全一致 → 是 S6850 模拟器的通病，与 HCL 小版本无关）。
- **官方文档 vs 实际表现**：以下三条 **H3C 官方配置指导未覆盖**（官方按真机行为描述），
  结论以本环境实测为准：① M-LAG 一致性检查误报；② MAD 开启导致设备卡顿；
  ③ DHCPv6 服务端不支持 VPN 绑定接口。

### 沉淀结果
本案例的可复用条目已提炼进 `references/gotchas.md`（A5/I1–I9/J1–J2/K1–K5/L1–L4 等）。
新增本案例时请同时：把新技术坑写进 gotchas 对应章节，并更新本文件顶部的**索引表**。

## C-002 园区综合实验（IRF + 双出口 + OSPF/RIP 双域）——工具链自身的坑

**场景**：`hcl_2015.net`，22 台设备（S6850×5、S5820V2×3、MSR36-20×5、AC/AP×2/PC×4/Phone×2）。
需求：IRF+BFD MAD、下联二层动态聚合、接 R1 三层动态聚合、DHCP server 在核心、
AC 旁挂本地转发、双 ISP（联通固定专线 + 移动 PPPoE）、NAT + nat server、
PBR+NQA 按终端选出口、PC1/PC4 同网段隔离、OSPF area0/area1 + 静默接口、SW8↔SW9↔JR3 RIP、
仅 PC3 可 SSH、配置备份到 FTP。
**结果**：IRF 成型（member1 Master / member2 Standby）、二层/三层聚合全 Selected、
**OSPF area0 与 area1 邻居都 Full**、RIP 域到 FTP 网段全程可达（核心 ping 通 192.168.200.100）、
R1 联通与移动链路均 ping 通、nat server 已配。
**未完成**：核心 DHCP 池/PBR 的最后一轮补齐未确认落盘；AC/AP/PC 的控制台端口一直没监听
（device 3/4/11/12/18/19/20/21 的 telnet 从未 LISTENING），无线与端到端验证做不了。
**代价最大的一步**：明知 gotchas K3 写了"HCL 上 MAD 开启会拖死设备、参考解法要求 MAD 口 shutdown"，
仍然把 MAD 激活，HX1/HX2 两个控制台卡死、CPU 97%，最后整个 HCL 只能退出。

### 1) 下发工具（最大的坑，浪费最多时间）

**根因（一句话串起本章）**：下发引擎把"每条命令后等提示符"当成了足够的安全保证，
却从不确认自己处在哪个视图、也不跟踪子视图嵌套；再加上它用提示符文本反推主机名、
用宽泛正则判定报错、在两条命令之间发裸字节，于是"命令没错、视图全错"，
并把大量假报错写进结论，掩盖了真正的问题。

**1.1 ★ 配置命令全报 `% Unrecognized`，但命令本身没错 —— 工具从没发 `system-view`**
- 症状：`sysname HX1` / `interface FortyGigE 1/0/53` / `shutdown` 全报
  `% Unrecognized command found at '^' position.`，看起来像"这批命令不支持"。
- 真因：控制台连上后停在**用户视图** `<H3C>`，工具只按"每条命令后等提示符"下发，从未进系统视图。
- 判据：回读 `<H3C>` vs `[H3C]`；`display` 类命令正常、配置类全废 → 100% 是视图问题，不是版本问题。
- 解法：下发引擎必须**显式进 system-view 并确认提示符变成 `[...]`**；每条配置命令前校验当前视图。

**1.2 ★ HCL 控制台跨 telnet 会话保持 CLI 视图**
- 症状：新连接进去后 `area 1` 报 Unrecognized，但同一串命令用"原始 socket 手敲"完全正常。
- 真因：上一轮把设备留在 `[SW8-ospf-1]`，新会话继承了这个视图；工具再从提示符反推
  "主机名"，把 `SW8-ospf-1` 当成了主机名，子视图判定全错。
- 解法：连上后**先 `return` + `quit` 回到用户视图**，再取主机名（只取 `-` 前第一段）；
  并且**不要发裸 `\r` 去唤醒控制台**（见 1.3）。

**1.3 ★ 裸 `\r` 会与"补空格答分页"叠成回车，把 CLI 顶出子视图**
- 症状：`ospf 1` 成功进 `[SW8-ospf-1]`，下一条 `area 1` 报 Unrecognized 并退回 `[SW8]`；
  抓字节发现设备先收到一个 `quit`。
- 真因：`_settle()` 为了确认提示符发了裸 `\r`（留在输入行），驱动又把 pagination 提示
  `---- More ----` 当作分页并补一个空格，`空格 + \r` 被设备当成回车提交，当前行被"回车"，
  表现就是退出子视图。
- 解法：**两条命令之间不要发任何裸字节**；用 `time.sleep(0.06)` 之类静默等待即可。

**1.4 `% Unrecognized` 判定的两个反向误判**
- 报错正则里带 `^\s*\^\s*$`（孤立 `^`）会把**正常回显里的 `^`** 当成报错，
  导致"子视图计数不增加"，后续父视图命令全落到错的地方。判状态要用**严格正则**
  （只认 `% Unrecognized/Wrong/...`、`Error:`、`This subnet overlaps`），
  宽松正则只用于**生成问题清单**给人看。
- `This subnet overlaps with another interface!` **不带 `%`**，只认 `%` 会漏检：
  `ip address` 被拒但脚本报 0 报错，地址其实没配上。
- 附带：同一地址不能既配 LoopBack 又配管理 SVI（本次 `LoopBack0 10.255.0.1/32` 与
  `Vlan-interface4094 10.255.0.1/24` 冲突）——管理地址只留一个地方。

**1.5 子视图嵌套要用"显式深度计数"，不要靠提示符文本反推**
- 有效模型：`sub=0` 用户视图、`sub=1` 系统视图、`>=2` 子视图。
  - 下一条是"进入块"的命令（`interface `/`ospf `/`area `/`acl `/`vlan `/`nqa `/`route-policy `…）：
    **先把已有子视图退掉（`ensure_view(system)`），命令成功后 `sub = 1`（重置，不是自增）**。
  - 下一条是普通配置命令：**不要**动视图（`ospf → area → network` 必须留在父视图里）。
  - `quit`：`sub -= 1`。
- 教训：把"每条配置命令前都 ensure 到系统视图"当默认行为是错的，正是它把 `area` 顶了出去。
- 前缀匹配自身的坑：`track `/`nqa schedule `/`ospf timer `/`port link-aggregation ` **不是**进入块，
  不能加进"进入块"前缀表，否则计数虚高、后面第二个同名块就下发到错视图。

### 2) IRF（第二个大坑）
**2.1 ★ HCL 上 IRF 物理口必须"交叉绑定"，否则永远 ISOLATE**
- 症状：`display irf link` 两个 IRF 口都 `UP`，LLDP 也能看到对端，但
  `display irf topology` 两个口都是 `ISOLATE`、邻居 `---`，两台各自是 Master。
- 真因（H3C 官方知识库《在华三模拟器 H3C Cloud Lab 上做堆叠时，堆叠失败》）：
  **`irf-port 1/1` 只能对 `irf-port 2/2`，`irf-port 1/2` 只能对 `irf-port 2/1`**；
  同号（1/1↔2/1）建不起来。本次两条链路正好都绑成了同号。
- 解法（本次实测有效）：member2 上把两个物理口对调绑定。
  ```
  # member 1
  irf-port 1/1  → port group interface FortyGigE 1/0/53
  irf-port 1/2  → port group interface FortyGigE 1/0/54
  # member 2（交叉）
  irf-port 2/1  → port group interface FortyGigE 2/0/54
  irf-port 2/2  → port group interface FortyGigE 2/0/53
  ```

**2.2 ★ IRF 口解绑：`undo port group interface <接口名>` 里接口名不能带空格**
```
undo port group interface FortyGigE2/0/53      ✅ 设备 ? 提示的正是这个形式
undo port group interface FortyGigE 2/0/53     ❌ % Wrong parameter found at '^' position.
port  group interface FortyGigE 2/0/54         ✅ 配置时反而带空格
```
另外 `undo` 要在**该物理口当前所在的那个 irf-port 视图**里执行，去别的 irf-port 里 undo 无效。

**2.3 ★ 绑定后必须 `irf-port-configuration active`，并先 `save force` 再重启**
设备在 `port group interface` 成功后会提示要先 save、再执行 `irf-port-configuration active`。
本次教训：第一次只改绑定、**没 save 就 reboot** → 启动配置里没有 IRF 口 → 重启后
`display irf configuration` 回到 `disable`，IRF 不成型。正确顺序：
`irf-port-configuration active` → `save force` → `reboot`。

**2.4 ★ renumber 之后必须先重启**
`irf member 1 renumber 2` 当场不生效（本次还会读超时），此刻仍要写 `irf-port 1/x`；
用 `2/x` 会报 `% Wrong parameter`。**renumber → save → reboot → 再用新编号配 irf-port**。

**2.5 ★★ 明知故犯的坑：HCL 上不要真的把 MAD 激活**
- K3 已写"HCL 不转发成员间 MAD 报文，BFD 会话建不起来；MAD 开启设备卡顿；
  参考解法要求 MAD 口 shutdown"。本次仍激活了 MAD（`mad bfd enable` + 两个 `mad ip address`），
  结果 BFD 会话 `Faulty`、CPU 97%、**HX1/HX2 两个控制台全部无响应**
  （屏幕持续刷 `.`，`Ctrl+C`/回车/`q`/`Ctrl+Z` 都无效），最后只能退出整个 HCL。
- 正确姿势（配置齐备 + MAD 口 shutdown，见 gotchas N5）。
- 控制台刷死后**软件无法恢复**，只能让用户在 HCL GUI 里 Stop/Start；
  **不要反复给刷死的控制台发探测字节**（每试一次都在加负担）。

### 3) 交换机侧命令/行为差异（本环境实测）
**3.1 ★ 成员口入组前属性必须与聚合口一致**（与 gotchas F5 同因，本次又踩）
```
port link-aggregation group 10
Can't assign the port to the aggregation group because its attribute configurations
are different than the aggregate interface.
```
顺序：成员口 `port link-type trunk` + `port trunk permit vlan ...` → 聚合口属性 →
成员口 `port link-aggregation group N`。**报这条错时端口并未入组**（聚合显示 0 Selected）。

**3.2 ★ 三层动态聚合：成员口必须先切三层**
- 症状：`port link-aggregation group 1` 对 `Route-Aggregation 1` 报
  `The link aggregation group does not exist.`（其实聚合口存在）。
- 真因：成员口还是二层口（`port link-mode bridge`）。
- 解法：
  ```
  interface GigabitEthernet 1/0/4
   port link-mode route        ← 会问 Continue? [Y/N]，自动答 Y
   port link-aggregation group 1
  ```
  切完 RAGG1 立刻起，OSPF 邻居秒变 Full。

**3.3 `silent-interface` 配到自己要建邻居的接口上 → "路由通但没邻居"**
- 症状：核心与 SW8 的 SVI 互 ping 通、`display ospf interface` 两边都 `State: DR`，
  但 `display ospf peer` 全空。
- 真因：核心 OSPF 里误配 `silent-interface Vlan-interface 92`（正是 area1 互联口）。
- 判据：**能 ping 通 + 接口有 OSPF 状态 + 没有邻居 ⇒ 第一时间查 `silent-interface` 列表。**
- 附带：OSPF 认证两端必须一致（一端 `area X authentication-mode simple` 另一端没配，同样"通但无邻居"）。

**3.4 S5820V2 上 `acl number 3999` 不存在**
`acl ?` 只有 `advanced/basic/copy/ipv6/logging/mac/trap` ⇒ 改 **`acl advanced 3999`**
（S6850 上两种都能过）。`ssh server acl 3999` 需要该 ACL 已存在。

**3.5 `arp detection enable` 在 S6850 上不认**（删掉即可，不影响 DHCP snooping 与端口安全）。

**3.6 端口安全要全局开关**：只配接口属性不生效，`display port-security` 显示 `Disabled`
⇒ 必须补全局 `port-security enable`。

### 4) MSR36-20（Comware 7.1.064 R0427P22）实测语法
见 gotchas Q 章差异表。要点：`Route-Aggregation`（无 Bridge-Aggregation）、
`dialer-group 1 rule ip permit`、`dialer bundle enable`、`dialer peer-name`、
`ip route-static ... track 1 preference 60`（track 在 preference 前）、`ip prefix-list`、
`filter-policy prefix-list X import`、`dhcp server ip-pool`、`undo dialer-group`（不带编号）。

**4.1 ★ 串口链路上做不了 PPPoE（R1↔R3 是 Ser1/0）**
`pppoe-server`/`pppoe-client` 只支持以太口 ⇒ "移动用 PPPoE 拨号"在**本拓扑上物理不可实现**。
本次降级为串口 PPP + 地址（`10.100.100.2 255.255.255.252`），Dialer1 保留 PPPoE/DDR 配置但不承载 IP。
**下次拿到类似题先看 ISP 链路是串口还是以太口**，是串口就提前说明并直接申请等价降级。

**4.2 NAT server 的两条常识**
- `nat server` 只映射指定协议/端口，**ICMP 不会被转换**：用 `ping 202.100.1.3` 验证永远是错的。
- 内网映射地址要求 R1 有回程路由（本次一开始无 192.168.200.0/24 → FTP `connect: Connection timed out`）。
  **先查路由，再怀疑 NAT。**

**4.3 NQA 探测目标要有主机路由，否则 track 与默认路由互锁**
用"经默认路由可达的地址"做 NQA 目标，track 一 Negative 撤掉默认路由 → 探测永久失败。
必须给探测目标加**不带 track 的明细路由**，并在模拟侧给它真实承载。

### 5) 并行作业的纪律（本次另一个大教训）
- **同一条控制台绝对不能被两个进程同时下发**。本次我的下发和子代理的下发撞在同一台 HX 上，
  回显出现交错，报出 `% Ambiguous command` / `% Wrong parameter` 一堆**假报错**，
  并且把互相冲突的配置写进去（PBR 里出现不存在的下一跳 `192.168.10.3`）。
- 纪律：**按设备分片，一台设备同一时刻只有一个下发者**；并行前明确"哪台归谁"。
- 子代理交付的"计划文件"必须**由主控复核关键项**（本次抓到两处真硬伤：
  ① 分部 AP 管理网段没有端口在对应 VLAN，DHCP 池是死的；② 两个 RIP 互联网段落在不同 VLAN，
  邻居根本建不起来）。复核成本远小于事后排障。

### 版本 / 环境
- HCL **5.10.3**；控制台 `127.0.0.1:(30000+device_id)`；**device_id 决定端口，见 `.net` 文件**。
- HX/SW8/J1/J2 = S6850 `Comware 7.1.070 Alpha 7170`；SW9/JR3/JR = S5820V2 `7.1.075 Alpha 7571`；
  R1/R2/R3/模拟终端/FTP = MSR36-20 `7.1.064 Release 0427P22`。
- **官方文档 vs 实际表现**：① HCL 上 IRF 物理口必须交叉绑定（真机文档不提）；
  ② HCL 上 MAD 开启会拖死设备（官方说 MAD 是保护机制）；③ S5820V2 无 `acl number`。
  **以上以本环境实测为准。**

### 沉淀结果
可复用条目已提炼进 `references/gotchas.md`（新增 **M/N/O/P/Q/R** 六章）；
检索同义词已加入 `references/aliases.md`。
