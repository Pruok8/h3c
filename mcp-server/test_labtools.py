#!/usr/bin/env python3
"""新增 5 个工具的离线回归测试（不需要 HCL 启动）。

覆盖：hcl_doctor / hcl_cfgdiff(list) / hcl_lab_state / hcl_report / hcl_memory_write。

配置快照（hcl_cfgdiff 的 snapshot/diff）需要一台能连的设备，这里只测 list；
真机路径在本工作区的 plugin-selftest / 手工调用里验证。

用法： python test_labtools.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:                       # pragma: no cover
    pass

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = "%s  %s" % (mark, name)
    if detail and not ok:
        line += "\n        %s" % detail.replace("\n", "\n        ")
    print(line)


def mcp_call(tool: str, arguments: dict, env_overrides: dict | None = None) -> str:
    """按 MCP 协议喂 initialize + tools/call，返回工具返回的文本。"""
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "test_labtools", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": tool, "arguments": arguments}},
    ]
    env = dict(os.environ)
    env.pop("H3C_MCP_CONFIG", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.update(env_overrides or {})
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run([sys.executable, str(SERVER)], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, cwd=str(HERE), timeout=120)
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if isinstance(msg, dict) and msg.get("id") == 2:
            result = msg.get("result") or {}
            return "\n".join(b.get("text", "") for b in (result.get("content") or [])
                             if isinstance(b, dict))
    return "（没有收到响应）stderr=%s" % proc.stderr[-500:]


def write_memory_fixture(root: Path) -> Path:
    """造一个和真记忆库同构的最小 cases.md（索引表 + 小节）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "cases.md").write_text(
        "# 案例记忆\n\n## 索引\n\n"
        "| 编号 | 日期 | 场景 | 一句话症状 | 关键词 |\n"
        "|---|---|---|---|---|\n"
        "| C-001 | 2026-09-17 | 旧的场景 | 旧的一句话 | 旧, 关键词 |\n"
        "\n---\n\n## C-001 旧的场景\n\n正文。\n",
        encoding="utf-8")
    (root / "gotchas.md").write_text(
        "# 坑与真相\n\n---\n\n## A. HCL / 设备控制台\n\n### A1「旧的坑」\n\n正文。\n",
        encoding="utf-8")
    (root / "aliases.md").write_text("# 同义词表\n\n卡顿|stall|hang\n", encoding="utf-8")
    return root


def test_doctor(tmp: Path, cfg_env: dict) -> None:
    text = mcp_call("hcl_doctor", {"probe": False}, cfg_env)
    check("doctor：输出分节报告", "== 1) 运行环境 ==" in text and "== 5) 设备可达性 ==" in text, text[:400])
    check("doctor：有结论段", "== 结论 ==" in text, text[-400:])
    check("doctor：配置来源指向插件生成的 JSON", str(tmp) in text, text[:800])
    check("doctor：认出拓扑 .net", "拓扑" in text and "lab.net" in text, text[:1200])
    check("doctor：认出记忆库三个文件", "cases.md" in text and "gotchas.md" in text, text[:1600])
    check("doctor：probe=false 时明确说明跳过", "跳过设备探测" in text, text[:2000])
    check("doctor：证据与状态目录都报可写/可用", text.count("✅") >= 6, text[:2000])


def test_cfgdiff_list(tmp: Path, cfg_env: dict) -> None:
    text = mcp_call("hcl_cfgdiff", {"action": "list", "name": "SW1"}, cfg_env)
    check("cfgdiff list：空目录时给出可操作提示",
          "没有 .cfg 快照" in text and "snapshot" in text, text[:400])
    text = mcp_call("hcl_cfgdiff", {"action": "bogus"}, cfg_env)
    check("cfgdiff：非法 action 被拒绝", "action 只能是" in text, text[:200])
    text = mcp_call("hcl_cfgdiff", {"action": "snapshot"}, cfg_env)
    check("cfgdiff：snapshot 缺 port 时报错", "需要 port" in text, text[:200])


def test_lab_state(tmp: Path, cfg_env: dict) -> None:
    text = mcp_call("hcl_lab_state", {}, cfg_env)
    check("state get：文件不存在时给出可读提示",
          "还不存在" in text and "lab-state.json" in text, text[:300])

    text = mcp_call("hcl_lab_state", {"action": "set", "data": {"step": 1, "lab": "hcl_2015"}}, cfg_env)
    check("state set：写入成功且是新文件（无备份）",
          "写入 2 个键" in text and "新建，无备份" in text, text[:400])
    state_file = tmp / "state" / "lab-state.json"
    check("state set：文件真的落盘且内容正确",
          state_file.is_file() and json.loads(state_file.read_text(encoding="utf-8"))["step"] == 1,
          str(state_file))

    text = mcp_call("hcl_lab_state", {"action": "merge", "data": {"step": 2}, "note": "第二阶段"}, cfg_env)
    check("state merge：合并且保留原键",
          "合并 1 个键" in text and "已备份到" in text, text[:400])
    data = json.loads(state_file.read_text(encoding="utf-8"))
    check("state merge：内容正确（原键保留 + 新键生效 + _note 写入）",
          data.get("lab") == "hcl_2015" and data.get("step") == 2 and data.get("_note") == "第二阶段",
          json.dumps(data, ensure_ascii=False))

    text = mcp_call("hcl_lab_state", {"action": "get"}, cfg_env)
    check("state get：能读回并打印 JSON", '"lab": "hcl_2015"' in text, text[:400])

    text = mcp_call("hcl_lab_state", {"action": "delete", "key": "step"}, cfg_env)
    check("state delete：删掉指定键", "删除 1 个键" in text and "step" in text, text[:300])
    check("state delete：键确实没了",
          "step" not in json.loads(state_file.read_text(encoding="utf-8")), "")

    text = mcp_call("hcl_lab_state", {"action": "history"}, cfg_env)
    check("state history：列出备份", "历史备份" in text and ".bak-" in text, text[:400])

    text = mcp_call("hcl_lab_state", {"action": "delete"}, cfg_env)
    check("state delete：缺 key 时报错", "需要 key" in text, text[:200])
    text = mcp_call("hcl_lab_state", {"action": "set"}, cfg_env)
    check("state set：缺 data 时报错", "需要 data" in text, text[:200])


