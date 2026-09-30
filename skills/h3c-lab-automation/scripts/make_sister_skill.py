#!/usr/bin/env python3
"""make_sister_skill.py - 从本 skill 生成**姊妹技能**（华为 / 思科 / 锐捷 …）。

思路：把本 skill 的**通用骨架**复制过去，厂商相关部分留成骨架待填。
通用骨架 = 五条铁律 + 记忆机制（cases/gotchas/aliases + remember/newcase/lintmem）
         + 工具链（verify/report/cfgdiff/cleanup/lab_state/doctor）
         + 通用 telnet 控制台驱动（hcldrv.py，eNSP/GNS3/EVE/真机 console 都能用）。
厂商相关 = 端口发现、CLI 语法、平台坑（各写进自己的 references/gotchas.md）。

用法::

    python make_sister_skill.py list                 # 看内置的厂商预设
    python make_sister_skill.py huawei --dest <目录>  # 生成 <目录>/huawei-lab-automation
    python make_sister_skill.py all --dest <目录>     # 三个都生成
    python make_sister_skill.py huawei --dest <目录> --force   # 覆盖已存在的

生成后：把整个目录拷到目标终端的 skills 目录，然后按 SKILL.md 里的 TODO 填空即可。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# 通用工具：与厂商无关，直接复制（src, dst）
GENERIC_SCRIPTS = [
    ("hcldrv.py", "hcldrv.py"),          # 通用 telnet 控制台驱动（IAC/翻页/提示符/超时）
    ("hcl_lab.py", "lab.py"),            # 批量下发引擎（plan.json → 逐条下发 + 报错检出 + 证据）
    ("verify.py", "verify.py"),          # 验证矩阵（一行一项）
    ("verify-example.json", "verify-example.json"),
    ("remember.py", "remember.py"),      # 查记忆
    ("newcase.py", "newcase.py"),        # 写回记忆
    ("lintmem.py", "lintmem.py"),        # 记忆体检
    ("lab_state.py", "lab_state.py"),    # 状态落盘
    ("report.py", "report.py"),          # 交付报告
    ("cfgdiff.py", "cfgdiff.py"),        # 配置差异（含"两台都缺"）
    ("cleanup.py", "cleanup.py"),        # 临时/旁路对象审计
    ("doctor.py", "doctor.py"),          # 自检
]

# 姊妹技能自己的必需文件清单（不含 h3c 专属的 .net 解析/SecureCRT 等）
SISTER_REQUIRED = [
    "SKILL.md",
    "references/environment.md",
    "references/gotchas.md",
    "references/cases.md",
    "references/aliases.md",
    "references/ts-playbook.md",
    "scripts/hcldrv.py",
    "scripts/lab.py",
    "scripts/ports.py",
    "scripts/verify.py",
    "scripts/remember.py",
    "scripts/newcase.py",
    "scripts/lintmem.py",
    "scripts/lab_state.py",
    "scripts/report.py",
    "scripts/cfgdiff.py",
    "scripts/cleanup.py",
    "scripts/doctor.py",
]

VENDORS = {
    "huawei": {
        "NAME": "Huawei", "cli": "VRP",
        "emu": "eNSP / EVE-NG / 真机",
        "doc": "support.huawei.com 产品文档（配置指南 / 命令参考）",
        "port_hint": "eNSP 每台设备有一个 console 端口（在 eNSP 界面或拓扑文件里），"
                     "先 `python scripts/ports.py`（需按本厂商补一个）确认。",
        "save": "save（VRP 里 `save` 会问 y，用 `save force` 或交互回答）",
    },
    "cisco": {
        "NAME": "Cisco", "cli": "IOS / IOS-XE / NX-OS",
        "emu": "GNS3 / EVE-NG / CML / 真机",
        "doc": "cisco.com Configuration Guides 与 Command Reference",
        "port_hint": "GNS3/EVE 设备右键 Console 或看拓扑文件里的 console 端口（多为 2000+ 段）。",
        "save": "write memory（或 copy running-config startup-config）",
    },
    "ruijie": {
        "NAME": "Ruijie", "cli": "RGOS",
        "emu": "RG-Cloud / 真机",
        "doc": "ruijie.com.cn 文档中心（配置指南 / 命令参考）",
        "port_hint": "RG-Cloud 设备详情里有 console 端口；真机走串口/SSH。",
        "save": "write（或 copy running-config startup-config）",
    },
}

SKILL_MD = """---
name: {slug}
description: Use when configuring, verifying or troubleshooting {NAME} devices ({cli}) in a lab ({emu}) — turning a topology plus requirements into per-device configs, pushing them over the console, reading back evidence, and keeping a reusable failure memory. Sister skill of `h3c-lab-automation`: same five rules, same memory mechanism, same toolchain; only the {NAME}-specific parts need filling in.
---

