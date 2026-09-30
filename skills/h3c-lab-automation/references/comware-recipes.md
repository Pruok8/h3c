# H3C Comware 配方（配置片段 + 验证命令）

写 `plan.json` 时照这里的片段改。**接口名以设备实际为准**：交换机 `GigabitEthernet1/0/x`，
路由器 `GigabitEthernet0/x`；先 `display interface brief` 确认，别照抄。

## 视图与提示符

| 提示符 | 视图 |
|---|---|
| `<SW1>` | 用户视图（`display`、`ping`、`save`） |
| `[SW1]` | 系统视图（配置） |
| `[SW1-GigabitEthernet1/0/1]` | 接口视图等子视图 |

`system-view` 进系统视图，`quit` 退一级，`return` 直接回用户视图。
`hcl_lab.py` 的 `commands` 只写正文，进出由脚本处理。

## 基础

```
sysname SW1
vlan 10
vlan 20
quit
interface GigabitEthernet 1/0/1
 port link-type trunk
 port trunk permit vlan 10 20
 quit
interface GigabitEthernet 1/0/2
 port link-type access
 port access vlan 10
 quit
interface Vlan-interface 10
 ip address 192.168.10.1 24
 quit
```

**保存**：`save force`（免交互）。不保存重启即丢。

## 链路聚合

```
interface Bridge-Aggregation 1
 port link-type trunk
 port trunk permit vlan 10 20
 link-aggregation mode dynamic
 quit
interface GigabitEthernet 1/0/1
 port link-aggregation group 1
 quit
```
成员口**不要**单独配 VLAN，属性从聚合口继承。

验证：`display link-aggregation verbose Bridge-Aggregation 1` → 成员口应为 **S(Selected)**，不该有 `I`。

## DRNI（H3C 的 M-LAG；老版本可能叫 `mlag`，结构相同）

顺序很重要：**先系统参数 → 再 peer-link → 最后 drni group**，顺序错成员口起不来。

```
drni system-mac 0001-0001-0001
drni system-number 1              # 两端必须不同
drni system-priority 100

interface Bridge-Aggregation 100  # peer-link
 port link-type trunk
 port trunk permit vlan all
 link-aggregation mode dynamic
 port drni peer-link 1
 undo stp enable                   # peer-link 必须关 STP
 quit

interface Bridge-Aggregation 1    # 跨设备聚合（朝向对端交换机）
 port link-type trunk
 port trunk permit vlan 10 20
 link-aggregation mode dynamic
 drni group 1
 quit

interface Vlan-interface 101
 ip address 10.1.101.1 30
 quit
drni keepalive ip destination 10.1.101.2 source 10.1.101.1
```

约束：`system-mac`/`system-priority`/STP 域两端**一致**，`system-number` **不同**。

**keepalive 的设计陷阱**：keepalive 若与 peer-link 走同一条链路，peer-link 全断时心跳同时断 →
**分裂（split-brain）检测失效**。要么另加一条物理链路专做 keepalive，要么明确接受该限制。

验证：`display drni summary`、`display drni keepalive`。

## 单臂路由（子接口）

```
interface GigabitEthernet 0/1
 quit
interface GigabitEthernet 0/1.20
 vlan-type dot1q vid 20
 ip address 192.168.20.1 24
 quit
```
对端交换机侧该口必须 `port link-type trunk` 且放通对应 VLAN。

> 三层设备自己就是网关时**不存在**单臂路由。若需求同时要"设备做三层网关"和"单臂路由"，
> 这是冲突的，要摆出取舍让用户选（常见调和：三层设备做 VLAN 主网关，另一台路由器用子接口做备用网关 + VRRP）。

## OSPF

```
ospf 1 router-id 10.1.255.1
 area 0.0.0.0
  network 10.1.30.0 0.0.0.3
  network 10.1.255.1 0.0.0.0
  quit
 quit
```
`network` 用**反掩码**。

验证：`display ospf peer`（Full）、`display ospf routing`、`display ospf brief`。

**隔离常用手法**：某网段**不进 OSPF** → 默认就没有该网段的路由，天然隔离；再用 ACL 兜底。