def test_memory_write(tmp: Path, cfg_env: dict) -> None:
    cases = tmp / "refs" / "cases.md"
    before = cases.read_text(encoding="utf-8")

    text = mcp_call("hcl_memory_write", {"kind": "case", "title": "新场景",
                                         "body": "**症状**：新的。", "tags": "x, y",
                                         "one_line": "一句话"}, cfg_env)
    check("memory_write：默认 dry_run 只预览不写",
          "预演 dry_run" in text and "C-002" in text, text[:400])
    check("memory_write：dry_run 后文件未被改动", cases.read_text(encoding="utf-8") == before, "")

    text = mcp_call("hcl_memory_write", {"kind": "case", "title": "新场景",
                                         "body": "**症状**：新的。", "tags": "x, y",
                                         "one_line": "一句话", "dry_run": False}, cfg_env)
    check("memory_write：dry_run=false 真正写入",
          "已写入" in text and "备份" in text, text[:400])
    after = cases.read_text(encoding="utf-8")
    check("memory_write：自动分配 C-002 并在索引表插入行",
          "| C-002 |" in after and "| C-001 |" in after, after[:900])
    check("memory_write：正文小节被追加",
          "## C-002 新场景" in after and "**症状**：新的。" in after, after[-400:])
    check("memory_write：写入前留了备份",
          any(p.name.startswith("cases.md.bak-") for p in cases.parent.iterdir()),
          str([p.name for p in cases.parent.iterdir()]))

    text = mcp_call("hcl_memory_write", {"kind": "case", "title": "重复", "body": "x",
                                         "id": "C-002", "dry_run": False}, cfg_env)
    check("memory_write：id 重复被拒绝", "已经有 C-002" in text, text[:300])

    text = mcp_call("hcl_memory_write", {"kind": "gotcha", "title": "新坑", "body": "**原因**：x。",
                                         "id": "A2", "dry_run": False}, cfg_env)
    gotchas = (tmp / "refs" / "gotchas.md").read_text(encoding="utf-8")
    check("memory_write：gotcha 追加到 gotchas.md",
          "已写入" in text and "### A2「新坑」" in gotchas, gotchas[-300:])

    text = mcp_call("hcl_memory_write", {"kind": "case", "title": "缺正文"}, cfg_env)
    check("memory_write：缺 body 时报错", "缺少 body" in text, text[:200])

    # 写回去之后应该能被 hcl_search_memory 搜到（闭环比什么都重要）
    text = mcp_call("hcl_search_memory", {"keywords": ["新场景"]}, cfg_env)
    check("memory_write：写回的内容能被 hcl_search_memory 检索到",
          "C-002" in text and "新场景" in text, text[:500])


def test_report(tmp: Path, cfg_env: dict) -> None:
    out = tmp / "report" / "r.md"
    text = mcp_call("hcl_report", {"title": "测试报告", "sections": "topology,state", "out": str(out)},
                    cfg_env)
    check("report：写出了文件", out.is_file() and "报告已写入" in text, text[:300])
    body = out.read_text(encoding="utf-8") if out.is_file() else ""
    check("report：包含标题与拓扑小节", "# 测试报告" in body and "## 拓扑" in body, body[:400])
    check("report：只包含请求的小节（不含 devices）", "## 设备可达性" not in body, body[:600])
    check("report：links 未提供时给出原因说明",
          "## 链路状态" not in body or "未提供" not in body, "")
    text = mcp_call("hcl_report", {"sections": "nope"}, cfg_env)
    check("report：非法 sections 被拒绝", "不认识的 sections" in text, text[:200])

    out2 = tmp / "report" / "r2.md"
    text = mcp_call("hcl_report", {"sections": "links", "out": str(out2)}, cfg_env)
    body2 = out2.read_text(encoding="utf-8") if out2.is_file() else ""
    check("report：问 links 但没给 links 时说明为什么不换算",
          "未提供 `links`" in body2 and "IRF 成员号" in body2, body2[:600])


def test_snapshot_key() -> None:
    """快照文件名片段：中文名**不能**塌缩成同一个 key。

    实测背景：早先的实现用 `[^0-9A-Za-z_.\\-]+` 把所有非 ASCII 都替换掉再 strip("_")，
    于是"模拟终端"→ 空串 → 回落 `device`。两台中文名设备会共用 `device-*.cfg` 前缀，
    `diff` 不传 `against` 时就会拿**另一台设备**的快照当基线（静默比错）。
    """
    import server as S

    check("snapshot_key：中文名保留（不再塌缩成 device）",
          S._snapshot_key("模拟终端") == "模拟终端", S._snapshot_key("模拟终端"))
    check("snapshot_key：中英混合保留（FTP服务器）",
          S._snapshot_key("FTP服务器") == "FTP服务器", S._snapshot_key("FTP服务器"))
    check("snapshot_key：纯中文/混合名互不相同（不会共用前缀）",
          len({S._snapshot_key("模拟终端"), S._snapshot_key("接入1"),
               S._snapshot_key("交换机"), S._snapshot_key("核心交换机")}) == 4,
          str([S._snapshot_key(n) for n in ("模拟终端", "接入1", "交换机", "核心交换机")]))
    check("snapshot_key：Windows 保留字符被换掉",
          not any(c in S._snapshot_key('a\\b/c:d*e?f"g<h>i|j') for c in '\\/:*?"<>|'),
          S._snapshot_key('a\\b/c:d*e?f"g<h>i|j'))
    check("snapshot_key：空格换成下划线",
          S._snapshot_key("HX1 IRF") == "HX1_IRF", S._snapshot_key("HX1 IRF"))
    check("snapshot_key：空名回落 device", S._snapshot_key("") == "device", S._snapshot_key(""))
    check("snapshot_key：纯符号也回落 device",
          S._snapshot_key("...") == "device", S._snapshot_key("..."))
    check("snapshot_key：Windows 保留名加尾下划线",
          S._snapshot_key("CON") == "CON_" and S._snapshot_key("com1") == "com1_",
          "%r %r" % (S._snapshot_key("CON"), S._snapshot_key("com1")))
    check("snapshot_key：长度截断到 40",
          len(S._snapshot_key("A" * 80)) == 40, str(len(S._snapshot_key("A" * 80))))
    check("snapshot_key：控制字符被换掉",
          "\x01" not in S._snapshot_key("a\x01b"), repr(S._snapshot_key("a\x01b")))