# {NAME} lab automation（姊妹技能）

本技能是 `h3c-lab-automation` 的**姊妹技能**：**方法论、记忆机制、工具链完全一样**，
只有厂商相关部分（端口发现 / CLI 语法 / 平台坑）需要按 {NAME} 填。

## 环境硬事实（新终端上先核对，改这里就行）

| 项 | 值 |
|---|---|
| 模拟器/真机 | {emu} |
| 控制台接入 | {port_hint} |
| CLI | {cli} |
| 官方文档 | {doc} |
| 保存配置 | {save} |
| Python | 3.8+，**只用标准库** |

## 铁律（与 h3c-lab-automation 一致，五条）

1. **省 token**：证据落文件、只把结论读进上下文；设备侧用 `\\\\| include` 过滤；
   状态写 `state.json`（`lab_state.py`）；同症状失败 2 次就换路子；汇报短。
2. **排障优先级：记忆 → 官方文档 → 外部搜索**
   ```bash
   python scripts/remember.py --list
   python scripts/remember.py <关键词>
   ```
   没命中再查 {doc}；设备上的 `display ?` / `show ?` 是**最权威的现场文档**。
3. **适配性优先**：一种写法不通就换等价写法，别同一写法反复调参；参考解法/官方示例优先照抄。
4. **交付判定**：下发无报错 + 回读断言通过 + 已保存。
5. **收工必须写回记忆**：`python scripts/newcase.py "标题" --symptom "..." --keywords "a,b"`
   → 填充根因/解法/验证 → 可复用的提炼进 `references/gotchas.md` → `python scripts/lintmem.py`。

## 工作流

1. **读输入**：先产出「设备表 + 规划表」（设备/型号/角色/连线；VLAN、网段、网关、路由域、策略）。
   有歧义先问，别默默替用户选。
2. **端口发现**：★ **本厂商的 `scripts/ports.py` 需要你自己补**
   （照抄 `h3c-lab-automation/scripts/hcl_ports.py` 的结构：枚举/推导端口 → 连上去读主机名与型号）。
3. **写 `plan.json`**：`{{"devices":[{{"name":"SW1","port":<端口>,"commands":[...],"verify":[...]}}]}}`
   —— 与 h3c 版同格式，`hcl_lab.py` 可直接复用（本目录已带该脚本需自行从 h3c skill 复制，
   或参考其"逐条下发 + 报错检出 + 证据落文件"的做法）。
4. **下发**：先 `--only` 单台样板，再铺开；报错必须逐条处理。
5. **验证**：把每条要求写成 `checklist.json`，用 `verify.py` 一次跑完（一行一项）。
6. **保存**：{save}。
7. **写回记忆 + 出交付报告**：`newcase.py` + `report.py`。

## 工具（都在 scripts/，与厂商无关，可直接用）

