#!/usr/bin/env node
/**
 * 把 server.py / 插件的几份副本拉齐，防止静默漂移。
 *
 * 布局（合并进 git 仓库之后）：
 *
 *   D:\DSH\NET\
 *     dsh-h3clab\                     <- ② 插件【活副本】（profile 里的 junction 指向它）
 *     dsh-h3c-lab\                    <- git 仓库根
 *       mcp-server\server.py          <- ① MCP 服务器真源（带 test_config.py / test_session.py）
 *       dsh-h3clab\                   <- ③ 仓库里的插件副本
 *       skills\h3c-lab-automation\
 *
 * ① → ②：server.py + 它 import 的同目录本地模块（例如 hcldrv.py）
 * ② → ③：插件包里除 node_modules / 证据 / __pycache__ 之外的全部发布文件
 *
 * 本脚本在**两种**位置都能跑：插件活副本里（②）、或仓库内的插件副本里（③，此时跳过 ②→③）。
 *
 * 用法：
 *   node scripts/sync-all.mjs               # 同步（只写有变化的文件）
 *   node scripts/sync-all.mjs --check       # 只校验，有漂移就退 2
 *   node scripts/sync-all.mjs --from <dir>  # 换一个上游目录
 * 环境变量：H3C_LAB_MCP_DIR 指定上游目录。
 */
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN_ROOT = resolve(HERE, '..')
const PARENT = resolve(PLUGIN_ROOT, '..')

/** 判断一个目录是不是 git 仓库根（有 .git 即可，兼容 .git 文件形式的 worktree）。 */
function isRepoRoot(dir) {
  return existsSync(join(dir, '.git'))
}

/**
 * 找 MCP 服务器真源目录：取第一个存在的候选。
 *
 * 之所以要一串候选：脚本既可能在插件活副本里跑（`D:\DSH\NET\dsh-h3clab`），
 * 也可能在仓库内的插件副本里跑（`D:\DSH\NET\dsh-h3c-lab\dsh-h3clab`），
 * 两种位置的相对路径不一样。
 */
function resolveUpstream() {
  const candidates = [
    process.env.H3C_LAB_MCP_DIR,
    join(PARENT, 'mcp-server'),                 // 插件在仓库里：<repo>\mcp-server
    join(PARENT, 'dsh-h3c-lab', 'mcp-server'),  // 插件是仓库外的活副本
    join(PARENT, 'h3c-lab-mcp')                 // 合并进仓库之前的旧布局
  ].filter(value => typeof value === 'string' && value.trim() !== '')
    .map(value => resolve(value))
  for (const candidate of candidates) {
    if (existsSync(join(candidate, 'server.py')))
      return candidate
  }
  return candidates[0]
}

/** 找仓库里的插件副本；插件自己就在仓库里时返回自身。 */
function resolveMirror() {
  const candidates = [
    PLUGIN_ROOT,                                       // 插件就在仓库里
    join(PARENT, 'dsh-h3c-lab', 'dsh-h3clab')          // 插件是仓库外的活副本
  ].map(value => resolve(value))
  for (const candidate of candidates) {
    if (isRepoRoot(join(candidate, '..')))
      return candidate
  }
  return null
}

const UPSTREAM = resolveUpstream()
const REPO_MIRROR = resolveMirror()

/** 不同步进仓库副本的东西（运行时产物 / 依赖 / 临时文件）。 */
const SKIP_DIRS = new Set(['node_modules', 'evidence', '__pycache__', '.git'])
const SKIP_SUFFIX = ['.pyc', '.log']
const SKIP_PATTERNS = [/\.bak(-\d{8}-\d{6})?$/, /~$/]

function parseArgs(argv) {
  let from = UPSTREAM
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

/** 把一个文件从 srcRoot 同步到 dstRoot。 */
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
  console.log(`[sync-all] 插件包：${PLUGIN_ROOT}${check ? '（--check，只校验）' : ''}`)

  // ---------- ① 上游 server.py -> ② 插件快照 ----------
  console.log(`\n① -> ②  ${from}  =>  ${join(PLUGIN_ROOT, 'scripts')}`)
  const entry = join(from, 'server.py')
  if (!existsSync(entry)) {
    console.error(`  找不到上游 server.py：${entry}`)
    console.error('  用 --from <dir> 或环境变量 H3C_LAB_MCP_DIR 指定真源目录。')
    process.exitCode = 1
    return
  }
  const serverFiles = ['server.py', ...localImportsOf(entry, from).map(name => `${name}.py`)]
  console.log(`  待同步：${serverFiles.join(', ')}`)
  for (const file of serverFiles)
    syncFile(from, join(PLUGIN_ROOT, 'scripts'), file, check)

  // ---------- ② 插件 -> ③ 仓库副本 ----------
  if (REPO_MIRROR === null) {
    console.log('\n② -> ③  跳过：找不到仓库副本目录（插件不在 git 仓库里，且没有 <父目录>\\dsh-h3c-lab）')
  }
  else if (REPO_MIRROR === PLUGIN_ROOT) {
    console.log('\n② -> ③  跳过：本插件就是仓库里的那一份，没有第三个副本')
  }
  else {
    console.log(`\n② -> ③  ${PLUGIN_ROOT}  =>  ${REPO_MIRROR}`)
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