def test_cfgdiff_targets() -> None:
    """批量目标解析：单台 / 批量 / 名字对齐 / 错误输入。"""
    import server as S

    check("targets：单台", S._cfgdiff_targets({"port": 30001}) == [(30001, "port-30001")],
          repr(S._cfgdiff_targets({"port": 30001})))
    check("targets：单台带名字", S._cfgdiff_targets({"port": 30001, "name": "R3"}) == [(30001, "R3")],
          repr(S._cfgdiff_targets({"port": 30001, "name": "R3"})))
    check("targets：批量 + names 一一对应",
          S._cfgdiff_targets({"ports": [30001, 30002], "names": ["A", "B"]})
          == [(30001, "A"), (30002, "B")],
          repr(S._cfgdiff_targets({"ports": [30001, 30002], "names": ["A", "B"]})))
    check("targets：names 短于 ports 时回落 port-<n>",
          S._cfgdiff_targets({"ports": [30001, 30002], "names": ["A"]})
          == [(30001, "A"), (30002, "port-30002")],
          repr(S._cfgdiff_targets({"ports": [30001, 30002], "names": ["A"]})))
    check("targets：中文设备名原样保留",
          S._cfgdiff_targets({"ports": [30015], "names": ["模拟终端"]}) == [(30015, "模拟终端")],
          repr(S._cfgdiff_targets({"ports": [30015], "names": ["模拟终端"]})))
    check("targets：都不给 -> 错误信息", isinstance(S._cfgdiff_targets({}), str),
          repr(S._cfgdiff_targets({})))
    check("targets：非法端口 -> 错误信息",
          isinstance(S._cfgdiff_targets({"ports": ["x"]}), str),
          repr(S._cfgdiff_targets({"ports": ["x"]})))


def test_batch_unreachable(tmp: Path, cfg_env: dict) -> None:
    """批量并发在"全连不上"时也必须逐台报错 + 给出合计（离线可测）。"""
    closed = [30050, 30051]

    text = mcp_call("hcl_cfgdiff", {"action": "snapshot", "ports": closed,
                                    "names": ["X", "Y"], "timeout": 5}, cfg_env)
    check("cfgdiff 批量：每台各自成段",
          "=== X (port 30050) ===" in text and "=== Y (port 30051) ===" in text, text[:400])
    check("cfgdiff 批量：失败逐台可见",
          text.count("❌") == 2, text[:400])
    check("cfgdiff 批量：给出合计与并发线程数",
          "合计 2 台：成功 0，失败 2" in text and "线程并发" in text, text[-300:])

    checklist = tmp / "unreachable.json"
    checklist.write_text(json.dumps({"checks": [
        {"id": "A1", "port": closed[0], "desc": "第一项", "cmd": "display version", "expect": ["x"]},
        {"id": "A2", "port": closed[0], "desc": "第二项", "cmd": "display version", "expect": ["x"]},
        {"id": "B1", "port": closed[1], "desc": "第三项", "cmd": "display version", "expect": ["x"]},
    ]}, ensure_ascii=False), encoding="utf-8")
    text = mcp_call("hcl_verify", {"checklist_json": str(checklist), "workers": 2, "timeout": 5}, cfg_env)
    # ★ 连不上设备 = **没能判定**，不是断言失败。老断言写成 "FAIL 3"，正是本次要修的
    #   "把未判定报成红"的同一个毛病 —— 连不上时既不能算 PASS，也不该算 FAIL。
    check("verify 并发：两台都连不上时，3 项都记「未判定」而不是 FAIL",
          "合计 3 项：PASS 0，FAIL 0（已判定 0）｜未判定 3：超时 0、错误 0、连不上 3、跳过 0" in text
          and "2 台设备连不上" in text, text[:600])
    check("verify 并发：连不上时**不产生真红清单**",
          "断言不成立（真红）" not in text, text[:600])
    check("verify 并发：未判定项逐条列出来（可读）",
          "[!] 未判定 3 项" in text and "UNREACHABLE" in text, text[:900])
    check("verify 并发：输出按端口顺序稳定（30050 在 30051 之前）",
          text.index("30050") < text.index("30051"), text[:500])
    check("verify 并发：接受 workers 参数且不报错",
          "错误" not in text.splitlines()[0] if text.splitlines() else False, text[:200])


def run_py(code: str, env_overrides: dict | None = None) -> subprocess.CompletedProcess:
    """在 server.py 所在目录里跑一段 python（用于猴补内部函数做离线验证）。"""
    env = dict(os.environ)
    env.pop("H3C_MCP_CONFIG", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.update(env_overrides or {})
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, cwd=str(HERE), timeout=180)