| 脚本 | 作用 |
|---|---|
| `hcldrv.py` | **通用 telnet 控制台驱动**（IAC 协商、`---- More ----` 翻页、提示符识别、超时诊断） |
| `verify.py` + `verify-example.json` | 验证矩阵：一项一行 PASS/FAIL，全量回显写报告 |
| `remember.py` / `newcase.py` / `lintmem.py` | 查记忆 / 写回记忆 / 记忆体检 |
| `lab_state.py` | 状态落盘 `state.json`（端口、地址规划、验证结果） |
| `report.py` | 把 verify 报告 + 拓扑 + 证据合成交付文档 |
| `cfgdiff.py` | 多设备配置差异（含"**两台都缺**"检查） |
| `cleanup.py` | 交付前审计临时/旁路对象（保留还是清除） |
| `doctor.py` | 自检（文件齐全/可编译/无绝对路径/记忆可查/索引一致） |

## ⚠️ 本技能需要你补的部分（生成时留空是有意的）

- [ ] `scripts/ports.py`：{NAME} 的端口/设备发现
- [ ] `scripts/lab.py`：批量下发（可从 h3c skill 的 `hcl_lab.py` 复制后改提示符与报错模式）
- [ ] `references/environment.md`：本终端的实际路径与版本
- [ ] `references/gotchas.md`：{NAME} 平台坑（先从空模板开始攒）
- [ ] `references/comware-recipes.md` 的对应物：{NAME} 常用配置片段 + 验证命令
- [ ] `references/ts-playbook.md`：排错卷的定位树（可先照抄 h3c 版结构）

## 迁移

整个目录拷到目标终端的 `${{DSH_HOME:-~/.dsh}}/skills/` 即可（Windows: `%USERPROFILE%\\.dsh\\skills`）。
**别只拷 SKILL.md** —— 记忆（`references/cases.md`、`gotchas.md`）和工具才是价值所在。
换终端后先跑 `python scripts/doctor.py`。
"""

GOTCHAS = """# 坑与真相（按症状索引）—— {NAME} / {cli}

> 出问题**第一动作是查记忆**：
> ```bash
> python scripts/remember.py <关键词>
> python scripts/remember.py --list
> ```
> 顺序：**记忆 → {doc} → 外部搜索**。
> 新的坑解决后：细节进 `cases.md`，可复用的提炼到本文件（一条一小节）。

## A. 控制台 / 接入

### A1（示例，替换成本厂商的真实坑）
**症状**：
**原因**：
**对策**：

## B. 二层

## C. 路由

## D. 策略 / 安全
"""

CASES = """# 案例记忆（每次配置作业后追加）—— {NAME}

> **出问题第一步查这里**，不是查文档。查法：`python scripts/remember.py <关键词>`。
> 写法：症状用**报错原文或你的原话**；每条给**可复制的命令**与**验证方式**。
> 新增案例用 `python scripts/newcase.py "标题" --symptom "..." --keywords "a,b"`（会自动插索引）。

## 索引

| 编号 | 日期 | 场景 | 一句话症状 | 关键词 |
|---|---|---|---|---|
"""

ENVIRONMENT = """# 环境事实（{NAME} / {emu}）

> **换终端只需要改这一个文件。** 其余 references 里的技术结论是跨终端通用的。

| 项 | 值 | 怎么确认 |
|---|---|---|
| 模拟器/真机 | {emu} | — |
| 设备控制台端口 | TODO | {port_hint} |
| 控制台接入方式 | telnet / ssh / 串口 | `python scripts/ports.py` |
| Python | 3.8+，只用标准库 | `python -V` |
| CLI | {cli} | — |
| 官方文档 | {doc} | — |
| 保存命令 | {save} | — |
| 拓扑与实验文件 | TODO | 记录绝对路径 |

## 环境变了怎么重新发现

1. 模拟器在跑吗（进程/界面）；
2. 设备起来了没 → `python scripts/ports.py`（若还没写，先补这个脚本）；
3. 扫不到设备 → 模拟器没启动 / 端口不对，**停下来问用户，别猜**。
"""

ALIASES = """# 记忆检索同义词表 —— {NAME}

`remember.py` 会读本文件：**同一行内用 `|` 分隔的词互为同义词**，
每个查询词只要同义词组内**任意一个**命中就算命中。

把**报错原文的英文**、**你的口头说法**、**官方术语**都塞进同一行 —— 漏了同义词等于记忆检索不到。

