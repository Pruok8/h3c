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
    check("verify 并发：两台都连不上时合计正确",
          "合计 3 项：PASS 0，FAIL 3" in text and "2 台连不上" in text, text[:500])
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


def test_cfgdiff_truncation(tmp: Path, cfg_env: dict) -> None:
    """max_lines 截断 + 完整 diff 落盘（离线：把取配置的函数换成假的）。

    这是**省 token 的关键行为**：一份几千行的配置 diff 直接进上下文能烧掉几万 token。
    """
    code = (
        "import json, sys\n"
        "sys.path.insert(0, %r)\n"
        "import server as S\n"
        "snap = S._snapshots_dir()\n"
        "base = snap / 'TRUNC-20260101-000000.cfg'\n"
        "base.write_text('\\n'.join('line%%d' %% i for i in range(100)), encoding='utf-8')\n"
        "big = '\\n'.join('line%%d' %% i for i in range(100)) + '\\n' + "
        "'\\n'.join('EXTRA-%%d' %% i for i in range(60)) + '\\n'\n"
        "S._capture_running_config = lambda port, timeout, max_chars: big\n"
        "short = S.tool_hcl_cfgdiff({'action':'diff','port':1,'name':'TRUNC','max_lines':3})\n"
        "full = S.tool_hcl_cfgdiff({'action':'diff','port':1,'name':'TRUNC','max_lines':0})\n"
        # 小 diff：截断提示比省下的行还长 -> 不应截断（净收益门槛）
        "S._capture_running_config = lambda port, timeout, max_chars: "
        "'\\n'.join('line%%d' %% i for i in range(100)) + '\\nA\\nB\\nC\\nD\\nE\\nF\\n'\n"
        "tiny = S.tool_hcl_cfgdiff({'action':'diff','port':1,'name':'TRUNC','max_lines':3})\n"
        "print(json.dumps({'short': short, 'full': full, 'tiny': tiny}))\n" % str(HERE)
    )
    proc = run_py(code, cfg_env)
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        check("cfgdiff 截断：无法取得内部输出", False,
              "rc=%s out=%r err=%s" % (proc.returncode, proc.stdout[-300:], proc.stderr[-500:]))
        return

    short, full, tiny = data.get("short", ""), data.get("full", ""), data.get("tiny", "")
    check("cfgdiff 截断：max_lines=3 时给出截断提示", "diff 已截断" in short, short[-400:])
    check("cfgdiff 截断：截断时给出完整 diff 的落盘路径",
          "完整内容见" in short and ".diff" in short, short[-400:])
    check("cfgdiff 截断：大 diff 截断后确实更短（省 token）",
          len(short) < len(full), "short=%d full=%d" % (len(short), len(full)))
    check("cfgdiff 截断：max_lines=0 时不截断且包含全部新增行",
          "diff 已截断" not in full and "EXTRA-59" in full, full[-300:])
    check("cfgdiff 截断：小 diff 不截断（净收益门槛，截断提示比省下的还长）",
          "diff 已截断" not in tiny and "F" in tiny, tiny[-300:])
    check("cfgdiff 截断：完整 diff 文件真的落盘且包含全部内容",
          _find_full_diff(cfg_env) is not None, "找不到 diff-TRUNC.diff")


def _find_full_diff(cfg_env: dict) -> Path | None:
    """在证据目录里找 diff-TRUNC.diff。"""
    cfg = json.loads(Path(cfg_env["H3C_MCP_CONFIG"]).read_text(encoding="utf-8-sig"))
    root = Path(cfg.get("evidence_root") or "")
    if not root.is_dir():
        return None
    hits = sorted(root.rglob("diff-TRUNC.diff"), key=lambda p: p.stat().st_mtime)
    return hits[-1] if hits else None


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
        test_cfgdiff_truncation(tmp, cfg_env)
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