def test_cfgdiff_detail_modes(tmp: Path, cfg_env: dict) -> None:
    """detail / grep / 批量摘要 / 截断 —— 项目实战下省 token 的关键行为。

    离线做法：把 `_capture_running_config` 换成假的，基线快照直接写文件。
    一条几千行的配置 diff 直接进上下文能烧掉几万 token，这里逐条钉住"只给该给的"。
    """
    code = (
        "import json, sys\n"
        "sys.path.insert(0, %r)\n"
        "import server as S\n"
        "snap = S._snapshots_dir()\n"
        "base = '\\n'.join('line%%d' %% i for i in range(100))\n"
        "basef = snap / 'TRUNC-20260101-000000.cfg'\n"
        "basef.write_text(base, encoding='utf-8')\n"
        "big = base + '\\n' + '\\n'.join('EXTRA-%%d' %% i for i in range(60)) + '\\n'\n"
        "S._capture_running_config = lambda port, timeout, max_chars: big\n"
        "out = {}\n"
        "out['short'] = S.tool_hcl_cfgdiff({'action':'diff','port':1,'name':'TRUNC','max_lines':3})\n"
        "out['full'] = S.tool_hcl_cfgdiff({'action':'diff','port':1,'name':'TRUNC','max_lines':0})\n"
        "out['summary'] = S.tool_hcl_cfgdiff({'action':'diff','port':1,'name':'TRUNC','detail':'summary'})\n"
        "out['changed'] = S.tool_hcl_cfgdiff({'action':'diff','port':1,'name':'TRUNC','detail':'changed'})\n"
        "out['grepped'] = S.tool_hcl_cfgdiff({'action':'diff','port':1,'name':'TRUNC',"
        "'grep':'EXTRA-1$|EXTRA-2$'})\n"
        # 小 diff：截断提示比省下的行还长 -> 不应截断（净收益门槛）
        "S._capture_running_config = lambda port, timeout, max_chars: base + '\\nA\\nB\\nC\\nD\\nE\\nF\\n'\n"
        "out['tiny'] = S.tool_hcl_cfgdiff({'action':'diff','port':1,'name':'TRUNC','max_lines':3})\n"
        # 批量：默认应只给摘要（否则 13 台 x 200 行没人受得了）
        "(snap / 'A-20260101-000000.cfg').write_text(base, encoding='utf-8')\n"
        "(snap / 'B-20260101-000000.cfg').write_text(base, encoding='utf-8')\n"
        "S._capture_running_config = lambda port, timeout, max_chars: big\n"
        "out['batch'] = S.tool_hcl_cfgdiff({'action':'diff','ports':[1,2],'names':['A','B'],'max_lines':0})\n"
        "out['batch_full'] = S.tool_hcl_cfgdiff({'action':'diff','ports':[1,2],'names':['A','B'],"
        "'detail':'full','max_lines':0})\n"
        "out['bad_detail'] = S.tool_hcl_cfgdiff({'action':'diff','port':1,'name':'TRUNC','detail':'nope'})\n"
        "out['bad_grep'] = S.tool_hcl_cfgdiff({'action':'diff','port':1,'name':'TRUNC','grep':'('})\n"
        "print(json.dumps(out))\n" % str(HERE)
    )
    proc = run_py(code, cfg_env)
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        check("cfgdiff 详情模式：无法取得内部输出", False,
              "rc=%s out=%r err=%s" % (proc.returncode, proc.stdout[-300:], proc.stderr[-500:]))
        return

    short, full = data.get("short", ""), data.get("full", "")
    summary, changed = data.get("summary", ""), data.get("changed", "")
    grepped, tiny = data.get("grepped", ""), data.get("tiny", "")
    batch, batch_full = data.get("batch", ""), data.get("batch_full", "")

    check("cfgdiff 截断：max_lines=3 时给出截断提示", "已截断" in short, short[-300:])
    check("cfgdiff 截断：截断时给出完整 diff 的落盘路径",
          "完整 diff: " in short and ".diff" in short, short[-300:])
    check("cfgdiff 截断：大 diff 截断后确实更短（省 token）",
          len(short) < len(full), "short=%d full=%d" % (len(short), len(full)))
    check("cfgdiff 截断：max_lines=0 时不截断且包含全部新增行",
          "已截断" not in full and "EXTRA-59" in full, full[-200:])
    check("cfgdiff 截断：小 diff 不截断（净收益门槛）",
          "已截断" not in tiny and "F" in tiny, tiny[-200:])

    check("cfgdiff detail=summary：不给任何 diff 行，只给增删行数 + 落盘路径",
          "EXTRA-0" not in summary and "新增 60 行" in summary and ".diff" in summary,
          summary[:400])
    check("cfgdiff detail=summary：返回行数极少（典型 3~4 行）",
          len(summary.splitlines()) <= 5, "lines=%d: %r" % (len(summary.splitlines()), summary[:300]))
    check("cfgdiff detail=changed：只列 +/− 行，不含 hunk 头与上下文",
          "+EXTRA-0" in changed and "@@" not in changed, changed[:300])
    check("cfgdiff detail=changed：改动行数正好等于新增 60 行",
          sum(1 for ln in changed.splitlines() if ln.startswith(("+", "-"))) == 60,
          "count=%d" % sum(1 for ln in changed.splitlines() if ln.startswith(("+", "-"))))

    check("cfgdiff grep：只留下匹配的改动行", "+EXTRA-1" in grepped and "+EXTRA-2" in grepped
          and "+EXTRA-3" not in grepped and "grep=" in grepped, grepped[:400])
    check("cfgdiff grep：说明匹配了几行 / 共几行",
          "匹配 2 / 全部改动 60 行" in grepped, grepped[:400])
    check("cfgdiff grep：改动行只输出匹配的 2 行（60 -> 2）",
          sum(1 for ln in grepped.splitlines() if ln.startswith(("+", "-"))) == 2,
          grepped[:400])

    check("cfgdiff 批量默认：逐台成段 + 摘要（不含 diff 正文）",
          "=== A (port 1) ===" in batch and "=== B (port 2) ===" in batch
          and "+EXTRA-0" not in batch, batch[:400])
    check("cfgdiff 批量默认：列出有改动的设备并告诉你怎么看细节",
          "有改动的设备" in batch and "grep=" in batch and "diff-<设备名>.diff" in batch,
          batch[-400:])
    check("cfgdiff 批量默认：返回行数与设备数同量级（不是 2×60 行）",
          len(batch.splitlines()) < 20, "lines=%d" % len(batch.splitlines()))
    check("cfgdiff 批量 detail=full：确实给了 diff 正文（但长得多）",
          "+EXTRA-0" in batch_full and len(batch_full.splitlines()) > len(batch.splitlines()) * 3,
          "full=%d batch=%d" % (len(batch_full.splitlines()), len(batch.splitlines())))

    check("cfgdiff：非法 detail 被拒绝", "detail 只能是" in data.get("bad_detail", ""),
          data.get("bad_detail", "")[:200])
    check("cfgdiff：非法 grep 正则被拒绝", "grep 不是合法正则" in data.get("bad_grep", ""),
          data.get("bad_grep", "")[:200])
    check("cfgdiff 截断：完整 diff 文件真的落盘且包含全部内容",
          _find_full_diff(cfg_env) is not None, "找不到 diff-TRUNC.diff")


