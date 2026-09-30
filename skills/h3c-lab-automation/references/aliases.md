# 记忆检索同义词表

`remember.py` 会自动读本文件：**同一行内的词互为同义词**（用 `|` 分隔）。
匹配规则变成「每个查询词 → 它的同义词组里**任意一个**命中即算这个词命中」，
所以中英文混着查也能查到。

**加词原则**：把**报错原文里的英文**、**你的口头说法**、**官方术语**都塞进同一行。
漏了同义词，等于记忆检索不到 —— 这是记忆机制最常见的失效原因。

卡顿|stall|hang|卡死|无响应|ConsoleTimeout|超时
DOWN(B)|DOWN B|No peer M-LAG|对端没有 M-LAG 接口|B 标志
一致性检查|consistency-check|Strict|type1|type2|配置不一致
MAD 闩锁|mad restore|MAD DOWN|multi-active|抢主|角色异常
绑定 VPN 清配置|Some configurations on the interface are removed|ip binding vpn-instance
DHCPv6|dhcp-alloc|Solicit|FF02::1:2|ipv6 dhcp|SLAAC|RA
链路掉线|link down|掉线|自行 DOWN|复位链路|shutdown undo shutdown
聚合口|link-aggregation|LACP|Selected|ACDEF|成员口|attribute configurations
VPN 实例|vpn-instance|VRF|RD|RT|vpn1|VPN-A|VPN-B
BGP 社区|community|advertise-community|local-preference|MED|apply cost|分流
OSPF 邻居|ospf peer|Full|Init|2-Way|hello|dead|timer
路由引入|import-route|redistribute|route-policy|tag|防环
静默接口|silent-interface|不发 hello|无动态路由报文
保存|save force|配置丢失|startup|重启后
HCL|模拟器|Simware|VirtualBox|拓扑|net 文件
端口|console|telnet|30001|控制台|端口发现
直连不通|ping 不通|丢包|packet loss|100% loss|端到端

未识别命令 => Unrecognized command, 命令不支持, 配置命令全废, system-view 没进, 视图不对
视图错位 => Wrong parameter, Ambiguous command, 子视图, quit 退多了, 用户视图, 系统视图, 提示符
IRF 不成型 => ISOLATE, 两台都 Master, 堆叠失败, irf-port 交叉, 交叉绑定, irf-port-configuration active
MAD 卡顿 => 控制台卡死, 控制台刷死, CPU 97%, 屏幕刷点, Faulty, mad bfd enable, 设备卡顿
聚合属性不一致 => Can't assign the port to the aggregation group, 0 Selected, 成员口属性
三层聚合不存在 => The link aggregation group does not exist, Route-Aggregation, port link-mode route
通但没邻居 => silent-interface, 静默接口, DR 但无邻居, OSPF 认证不一致
地址重叠 => This subnet overlaps, overlaps, 静默失败, LoopBack 冲突
PPPoE 串口 => pppoe-server bind, pppoe-client, 串口不支持 PPPoE, Dialer 无地址
