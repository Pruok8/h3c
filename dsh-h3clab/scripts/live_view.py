#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""live_view.py —— 实时看 agent 在设备上干了什么。

为什么单独做一个网页：DSH 客户端插件的面板要改 `lib/client.js`，而客户端 bundle 只在
`pnpm run dev:web` 跑着的时候才热更，否则要重启 DSH。这个页面**不需要重启任何东西**：
它直接 tail 两个数据源——

  1. `<state_dir>/activity.jsonl` —— MCP 服务器为每次工具调用追加的一行流水；
  2. `<evidence_root>/<时间戳>/<设备>.txt` —— 每次下发的逐条命令 + 回显。

只用标准库（http.server），不装任何东西。用法：

  python live_view.py [--port 8899] [--state DIR] [--evidence DIR]

然后浏览器打开 http://127.0.0.1:8899/
"""
from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_HOME = Path(os.environ.get("USERPROFILE") or Path.home()) / ".dsh" / "h3clab"

STATE = DEFAULT_HOME
EVIDENCE = DEFAULT_HOME / "evidence"

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>H3CLab · 实时操作</title>
<style>
 :root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#dfe3ea;--dim:#8b93a3;
       --ok:#48d597;--warn:#e2b34a;--err:#f06a6a;--acc:#5aa9f0}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:13px/1.5 "Cascadia Mono",Consolas,"Microsoft YaHei",monospace}
 header{padding:10px 14px;border-bottom:1px solid var(--line);display:flex;
        align-items:center;gap:14px;background:var(--panel);position:sticky;top:0;z-index:5}
 header h1{font-size:14px;margin:0;font-weight:600}
 .dot{width:8px;height:8px;border-radius:50%;background:var(--ok);
      box-shadow:0 0 8px var(--ok);animation:p 1.4s infinite}
 @keyframes p{50%{opacity:.35}}
 .meta{color:var(--dim);font-size:12px;margin-left:auto;white-space:nowrap}
 main{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:12px;height:calc(100vh - 92px)}
 section{background:var(--panel);border:1px solid var(--line);border-radius:8px;
         display:flex;flex-direction:column;min-height:0;overflow:hidden}
 section h2{font-size:12px;margin:0;padding:8px 12px;color:var(--dim);
            border-bottom:1px solid var(--line);font-weight:600;letter-spacing:.04em}
 .body{overflow:auto;padding:8px 12px;flex:1;min-height:0}
 .row{padding:3px 0;border-bottom:1px dashed #1e222b;white-space:pre-wrap;word-break:break-all}
 .t{color:var(--dim)}
 .tool{color:var(--acc);font-weight:600}
 .ok{color:var(--ok)} .err{color:var(--err)} .warn{color:var(--warn)}
 .head{color:#a9b3c4}
 pre{margin:0;white-space:pre-wrap;word-break:break-all;font-size:12.5px}
 .cmd{color:var(--acc)}
 @media(max-width:900px){main{grid-template-columns:1fr;height:auto}}
</style></head><body>
<header>
  <span class="dot"></span><h1>H3CLab · 实时操作</h1>
  <span class="meta" id="meta">连接中…</span>
</header>
<main>
  <section><h2>操作流水（每次工具调用）</h2><div class="body" id="feed"></div></section>
  <section><h2 id="evname">最新设备回显</h2><div class="body"><pre id="ev"></pre></div></section>
</main>
<script>
const esc = s => (s??'').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function renderFeed(rows){
  const el = document.getElementById('feed');
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
  el.innerHTML = rows.map(r => {
    const t = `<span class="t">${esc(r.ts||'').slice(11)}</span>`;
    if (r.kind === 'call')
      return `<div class="row">${t} <span class="tool">▶ ${esc(r.tool)}</span> <span class="head">${esc(r.args||'')}</span></div>`;
    if (r.kind === 'done')
      return `<div class="row">${t} <span class="ok">✔ ${esc(r.tool)}</span> <span class="t">${esc(r.seconds)}s / ${esc(r.chars)} 字符</span>\n     <span class="head">${esc(r.head||'')}</span></div>`;
    if (r.kind === 'error')
      return `<div class="row">${t} <span class="err">✘ ${esc(r.tool)}</span> <span class="err">${esc(r.error||'')}</span></div>`;
    return `<div class="row">${t} ${esc(r.text||r.kind||'')}</div>`;
  }).join('');
  if (atBottom) el.scrollTop = el.scrollHeight;
}
function renderEvidence(d){
  const el = document.getElementById('ev');
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
  document.getElementById('evname').textContent = d.file ? ('最新设备回显 · ' + d.file) : '最新设备回显（暂无）';
  el.innerHTML = (d.lines||[]).map(l => {
    if (l.startsWith('$ ')) return `<span class="cmd">${esc(l)}</span>`;
    if (/% Unrecognized|% Wrong|% Too many/.test(l)) return `<span class="err">${esc(l)}</span>`;
    return esc(l);
  }).join('\n');
  if (atBottom) el.scrollTop = el.scrollHeight;
}
async function tick(){
  try{
    const r = await fetch('/api/feed?n=400', {cache:'no-store'});
    const d = await r.json();
    renderFeed(d.activity || []);
    renderEvidence(d.evidence || {});
    document.getElementById('meta').textContent =
      `${new Date().toLocaleTimeString()} · 流水 ${ (d.activity||[]).length } 条 · ` +
      `${(d.evidence&&d.evidence.file)||'—'}`;
  }catch(e){
    document.getElementById('meta').textContent = '连接失败：' + e;
  }
}
tick(); setInterval(tick, 1000);
</script></body></html>
"""


def _read_activity(n: int) -> list[dict]:
    path = STATE / "activity.jsonl"
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"kind": "raw", "text": line})
    return out


def _newest_evidence(max_lines: int = 400) -> dict:
    if not EVIDENCE.is_dir():
        return {}
    files = [p for p in EVIDENCE.rglob("*.txt") if p.is_file()]
    if not files:
        return {}
    newest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        lines = newest.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    return {"file": "%s / %s" % (newest.parent.name, newest.name),
            "mtime": time.strftime("%H:%M:%S", time.localtime(newest.stat().st_mtime)),
            "lines": lines[-max_lines:]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):        # 别把访问日志打到控制台
        pass

    def do_GET(self):                     # noqa: N802
        if self.path.startswith("/api/feed"):
            n = 400
            if "n=" in self.path:
                try:
                    n = max(1, min(2000, int(self.path.split("n=")[1].split("&")[0])))
                except ValueError:
                    pass
            body = json.dumps({"activity": _read_activity(n),
                               "evidence": _newest_evidence()},
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    global STATE, EVIDENCE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--state", default=str(DEFAULT_HOME))
    ap.add_argument("--evidence", default="")
    args = ap.parse_args()
    STATE = Path(args.state)
    EVIDENCE = Path(args.evidence) if args.evidence else STATE / "evidence"

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("实时操作窗口: http://127.0.0.1:%d/" % args.port)
    print("  流水  : %s" % (STATE / "activity.jsonl"))
    print("  回显  : %s" % EVIDENCE)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