def test_run_truncation(tmp: Path, cfg_env: dict) -> None:
    """`hcl_run_command` 截断**必须落盘**。

    以前是"截断就丢"，所以默认只能给到 8000 字符；项目里 `display current-configuration`
    动辄几万字符，结果就是要么灌爆上下文、要么丢数据。现在默认 4000 + 全文落盘。
    """
    code = (
        "import json, sys\n"
        "sys.path.insert(0, %r)\n"
        "import server as S\n"
        "BIG = 'X' * 20000\n"
        "class FakeCon:\n"
        "    text = '<H3C>'\n"
        "    def command(self, cmd, timeout=20):\n"
        "        return BIG\n"
        "    def close(self):\n"
        "        pass\n"
        "S._connect = lambda port, **kw: FakeCon()\n"
        "text = S.tool_hcl_run_command({'port': 1, 'command': 'display current-configuration'})\n"
        "print(json.dumps({'chars': len(text), 'tail': text[-600:], 'body': text[:120]}))\n" % str(HERE)
    )
    proc = run_py(code, cfg_env)
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        check("run 截断：无法取得内部输出", False,
              "rc=%s out=%r err=%s" % (proc.returncode, proc.stdout[-300:], proc.stderr[-500:]))
        return

    tail = data.get("tail", "")
    check("run 截断：返回体被压到默认上限附近（不再整段灌上下文）",
          data.get("chars", 0) < 6000, "chars=%s" % data.get("chars"))
    check("run 截断：明确标注截断与原始长度",
          "已截断" in tail and "20000" in tail, tail[-300:])
    check("run 截断：给出完整回显的落盘路径", "run-1.txt" in tail, tail[-300:])
    check("run 截断：提示用 Comware 自带管道缩小范围（项目里最省的做法）",
          "| include" in tail, tail[-300:])
    # 落盘文件必须真的有全文
    cfg = json.loads(Path(cfg_env["H3C_MCP_CONFIG"]).read_text(encoding="utf-8-sig"))
    hits = sorted(Path(cfg["evidence_root"]).rglob("run-1.txt"), key=lambda p: p.stat().st_mtime)
    check("run 截断：落盘文件存在且是完整 20000 字符",
          bool(hits) and len(hits[-1].read_text(encoding="utf-8")) == 20000,
          "hits=%d" % len(hits))


def test_apply_plan_dry_run(tmp: Path, cfg_env: dict) -> None:
    """dry-run 默认**不回显整份计划**，改为预检（项目实战里这是最大的纯浪费）。

    计划文件本来就是你写的——13 台 × 几十条命令每预演一次就重打一遍。
    预检给出的才是有信息量的东西：逐台条数、破坏性命令、自动应答命中、未给 port 的设备。
    """
    plan = {"devices": [
        {"name": "SW1", "port": 30001,
         "commands": ["sysname SW1"] + ["vlan %d" % i for i in range(1, 40)]},
        {"name": "SW2", "port": 30002, "commands": ["save force", "reboot force"],
         "verify": ["display vlan 1"]},
        {"name": "SW3", "commands": ["vlan 10"]},
    ]}
    f = tmp / "plan-dry.json"
    f.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    text = mcp_call("hcl_apply_plan", {"plan_json": str(f), "dry_run": True}, cfg_env)
    check("dry-run：默认**不**回显整份计划（只有抽样）",
          "vlan 20" not in text and "vlan 1" in text, text[:400])
    check("dry-run：给出逐台条数",
          "SW1 (port=30001) 下发 40 条 / 验证 0 条" in text, text[:400])
    check("dry-run：给出总计（命令/验证/设备）",
          "合计下发 43 条命令、1 条验证命令（3 台设备）" in text, text[:600])
    check("dry-run：标出破坏性命令（会被拒绝）",
          "破坏性" in text and "reboot force" in text, text[:800])
    check("dry-run：标出会被自动答 Y 的命令",
          "自动应答 Y" in text and "save force" in text, text[:800])
    check("dry-run：标出未给 port 的设备",
          "未给 port" in text and "SW3" in text, text[:800])
    check("dry-run：提示 echo_commands=true 可看全部",
          "echo_commands=true" in text, text[-300:])

    echoed = mcp_call("hcl_apply_plan",
                      {"plan_json": str(f), "dry_run": True, "echo_commands": True}, cfg_env)
    check("dry-run：echo_commands=true 时回显全部命令",
          "vlan 20" in echoed and "vlan 39" in echoed, echoed[:400])
    check("dry-run：默认只抽样（≤3 条/台），全量回显才是全部 40 条 vlan",
          text.count("    vlan ") <= 3 and echoed.count("    vlan ") == 40,
          "default=%d echoed=%d" % (text.count("    vlan "), echoed.count("    vlan ")))
    check("dry-run：全量回显确实比默认长（这就是默认省下的量）",
          len(echoed) > len(text), "echoed=%d default=%d" % (len(echoed), len(text)))


