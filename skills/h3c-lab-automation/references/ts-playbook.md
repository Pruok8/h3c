# 排错卷（TS）手册：分层定位树 + 故障注入清单

> `lab4_ts` 这类"排错卷"的玩法是**先注入故障，再让你定位**。
> 本手册两部分：**上半部分是定位树**（给出症状后按层往下查），
> **下半部分是注入清单**（自己造故障练手 / 出题）。
>
> 纪律：① 一次只改**一处**；② 改前 `hcl_lab.py --snapshot` 存快照；
> ③ 每步都留证据（`--out`）；④ 定位到根因再动手，别边猜边改。

## 一、通用定位顺序（从下往上，别跳层）

```
端到端不通
  │
  ├─① 物理/链路：display interface brief        → 端口 UP 吗？ADM 吗？
  │     └ 掉线/ADM 不是配置问题 → linkwatch.py 复位（两端一起）
  │
  ├─② 二层：display vlan / display link-aggregation verbose
  │     └ 成员口是 Selected(S) 还是 Unselected(U)？→ 属性不一致 / 对端没协商
  │
  ├─③ 邻居：display ospf peer / display isis peer / display bgp peer
  │     └ 空表 ≠ 配置没下发，先查 Hello/Dead、network-type、两端是否同一路由表
  │
  ├─④ 路由：display ip routing-table [vpn-instance X] / display ospf routing
  │     └ "邻居 Full 但没路由" = 两端拓扑描述不一致（P2P vs Broadcast）
  │
  ├─⑤ MPLS：display mpls ldp lsp / display mpls lsp
  │     └ 没有 LSP → 先看 lsr-id 是否可达、接口是否 mpls ldp enable
  │
  ├─⑥ VPN/BGP：display bgp peer vpnv4 / display bgp routing-table vpnv4 <prefix>
  │     └ 看 RT 匹不匹配、Community 传没传（advertise-community）、next-hop 是谁
  │
  └─⑦ 策略：route-policy / acl / import-route 的 if-match 到底命中了没有
        └ 用 display bgp routing-table ... 的 Community/Local_Pref/MED 列反推
```

**每一层的"假象"都要防**：
- ③ 层：只在一台看 `Full` 不算数，**两端都要看**；
- ④ 层：`display ospf 1 peer` 查不到内容时，先怀疑"进程号属于 vpn 实例、display 解析到了全局"；
- ⑥ 层：路由在 BGP 表里 ≠ 会进路由表（next-hop 不可达 / RT 不匹配 / 被策略拒）。

## 二、本实验（C-001）验证过的注入点与定位命令

| # | 注入什么 | 期望症状 | 定位命令与判据 |
|---|---|---|---|
| 1 | SW1/SW2 的某条成员链路 `shutdown` | 端到端丢包；该聚合口选择端口数变 1 | `display link-aggregation summary`（Selected Ports） |
| 2 | 两台 M-LAG 交换机其中一台 `m-lag consistency-check` 改回 Enabled+Strict | M-LAG 组变 `DOWN (C)`/`DOWN (B)` | `display m-lag consistency-check status` + `display m-lag summary` |
| 3 | 在 peer-link 上制造闪断 | 一台 `MAD DOWN state: Yes`，M-LAG 全组 DOWN | `display m-lag role`（关键字段 MAD DOWN state） |
| 4 | 把一端 OSPF `network-type p2p`、另一端保持 broadcast | 邻居 `Full/ -` 但无路由 | `display ospf <pid> lsdb router`（看 Link Type：P2P vs TransNet） |
| 5 | 改一端 `ospf timer hello` | 邻居表**空** | 两端 `display current-configuration interface <if>` 对 hello/dead |
| 6 | 删掉 PE3 的 `bgp 65001` 下 vpn 实例 import-route | 对端学不到该侧业务网段 | `display current-configuration configuration bgp`（看实例在不在） |
| 7 | 改 ASBR 一侧 eBGP peer 地址 | 一条 MP-EBGP 恒 `Idle` | `display bgp peer vpnv4`（哪条没 Established） |
| 8 | 删掉某条 eBGP 的 `advertise-community` | 社区不传 → A/B 不分流 | `display bgp routing-table vpnv4 <prefix>`（Community 列为空） |
| 9 | 两条跨域链路 import 策略配成同一个 | 两个业务网段走同一条链路 | `display ip routing-table vpn-instance vpn1`（出接口是否相同） |
| 10 | 把业务 SVI 的 `ospf area` 去掉（保留 silent） | 该网段不再被发布 | 对端 `display ip routing-table` 里该网段消失 |
| 11 | 业务 SVI 加 `import-route direct` | 业务网段上出现动态路由报文 | 抓 `display ospf interface` / 邻居表 |
| 12 | 把 DHCPv6 服务端的 SVI 绑上 `vpn-instance` | 客户端恒 `SOLICIT`，服务端 `Packets received: 0` | `display ipv6 dhcp server statistics` + `display ipv6 dhcp client statistics` |
| 13 | 删掉 Server 的默认路由 | PE 能 ping 通 SVI 但 ping 不通 Server | 两端分别 ping 定位（见 C-001 §6.1） |
| 14 | 关掉 PE1↔PE2 的 BFD | OSPF 邻居仍在，BFD 会话 Down | `display bfd session` |
| 15 | IRF 某成员掉电/重启 | IRF 主备切换，业务短断 | `display irf`（MemberID/Role） |

## 三、注入/回滚纪律

```bash
# 注入前：存快照（回滚的唯一依据）
python scripts/hcl_lab.py plan.json --snapshot            # 抓 display current-configuration
# 回滚某一节
python scripts/restore.py <snapshot>.txt --section "interface Vlan-interface 30" \
    --device SW1 --port 30008 --out plan-restore.json
# 回滚后必须验证，不要只看"命令下发成功"
python scripts/verify.py checklist.json
```

**出题（写排错卷）时的建议**：一次只注入 1~3 处，且**都必须在"期望症状 → 定位命令"上有唯一解**；
否则学员会靠猜。把答案写进 `cases.md`（编号 C-xxx），下次直接复用。
