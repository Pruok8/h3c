# 坑与真相（按症状索引）

> **这是记忆的第二层（知识库）。出问题第一动作是查记忆：**
> ```bash
> python scripts/remember.py <关键词>        # 同时搜本文件与 cases.md
> python scripts/remember.py --list          # 先看有哪些条目
> ```
> 顺序：**记忆 → 厂商官方文档 → 外部搜索**（见 SKILL.md 铁律 2）。
> 解决新问题后**务必写回**：细节进 `cases.md`，可复用的提炼到本文件（见铁律 5）。

全是这次真机实测踩出来的。遇到反直觉的失败先来这里查。

---

## A. HCL / 设备控制台

### A1「一连上就被对端关闭」
**症状**：`socket.recv` 返回空，`ConsoleError: 对端关闭了连接`；用诊断脚本单独连却完全正常。

**原因**：不是独占，也不是驱动逻辑错。是**用一次性的裸 TCP 连接去探测端口、立刻关掉，紧接着开真连接**，HCL 的 telnet 代理在那次握手里被搅乱了。

**对策**：扫描端口时**直接拿真连接去试**（`hcl_ports.py` 里的 `try_connect` / `hcl_lab.py` 的 `find_port_by_hostname` 都是这么做的），不要先裸探一次。

**补充事实**：HCL 控制台**允许并发客户端** —— SecureCRT 连着的设备，Python 驱动照样能连上并跑命令，实测通过。所以不用担心抢占。

### A2「连上了但等不到提示符，一直在刷 Automatic configuration」
**症状**：控制台持续输出
```
Automatic configuration attempt: 7.
Not ready for automatic configuration: no interface available.
Automatic configuration is running, press CTRL_C or CTRL_D to break.
```
**原因**：空配设备起来后卡在自动配置（zeroconf）阶段，此时**根本没有 CLI 提示符**。

**对策**：发 **Ctrl+C（`\x03`）** 打断，设备会回
```
Automatic configuration is aborted.
Line con0 is available.
Press ENTER to get started.      ← 还要再敲一次回车
<H3C>
```
`hcldrv.Console.prep()` 已自动做这套流程（识别关键字 → 发 `\x03` → 发 `\r` → 等提示符）。

### A3「自测 12 项全过，真机却连不上」
**原因**：`mock_device.py` 是理想化假设备，**不会**出现 A2 的 auto-config 行为。

**对策**：改完驱动必须**同时**跑：假设备自测（逻辑）+ 真机 `probe_hcl.py`（现实）。两者不可互相替代。

### A4「端口是通的（LISTENING），但会话显示『未连接』」
见 D2（SecureCRT 会话端口键）。

### A5 端口号 ↔ 设备名 的权威来源是 `.net` 文件，不是猜
HCL 把实验拓扑存成 `*.net`（INI 风格）。**每台设备的控制台端口 = `30000 + device_id`**，
而 `device_id` 和**逐端口的连线**都在文件里：

```ini
[[MSR36-20 PE1]]
    device_id = 1            # ⇒ telnet 127.0.0.1:30001
    slot0 = MSR36 384        # 型号/模板
    GE_0/2 = ASBR GE_0/1     # 连线：本端端口 = 对端设备 对端端口
    GE_0/0 = SW1 GE_0/20
```

**排查"某条链路为什么不通"时第一步就该来这里核线**，不要凭 `display interface brief`
猜哪个口该连谁。实测踩过：以为 PE 上联用了 `GE5/0`（其实 `GE5/0` 根本没接线），
真实用的是 `GE0/0 + GE6/0` —— 只有 `.net` 能给出答案。

常见存放位置（本机实测）：
`D:\NET\ie\e\kongpei\lab4_ts.net`（`<HCL安装目录>\sessions`、`D:\NET\ie\...` 也有一堆历史实验）；
`<HCL安装目录>\deviceInfo.ini` 是设备型号清单。
**`.net` 里的端口号写法是 `GE_0/x`（带下划线）**，而设备上回显是 `GigabitEthernet1/0/x`（S6850 从 1 开始）。

---

## B. telnet 协议层

### B1 `telnetlib` 不存在
**原因**：Python 3.13 已移除该模块。

**对策**：`hcldrv.py` 自带实现 —— 拒绝一切选项协商（`WILL`→`DONT`、`DO`→`WONT`）、跳过 `SB..SE`、处理 `IAC IAC` 转义、容忍控制序列被 recv 边界截断。

### B2 长输出卡在 `---- More ----`
**对策**：驱动识别分页标记后自动补空格，并把它从可见文本里摘掉。`con.answered_more` 可看翻了几次（自测里 240 行配置翻了 29 次）。

另外 `prep()` 会先发 `screen-length disable` 关掉分页（有些设备/视图不支持，失败不影响后续）。

### B3 取回显时拿到的是"上一条命令的输出"或空
**原因**：`read_until` 在整个累积文本里找提示符，会立刻命中**上一个**提示符。

**对策**：`command()` 记录发送前的文本长度，只在新文本里找提示符（`start=base`）。自己写新命令时务必保持这个模式。

### B4 回显里混进了命令本身和结尾提示符
**对策**：`command()` 会剥掉第一行（若与命令相同）和最后一行（若匹配提示符）。

---

## C. Python / 环境

### C1 `TypeError: '_thread._ThreadHandle' object is not callable`
**原因**：Python 3.13 的 `threading.Thread` 有内部属性 `_handle`，**遮蔽了同名方法**。

**对策**：`Thread` 子类里别用 `_handle` 命名（`mock_device.py` 用 `_serve_conn`）。

### C2 中文输出乱码
**对策**：跑 Python 前设 `$env:PYTHONIOENCODING='utf-8'`、`$env:PYTHONUTF8='1'`，并 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`。

---

## D. SecureCRT

### D1「命令行上出现意外的参数 '/SCRIPT'」
**原因**：这个 **8.7.2 版本不认 `/SCRIPT`**（它的 CHM 帮助里写了这一项，但帮助比 exe 新）。可用开关只有 `/F` `/S` `/T` `/TELNET` `/L` `/PASSWORD` `/LOG` `/ARG` 等。

**对策**：用**会话登录脚本**注入脚本，见 `securecrt-automation.md`。

### D2 会话能打开，但显示"未连接"
**原因**：非 SSH 协议的端口键写法错了。SSH2 用 `D:"[SSH2] Port"`，但 **Telnet/RDP 这类用朴素的 `D:"Port"`**（铁证：官方 `Default_RDP.ini` 里是 `D:"Port"=00000d3d"` = 3389）。写成 `D:"[Telnet] Port"` 会被忽略 → 端口回落成 23 → 连不上 30001。

**对策**：`D:"Port"=%08x`（30001 = `00007531`）。`make_crt_session.py` 已修正。

### D3 登录脚本静默不跑（最难查）
**原因**：`D:"Use Login Script"` 是 **Automate logon** 开关，置 1 时**脚本不执行且不报错**。官方文档 `Creating_Python_Scripts.htm` 明写：要跑 logon script，**必须先关掉 Automate logon**。

**对策**：正确组合
```
D:"Use Login Script"=00000000     ← Automate logon 必须关
D:"Use Script File"=00000001      ← Logon script 勾上
S:"Script Filename V2"=<脚本绝对路径>
```

### D4 `/F` 新配置目录起不来
**症状**：进程起来后弹「创建SecureCRT密码短语」，然后自己退出。

**对策**：**整套复制已有配置**（含 `Global.ini`）到 `/F` 目录，就不会弹首次运行对话框。