def _find_full_diff(cfg_env: dict) -> Path | None:
    """在证据目录里找 diff-TRUNC.diff。"""
    cfg = json.loads(Path(cfg_env["H3C_MCP_CONFIG"]).read_text(encoding="utf-8-sig"))
    root = Path(cfg.get("evidence_root") or "")
    if not root.is_dir():
        return None
    hits = sorted(root.rglob("diff-TRUNC.diff"), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


def test_allow_destructive_wiring(tmp: Path, cfg_env: dict) -> None:
    """`allow_destructive` / `verify_changed` 必须真的从工具参数传到执行层。

    `allow_destructive` 传错方向就是真重启设备；`verify_changed` 传错方向是回读校验
    静默不做（工具继续报"0 报错"）。这里把 `_apply_one_device` 换掉，只在进程内看实参，
    **不碰任何设备**。
    """
    plan = tmp / "destructive.json"
    plan.write_text(json.dumps({"devices": [
        {"name": "SW1", "port": 30008, "commands": ["reboot force"]}]},
        ensure_ascii=False), encoding="utf-8")
    code = (
        "import json, sys\n"
        "sys.path.insert(0, %r)\n"
        "import server as S\n"
        "seen = {}\n"
        "def fake(dev, apply=False, save=False, timeout=60.0, out_dir=None,\n"
        "         allow_destructive=False, verify_changed=False):\n"
        "    seen['allow'] = allow_destructive\n"
        "    seen['verify'] = verify_changed\n"
        "    return {'name': dev.get('name','?'), 'applied': 0, 'errors': [], 'warnings': [],\n"
        "            'verify_ok': 0, 'evidence': None, 'destructive': [], 'change_report': []}\n"
        "S._apply_one_device = fake\n"
        "plan = %r\n"
        "S.tool_hcl_apply_plan({'plan_json': plan, 'dry_run': False})\n"
        "print(json.dumps(seen))\n"
        "S.tool_hcl_apply_plan({'plan_json': plan, 'dry_run': False, 'allow_destructive': True})\n"
        "print(json.dumps(seen))\n"
        "S.tool_hcl_apply_plan({'plan_json': plan, 'dry_run': False, 'allow_destructive': False})\n"
        "print(json.dumps(seen))\n"
        "out = S.tool_hcl_apply_plan({'plan_json': plan, 'dry_run': False, 'verify_changed': True})\n"
        "print(json.dumps(seen))\n"
        "print(json.dumps({'precheck_in_real': '== ' in out and '\\u9884\\u68c0' in out}))\n"
        % (str(HERE), str(plan))
    )
    proc = run_py(code, cfg_env)
    lines = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    if len(lines) < 5:
        check("allow_destructive 透传：无法取得内部输出（%r）"
              % (proc.stdout[-200:] + proc.stderr[-300:]), False)
        return
    d0, d1, d2, d3, d4 = (json.loads(l) for l in lines[:5])
    check("allow_destructive 默认不传 -> 执行层收到 False（真机上就是拒绝 reboot）",
          d0.get("allow") is False, repr(d0))
    check("allow_destructive=true -> 执行层收到 True", d1.get("allow") is True, repr(d1))
    check("allow_destructive=false -> 执行层收到 False", d2.get("allow") is False, repr(d2))
    check("verify_changed 默认不传 -> 执行层收到 False（不做回读，不额外读配置）",
          d0.get("verify") is False, repr(d0))
    check("verify_changed=true -> 执行层收到 True", d3.get("verify") is True, repr(d3))
    check("真实下发也会打预检段", d4.get("precheck_in_real") is True, repr(d4))


def test_apply_plan_precheck(tmp: Path, cfg_env: dict) -> None:
    """dry-run 的 `预检` 段：5 类本次真金白银踩过的坑都要在预演里就报出来。"""
    plan = {"devices": [
        {"name": "SW1", "port": 30001, "commands": [
            "vlan 301",
            "vlan 301",                                  # 1a 同一条命令重复
            "interface GigabitEthernet1/0/1",
            "interface GigabitEthernet1/0/1",            # 1b 同一接口反复进
            "undo port access vlan 301",                 # 4 后缀不收参数
            "ip address 10.0.31.1 30",                   # 5 同网段双 IP
            "ospf 1",
            "ospf 2",                                    # 3 多个 OSPF 进程号
        ]},
        {"name": "SW2", "port": 30002, "commands": ["ip address 10.0.31.2 30"]},
        {"name": "SW3", "port": 30003, "commands": ["reboot force"]},   # 2 reboot 前没 save
    ]}
    f = tmp / "plan-precheck.json"
    f.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    text = mcp_call("hcl_apply_plan", {"plan_json": str(f), "dry_run": True}, cfg_env)
    check("dry-run：有独立的「预检」段", "== 预检" in text, text[-1000:])
    check("dry-run 预检：重复命令", "重复" in text and "vlan 301" in text, text[-1200:])
    check("dry-run 预检：同一接口反复进入", "反复进入" in text, text[-1200:])
    check("dry-run 预检：reboot 之前没有 save（IRF 死循环 40 分钟的根因）",
          "之前没有 `save`" in text and "reboot force" in text, text[-1200:])
    check("dry-run 预检：多个 OSPF 进程号", "OSPF 进程号" in text, text[-1200:])
    check("dry-run 预检：后缀不收参数黑名单 + Too many parameters",
          "后缀不收参数" in text and "Too many parameters" in text, text[-1200:])
    check("dry-run 预检：同网段双 IP", "同一网段 10.0.31.0/30" in text, text[-1200:])
    check("dry-run 预检：预演依旧不改设备、破坏性仍被拒绝",
          "不会碰设备" in text and "破坏性" in text, text[:800])

    clean = {"devices": [{"name": "SW9", "port": 30009, "commands": [
        "save force", "reboot force", "ospf 1", "vlan 10", "ip address 10.9.9.2 30"]}]}
    cf = tmp / "plan-precheck-clean.json"
    cf.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
    text2 = mcp_call("hcl_apply_plan", {"plan_json": str(cf), "dry_run": True}, cfg_env)
    check("dry-run 预检：干净计划给 ✅（不制造假警报）", "✅ 未发现" in text2, text2[-1000:])
    check("dry-run 预检：verify_changed 默认不宣传回读（省 token）",
          "verify_changed" not in text2, text2[-400:])

    text3 = mcp_call("hcl_apply_plan",
                     {"plan_json": str(f), "dry_run": True, "verify_changed": True}, cfg_env)
    check("dry-run：verify_changed=true 时说明回读校验会做什么",
          "verify_changed=true" in text3 and "current-configuration" in text3, text3[-1200:])


def test_verify_log_after_prompt(tmp: Path, cfg_env: dict) -> None:
    """★ 端到端复现 2026-10-15 那次"假红"：异步日志把提示符挤到非行尾。

    真实现场（20 台设备，63 条断言，跑 4 次共 11 条假红）：
    设备把 `%Oct 15 ... PING/6/...` 这类**异步日志**紧贴在提示符后面吐出来，
    老 `_PROMPT` 是行尾锚定的（`[>\\]]\\s*$`），于是**永远匹配不上** —— 回显早就
    到齐了却干等到 timeout，然后被当成"断言不成立"报成 FAIL。

    这里用一个真 socket 假设备把那个字节序列**原样重放**，验证：
      A. 粘日志的提示符仍能读通 -> 断言 PASS（不再假红）
      B. 真超时的命令 -> 记 TIMEOUT（未判定），**不记 FAIL**
      C. 真不成立的断言 -> 记 FAIL（真红没被吞掉）
    """
    import socket
    import threading

    port = 39177
    log_line = ("%Oct 15 05:54:48:210 2026 BR-PE PING/6/PING_VPN_STATISTICS: "
                "Ping statistics for 172.16.100.11 in VPN instance vpn-office: "
                "5 packet(s) transmitted, 0 packet(s) received, 100.0% packet loss.")
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                srv.settimeout(0.3)
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=session, args=(conn,), daemon=True).start()

    def session(conn) -> None:                            # noqa: ANN001
        conn.settimeout(30.0)
        try:
            conn.sendall(b"\r\nPress ENTER to get started.\r\n<BR-PE>")
            buf = b""
            while not stop.is_set():
                try:
                    data = conn.recv(4096)
                except (socket.timeout, OSError):
                    return
                if not data:
                    return
                buf += data
                while b"\r" in buf or b"\n" in buf:
                    idx = min([i for i in (buf.find(b"\r"), buf.find(b"\n")) if i >= 0])
                    raw, buf = buf[:idx], buf[idx + 1:]
                    if buf.startswith(b"\n"):
                        buf = buf[1:]
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        conn.sendall(b"\r\n<BR-PE>")
                        continue
                    if line == "screen-length disable":
                        conn.sendall(b"%s\r\n<BR-PE>" % line.encode())
                        continue
                    if line.startswith("display ospf peer"):
                        # ★ 核心：提示符 + 异步日志**同一个分片**吐出来
                        conn.sendall((
                            "%s\r\n"
                            "         OSPF Process 1 with Router ID 1.1.10.1\r\n"
                            " Router ID       Address         Pri Dead-Time  State\r\n"
                            " 1.1.10.10       10.0.1.1        1   32         Full/BDR\r\n"
                            "<BR-PE>%s\r\n" % (line, log_line)).encode())
                        continue
                    if line.startswith("display vrrp"):
                        # ★ 另一种长相：提示符与日志**分两行**
                        conn.sendall((
                            "%s\r\nGE0/0.110  110  Master\r\n<BR-PE>\r\n%s\r\n"
                            % (line, log_line)).encode())
                        continue
                    if line.startswith("display never-answers"):
                        continue                              # 什么都不回 -> 真超时
                    if line.startswith("display wrong-value"):
                        conn.sendall(("%s\r\nVALUE-IS-X\r\n<BR-PE>\r\n"
                                      % line).encode())
                        continue
                    conn.sendall(("%s\r\nok\r\n<BR-PE>" % line).encode())
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    threading.Thread(target=serve, daemon=True).start()
    try:
        checklist = tmp / "lograce.json"
        checklist.write_text(json.dumps({"checks": [
            {"id": "R1", "port": port, "desc": "粘日志的提示符仍读通",
             "cmd": "display ospf peer", "expect": ["Full/BDR"]},
            {"id": "R2", "port": port, "desc": "分两行的日志也读通",
             "cmd": "display vrrp", "expect": ["110\\s+Master"]},
            {"id": "R3", "port": port, "desc": "真超时（设备不回）",
             "cmd": "display never-answers", "expect": ["x"], "timeout": 2},
            {"id": "R4", "port": port, "desc": "真不成立（值不对）",
             "cmd": "display wrong-value", "expect": ["EXPECTED-TOKEN"]},
        ]}, ensure_ascii=False), encoding="utf-8")
        text = mcp_call("hcl_verify",
                        {"checklist_json": str(checklist), "workers": 1,
                         "timeout": 2, "attempts": 1}, cfg_env)

        check("假红修复：提示符后粘异步日志的命令仍 PASS（R1）",
              "PASS" in text and "R1" in text
              and not any(ln.startswith("TIMEOUT") and "R1" in ln for ln in text.splitlines()),
              text[:900])
        check("假红修复：提示符与日志分两行时也 PASS（R2）",
              not any(ln.startswith(("TIMEOUT", "FAIL")) and "R2" in ln
                      for ln in text.splitlines()), text[:900])
        check("假红修复：真超时记 TIMEOUT，**不记 FAIL**（R3）",
              any(ln.startswith("TIMEOUT") and "R3" in ln for ln in text.splitlines())
              and not any(ln.startswith("FAIL") and "R3" in ln for ln in text.splitlines()),
              text[:1200])
        check("假红修复：真不成立的断言仍是 FAIL（R4，真红没被吞）",
              any(ln.startswith("FAIL") and "R4" in ln for ln in text.splitlines()),
              text[:1200])
        check("假红修复：合计行把已判定/未判定分开计数",
              "合计 4 项：PASS 2，FAIL 1（已判定 3）｜未判定 1：超时 1、错误 0、"
              "连不上 0、跳过 0" in text, text[-900:])
        check("假红修复：真红单独列出来，且只列 R4",
              "断言不成立（真红）：R4" in text, text[-900:])
        check("假红修复：未判定项单独列出并说明**不是断言失败**",
              "[!] 未判定 1 项" in text and "不是断言失败" in text
              and "TIMEOUT" in text, text[-900:])
    finally:
        stop.set()
        try:
            srv.close()
        except Exception:
            pass