## MSTP

```
stp region-configuration
 region-name H3C-LAB
 revision-level 1
 instance 1 vlan 10
 instance 2 vlan 20
 active region-configuration
 quit
stp global enable
stp instance 0 root primary        # 备根用 root secondary
```
同域内 region-name / revision-level / instance 映射**必须完全一致**。

验证：`display stp region-configuration`、`display stp instance 1 brief`。

## VRRP

```
interface Vlan-interface 10
 ip address 192.168.10.2 24
 vrrp vrid 10 virtual-ip 192.168.10.1
 vrrp vrid 10 priority 120       # 默认 100，大的当 Master
 quit
```
验证：`display vrrp verbose`（看 State 与设计是否一致）。

## 静态路由

```
ip route-static 192.168.88.0 24 10.1.89.1
ip route-static 0.0.0.0 0.0.0.0 10.1.89.254
```
验证：`display ip routing-table`、`display ip routing-table protocol static`。

## ACL

```
acl advanced 3000
 rule 10 deny ip source 192.168.10.0 0.0.0.255 destination 192.168.20.0 0.0.0.255
 rule 30 permit ip
 quit
interface GigabitEthernet 0/1.20
 packet-filter 3000 inbound
 quit
```
验证：`display acl 3000`、`display packet-filter interface GigabitEthernet 0/1.20`。

## IPsec（站点到站点）

两端对称配置，`remote-address` 指向对端，感兴趣流用 `security acl`。

```
acl advanced 3100
 rule 0 permit ip source 192.168.10.0 0.0.0.255 destination 192.168.88.0 0.0.0.255
 rule 5 permit ip source 192.168.88.0 0.0.0.255 destination 192.168.10.0 0.0.0.255
 quit
ike proposal 10
 encryption-algorithm aes-cbc-256
 authentication-algorithm sha256
 dh group14
 quit
ike keychain DSH-KEY
 pre-shared-key address 10.1.45.2 32 key simple <PSK>
 quit
ike profile DSH-PROF
 keychain DSH-KEY
 match remote identity address 10.1.45.2 32
 quit
ipsec transform-set DSH-TS
 esp encryption-algorithm aes-cbc-256
 esp authentication-algorithm sha256
 quit
ipsec policy DSH-POL 10 isakmp
 security acl 3100
 ike-profile DSH-PROF
 transform-set DSH-TS
 remote-address 10.1.45.2
 quit
interface GigabitEthernet 0/1
 ipsec apply policy DSH-POL
 quit
```
验证：`display ike sa`、`display ipsec sa`（看 RD / RD_V2）。

## 验证命令速查

| 目的 | 命令 |
|---|---|
| 接口状态 | `display interface brief` / `display ip interface brief` |
| 当前配置 | `display current-configuration`（可加 ` | include <关键字>`） |
| 单口配置 | `display this`（在接口视图下） |
| 设备信息 | `display version` / `display device` |
| 聚合 | `display link-aggregation verbose` |
| DRNI | `display drni summary` / `display drni keepalive` |
| OSPF | `display ospf peer` / `display ospf routing` |
| 路由表 | `display ip routing-table` |
| MSTP | `display stp brief` / `display stp instance N brief` |
| VRRP | `display vrrp verbose` |
| IPsec | `display ike sa` / `display ipsec sa` |
| 连通性 | `ping -c 5 <ip>` / `tracert <ip>` |

## 常见报错含义

| 报文 | 含义 |
|---|---|
| `% Unrecognized command found at '^' position.` | 命令不存在（版本不支持 / 拼错）。`^` 指向出错位置 |
| `% Wrong parameter found at '^' position.` | 参数错（掩码写成正掩码、VLAN 号超范围等） |
| `% Incomplete command found at '^' position.` | 命令没写完整 |
| `% Ambiguous command found at '^' position.` | 命令有歧义，需要多打几个字符 |
| `% Too many parameters` | 参数给多了 |
| `Permission denied.` | 当前视图不允许该命令 |

`hcl_lab.py` 会自动检出这些并计入报错（退出码非 0）。