### D5 启动第二个实例，参数却被已有实例吃掉
**症状**：`Start-Process` 之后新进程消失，报错弹窗出现在**原来那个**实例上。

**原因**：单实例接管 —— 命令行参数被转交给已运行的实例。

**对策**：要么接受接管（用**默认配置目录**里的会话名，`/S <名称>` 才会被认出来），要么先关掉已有实例。

### D6 脚本报 `AttributeError: 'int' object has no attribute 'decode'`
**原因**：SecureCRT 脚本 API 返回值类型不统一 —— `tab.Index` 是 **int**，`Session.Connected` 是 bool。

**对策**：写个 `U()` 统一转字符串：非 `str/unicode` 先 `unicode(value)` 再考虑 decode。

### D7 从 harness 启动的 SecureCRT 写文件被拒
**原因**：继承 DSH 沙箱。

**对策**：产物写到工作区；或让用户自己启动 SecureCRT。

### D8 菜单点不动
**原因**：BCG 菜单栏是自绘的，`GetMenu()` / `GetMenuItemCount()` 对它返回 0 / -1，Win32 菜单 API 无效。

**对策**：用 `SC_KEYMENU`（`WM_SYSCOMMAND` + `0xF100` + 加速键字母）或键盘；**验证靠截图**（`shot.ps1` + 看图），不要试图枚举菜单文字。

### D9 没有外部自动化接口
**事实**：扫注册表 + `Dispatch` 全部失败 —— 该版本**没有 COM/ActiveX ProgID**。别在这条路上浪费时间。

### D10 截图拍到了别的窗口 / 拍到桌面
**原因**：`SetForegroundWindow` 会被 Windows 拦截，`CopyFromScreen` 拍到的是屏幕上实际可见的东西（可能是盖在上面的浏览器）。

**对策**：`shot.ps1` 用 **`PrintWindow(hwnd, hdc, 2)`**（`PW_RENDERFULLCONTENT`），只渲染目标窗口自身内容，不受遮挡影响，也不侵犯隐私。注意：某些自绘终端区域在 PrintWindow 下可能空白 —— 那时以设备回显文件为准。

---

## F. M-LAG（H3C S6850 / HCL 模拟器）

### F1 关键字是 `m-lag`，不是 `drni`
很多教材和文档写 `drni`（DRNI 是 H3C 的 M-LAG 实现名），但 **这个 S6850 镜像只认 `m-lag`**。
先用 `m-lag ?` 确认。接口级命令挂在 **`port`** 下面，不在 `m-lag` 下面：

```
port m-lag peer-link <1>      # 把聚合口设为 peer-link
port m-lag group <1-1024>     # 把聚合口加入某个 M-LAG 组（IPP）
```

### F2 顺序错了就永远不通：先 M-LAG 系统参数，再建 peer-link
`m-lag system-mac` / `system-number` 会弹确认：

```
Changing the system MAC address might flap the peer link and cause M-LAG
system setup failure. Continue? [Y/N]:
```

如果 **peer-link 先建好、之后才改系统参数**，M-LAG 系统的 DRCP 就**不会携带各 M-LAG 接口信息**。症状：

```
display m-lag summary   -> 每个组 DOWN (B)     # B = No peer M-LAG interface configured
display m-lag troubleshooting -> "no peer M-LAG interface was detected"
display m-lag verbose   -> Peer M-LAG interface LACP MAC: Effective=N/A
```

而 `display m-lag system` 却显示两端 system MAC/number/priority/角色**全部正常** —— 很容易误判。
**修法**：`undo interface Bridge-Aggregation <peer-link号>` 再重建 peer-link。

### F3 动过 M-LAG 组集合，就要再重建一次 peer-link
新增/删除/重建任何 M-LAG 成员组后，对端会丢失该组的检测信息（又回到 `DOWN (B)`）。
**两台必须各自重建 peer-link**，而且组集合要保持左右对称，否则连原本正常的组也会被带下去。

### F4 从设备有 300 秒 Restore delay
```
display m-lag system
  Timer            State      Value(s)
  Restore delay    Enabled    300        <- 只在 Secondary 那台上 Enabled
```
Secondary 设备在这个窗口内**不会把自己的 M-LAG 接口拉起来**。而且**每次重建 peer-link 都会重置它** ——
所以连续重建之后不要立刻判定失败，静置 5 分钟再看。

### F5 成员口必须先对齐属性，才能加入聚合组
S6850 上直接加组会报：

```
Can't assign the port to the aggregation group because its attribute
configurations are different than the aggregate interface.
```

→ 顺序必须是：先 `port link-type trunk` + `port trunk permit vlan ...`，**再** `port link-aggregation group N`。
（S5820V2 等型号没这么严格，同一份配置在 Server 上一次就过了，所以别以为规则不存在。）

### F6 这个模拟器镜像上 M-LAG 会漂移，别拿它当稳定底座
实测能通（BAGG1 两成员 `S`、Server 双 VLAN `up/up`），但状态会自己回退：
多次查询可能拿到完全不同的组合（这组 UP 那组 DOWN），重建 peer-link 能恢复一阵子。
**结论**：核心业务实验（MPLS / BGP / VPN 等）不要依赖 M-LAG 收敛；
M-LAG 只作为"配置已具备"交付，验证时以当场回读为准。

### F7 链路闪断会触发 MAD，把 M-LAG 闩死在 DOWN
**症状**：配置全对、peer-link UP、keepalive UP，但 `display m-lag summary` 三组全 `DOWN (B)`；
且 `display m-lag summary` 里 `Peer state` 一栏会自相矛盾（Local DOWN(B) / Peer UP）。
**真凶要用这条命令才看得到**：

```
display m-lag role
  Factors              Local        Peer
  Effective role       Secondary    Primary     <- 配置其实是 Primary（role priority 0）
  MAD DOWN state       Yes          No          <- 关键：被 MAD 置 DOWN
  Effective role trigger: Peer link calculation
  Effective role reason:  MAD status
```

MAD 一旦置位，本端就不向对端宣告自己的 M-LAG 接口 → 对端也报 `DOWN (B)` → **双向死锁**。

**清闩锁**（`m-lag mad restore` 有守卫，两条链路都 UP 时会拒绝执行）：

```
m-lag mad restore
  Can't execute this command when the peer link or keepalive link is up.
```

→ 顺序必须是：**先关 keepalive，再关 peer-link**（先关 peer-link 会让对端误触发 MAD），
然后 `m-lag mad restore`，最后逐条 `undo shutdown` 恢复。验证 `display m-lag role` 变成
`MAD DOWN state No` / `Effective role Primary`。

### F8 M-LAG 排错的四条必查命令（一条都不能省）
| 命令 | 看什么 | 判读 |
|---|---|---|
| `display m-lag role` | MAD DOWN state | `Yes` ⇒ 见 F7 |
| `display m-lag drcp statistics` | peer-link 行 `Sent/Received` | `*BAGG1 UP 112 109/0/0` ⇒ DRCP 正常，问题不在 peer-link |
| `display m-lag system` | 对端 system number/MAC/priority | 能读到 ⇒ DRCP 报文确实交换成功 |
| `display m-lag consistency-check status` | 全局 `Enabled` / `Strict` | **Strict 模式下任何 LAGG/VLAN/STP 不一致都会让接口保持 DOWN**（症状是 `DOWN (C)`） |

`display m-lag summary` 的字母含义只有三个：`A`=聚合口 down，`B`=对端没有 M-LAG 接口，`C`=一致性检查失败。
`display m-lag drcp` / `display m-lag consistency` 是**不完整命令**，要接 `statistics` / `type1|type2`。