def test_verify_attempts_retry(tmp: Path, cfg_env: dict) -> None:
    """未判定时的重试：第一次超时、第二次就通的设备，重试后应 PASS。

    ★ 实测依据：`A18-VPN-DCPE-TO-BRANCH` 单跑第一次超时、重试才通。
      但重试**必须如实标出**（"（重试 2 次）"），不能悄悄变成 PASS。
    """
    import socket
    import threading

    port = 39178
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    stop = threading.Event()
    seen: list[str] = []

    def serve() -> None:
        while not stop.is_set():
            try:
                srv.settimeout(0.3)
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=session, args=(conn,), daemon=True).start()

    def session(conn) -> None:                            # noqa: ANN001
        conn.settimeout(30.0)
        try:
            conn.sendall(b"\r\n<SW1>")
            buf = b""
            while not stop.is_set():
                try:
                    data = conn.recv(4096)
                except (socket.timeout, OSError):
                    return
                if not data:
                    return
                buf += data
                while b"\r" in buf or b"\n" in buf:
                    idx = min([i for i in (buf.find(b"\r"), buf.find(b"\n")) if i >= 0])
                    raw, buf = buf[:idx], buf[idx + 1:]
                    if buf.startswith(b"\n"):
                        buf = buf[1:]
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        conn.sendall(b"\r\n<SW1>")
                        continue
                    if line.startswith("screen-length disable"):
                        conn.sendall(b"%s\r\n<SW1>" % line.encode())
                        continue
                    if line.startswith("display flaky"):
                        seen.append(line)
                        if len(seen) == 1:
                            continue          # 第一次：什么都不回 -> 超时
                        conn.sendall(("%s\r\nFLAKY-OK\r\n<SW1>" % line).encode())
                        continue
                    conn.sendall(("%s\r\nok\r\n<SW1>" % line).encode())
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    threading.Thread(target=serve, daemon=True).start()
    try:
        checklist = tmp / "flaky.json"
        checklist.write_text(json.dumps({"checks": [
            {"id": "F1", "port": port, "desc": "第一次超时、重试通过",
             "cmd": "display flaky", "expect": ["FLAKY-OK"], "timeout": 2},
        ]}, ensure_ascii=False), encoding="utf-8")
        text = mcp_call("hcl_verify",
                        {"checklist_json": str(checklist), "workers": 1,
                         "timeout": 2, "attempts": 2}, cfg_env)
        check("重试：第一次超时、第二次通过 -> 最终 PASS",
              any(ln.startswith("PASS") and "F1" in ln for ln in text.splitlines()),
              text[:900])
        check("重试：如实标出「重试」而不是悄悄 PASS",
              "重试 2 次" in text, text[:900])
        check("重试：真的重发过命令（设备侧看到 2 次）", len(seen) == 2, repr(seen))
        check("重试：合计里没有未判定",
              "未判定 0" in text, text[-600:])

        # attempts=1 -> 不重试，必须如实记 TIMEOUT
        seen.clear()
        text1 = mcp_call("hcl_verify",
                         {"checklist_json": str(checklist), "workers": 1,
                          "timeout": 2, "attempts": 1}, cfg_env)
        check("重试：attempts=1 时不重试，如实记 TIMEOUT（不粉饰成 PASS）",
              any(ln.startswith("TIMEOUT") and "F1" in ln for ln in text1.splitlines())
              and len(seen) == 1, text1[:900] + repr(seen))
    finally:
        stop.set()
        try:
            srv.close()
        except Exception:
            pass


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="h3clab-labtools-"))
    try:
        refs = write_memory_fixture(tmp / "refs")
        net = tmp / "lab.net"
        net.write_text("[[S5820V2 SW1]]\ndevice_id = 1\n", encoding="utf-8")
        cfg = {
            "state_dir": str(tmp / "state"),
            "evidence_root": str(tmp / "evidence"),
            "references_dir": str(refs),
            "net_file": str(net),
            "ports": [30001, 30002],
        }
        cfg_path = tmp / "server-config.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        cfg_env = {"H3C_MCP_CONFIG": str(cfg_path), "DSH_HOME": str(tmp / "dsh")}

        test_doctor(tmp, cfg_env)
        test_cfgdiff_list(tmp, cfg_env)
        test_snapshot_key()
        test_cfgdiff_targets()
        test_batch_unreachable(tmp, cfg_env)
        test_verify_log_after_prompt(tmp, cfg_env)
        test_verify_attempts_retry(tmp, cfg_env)
        test_cfgdiff_detail_modes(tmp, cfg_env)
        test_run_truncation(tmp, cfg_env)
        test_apply_plan_dry_run(tmp, cfg_env)
        test_apply_plan_precheck(tmp, cfg_env)
        test_allow_destructive_wiring(tmp, cfg_env)
        test_lab_state(tmp, cfg_env)
        test_memory_write(tmp, cfg_env)
        test_report(tmp, cfg_env)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("-" * 62)
    print("总计：%d 通过 / %d 失败" % (passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
