#!/usr/bin/env node
/**
 * 把 server.py / 插件的三份副本拉齐，防止静默漂移。
 *
 *   ① 源        <工作区>\h3c-lab-mcp\server.py      —— MCP 服务器的真源（带 test_config.py 等测试）
 *   ② 插件快照  <工作区>\dsh-h3clab\scripts\        —— 随包发布、DSH 实际运行的那一份
 *   ③ 仓库副本  <工作区>\dsh-h3c-lab\dsh-h3clab\    —— git 里发布出去的那一份
 *
 * ① → ②：server.py + 它 import 的同目录本地模块（例如 hcldrv.py）
 * ② → ③：插件包里除 node_modules / 证据 / __pycache__ 之外的全部发布文件
 *
 * 用法：
 *   node scripts/sync-all.mjs               # 同步（只写有变化的文件）
 *   node scripts/sync-all.mjs --check       # 只校验，有漂移就退 2
 *   node scripts/sync-all.mjs --from <dir>  # 换一个上游目录
 * 环境变量：H3C_LAB_MCP_DIR 指定上游目录。
 */
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN_ROOT = resolve(HERE, '..')
const WORKSPACE = resolve(PLUGIN_ROOT, '..')
const DEFAULT_UPSTREAM = process.env.H3C_LAB_MCP_DIR ?? join(WORKSPACE, 'h3c-lab-mcp')
const REPO_MIRROR = join(WORKSPACE, 'dsh-h3c-lab', 'dsh-h3clab')

/** 不同步进仓库副本的东西（运行时产物 / 依赖 / 临时文件）。 */
const SKIP_DIRS = new Set(['node_modules', 'evidence', '__pycache__', '.git'])
const SKIP_SUFFIX = ['.pyc', '.log']
const SKIP_PATTERNS = [/\.bak(-\d{8}-\d{6})?$/, /~$/]

function parseArgs(argv) {
  let from = DEFAULT_UPSTREAM
  let check = false
  for (let index = 0; index < argv.length; index++) {
    if (argv[index] === '--from')
      from = argv[++index] ?? from
    else if (argv[index] === '--check')
      check = true
  }
  return { from: resolve(from), check }
}

function shouldSkip(name) {
  if (SKIP_DIRS.has(name))
    return true
  if (SKIP_SUFFIX.some(suffix => name.toLowerCase().endsWith(suffix)))
    return true
  return SKIP_PATTERNS.some(pattern => pattern.test(name))
}

/** 递归列出目录下所有应发布的文件（相对路径，正斜杠）。 */
function listFiles(root, prefix = '', out = []) {
  for (const entry of readdirSync(join(root, prefix), { withFileTypes: true })) {
    if (shouldSkip(entry.name))
      continue
    const rel = prefix === '' ? entry.name : `${prefix}/${entry.name}`
    if (entry.isDirectory())
      listFiles(root, rel, out)
    else if (entry.isFile())
      out.push(rel)
  }
  return out
}

/** 找出 server.py 直接 import 的同目录本地模块。 */
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

const state = { copied: 0, same: 0, drifted: [] }

/** 把一个文件从 src 同步到 dst。 */
function syncFile(srcRoot, dstRoot, rel, check) {
  const src = join(srcRoot, rel)
  const dst = join(dstRoot, rel)
  const sourceText = readFileSync(src, 'utf8')
  const same = existsSync(dst) && readFileSync(dst, 'utf8') === sourceText
  if (same) {
    state.same++
    return
  }
  state.drifted.push(rel)
  if (check) {
    console.log(`  漂移   ${rel}`)
    return
  }
  mkdirSync(dirname(dst), { recursive: true })
  copyFileSync(src, dst)
  console.log(`  已复制 ${rel}`)
  state.copied++
}

function main() {
  const { from, check } = parseArgs(process.argv.slice(2))
  console.log(`[sync-all] 工作区：${WORKSPACE}${check ? '（--check，只校验）' : ''}`)

  // ---------- ① 上游 server.py -> ② 插件快照 ----------
  console.log(`\n① -> ②  ${from}  =>  ${join(PLUGIN_ROOT, 'scripts')}`)
  const entry = join(from, 'server.py')
  if (!existsSync(entry)) {
    console.error(`  找不到上游 server.py：${entry}（用 --from 或 H3C_LAB_MCP_DIR 指定）`)
    process.exitCode = 1
    return
  }
  const serverFiles = ['server.py', ...localImportsOf(entry, from).map(name => `${name}.py`)]
  console.log(`  待同步：${serverFiles.join(', ')}`)
  for (const file of serverFiles)
    syncFile(from, join(PLUGIN_ROOT, 'scripts'), file, check)

  // ---------- ② 插件 -> ③ 仓库副本 ----------
  console.log(`\n② -> ③  ${PLUGIN_ROOT}  =>  ${REPO_MIRROR}`)
  if (!existsSync(REPO_MIRROR)) {
    console.log('  跳过：找不到仓库副本目录（这不是错误，只是没有第三个副本要同步）')
  }
  else {
    const files = listFiles(PLUGIN_ROOT)
    for (const rel of files)
      syncFile(PLUGIN_ROOT, REPO_MIRROR, rel, check)
    const mirrorFiles = new Set(listFiles(REPO_MIRROR))
    const extra = [...mirrorFiles].filter(rel => !files.includes(rel)).sort()
    if (extra.length > 0) {
      console.log(`  ⚠ 仓库副本里有 ${extra.length} 个插件包中不存在的文件（未删除，请人工确认）：`)
      for (const rel of extra.slice(0, 10))
        console.log(`      ${rel}`)
      if (extra.length > 10)
        console.log(`      …还有 ${extra.length - 10} 个`)
    }
  }

  // ---------- 汇总 ----------
  console.log('')
  if (check) {
    if (state.drifted.length === 0) {
      console.log(`[sync-all] 一致：${state.same} 个文件全部相同。`)
    }
    else {
      console.log(`[sync-all] 发现 ${state.drifted.length} 个漂移文件（一致 ${state.same} 个）。跑 node scripts/sync-all.mjs 修复。`)
      process.exitCode = 2
    }
  }
  else {
    console.log(`[sync-all] 完成：复制 ${state.copied} 个，未变化 ${state.same} 个。`)
  }

  // 顺带自检：快照里 8 个工具名都在
  const snapshot = readFileSync(join(PLUGIN_ROOT, 'scripts', 'server.py'), 'utf8')
  const toolNames = [
    'hcl_list_devices', 'hcl_topology', 'hcl_run_command', 'hcl_get_facts',
    'hcl_apply_plan', 'hcl_verify', 'hcl_search_memory', 'hcl_link_watch'
  ]
  const missing = toolNames.filter(name => !snapshot.includes(name))
  console.log(`[sync-all] 工具名自检：${missing.length === 0 ? `${toolNames.length}/${toolNames.length} 命中` : `缺少 ${missing.join(', ')}`}`)
  if (missing.length > 0)
    process.exitCode = 1
}

main()