邻居|neighbor|adjacency|peer|Full|Established
掉线|link down|down|shutdown|掉端口
不通|ping 不通|丢包|packet loss|unreachable
配置丢失|save|write memory|startup|重启后
卡顿|stall|hang|无响应|超时|timeout
端口|console|telnet|ssh|控制台
"""

TS_PLAY = """# 排错卷（TS）手册 —— {NAME}

> 排错卷玩法：**先注入故障，再定位**。纪律：一次只改一处；改前存快照；
> 每步留证据；定位到根因再动手。

## 一、通用定位顺序（从下往上，别跳层）

```
端到端不通
 ├─① 物理/链路：接口 brief（UP / ADM / DOWN）
 ├─② 二层：vlan / 聚合成员是否 Selected
 ├─③ 邻居：路由协议邻居表（**两端都要看**）
 ├─④ 路由：路由表里该有的在不在（"邻居 Full 却无路由"另有原因）
 ├─⑤ 标签/隧道：LSP / SA / 会话状态
 ├─⑥ VPN/策略：RT 匹配、Community 传递、next-hop 可达、策略命中
 └─⑦ 终端设备：默认网关、源地址、vpn 上下文
```

## 二、注入点清单（自己攒，示例格式）

| # | 注入什么 | 期望症状 | 定位命令与判据 |
|---|---|---|---|
| 1 | TODO | TODO | TODO |
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make(vendor: str, dest: Path, force: bool) -> int:
    v = VENDORS[vendor]
    slug = "%s-lab-automation" % vendor
    target = dest / slug
    if target.exists() and not force:
        print("已存在（加 --force 覆盖）：%s" % target)
        return 1
    ctx = dict(v, slug=slug)
    write(target / "SKILL.md", SKILL_MD.format(**ctx))
    write(target / "references" / "environment.md", ENVIRONMENT.format(**ctx))
    write(target / "references" / "gotchas.md", GOTCHAS.format(**ctx))
    write(target / "references" / "cases.md", CASES.format(**ctx))
    write(target / "references" / "aliases.md", ALIASES.format(**ctx))
    write(target / "references" / "ts-playbook.md", TS_PLAY.format(**ctx))

    copied, missed = [], []
    for src_name, dst_name in GENERIC_SCRIPTS:
        src = HERE / src_name
        if src.exists():
            (target / "scripts").mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target / "scripts" / dst_name)
            copied.append(dst_name)
        else:
            missed.append(src_name)
    # 姊妹技能的 doctor.py 要检查**自己**的文件清单（不是 h3c 的），否则会一直误报缺 SecureCRT 等
    doc = target / "scripts" / "doctor.py"
    if doc.exists():
        text = doc.read_text(encoding="utf-8")
        block = "REQUIRED = [\n" + "".join('    "%s",\n' % f for f in SISTER_REQUIRED) + "]"
        text = re.sub(r"REQUIRED = \[.*?\n\]", block, text, count=1, flags=re.S)
        text = text.replace("scripts/hcl_ports.py", "scripts/ports.py")
        doc.write_text(text, encoding="utf-8")

    print("生成 %s：%d 个文件（复制通用工具 %d 个%s）"
          % (target, len(list(target.rglob("*"))), len(copied),
             "；缺：" + ", ".join(missed) if missed else ""))
    print("  下一步：按 SKILL.md 末尾的 TODO 补 scripts/ports.py 与 references/*")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从 h3c skill 生成姊妹技能")
    ap.add_argument("vendor", choices=list(VENDORS) + ["all", "list"])
    ap.add_argument("--dest", default=".", help="输出目录（会在其下建 <vendor>-lab-automation）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在")
    args = ap.parse_args()

    if args.vendor == "list":
        for k, v in VENDORS.items():
            print("  %-8s %-34s %s" % (k, v["NAME"] + " / " + v["cli"], v["emu"]))
        return 0

    dest = Path(args.dest).resolve()
    who = list(VENDORS) if args.vendor == "all" else [args.vendor]
    rc = 0
    for w in who:
        rc |= make(w, dest, args.force)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
