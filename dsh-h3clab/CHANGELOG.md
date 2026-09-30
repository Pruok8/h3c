# 变更记录

本文件记录 `dsh-h3clab` 的版本变更。版本号同时写在 `package.json` 的 `version` 里
（自测有一条断言盯着两者一致，防止漂移）。

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
- **工具描述压缩 21%**（2676 → 2107 字符）。说明白一点：13 个工具的完整 schema 里
  **参数说明才是大头**，每轮请求的 schema 约 10 KB（≈4000 token），这次只省下约 77 token。
  真正的 token 大头是**输出**，所以上面两条才是关键。

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
