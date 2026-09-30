#!/usr/bin/env node
/**
 * 把并行开发的 MCP 服务器（D:\DSH\NET\h3c-lab-mcp）同步/打包进本插件的
 * scripts/ 目录，使 dsh-h3clab 自带一份可运行的服务器副本。
 *
 * 复制内容：
 *   - server.py            必需
 *   - server.py 里 import 到的同目录本地模块（例如 hcldrv.py）
 * 跳过 __pycache__、*.bak、测试脚本等。
 *
 * 用法：
 *   node scripts/sync-server.mjs [--from <dir>] [--check]
 *   --check 只校验副本是否与源一致，不写入。
 * 环境变量：H3C_LAB_MCP_DIR 指定源目录。
 */
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const DEST_DIR = resolve(HERE)
const DEFAULT_SOURCE = process.env.H3C_LAB_MCP_DIR ?? 'D:\\DSH\\NET\\h3c-lab-mcp'

/** 解析命令行参数。 */
function parseArgs(argv) {
  let from = DEFAULT_SOURCE
  let check = false
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index]
    if (arg === '--from')
      from = argv[++index] ?? from
    else if (arg === '--check')
      check = true
  }
  return { from: resolve(from), check }
}

/**
 * 找出 server.py 直接 import 的同目录本地模块。
 * 只认 `import x` / `from x import ...` 且同目录存在 x.py 的名字。
 */
function localImportsOf(pyPath, sourceDir) {
  const text = readFileSync(pyPath, 'utf8')
  const names = new Set()
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim()
    let match = /^import\s+([A-Za-z_][\w.]*)/.exec(trimmed)
    if (match !== null)
      names.add(match[1].split('.')[0])
    match = /^from\s+([A-Za-z_][\w.]*)\s+import\s+/.exec(trimmed)
    if (match !== null)
      names.add(match[1].split('.')[0])
  }
  return [...names].filter(name => existsSync(join(sourceDir, `${name}.py`))).sort()
}

/** 主流程。 */
function main() {
  const { from, check } = parseArgs(process.argv.slice(2))
  const entry = join(from, 'server.py')
  if (!existsSync(entry)) {
    console.error(`[sync-server] 找不到源文件：${entry}`)
    console.error('[sync-server] 请先用 --from <dir> 或 H3C_LAB_MCP_DIR 指定 h3c-lab-mcp 目录。')
    process.exitCode = 1
    return
  }
  if (!existsSync(DEST_DIR))
    mkdirSync(DEST_DIR, { recursive: true })

  const modules = localImportsOf(entry, from)
  const files = ['server.py', ...modules.map(name => `${name}.py`)]
  console.log(`[sync-server] 源目录：${from}`)
  console.log(`[sync-server] 目标目录：${DEST_DIR}`)
  console.log(`[sync-server] 待复制：${files.join(', ')}${check ? '（--check，仅校验）' : ''}`)

  const sourceNames = new Set(readdirSync(from))
  let changed = 0
  for (const file of files) {
    const source = join(from, file)
    const dest = join(DEST_DIR, file)
    const size = statSync(source).size
    const same = existsSync(dest) && readFileSync(dest, 'utf8') === readFileSync(source, 'utf8')
    if (check) {
      console.log(`${same ? '一致  ' : '不一致'} ${file}（源 ${size} 字节）`)
      if (!same)
        changed++
      continue
    }
    if (same) {
      console.log(`未变化 ${file}（${size} 字节）`)
      continue
    }
    copyFileSync(source, dest)
    console.log(`已复制 ${file}（${size} 字节）`)
    changed++
  }

  // 同步后的自检：8 个工具名都在，且不是只支持 Content-Length 分帧。
  const copied = readFileSync(join(DEST_DIR, 'server.py'), 'utf8')
  const toolNames = [
    'hcl_list_devices', 'hcl_topology', 'hcl_run_command', 'hcl_get_facts',
    'hcl_apply_plan', 'hcl_verify', 'hcl_search_memory', 'hcl_link_watch'
  ]
  const missing = toolNames.filter(name => !copied.includes(name))
  console.log(`[sync-server] 工具名自检：${missing.length === 0 ? '8/8 命中' : `缺少 ${missing.join(', ')}`}`)
  console.log(`[sync-server] 分帧自检：${copied.includes('Content-Length') ? '兼容 Content-Length 输入（同时支持一行一 JSON）' : '仅一行一 JSON 分帧'}`)
  console.log(`[sync-server] 未复制源目录中的其他文件：${[...sourceNames].filter(name => name.endsWith('.py') && !files.includes(name)).join(', ') || '（无）'}`)
  if (check && changed > 0)
    process.exitCode = 2
}

main()