### F9 「接入侧是 access 口」⇒ L3 必须配在聚合口本身，不能用 dot1q 子接口
M-LAG 成员聚合口若配成 `port access vlan 30`，它对**被双归的设备**发的是**无标签**帧。
所以 PE 侧必须：

```
interface Route-Aggregation 1
 ip binding vpn-instance <vpn>
 ip address 10.10.0.4 255.255.255.248       # 直接配在聚合口上
```

**不能**用 `Route-Aggregation 1.30` + `vlan-type dot1q vid 30`（标签帧会被 access 口丢掉）。
同理，PE 的物理成员口若是 MSR 路由器，参考解法会显式写 `port link-mode route`。

### F10 该 S6850 镜像上 M-LAG 冷启动死锁（外部同类案例）
[模拟器重启后M-LAG组建失败经验分享](https://www.emulatedlab.com/thread-2384-1-1.html) 记录同平台同症状：
S6850 物理口**默认为三层口**，加载预配置时物理口没能真正加入二层聚合口，导致 M-LAG 组建失败；
并且 **M-LAG 组建失败会让交换机频繁重启**。

该帖的修复手势（本人复现时**只成功了一半**：曾观察到 SW1 的 BAGG2/BAGG4 由 `DOWN (B)` 变 `UP`，
随后又被链路掉线事件打回）：

```
undo interface Bridge-Aggregation 1          # 删掉 peer-link 聚合口
interface Bridge-Aggregation 1               # 重建
interface range GigabitEthernet 1/0/17 GigabitEthernet 1/0/18
 port link-mode bridge                       # ← 必须显式
 port link-aggregation group 1
interface Bridge-Aggregation 1
 port link-type trunk
 port trunk permit vlan all
 link-aggregation mode dynamic
 port m-lag peer-link 1
```

**注意**：本体镜像里工作正常的设备，成员口 `display current-configuration` 里**本来就带**
`port link-mode bridge`，所以别只看这一条就以为没问题 —— 要按 F8 四条命令定位。
**M-LAG 成员聚合口重建时**，成员口必须以**默认属性**加入（见 F5），也就是
「先 `undo interface Bridge-Aggregation N` → 重建 → 成员口 `port link-aggregation group N`
→ 再在聚合口上配 `port access vlan 30` / trunk / `link-aggregation mode dynamic` / `port m-lag group N`」。

### F11 这批链路会整体自行掉线（不是重启）
反复观察到 **SW1/SW2 的 `GE1/0/20/21/22` 连同对端 PE1/PE2 的 `GE0/0+GE6/0`、
Server 的 `GE1/0/17/18` 同时变 DOWN**，而 peer-link(17/18)、keepalive(19) 始终保持 UP；
`display version` 的 uptime **不变**（排除重启），`Last time when physical state changed to down`
能拿到具体掉线时刻。

**恢复手法**（配置无关，纯链路层复位）：

```
interface range GigabitEthernet 1/0/20 GigabitEthernet 1/0/21 GigabitEthernet 1/0/22
 shutdown          # 等 15 秒
 undo shutdown
```

**对端也要一起复位**，否则只起一半（曾只复位 SW1 侧 + PE1 GE0/0 + PE2 GE6/0，
结果只有这 2 条起来，另 2 条仍然 DOWN）。判据：`display interface brief` 里出现 `1G(a) F(a)`。

### F12 ★ 决定性解法：`m-lag consistency-check disable`（S6850 模拟器必做）
**这是本课题最终打通 M-LAG 的那一条命令。** 前面的 F2/F3/F10（重建 peer-link、重建成员聚合口、
清 MAD 闩锁）全都做对了，但 M-LAG 仍卡在 `DOWN (B)`，差的就是它。

```
display m-lag consistency-check status
                 Global Consistency Check Configuration
Local status     : Enabled           Peer status     : Enabled
Local check mode : Strict            Peer check mode : Strict
                 Consistency Check on Modules
Module           Type1           Type2
LAGG             Check           Check
VLAN             Check           Check
STP              Check           Check
```

**Strict 模式下模拟器会误报"两台配置不一致"并直接关闭 M-LAG 接口**（症状既可能是 `DOWN (C)`，
也可能表现为 `DOWN (B)`）。实测两台 `display current-configuration` 逐行 diff 只有主机名/环回/
VLAN 地址/VRRP 优先级等**预期差异**，仍被判不一致。

```
system-view
m-lag consistency-check disable      # 两台都要
```

执行后配合成员聚合口复位即可拉起：

```
interface Bridge-Aggregation 2
 shutdown
 undo shutdown
quit
# 2/3/4 各来一遍，两台都做
```

复位后即应看到 `display m-lag summary` 三组 `UP`，且接入侧
`display link-aggregation summary` 的 `Partner ID` 变成 M-LAG 虚 MAC（如 `0x64, 0001-0001-0001`）、
`Selected Ports` 由 0 变 2。8 条成员口 `display link-aggregation verbose` 全部 `Selected {ACDEF}`。

> 同平台同症状的外部案例（环境与本次完全一致：HCL 5.10.1 + S6850 / Version 7.1.070 Alpha 7170）：
> [H3C交换机S6850配置M-LAG基本功能](https://blog.csdn.net/gtj0617/article/details/135542399)
> （原文：*"可能是模拟器问题…会导致配置一致性检查而关闭 M-LAG 接口…可以通过命令暂时关闭
> M-LAG 配置一致性检查"*）

---

## G. 控制台交互类

### G1 `[Y/N]` 确认会被误判成提示符
`Continue? [Y/N]` 这种确认，若被当成提示符，后续命令会被当作确认答案吃掉 —— 命令静默不生效。
`hcldrv` 已把 Y/N 排除出提示符正则，并提供 `auto_confirm`（`hcl_lab.py --auto-confirm`）自动答 `Y`。
**大量 Comware 命令（改系统参数、改成员口属性、undo 逻辑接口）都会问这一句**，批量下发时务必带上。

### G2 控制台日志会把提示符冲散
LACP 超时、接口 up/down 的 `%` 日志插进回显，`command()` 会等不到提示符而超时。
`hcl_lab.py` 在下发前会先 `undo terminal monitor` 关掉本会话日志输出。

### G3 控制台会话的**视图状态跨连接保持**
断开重连后设备仍停在上次的视图里。所以"某条命令不识别"经常是视图不对，不是命令不对。
`ask.py` 会先打印当前提示符并退回用户视图；要进系统视图请在脚本里显式写 `system-view`。

---

## H. OSPF / BFD / MPLS 配置类

### H1 `ospf network-type p2p` 会**让 OSPF 完全起不来**（路由器↔交换机链路）
实测（HCL 5.10.3）：

| 链路 | 网络类型 | 结果 |
|---|---|---|
| PE3(MSR36) ↔ SW3-IRF(S5820V2) | `p2p` | **邻居起不来**（单播 ping 通、BFD Up，就是 OSPF 无邻居） |
| 同上 | `broadcast` | **秒起** Full/DR + Full/BDR |
| PE1(MSR36) ↔ PE2(MSR36) 子接口 | `p2p` | **正常 Full** |

即：**路由器↔路由器**的 P2P 正常，**路由器↔交换机**的 P2P 不行。
排查时先看 `display ospf interface <if>`：两端都显示 `State: P-2-P`、Hello/Dead 一致、`Enabled by network configuration`，
但 `display ospf peer` 为空 —— 见到这个组合就换 `broadcast` 验证一下，别在定时器上耗。

### H2 OSPF 接口"已经使能"要等一会儿才看得见
`display ospf interface` 在配置刚下发后可能显示空（只有 Area 标题），过一会儿才出现接口。
**别据此判定 `network` 语句没生效**。同样地，批量下发脚本里"验证跑在别的设备配置之前"也会看到空邻居
（设备是顺序处理的），要**全部下发完再统一回读**。

### H3 跨 AS 的 LDP 需要先打通双方 LSR-ID
两个 AS 不共享 IGP 时，LDP 会话建不起来（`display mpls ldp peer` 显示 `Non Existent`），
因为 LDP 用 **LSR-ID（Loopback）** 作为传输地址。补静态路由即可：

```
# ASBR 侧
ip route-static <PE3的loopback> 32 <PE3直连地址>
# PE3 侧
ip route-static <ASBR的loopback> 32 <ASBR直连地址>
```
两条链路各配一条可得到等价路由（ECMP），LSP 表里能看到两个出接口。

### H4 顺序：`mpls lsr-id` 和全局 `mpls ldp` 先配，接口才能 `mpls ldp enable`
否则接口上报 `The feature LDP has not been enabled.`（这句不在默认报错模式里，容易被漏看）。
正确顺序：`mpls lsr-id x.x.x.x` → `mpls ldp` → 接口 `mpls enable` + `mpls ldp enable`。

### H5 Option B 的 ASBR 关键配置
ASBR 不需要为每个 VPN 建实例，在 VPNv4 地址族里对跨域 eBGP 邻居使能并关掉 RT 过滤即可：

```
bgp 65000
 peer <对端AS的直连地址> as-number 65001
 address-family vpnv4
  peer <对端AS的直连地址> enable
  undo policy vpn-target        # Option B：不做本地 VPN 实例，直接转发带标签的 VPNv4 路由
```

### H6 有用的 display 命令（这版和高版本不一样）
| 想看的 | 别用 | 用 |
|---|---|---|
| BGP 邻居 | `display bgp peer`（Incomplete） | `display bgp peer vpnv4` / `display bgp peer ipv4` |
| BGP VPNv4 邻居 | `display bgp vpnv4 all peer`（不识别） | `display bgp peer vpnv4` |
| OSPF 概况 | `display ospf brief`（Wrong parameter） | `display ospf interface` + `display ospf peer` |
| 配置回读 | — | `display current-configuration configuration bgp` / `... ospf` |

### H7 OSPF「邻居 Full，却一条路由都算不出来」——两个隐形不一致
这是最难查的一类，两个原因都会让邻居**显示 Full** 但 SPF 出不来路由：

**① 两端 `ospf network-type` 不一致**（P2P ↔ Broadcast）

```
# 症状
display ospf 101 routing      -> Total nets: 1   Intra area: 1
display ospf 101 lsdb router  -> 对端 Router-LSA 明明完整（含所有 StubNet）
display ospf 101 peer         -> Full/ -  （注意 State 是 "Full/ -"，没有 DR/BDR 角色）

# 判据：看自己 Router-LSA 里那条链路是 P2P 还是 TransNet
Link ID: 7.7.7.7    Link Type: P2P        <- 本端当点对点
Link ID: 10.128.1.2 Link Type: TransNet   <- 对端当广播网
```

两端描述的不是同一种链路 → SPF 拼不出拓扑。**修法**：把两端统一成 `ospf network-type broadcast`
（实测在这套 HCL 上 P2P↔Broadcast 能 Full 但不算路由，**两端都 P2P 反而连邻居都建不起来**）。

**② 两端 Hello/Dead 不一致**（这条更狠：邻居**根本建不起来**）

```
interface Vlan-interface 128
 ospf timer hello 5        <- 一端 5/20
 ospf timer dead 20
另一端是默认 10/40
```

`display ospf peer` 直接**空输出**。修法：`undo ospf timer hello` / `undo ospf timer dead` 回到默认，
或两端配成同一组值。**排查时一定要把两端的 `display current-configuration interface <if>` 拉出来逐行对**，
只在一边 `display ospf peer` 看到 Full 是不够的。

> 顺带：`ospf bfd enable` 只配在一端时同样会妨碍邻居建立，建议两端同配或同不配。

### H8 「VPN 实例里 OSPF 起不来」时的次序
PE 侧（H3C 路由器）多实例的可用写法：

```
ip vpn-instance VPN-A
ospf 100 vpn-instance VPN-A          # 进程号可以按实例区分
 vpn-instance-capability simple      # 必须，否则要跑 sham-link 之类
```

**同一个 OSPF 进程号不能同时出现在两个 VPN 实例里**（会报
`OSPF Process ID already exists in another instance.`），跨实例要么换进程号，
要么按参考解法只用一个 `vpn1`（RD/RT 100:1）——后者才是这类 LAB 的常规答案。

---

## E. 配置下发

### E1 `quit` 把会话踢回用户视图，后续命令全报错
**对策**：`hcl_lab.py` 每条命令后检查提示符（`<...>`=用户视图 / `[...]`=系统视图），发现回到用户视图且还有命令，自动重新 `system-view`。

### E2 命令需要 `[Y/N]` 确认
**对策**：`--auto-confirm` 自动答 `Y`；不开则记成警告，**不会**盲答。`save` 用 `save force` 免交互。

### E3 配置下发成功但重启后丢失
**原因**：没保存。

**对策**：`--save`（执行 `save force`）。交付前必须保存。

---

## I. IPv6 / DHCPv6 / VPN 绑定类

### I1 `ip binding vpn-instance` 会**清掉该接口上的其它配置**（尤其 IPv6）
回显会直接告诉你：

```
[SW3-IRF1-Vlan-interface130] ip binding vpn-instance vpn1
Some configurations on the interface are removed.
```

实测被清掉的是：**IPv6 地址、`ipv6 dhcp select server`、`ipv6 nd autoconfig *-flag`、`undo ipv6 nd ra halt`**
（`display ipv6 interface brief` 全部变 `Unassigned`，`display current-configuration | include ipv6` 空）。
**IPv4 地址同样会被清掉**，必须随后重配。

**对策**：绑定 VPN 实例后，把该接口的 IPv4/IPv6/OSPF/DHCPv6 配置**整体重下一遍**，
并回读确认。顺序：`undo ip address` → `ip binding vpn-instance X` → `ip address ...` → `ipv6 ...` → `ospf ...`。

### I2 同一个 VPN 实例里，两端的 OSPF 进程号**可以不同**
`ospf 1 vpn-instance vpn1`（SW1/SW2/IRF）与 `ospf 200 vpn-instance vpn1`（PE1/PE2）
能正常建邻 —— 进程号是本地概念，只要**接口所在的路由表（VPN 实例）一致、area 一致、Hello/Dead 一致**。

### I3 `display ospf <pid>` 查不到 VPN 实例时，先怀疑"进程号被全局实例占用了"
查过一回：`display ospf 1 peer` / `display ospf 1 interface` 返回空表，
但接口上的 `ospf 1 area 0.0.0.0` 确实配着。原因是该进程号属于 **vpn 实例**，
而 display 先解析到全局。**判据**：不要只看 display，直接
`display current-configuration interface Vlan-interface X` 看接口上有没有 `ospf <pid> area`。
（用只属于该 VPN 的进程号，如 `ospf 100/200`，display 就能正常解析。）

### I4 DHCPv6 服务端：光有 `ipv6 dhcp select server` 不够，还要绑池
```
display ipv6 dhcp server
Interface                       Pool
Vlan-interface130               global        ← 没绑池时是 global，客户端会一直卡在 SOLICIT
```
**修法**（接口视图）：
```
ipv6 dhcp server apply pool <池名>
```
之后应显示 `Vlan-interface130   <池名>`。

### I5 DHCPv6 地址池要用 `network` + `gateway-list`，不要用 `address range`
`address range` 形式在本镜像上 `Total address number: 0`（不生效）：

```
ipv6 dhcp pool vlan130
 network 1:0:0:1::/64
 gateway-list 1:0:0:1::1
```

### I6 SLAAC 是否生效，看三个点
1. 网关侧接口有**同前缀的 IPv6 地址**（如 `ipv6 address 1:0:0:2::1/64`）；
2. 网关侧 **`undo ipv6 nd ra halt`**（默认 ra halt，不发 RA → 客户端拿不到前缀）；
3. 客户端 `ipv6 address auto`。
成功后 `display ipv6 interface brief` 会显示 EUI-64 地址，如
`1::2:98E1:ADFF:FE2A:502, subnet is 1:0:0:2::/64 [AUTOCFG]`（`[AUTOCFG]` 即 SLAAC 得来）。

### I7 DHCPv6 客户端一直 SOLICIT 时，先看服务端收没收到
```
display ipv6 dhcp server statistics
Packets received              :  0
Solicit                   :  0
```
若恒为 0，说明客户端发往 **`FF02::1:2`** 的组播没到服务端 —— 此时**不要再去改池配置**，
要查组播路径：`display ipv6 interface <SVI>` 看是否已加入 `FF02::1:2`，
以及 `display mld-snooping`。单播 IPv6 通（能 ping 通网关）**不代表**该组播能通。

### I8 ★ 判定"是配置问题还是模拟器组播问题"的两步法
**第一步：两侧计数器对账**（这一步就能把责任分清）

```
# 客户端
display ipv6 dhcp client statistics
   Packets sent : 7   Solicit : 7          ← 客户端发了
   Packets received : 0  Advertise : 0     ← 没收到应答
# 服务端
display ipv6 dhcp server statistics
   Packets received : 0  Solicit : 0       ← 服务端也没收到 ⇒ 帧在 L2 上就丢了
```
「客户端发了 7 个 / 服务端收到 0 个」= **中间的组播转发有问题**，池、绑定、VPN 实例全都是无辜的。

**第二步：直接实测 IPv6 组播**

```
ping ipv6 -c 2 -i <出接口> ff02::1     # all-nodes
ping ipv6 -c 2 -i <出接口> ff02::2     # all-routers（对端是路由器就该回）
```
（**必须带 `-i <出接口>`**，否则报 `Outbound interface is required for the ping of a multicast address.`）

实测本课题：PC→IRF 方向 `ff02::1` / `ff02::2` **均 100% 丢包**，而**同链路 IPv6 单播 0% 丢包**、
**反方向（IRF→PC）的 RA 组播正常**（所以 SLAAC 成功、DHCPv6 失败）。
⇒ 定判为 **HCL 运行时该方向 IPv6 组播不转发**，属平台问题，不要再折腾配置。

> 关键鉴别点：**SLAAC 成功不能证明组播双向正常** —— SLAAC 只依赖"网关→主机"单向的 RA 组播。
> DHCPv6 需要"主机→服务器"方向的组播（SOLICIT→FF02::1:2），方向相反，会单独失效。

### I9 ★★ DHCPv6 服务端**不支持绑定了 VPN 实例的接口**（本次最关键的定位）
**症状**：服务端配置齐全（池的 `network`+`gateway-list`、接口 `ipv6 dhcp select server`、
`ipv6 dhcp server apply pool <池>` 已把接口从 `Pool: global` 绑到命名池、M/O 标志、RA 都有），
客户端也确实是 Stateful 并发出了请求，但两边计数器对不上：
```
客户端: Packets sent : 7  Solicit : 7    /  Packets received : 0
服务端: Packets received : 0  Solicit : 0
```
`ping ipv6 -i <该 SVI> ff02::1 / ff02::2` 也 **100% 丢包**（同链路 IPv6 单播却是 0% 丢包）。

**决定性对照实验**：在同一条物理链路上另建一个**全局表**的 SVI（同 VLAN 也行、另开 VLAN 也行），
配上同样的 DHCPv6 服务端，客户端**立刻拿到地址**并且 `State: OPEN`、
服务端 `Ip-in-use: 1 / Packets received: 2 / Solicit: 1`。
⇒ **根因就是接口上的 `ip binding vpn-instance`。**

**此平台没有 DHCPv6 的 VPN 写法**（已用 `?` 逐层确认）：
```
ipv6 dhcp server apply ?      → 只有 pool（没有 vpn-instance）
ipv6 dhcp pool ?              → 只接受池名
ipv6 dhcp ?                   → advertise/class/client/dscp/log/option-group/policy/
                                pool/prefix-pool/server/snooping（无 vpn 相关）
```

**对策（两条路，按需要选）**：
1. **业务 IPv4 必须进 VPN 时**：业务 SVI 保持绑 `vpn1`，**另开一个全局表网段专门跑 IPv6
   SLAAC/DHCPv6**（本次做法：IRF↔PC 之间加 `vlan 900` + 全局 SVI + DHCPv6 服务端，
   PC 侧加对应子接口 `ipv6 address dhcp-alloc`）。SLAAC 在 VPN 绑定接口上是能用的
   （只需网关→主机方向的 RA），所以 SLAAC 可留在原 SVI，DHCPv6 放全局 SVI。
2. **不要求 IPv4 走 VPN 时**：直接把业务 SVI 放全局表（**参考解法的做法**就是全局），
   DHCPv6 与业务同段即可。

> 排查顺序建议：先做上面的**对照实验**（一个全局 SVI 试一把），
> 5 分钟就能判定是"VPN 绑定"还是"平台组播"，比反复调池子高效得多。

---

## J. BGP 社区选路 / 多链路分流

### J1 基于 community 的 A/B 分流，两个必备条件缺一不可
典型需求：两条跨域链路，A 业务走链路1、B 业务走链路2（负载分担）。

**条件一：每条链路的 import 策略必须不同**
```
bgp 65001
 address-family vpnv4
  peer <链路1对端> route-policy server1 import    # 惩罚 B 业务 → B 不走这条
  peer <链路2对端> route-policy server2 import    # 惩罚 A 业务 → A 不走这条
```
策略本体（用 community-list 匹配打标）：
```
ip community-list advanced c1 permit 65000:1      # A 业务
ip community-list advanced c2 permit 65000:2      # B 业务
route-policy server1 permit node 10
 if-match community name c2
 apply cost 10
route-policy server1 permit node 20
route-policy server2 permit node 10
 if-match community name c1
 apply cost 10
route-policy server2 permit node 20
```
**实测踩过的坑**：两条链路都写成 `server1` → 无分流。改成一 `server1` 一 `server2` 立刻生效。

**条件二：`advertise-community` 必须在每一条相关 eBGP 链路上都配**
**这是最容易漏、且症状最迷惑的一条。** 社区只在配了 `advertise-community` 的链路上传递，
没配的那条链路把社区剥掉 → 对端 `if-match community` 匹配不到 → 该链路不参与分流。

```
bgp 65000
 address-family vpnv4
  peer <PE3 链路1地址> advertise-community     # ← 两条都要
  peer <PE3 链路2地址> advertise-community
```
**实测踩过的坑**：只配了一条 → 只有一条链路上的路由带社区，两个业务网段仍选同一条路径。
补齐后立刻分流：
```
10.10.1.0/24  BGP via 100.1.0.14  GE0/0     ← 社区 65000:1
10.10.2.0/24  BGP via 100.1.0.18  GE6/0     ← 社区 65000:2
```

**排查命令**（看上没上社区、选没选对路）：
```
display bgp routing-table vpnv4 10.10.1.0        # 看 Community / From / Original nexthop
display ip routing-table vpn-instance vpn1       # 看两个网段最终的下一跳/出接口是否不同
```
若两个网段的 `From` 相同，先查**对端 ASBR 是不是只在一条链路上 `advertise-community`**，
再查**两条链路的 import 策略是不是配成了同一个**。

### J2 打标用 `apply community`，选路用 `apply cost`（或 local-preference）
- 出向打标：`route-policy xxx permit node 10 / if-match ip address prefix-list A / apply community 65000:1`
  并挂到出向 peer：`peer <ASBR> route-policy xxx export`，同时该 peer 要 `advertise-community`。
- 入向分流：`apply cost 10`（改 MED，数值小者优）或 `apply local-preference`（改本地优先级，数值大者优）。
- **注意**：`if-match community name c1` 用的是 **community-list 名**，不是社区值本身；
  社区列表要先定义，且高级（advanced）模式才能按值匹配。

---

## K. IRF 堆叠 / BFD MAD

### K1 BFD MAD 正确配置（照这个抄）
```
vlan 4094
 description IRF-BFD-MAD
#
interface Vlan-interface 4094
 mad bfd enable
 mad ip address 172.16.0.1 255.255.255.252 member 1
 mad ip address 172.16.0.2 255.255.255.252 member 2
#
interface GigabitEthernet 1/0/19        # 成员1 的 MAD 口
 port link-mode bridge
 port access vlan 4094
 undo stp enable                          # ★ 必做：MAD 链路两端同属一台 IRF，STP 会当自环
#
interface GigabitEthernet 2/0/19        # 成员2 的 MAD 口
 port link-mode bridge
 port access vlan 4094
 undo stp enable
```

### K2 MAD 会话起不来时的逐项排除清单（照顺序做，能把"配置问题"和"平台问题"分开）
```
display mad verbose
  MAD BFD enabled interface: Vlan-interface4094
  MAD status                 : Faulty          ← Faulty = BFD 会话没起来
  Member ID   MAD IP address       Neighbor   MAD status
  1           172.16.0.1/30        2          Faulty
  2           172.16.0.2/30        1          Faulty
```
| 步骤 | 命令 | 期望 |
|---|---|---|
| 1 | `display irf` | 主备正常（Member1 `*+` Master / Member2 `+` Standby） |
| 2 | `display interface Vlan-interface 4094` | `UP/UP` 且 `Internet address: 172.16.0.1/30 (Mad)`（带 `(Mad)` 标记才对） |
| 3 | `display interface GigabitEthernet 1/0/19` | `UP`，`PVID: 4094`，`Port link-type: Access` |
| 4 | `display lldp neighbor-information list` | 该口能看到对端成员的口 → **二层链路本身是通的** |
| 5 | `display stp brief` | 19 口**不出现**在表里（= `undo stp enable` 生效） |
| 6 | `ping -a <本端 MAD IP> <对端 MAD IP>` | 通（**不通就说明 MAD 报文路径不通**） |
| 7 | `display arp` | 有 172.16.0.x 表项 |

**踩过的坑**：`port access vlan X` **不会**重置同接口上的 `undo stp enable`（实测改 VLAN 后仍在），
所以改 VLAN 不用重关 STP。

### K3 ★ 在 HCL 上 BFD MAD 建不起来（配置全对也没用）
本课题实测：上面 K2 的 1–7 步中 1–5 全部正常（含从**备机控制台独立复核**，状态一致），
第 6 步恒为 `Request time out`、第 7 步无 ARP 表项 → `MAD status: Faulty`。

进一步排除：
- **换 MAD VLAN**（4094 → 1000）→ 仍 `Faulty`，**排除"保留 VLAN"因素**；
- STP 已关（两口不在 `display stp brief`）→ 排除 STP；
- LLDP 能看到对端 → 排除链路/线缆接错。

**结论**：MAD 报文的设计目的就是**绕开 IRF fabric 走专用 MAD 链路**，
而 HCL 运行时不转发成员间 MAD 流量，因此 BFD 会话无法建立 —— **属平台限制**。
**IRF2.0 本身完全正常**（主备、单一逻辑设备、业务流量正常穿越）。

**★ 参考解法对实验环境的原话（务必照做）**：
> *SW3-IRF1：**实验环境建议关闭 mad 口，mad 开启设备会卡顿**；`< shutdown 0/19 口 >`*

**本次现场验证了这条警告的后果**（代价不小，务必提前关）：
MAD 开启后该 IRF **控制台卡死**（命令下发 0 条、`ConsoleTimeout`），并且
**IRF 面向下联设备的 `Bridge-Aggregation` LACP 协商楔死**（两侧本地标志只剩 `{A}`/`{AC}`、
聚合 DOWN、下联设备所有子接口 DOWN）。
**恢复手法**：
1. 先在能连上时（或等控制台缓过来后）`shutdown` 两个 MAD 口 → `display interface brief` 显示 `ADM`；
2. **删除并重建那个楔死的聚合口**（`undo interface Bridge-Aggregation N` → 重建 →
   成员口设好 `port link-type trunk` + `port trunk permit vlan ...` 后 `port link-aggregation group N`
   → 再配聚合口的 trunk/mode），两侧随即恢复 `{ACDEF}` Selected。
只做第 1 步不够（复位端口也无效），第 2 步才是解楔死的有效手段。
**并把 `shutdown` 后的状态 `save force` 落盘**，否则重启后 MAD 又启用、又卡顿。

> 顺带：该参考解法的 MAD VLAN 用的是 **4093**（不是常见的 4094），照抄即可。
交付时把 BFD MAD 作为"配置已具备、受平台限制无法验证收敛"的项目说明即可。

### K4 `undo mad bfd enable` 会连带清掉 `mad ip address`
想让 MAD 换个 VLAN 时注意：`undo mad bfd enable` 之后，两条 `mad ip address` **也会一起消失**
（`display current-configuration interface Vlan-interface X` 只剩 `mad bfd enable`）。
**必须先 `undo mad bfd enable`，重配时再把两条 `mad ip address` 补回来**，否则 MAD 表里两个成员都不见了。
（另：同一时刻只允许一个接口 `MAD BFD enabled` —— 新接口启用前必须先关旧接口。）

### K5 附带学到的两个语法坑
**① 从聚合组里摘端口，`undo port link-aggregation group` 后面不能带编号**
```
undo port link-aggregation group 1     → % Too many parameters found at '^' position.
undo port link-aggregation group       → ✅
```
（同理 `undo port m-lag group` 也不带编号 —— 在 M-LAG 那边踩过同样的坑。带编号的写法只在**配置**时用。）

**② 不要把 IPv6 组播不通归咎于"聚合口子接口"**
曾在 PC 侧做过对照实验：把 RAGG1 的两个成员**摘掉一个**（只留 GE0/0，聚合仍 UP），
再测 `ping ipv6 -i Route-Aggregation 1.130 ff02::2` —— **仍然 100% 丢包**。
⇒ 与 LAG 成员哈希/子接口无关，是 HCL 对 PC→交换机方向 IPv6 组播的普遍限制。
（实验做完记得把成员加回：`port link-aggregation group 1`。）

> 注：后来的 I9 进一步定位到——**真正的开关是接口有没有绑 VPN 实例**：
> 同一个 DHCPv6 服务端配在**全局 SVI** 上立刻就通了。所以本节的"普遍限制"
> 更准确的表述是"**PC→IRF 方向、且服务端在 VPN 绑定接口上**"这一组合不行。

---

## L. 方法论：排障优先级、适配性与省 token 取证

### L1 排障第一动作：**查厂商官方文档**（不管 H3C / 华为 / 思科 / 锐捷）
| 厂商 | 官方来源 |
|---|---|
| H3C | 官网「配置指导」+「命令参考」（**认准版本**，如 Comware 7 / S6850）、`kms.h3c.com` 知识库、`zhiliao.h3c.com` 问答 |
| 华为 | `support.huawei.com` 产品文档（配置指南 / 命令参考） |
| 思科 | `cisco.com` Configuration Guides / Command Reference |
| 锐捷 | `ruijie.com.cn` 文档中心 |

顺序：**厂商官方文档 → 官方知识库/问答 → 同厂商其他平台案例 → 第三方博客**。
官方没解决再往下，并在结论里写明"官方文档未覆盖 / 未能解决"。

**还有一份最权威的"现场文档"**：设备上的 `display xxx ?`。
同一命令在不同版本支持的参数可能不同，**不确定语法就先 `?`**，别猜：
```
ipv6 dhcp ?                    # 看这一版到底有哪些子命令
ipv6 dhcp server apply ?       # 看这个位置到底收什么参数
```
（本课题就是靠 `?` 确认了"此平台 DHCPv6 没有 vpn-instance 写法"，直接排除了整条错误路线。）

### L2 适配性优先：**能落地才算完成**，别硬啃最新特性
同一功能常有多种写法，**经典/参考解法写法适配性通常更好**。一种写法不通就**换等价写法**，
不要在同一个写法上反复调参数。本项目真实替换记录：

| 原始写法（不通/不稳） | 换成的等价写法 | 结果 |
|---|---|---|
| DHCPv6 池 `address range` | `network <prefix>` + `gateway-list` | 立刻生效 |
| 业务网段 `import-route direct` + `silent-interface` | `silent-interface` + 区域网段发布（`ospf 1 area 0.0.0.0`） | 立即被发布且无动态报文 |
| 两个 VPN 实例（VPN-A/VPN-B） | 单一 `vpn1`（RD/RT 100:1，参考解法口径） | 简化且互通 |
| PE 侧 `RAGG1.30` dot1q 子接口 | L3 直接配在聚合口上（对端是 access 口） | 立即可用 |
| MAD VLAN 4094 | 照抄参考解法的 **4093** | 与参考一致 |
| OSPF 两端 `p2p`/`broadcast` 不一致 | 两端统一（本环境用 broadcast） | 邻居 Full 且算出路由 |

**判断准则**：某特性在本环境/本版本反复不成立 → 换等价方案并写明取舍，
**不要为了"用上新特性"卡住整个交付**。

### L3 两击规则
同一症状失败 **2 次**就停手换路子：查官方文档 / 搜同厂商案例 / 直接问用户。
本次教训：M-LAG 试了重建 peer-link、重建成员聚合口、清 MAD 闩锁、反复复位端口……
到第 10 多轮才去搜案例，一条 `m-lag consistency-check disable` 解决。
**该搜的时候不搜，是最贵的浪费。**

### L4 省 token 的取证方式
1. **验证用 `scripts/verify.py`**：每项一行 `PASS/FAIL`，全量回显进报告文件
   （一次完整 20 项验证 ≈ 20 行输出，手工做要几千 token）。
2. **下发/回读用 `hcl_lab.py --out <目录>`**，分析时读文件，不重跑命令。
3. **设备侧过滤**：`display xxx | include <关键词>`，只让匹配行回来。
4. **状态落盘** `state.json`（端口→设备、地址规划、VLAN/接口映射、各项最近结果），新一轮先读它。
5. **汇报要短**：对话里只说结论与下一步，明细放文档。
6. `verify.py` 清单要给慢命令单独设 `timeout`（`ping` / `save force` / `undo interface ...`
   默认 15s 会误报 FAIL）。
---

## M. 下发引擎与视图状态（C-002 血泪章）

### M0 ★★ 配置命令报 `% Unrecognized` 时，先怀疑"视图不对"，再怀疑"命令不支持"
```bash
display clock          # 能回显 → 会话正常
sysname TMP            # 报 Unrecognized → 99% 是在用户视图 <H3C> 下敲的配置命令
```
- 配置命令只在 `[H3C]`（系统视图）下有效；用户视图是 `<H3C>`。
- 下发引擎必须：**进 system-view 并确认提示符变成 `[...]`**，每条配置命令前校验视图。
  C-002 里"整批命令全报 Unrecognized"就是这个原因，白查了半天版本兼容性。

### M1 ★ HCL 控制台跨会话保持 CLI 视图
新 telnet 会话会继承上一次留下的视图。表现：同一串命令"手敲正常、脚本报错"。
```
# 连上后先做
return            # 从任意子视图回到系统视图
quit              # 系统视图 → 用户视图
# 然后才取主机名：只取提示符括号内 '-' 之前的第一段（[SW8-ospf-1] → SW8）
```

### M2 ★ 两条命令之间不要发裸 `\r`
驱动把 `---- More ----` 当分页补一个空格；若输入行里已有一个裸 `\r`，
`空格 + \r` 会被设备当成回车提交，**把 CLI 从子视图顶出去**
（表现为下一条 `area 1` 报 `% Unrecognized`）。要等就 `time.sleep()`，不要发字节探测提示符。

### M3 视图状态机：显式深度计数（照这个实现）
| 当前 | 下一条命令 | 动作 |
|---|---|---|
| 任意 | `interface `/`ospf `/`area `/`acl `/`vlan `/`nqa `/`route-policy `/`line `/`local-user `/`dhcp server ip-pool ` … | 先 `ensure_view(system)`（退出已有子视图），执行后 `sub = 1` |
| 子视图内 | 普通配置命令（`network `/`port `/`ip address `/`rule `/`ospf timer ` …） | **什么都不做**，留在当前视图 |
| 任意 | `quit` | `sub -= 1` |
| 用户视图 | 任意配置命令 | 先 `ensure_view(system)`，`sub = 0` |

**"进入块"前缀表里绝不能有**：`track `、`nqa schedule `、`ospf timer `、`ospf bfd `、
`port link-aggregation `、`display `、`undo `。

### M4 报错判定要用两套正则
- **严格**（用于状态决策）：`%\s*(Unrecognized|Wrong|Incomplete|Ambiguous|Too many|Invalid|Not supported|...)`、`Error:`、`This subnet overlaps`。
- **宽松**（仅用于生成问题清单）：可额外包含孤立 `^`。
- 孤立 `^` 在正常回显里也可能出现；把它当错误会导致状态计数错乱。
- `This subnet overlaps with another interface!` **不带 `%`**，只认 `%` 会漏检。

### M5 地址不能重复占：LoopBack 与管理 SVI
`LoopBack0 10.255.0.1/32` 与 `Vlan-interface4094 10.255.0.1/24` 会报
`This subnet overlaps with another interface!` 且**静默失败**（回显不带 `%`）。管理地址只配一处。

---

## N. IRF 堆叠（HCL 实测，补齐 K 章）

### N1 ★★ HCL 上 IRF 物理口必须交叉绑定
`irf-port 1/1 ↔ irf-port 2/2`、`irf-port 1/2 ↔ irf-port 2/1`。
同号（1/1↔2/1）会：`display irf link` 两个口都 `UP`、LLDP 看得到对端，
但 `display irf topology` 全是 `ISOLATE`、两台各自 Master。
（来源：H3C 官方知识库《在华三模拟器 H3C Cloud Lab 上做堆叠时，堆叠失败》）

### N2 IRF 口解绑的语法
```
undo port group interface FortyGigE2/0/53     ✅（设备 ? 给的形式，接口名不带空格）
undo port group interface FortyGigE 2/0/53    ❌ % Wrong parameter
port  group interface FortyGigE 2/0/54        ✅ 配置时带空格
```
且必须在**该物理口当前所在的那个 irf-port 视图**里 undo。

### N3 改完绑定必须 active + save，再 reboot
```
irf-port-configuration active     # 设备会明确提示要先做这个
save force
reboot
```
只改绑定不 save 就重启 → 启动配置里 IRF 口仍是 `disable` → IRF 不成型（C-002 实际踩过）。

### N4 renumber 之后必须先重启
`irf member 1 renumber 2` 当场不生效（本次还会读超时），此刻仍要写 `irf-port 1/x`；
用 `2/x` 会报 `% Wrong parameter`。**renumber → save → reboot → 再用新编号配 irf-port**。

### N5 ★★ HCL 上 MAD 只配不激活（K3 的强制版）
K3 已写"HCL 不转发成员间 MAD 报文、MAD 开启设备卡顿、参考解法要求 MAD 口 shutdown"。
C-002 里仍激活了 MAD，结果：CPU 97%、**两个控制台全部无响应**（屏幕刷 `.`，
`Ctrl+C`/回车/`q`/`Ctrl+Z` 全无效），只能 GUI 重启。
```
# 正确姿势：配置齐备 + MAD 口 shutdown
vlan 8
interface Vlan-interface 8
 mad bfd enable
 mad ip address 10.255.253.1 255.255.255.252 member 1
 mad ip address 10.255.253.2 255.255.255.252 member 2
interface GigabitEthernet 1/0/48
 port access vlan 8
 undo stp enable
 shutdown                      ★
interface GigabitEthernet 2/0/48
 port access vlan 8
 undo stp enable
 shutdown                      ★
save force
```
- 控制台刷死后**软件无法恢复**：让用户在 HCL GUI 里 Stop/Start。
  **不要反复给刷死的控制台发探测字节**，只会加重负载。

---

## O. 聚合（补充 F5 / C-001 1.2）

### O1 ★ 成员口入组前属性必须与聚合口一致（顺序不能反）
```
port link-aggregation group 10
Can't assign the port to the aggregation group because its attribute configurations
are different than the aggregate interface.
```
顺序：成员口 `port link-type trunk` + `port trunk permit vlan ...` → 聚合口属性 → 成员口 `port link-aggregation group N`。
**报这条错时端口并未入组**，`display link-aggregation summary` 会显示该聚合 0 Selected。

### O2 ★ 三层动态聚合：成员口必须先切 `port link-mode route`
否则 `port link-aggregation group N` 对 `Route-Aggregation N` 报
`The link aggregation group does not exist.`（聚合口其实存在）。
```
interface GigabitEthernet 1/0/4
 port link-mode route        # 会问 Continue? [Y/N]，自动答 Y
 port link-aggregation group 1
```
切完立刻起聚合，OSPF 邻居随之 Full。

---

## P. "通但没有邻居"（路由/协议层第一判据）

### P1 ★ 接口能 ping 通 + OSPF 接口有状态 + 没有邻居 ⇒ 先查 `silent-interface`
C-002 里核心 OSPF 误配 `silent-interface Vlan-interface 92`（正是 area1 互联口），
表现：两边 SVI 互 ping 通、`display ospf interface` 都 `State: DR`、`display ospf peer` 全空。
```
display current-configuration configuration ospf | include silent-interface
undo silent-interface Vlan-interface 92
```

### P2 OSPF 认证两端必须一致
一端 `area X authentication-mode simple` 而另一端没配，同样"通但无邻居"，且无任何报错。

### P3 NAT server 验证不要用 ping
`nat server` 只转换指定协议/端口，**ICMP 不转换**。验证要用对应协议（FTP/HTTP）。
且内网映射地址必须能从 R1 路由到（本次 R1 无 192.168.200.0/24 → FTP `connect: Connection timed out`）。

### P4 NQA 探测目标与默认路由的互锁
用"经默认路由可达的地址"做 NQA 目标 → track 一 Negative 撤掉默认路由 → 探测永久失败。
给探测目标配**不带 track 的明细路由**，并在模拟侧给它真实承载（如对端 `LoopBack 1 = 114.114.114.114/32`）。

---

## Q. MSR36-20（Comware 7.1.064 R0427P22）语法差异表

| 功能 | 不可用写法 | 可用写法 |
|---|---|---|
| 三层聚合 | `interface Bridge-Aggregation 1` | `interface Route-Aggregation 1` |
| DDR 规则 | `dialer-rule 1 ip permit` | `dialer-group 1 rule ip permit` |
| dialer bundle | `dialer bundle 1` | `dialer bundle enable` / `dialer bundle-member 1` |
| 对端名 | `dialer user X` | `dialer peer-name X` |
| 静态路由 track | `... preference 60 track 1` | **`... track 1 preference 60`** |
| 前缀列表 | `ip ip-prefix NAME ...` | `ip prefix-list NAME ...` |
| 过滤 | `filter-policy import ip-prefix NAME` | `filter-policy prefix-list NAME import` |
| 地址池 | `ip pool NAME` | `dhcp server ip-pool NAME` |
| NQA 重建 | 重发 `type icmp-echo` | 先 `undo nqa entry <admin> <name>` |
| 删 dialer 规则 | `undo dialer-group 1` | `undo dialer-group` |

### Q1 ★ 串口链路做不了 PPPoE
`pppoe-server` / `pppoe-client` 只在**以太口**可用。拿到"某 ISP 用 PPPoE 拨号"的需求时，
**先看 ISP 互联链路是串口还是以太口**：串口就直接说明不可实现并申请做等价降级
（串口 PPP + 地址，Dialer 配置保留但不承载 IP），不要做到一半才发现。

---

## R. 并行作业纪律（多 agent / 多进程）

### R1 ★★ 同一条控制台同一时刻只能有一个下发者
两个进程同时往一台设备下发时，回显里会出现
`% Ambiguous command` / `% Wrong parameter` 这类**假报错**，
更糟的是会把互相冲突的配置都写进去（C-002 里 PBR 被写进一个不存在的下一跳 `192.168.10.3`）。
**并行前先按设备划片，明确"哪台归谁"。**

### R2 子代理产出的"配置计划"必须由主控复核关键项
C-002 里子代理计划有两处真硬伤：
① 分部 AP 管理网段没有任何端口在对应 VLAN（SVI down、DHCP 池谁也服务不到）；
② 两个 RIP 互联网段落在不同 VLAN（邻居永远建不起来）。
复核一遍的成本远小于事后排障。

### R3 共享工具文件要约定所有权
本次 `tool.py` 被会话中的多个执行者先后修改，出现两次回归
（"每条配置命令前弹出接口子视图"、"`sub` 计数把 `track`/`nqa schedule` 当进入块"），
每次都导致整批计划报错。**工具文件归一个人改**，改完先拿一台做样板验证。

