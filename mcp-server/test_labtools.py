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
