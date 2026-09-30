<#
真实 MCP 服务器（scripts/server.py）协议冒烟探针（Windows / PowerShell）。

为什么需要它：DSH 的 Windows 受限沙箱禁止被托管进程创建命名管道，Node 的
`spawn(..., { stdio: ['pipe','pipe','pipe'] })` 会抛 EPERM，因此 `node selftest.mjs`
在该环境里走内存流回退。而 PowerShell 自己的管道不受该限制，所以可以用
`... | python server.py` 直接验证真实服务器的线协议。

验证内容：
  1. 一行一个 JSON（换行分隔）分帧；
  2. initialize 握手结果与 dsh-h3clab 客户端一致（protocolVersion=2024-11-05）；
  3. notifications/initialized 不产生响应；
  4. tools/list 返回 8 个工具；
  5. 只读工具 hcl_topology / hcl_search_memory 的真实输出；
  6. 未启动 HCL 时 hcl_list_devices 给出可读结果（而非崩溃）。

用法（在包根目录）：
  pwsh -File scripts/probe-server.ps1
  pwsh -File scripts/probe-server.ps1 -NetFile D:\path\to\lab.net -Python python
#>
param(
  [string]$Server = (Join-Path $PSScriptRoot 'server.py'),
  [string]$NetFile = (Join-Path $PSScriptRoot '..\test\fixtures\lab.net'),
  [string]$Python = 'python'
)

$ErrorActionPreference = 'Continue'
# 与 lib/index.js 给子进程的环境一致：强制 UTF-8，避免中文日志在 cp936 下乱码。
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

if (-not (Test-Path $Server)) {
  Write-Output "找不到服务器脚本：$Server（先运行 node scripts/sync-server.mjs）"
  exit 1
}
$NetFileJson = ConvertTo-Json $NetFile -Compress

$requests = @(
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"dsh-h3clab-probe","version":"0.1.0"}}}',
  '{"jsonrpc":"2.0","method":"notifications/initialized"}',
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}',
  # 括号不能省：PowerShell 里逗号运算符优先级高于 +，否则会拼出非法 JSON。
  ('{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"hcl_topology","arguments":{"net_file":' + $NetFileJson + '}}}'),
  '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"hcl_search_memory","arguments":{"keywords":["console"],"any":true,"max":1}}}',
  '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"hcl_list_devices","arguments":{}}}'
)

# stderr 丢弃（服务器把日志写 stderr，客户端会转发到 DSH 日志）。
$lines = $requests | & $Python $Server 2>$null

Write-Output "服务器：$Server"
Write-Output "响应行数：$($lines.Count)（6 个请求，其中 1 个是通知，应得 5 行）"
foreach ($line in $lines) {
  if ($null -eq $line -or "$line".Trim() -eq '') { continue }
  $obj = $null
  try { $obj = "$line" | ConvertFrom-Json } catch { Write-Output "非 JSON 行：$line"; continue }
  $summary = ''
  if ($obj.result -and $obj.result.tools) {
    $summary = "tools=" + (($obj.result.tools | ForEach-Object { $_.name }) -join ',')
  }
  elseif ($obj.result -and $obj.result.content) {
    $text = ($obj.result.content | Where-Object { $_.type -eq 'text' } | ForEach-Object { $_.text }) -join "`n"
    $first = ($text -split "`n")[0]
    $summary = "isError=$($obj.result.isError) 首行=$first"
  }
  elseif ($obj.result -and $obj.result.protocolVersion) {
    $summary = "serverInfo=$($obj.result.serverInfo.name) protocolVersion=$($obj.result.protocolVersion)"
  }
  else {
    $summary = ($line -replace '\s+', ' ').Substring(0, [Math]::Min(120, "$line".Length))
  }
  Write-Output ("id={0}  {1}" -f $obj.id, $summary)
}

if ($lines.Count -eq 5) { exit 0 } else { exit 1 }
