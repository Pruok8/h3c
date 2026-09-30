#!/usr/bin/env node
/**
 * dsh-h3clab 自测。
 *
 * 用 test/mock-mcp-server.mjs 做一条真实的 stdio MCP 链路，验证：
 *   1. 握手（initialize + notifications/initialized）、tools/list、tools/call；
 *   2. isError / JSON-RPC error 转成可读失败；
 *   3. 超时会 kill 子进程，且下一次调用能懒重启；
 *   4. lib/index.js 可加载，apply 用假 ctx 会注册 8 个工具，字段齐全；
 *   5. 通过工具定义端到端打到 mock 服务器（含 dry_run 默认预演）。
 *
 * 传输说明：首选 `spawn(node, [mock], { stdio: ['pipe','pipe','pipe'] })`。
 * 若宿主沙箱禁止创建命名管道（Windows 受限沙箱会同步抛 EPERM），自测会打印
 * 警告并自动改用 test/in-process-child.mjs 的内存流 child，协议层断言照常执行。
 *
 * 运行：node selftest.mjs
 */
import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const MOCK_SERVER = resolve(HERE, 'test', 'mock-mcp-server.mjs')
const PACKAGE_JSON = resolve(HERE, 'package.json')
const PATCH_YML = resolve(HERE, 'cordis.patch.yml')

const results = []
let failures = 0

/** 记录一条断言结果。 */
async function check(name, body) {
  try {
    await body()
    results.push(`[PASS] ${name}`)
  }
  catch (error) {
    failures++
    results.push(`[FAIL] ${name} :: ${error instanceof Error ? error.message : String(error)}`)
  }
  console.log(results.at(-1))
}

/** 找到提供 @deepseek-ai/dsh-tools 的 peer 目录。 */
function findPeerRoot() {
  const candidates = []
  if (process.env.DSH_H3CLAB_PEER_ROOT)
    candidates.push(process.env.DSH_H3CLAB_PEER_ROOT)
  const nodeDir = dirname(process.execPath)
  candidates.push(resolve(nodeDir, 'node_modules', '@deepseek-ai'))
  candidates.push(resolve(nodeDir, 'node_modules', '@deepseek-ai', 'dsh', 'node_modules', '@deepseek-ai'))
  const dshHome = process.env.DSH_HOME
  if (dshHome) {
    const profiles = resolve(dshHome, 'profiles')
    if (existsSync(profiles)) {
      for (const profile of readdirSync(profiles)) {
        candidates.push(resolve(profiles, profile, 'node_modules', '@deepseek-ai'))
        candidates.push(resolve(profiles, profile, 'node_modules', '@deepseek-ai', 'dsh', 'node_modules', '@deepseek-ai'))
      }
    }
  }
  return candidates.find(candidate => existsSync(resolve(candidate, 'dsh-tools', 'package.json'))) ?? null
}

/**
 * 让本目录能解析 peer 依赖（@deepseek-ai/dsh-tools 等）。
 * 未安装依赖时，把本机 DSH 自带的 @deepseek-ai 目录以 junction 方式挂到
 * node_modules/@deepseek-ai；正常安装过依赖的副本会直接命中。
 */
function ensurePeerDependencies() {
  const linkDir = resolve(HERE, 'node_modules', '@deepseek-ai')
  if (existsSync(resolve(linkDir, 'dsh-tools', 'package.json')))
    return { linkDir, created: false }
  const peerRoot = findPeerRoot()
  if (peerRoot === null)
    throw new Error('找不到 @deepseek-ai 依赖；请先安装 peerDependencies，或用 DSH_H3CLAB_PEER_ROOT 指向含 dsh-tools 的 @deepseek-ai 目录')
  mkdirSync(dirname(linkDir), { recursive: true })
  symlinkSync(peerRoot, linkDir, 'junction')
  return { linkDir, created: true, peerRoot }
}

/** 探测沙箱是否允许命名管道 stdio（生产路径的前提）。 */
function probePipeStdio() {
  try {
    const child = spawn(process.execPath, ['-e', 'process.exit(0)'], { stdio: ['pipe', 'pipe', 'pipe'] })
    child.on('error', () => {})
    if (typeof child.unref === 'function')
      child.unref()
    return { ok: true, detail: '可用' }
  }
  catch (error) {
    return { ok: false, detail: error?.code ?? String(error) }
  }
}

/** 简单等待。 */
const delay = ms => new Promise(resolveDelay => setTimeout(resolveDelay, ms))

const peer = ensurePeerDependencies()
const pipeProbe = probePipeStdio()
const REAL_SPAWN = pipeProbe.ok
const TRANSPORT = REAL_SPAWN ? '真实子进程 + pipe stdio' : '内存流 child（沙箱回退）'

console.log(`依赖解析：node_modules/@deepseek-ai -> ${peer.created ? `新建 junction ${peer.peerRoot}` : '已存在（复用）'}`)
console.log(`node ${process.version} / mock 服务器 ${MOCK_SERVER}`)
const pythonProbe = spawnSync('python', ['-V'], { encoding: 'utf8' })
console.log(`python: ${pythonProbe.error ? `无法探测（${pythonProbe.error.code ?? pythonProbe.error.message}）` : (pythonProbe.stdout ?? '').trim() || '未知'}`)
console.log(`spawn pipe stdio: ${pipeProbe.detail}`)
if (!REAL_SPAWN) {
  console.log('[WARN] 本会话沙箱禁止创建命名管道，自测改用内存流 child（test/in-process-child.mjs）。')
  console.log('[WARN] 协议/超时/懒重启断言照常执行；"真实子进程 + pipe stdio" 这一条在本会话无法验证，')
  console.log('[WARN] 请在普通终端（非受限沙箱）重跑 node selftest.mjs 以覆盖它。')
}
console.log('---')

// 动态 import：必须在确认 peer 依赖可解析之后。
const { McpStdioClient, extractToolText } = await import('./lib/mcp-client.js')
const { createInProcessMockChild } = await import('./test/in-process-child.mjs')
const plugin = await import('./lib/index.js')

/** 建一个指向 mock 服务器的客户端（受限沙箱下注入内存流 child）。 */
function createClient(options = {}) {
  const logs = []
  const client = new McpStdioClient({
    command: process.execPath,
    args: [MOCK_SERVER],
    cwd: dirname(MOCK_SERVER),
    timeoutMs: options.timeoutMs ?? 5000,
    serverName: 'mock',
    log: (level, message) => logs.push(`${level}: ${message}`),
    ...REAL_SPAWN ? {} : { createChild: () => createInProcessMockChild() }
  })
  return { client, logs }
}

// ---------------------------------------------------------------------------
// 1) 协议层
// ---------------------------------------------------------------------------
const { client, logs } = createClient()

await check(`握手 + tools/list 能拉到 8 个 MCP 工具（${TRANSPORT}）`, async () => {
  const tools = await client.listTools()
  assert.equal(tools.length, 8, `期望 8 个工具，实到 ${tools.length}`)
  assert.ok(tools.some(tool => tool.name === 'hcl_list_devices'))
  assert.ok(logs.some(line => line.includes('握手完成')), '日志里没有握手完成记录')
})

await check('callTool 能拿到 content[].text', async () => {
  const raw = await client.callTool('hcl_list_devices')
  assert.match(extractToolText(raw), /devices: 2001=SW1/)
})

await check('callToolText 透传参数（port/command）', async () => {
  const text = await client.callToolText('hcl_run_command', { port: 2001, command: 'display version' })
  assert.match(text, /port 2001 <display version>/)
})

await check('stderr 被转发到日志出口', async () => {
  await delay(50)
  assert.ok(logs.some(line => line.includes('[stderr]')), `日志里没有 [stderr]：${logs.slice(0, 3).join(' | ')}`)
})

await check('isError 结果 -> 可读失败', async () => {
  await assert.rejects(
    () => client.callToolText('hcl_fail'),
    /hcl_fail 执行失败：device unreachable/
  )
})

await check('MCP error 响应 -> 可读失败', async () => {
  await assert.rejects(
    () => client.callTool('hcl_does_not_exist'),
    /tools\/call hcl_does_not_exist 失败（code=-32602）/
  )
})

// ---------------------------------------------------------------------------
// 2) 超时 + 懒重启
// ---------------------------------------------------------------------------
const { client: timeoutClient } = createClient({ timeoutMs: 300 })

await check('超时按 timeoutMs 报错并结束子进程', async () => {
  await assert.rejects(
    () => timeoutClient.callTool('hcl_slow', {}, 300),
    /超时（300 ms）/
  )
  assert.equal(timeoutClient.alive(), false, '超时后子进程应已结束')
})

await check('超时后下一次调用能懒重启（重新 spawn + 重新握手）', async () => {
  const text = await timeoutClient.callToolText('hcl_get_facts', { port: 2002 })
  assert.match(text, /facts port 2002/)
  assert.equal(timeoutClient.alive(), true)
})
timeoutClient.close()

if (!REAL_SPAWN) {
  // 顺带验证：真实 spawn 不可用（受限沙箱 EPERM）时，插件给出可读失败而不是崩掉。
  const raw = new McpStdioClient({
    command: process.execPath,
    args: [MOCK_SERVER],
    timeoutMs: 5000,
    serverName: 'mock',
    log: () => {}
  })
  await check(`真实 spawn 抛 ${pipeProbe.detail} 时给出可读失败（不崩插件）`, async () => {
    await assert.rejects(
      () => raw.callTool('hcl_list_devices'),
      /无法连接 H3C 实验 MCP 服务器/
    )
  })
  raw.close()
}

// ---------------------------------------------------------------------------
// 3) 插件入口 + 8 个工具注册
// ---------------------------------------------------------------------------
await check('lib/index.js 导出 name / inject / apply', async () => {
  assert.equal(plugin.name, 'dsh-h3clab')
  assert.deepEqual(plugin.inject, ['tools'])
  assert.equal(typeof plugin.apply, 'function')
})

const captured = []
const logs2 = []
const disposers = []
const fakeCtx = {
  tools: { register: tool => captured.push(tool) },
  logger: () => ({
    debug: message => logs2.push(`debug: ${message}`),
    info: message => logs2.push(`info: ${message}`),
    warn: message => logs2.push(`warn: ${message}`)
  }),
  effect: (body) => {
    const disposer = body()
    disposers.push(disposer)
    return () => {}
  },
  on: () => {}
}

plugin.apply(fakeCtx, {
  pythonCommand: process.execPath,
  serverPath: MOCK_SERVER,
  toolCallTimeoutMs: 5000
})

await check('假 ctx 下 apply 注册 8 个工具', async () => {
  assert.equal(captured.length, 8, `期望 8 个，实到 ${captured.length}`)
  const names = captured.map(tool => tool.name).sort()
  assert.deepEqual(names, Object.keys(plugin.TOOL_MAPPING).sort())
  assert.ok(logs2.some(line => line.includes('已注册 8 个 H3C 工具')), '缺少注册日志')
})

await check('每个工具都有 name/description/parameters/output.schema/output.render/execute', async () => {
  for (const tool of captured) {
    assert.equal(typeof tool.name, 'string')
    assert.ok(tool.description.length > 20, `${tool.name} 描述过短`)
    assert.equal(typeof tool.parameters, 'object')
    assert.equal(tool.output.schema.type, 'object')
    assert.deepEqual(tool.output.schema.required, ['text'])
    assert.equal(typeof tool.output.render, 'function')
    assert.equal(typeof tool.execute, 'function')
    assert.equal(typeof tool.presentCall, 'function')
    assert.equal(typeof tool.timeoutMs, 'number')
  }
})

await check('工具名 -> MCP 工具名映射正确', async () => {
  assert.deepEqual(plugin.TOOL_MAPPING, {
    h3c_devices: 'hcl_list_devices',
    h3c_topology: 'hcl_topology',
    h3c_run: 'hcl_run_command',
    h3c_facts: 'hcl_get_facts',
    h3c_verify: 'hcl_verify',
    h3c_link_watch: 'hcl_link_watch',
    h3c_memory_search: 'hcl_search_memory',
    h3c_apply_plan: 'hcl_apply_plan'
  })
})

await check('presentCall.kind 使用 dsh-tools 声明的合法枚举值', async () => {
  // defineTool 会在参数不合法时让 presentCall 返回 undefined（回放保护），
  // 因此这里必须用每个工具的最小合法参数调用，顺带验证参数 schema 可用。
  const legal = new Set(['read', 'edit', 'delete', 'move', 'search', 'execute', 'fetch', 'other'])
  const sample = {
    h3c_devices: {},
    h3c_topology: {},
    h3c_run: { port: 2001, command: 'display version' },
    h3c_facts: { port: 2001 },
    h3c_verify: { checklist_json: '[]' },
    h3c_link_watch: { links: [{ port: 2001, intf: 'GE1/0/1', peer_port: 2002, peer_intf: 'GE1/0/1' }] },
    h3c_memory_search: { keywords: ['vlan'] },
    h3c_apply_plan: { plan_json: '[]' }
  }
  for (const tool of captured) {
    const view = tool.presentCall(sample[tool.name])
    assert.ok(view !== undefined, `${tool.name}.presentCall 返回 undefined`)
    assert.ok(legal.has(view.kind), `${tool.name} 的 kind=${view.kind} 非法`)
  }
})

// ---------------------------------------------------------------------------
// 4) 端到端：经工具定义打到 mock MCP 服务器
// ---------------------------------------------------------------------------
// 真实子进程模式下直接用 apply 注册出来的工具；沙箱回退模式下用同一个工厂
// createH3cTools + 注入内存流 child 的工具（apply 本身已在上面验证）。
const e2eClient = REAL_SPAWN ? null : createClient().client
const e2eTools = REAL_SPAWN
  ? captured
  : plugin.createH3cTools(e2eClient, { toolCallTimeoutMs: 5000 })

const toolByName = name => e2eTools.find(tool => tool.name === name)
const exec = { signal: new AbortController().signal }

await check(`h3c_devices.execute 端到端返回文本 + render 成 content（${TRANSPORT}）`, async () => {
  const value = await toolByName('h3c_devices').execute({ ports: [2001, 2002] }, exec)
  assert.match(value.text, /filtered: \[2001,2002\]/)
  const content = toolByName('h3c_devices').output.render({}, value)
  assert.deepEqual(content, [{ type: 'text', text: value.text }])
})

await check('h3c_apply_plan 默认 dry_run=true（只预演），显式 false 才真下发', async () => {
  const tool = toolByName('h3c_apply_plan')
  const preview = await tool.execute({ plan_json: '[]' }, exec)
  assert.match(preview.text, /dry_run=true/)
  assert.match(tool.presentCall({ plan_json: '[]' }).title, /dry run/)
  const real = await tool.execute({ plan_json: '[]', dry_run: false }, exec)
  assert.match(real.text, /dry_run=false/)
  assert.match(tool.presentCall({ plan_json: '[]', dry_run: false }).title, /REAL push/)
})

await check('工具内部异常 -> 带工具名的可读失败', async () => {
  const broken = toolByName('h3c_run')
  await assert.rejects(
    () => broken.execute({ port: 1, command: 'x' }, { signal: AbortSignal.abort() }),
    /h3c_run 调用失败：调用已被取消/
  )
})

await check('插件卸载时通过 ctx.effect 回收子进程', async () => {
  assert.equal(disposers.length, 1, '应注册 1 个 effect disposer')
  assert.equal(typeof disposers[0], 'function')
  disposers[0]()
})
if (e2eClient !== null)
  e2eClient.close()

// ---------------------------------------------------------------------------
// 5) 静态检查：包元数据与 patch 文件
// ---------------------------------------------------------------------------
await check('package.json 是合法的 dsh bundle', async () => {
  const pkg = JSON.parse(readFileSync(PACKAGE_JSON, 'utf8'))
  assert.equal(pkg.name, 'dsh-h3clab')
  assert.equal(pkg.version, '0.1.0')
  assert.equal(pkg.type, 'module')
  assert.equal(pkg.main, './lib/index.js')
  assert.equal(pkg.dsh.bundle.patch, './cordis.patch.yml')
  for (const entry of ['lib', 'scripts', 'cordis.patch.yml', 'README.md'])
    assert.ok(pkg.files.includes(entry), `files 缺少 ${entry}`)
  for (const dep of ['@deepseek-ai/cordis', '@deepseek-ai/dsh-tools', '@deepseek-ai/schemastery'])
    assert.ok(pkg.peerDependencies[dep], `peerDependencies 缺少 ${dep}`)
  assert.equal(pkg.scripts.selftest, 'node selftest.mjs')
  assert.equal(pkg.scripts['sync-server'], 'node scripts/sync-server.mjs')
})

await check('cordis.patch.yml 是 insert 形式的 bundle patch', async () => {
  const yml = readFileSync(PATCH_YML, 'utf8')
  assert.match(yml, /^- insert:/m)
  assert.match(yml, /id: dsh-h3clab/m)
  assert.match(yml, /name: dsh-h3clab/m)
  assert.match(yml, /pythonCommand:/m)
  assert.match(yml, /serverPath:/m)
})

await check('resolveConfig 补全默认值（serverPath 默认指向包内 scripts/server.py）', async () => {
  const resolved = plugin.resolveConfig({})
  assert.equal(resolved.pythonCommand, 'python')
  assert.equal(resolved.toolCallTimeoutMs, 240000)
  assert.equal(resolved.serverName, 'h3clab')
  assert.equal(resolved.serverPath, resolve(HERE, 'scripts', 'server.py'))
  assert.ok(existsSync(resolved.serverPath), '包内 scripts/server.py 应存在（npm run sync-server 生成）')
  // 相对路径按包根目录解析
  assert.equal(plugin.resolveConfig({ serverPath: 'scripts/server.py' }).serverPath, resolved.serverPath)
})

await check('cordis.patch.yml 暴露了实验层配置项', async () => {
  const yml = readFileSync(PATCH_YML, 'utf8')
  for (const key of ['host:', 'ports:', 'devices:', 'netFile:', 'evidenceRoot:',
                     'referencesDir:', 'serverConfigPath:', 'env:'])
    assert.match(yml, new RegExp(`^\\s+${key}`, 'm'), `patch 缺少 ${key}`)
  // netFile 必须留空：拓扑不能猜（D:\NET 下有十几个不同实验的 .net）
  assert.match(yml, /^\s+netFile: ''\s*$/m, 'netFile 默认必须为空字符串')
})

await check('resolveConfig 解析实验层配置（ports/devices/netFile/证据与记忆目录/env）', async () => {
  const resolved = plugin.resolveConfig({
    host: '127.0.0.1',
    ports: [30009, 30002, 30002, '30001', 70000, 'x'],
    devices: { SW1: 30008, SW2: '30009', bad: 'abc' },
    netFile: 'D:\\lab\\x.net',
    evidenceRoot: 'D:\\ev',
    referencesDir: 'D:\\refs',
    serverConfigPath: 'D:\\cfg.json',
    env: { H3C_MCP_DEBUG_DUMP: 'D:\\dump.txt', EMPTY: null }
  })
  assert.deepEqual(resolved.ports, [30001, 30002, 30009], 'ports 应去重升序并丢掉越界/非数字项')
  assert.deepEqual(resolved.devices, { SW1: 30008, SW2: 30009 }, 'devices 应丢掉非法项')
  assert.equal(resolved.netFile, 'D:\\lab\\x.net')
  assert.equal(resolved.evidenceRoot, 'D:\\ev')
  assert.equal(resolved.referencesDir, 'D:\\refs')
  assert.equal(resolved.serverConfigPath, 'D:\\cfg.json')
  assert.deepEqual(resolved.env, { H3C_MCP_DEBUG_DUMP: 'D:\\dump.txt' }, 'env 应丢掉 null 值')
  // 相对路径按包根目录解析（与 serverPath 一致）
  assert.equal(plugin.resolveConfig({ evidenceRoot: 'ev' }).evidenceRoot, resolve(HERE, 'ev'))
})

await check('默认证据/记忆/配置目录落在 DSH_HOME 下，且不在插件包内', async () => {
  const resolved = plugin.resolveConfig({})
  const dshHome = (plugin.dshHome()).toLowerCase()
  for (const [label, value] of [['evidenceRoot', resolved.evidenceRoot],
                                ['referencesDir', resolved.referencesDir],
                                ['serverConfigPath', resolved.serverConfigPath]]) {
    assert.ok(value.toLowerCase().startsWith(dshHome), `${label} 应在 DSH_HOME 下：${value}`)
    assert.ok(!value.toLowerCase().startsWith(HERE.toLowerCase()), `${label} 不应落在插件包内：${value}`)
  }
  assert.equal(resolved.netFile, '', 'netFile 默认必须为空（不猜拓扑）')
  assert.deepEqual(resolved.ports, [])
  assert.deepEqual(resolved.devices, {})
})

await check('buildServerConfig 只写该写的键（空 ports / 空 netFile 一律不写）', async () => {
  const empty = plugin.buildServerConfig(plugin.resolveConfig({}))
  assert.ok(!('ports' in empty), 'ports 为空时不应写入该键')
  assert.ok(!('net_file' in empty), 'netFile 为空时不应写入该键')
  assert.equal(empty.host, '127.0.0.1')
  assert.ok(typeof empty.evidence_root === 'string' && empty.evidence_root !== '')
  assert.ok(typeof empty.references_dir === 'string' && empty.references_dir !== '')

  const full = plugin.buildServerConfig(plugin.resolveConfig({
    ports: [30002, 30001], devices: { SW1: 30008 }, netFile: 'D:\\lab\\x.net'
  }))
  assert.deepEqual(full.ports, [30001, 30002])
  assert.deepEqual(full.devices, { SW1: 30008 })
  assert.equal(full.net_file, 'D:\\lab\\x.net')
})

await check('writeServerConfig 落盘合法 JSON；内容不变时不重写文件', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'dsh-h3clab-selftest-'))
  try {
    const target = join(dir, 'server-config.json')
    const resolved = plugin.resolveConfig({ serverConfigPath: target, ports: [30002, 30001] })
    const logs = []
    const log = (level, message) => logs.push(`${level}:${message}`)

    assert.equal(plugin.writeServerConfig(resolved, log), target, '应返回配置文件的绝对路径')
    const first = JSON.parse(readFileSync(target, 'utf8'))
    assert.deepEqual(first.ports, [30001, 30002], '写出的 JSON 应含解析后的 ports')
    assert.ok(!('net_file' in first), 'netFile 为空时 JSON 里不该有 net_file')

    // 内容不变 => 不应重写（用 mtime 判断）
    const mtimeBefore = statSync(target).mtimeMs
    await new Promise(resolveDelay => setTimeout(resolveDelay, 20))
    plugin.writeServerConfig(resolved, log)
    assert.equal(statSync(target).mtimeMs, mtimeBefore, '内容未变时不该重写文件')

    // 内容变化 => 应重写
    plugin.writeServerConfig(plugin.resolveConfig({ serverConfigPath: target, ports: [30005] }), log)
    assert.deepEqual(JSON.parse(readFileSync(target, 'utf8')).ports, [30005])

    // 写不了（目标是目录）=> 返回 null 而不是抛异常
    const asDir = join(dir, 'a-directory')
    mkdirSync(asDir)
    assert.equal(plugin.writeServerConfig(plugin.resolveConfig({ serverConfigPath: asDir }), log), null,
      '写失败应返回 null（降级到服务器内置默认配置），而不是抛异常')
    assert.ok(logs.some(item => item.startsWith('warn:')), '写失败应留下 warn 日志')
  }
  finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

await check('lib/tools.js 暴露了 server.py 支持的全部参数（不再丢参数）', async () => {
  const { createH3cTools } = await import('./lib/tools.js')
  const stubs = createH3cTools({ callToolText: async () => '' }, { toolCallTimeoutMs: 1000 })
  const paramsOf = (toolName) => {
    const tool = stubs.find(item => item.name === toolName)
    assert.ok(tool, `找不到工具 ${toolName}`)
    const schema = tool.parameters ?? tool.inputSchema ?? {}
    const props = schema.properties ?? schema
    return Object.keys(props)
  }
  for (const [toolName, expected] of [
    ['h3c_devices', ['ports', 'model', 'prompt_timeout', 'workers']],
    ['h3c_facts', ['port', 'timeout']],
    ['h3c_verify', ['checklist_json', 'only', 'timeout']],
    ['h3c_link_watch', ['links', 'timeout']],
    ['h3c_memory_search', ['keywords', 'any', 'max', 'max_lines']],
    ['h3c_apply_plan', ['plan_json', 'only', 'save', 'dry_run', 'timeout', 'workers']]
  ]) {
    const actual = paramsOf(toolName)
    for (const name of expected)
      assert.ok(actual.includes(name), `${toolName} 缺少参数 ${name}（实际：${actual.join(', ')}）`)
  }
})

await check('scripts/server.py 与上游 h3c-lab-mcp 保持一致（快照未漂移）', async () => {
  const upstream = resolve(HERE, '..', 'h3c-lab-mcp', 'server.py')
  if (!existsSync(upstream)) {
    console.log('        （跳过：找不到上游 D:\\DSH\\NET\\h3c-lab-mcp\\server.py）')
    return
  }
  const local = readFileSync(resolve(HERE, 'scripts', 'server.py'), 'utf8')
  const remote = readFileSync(upstream, 'utf8')
  assert.equal(local, remote, '插件里的 server.py 快照与上游不一致，请跑 node scripts/sync-server.mjs')
})

await check('watchPath：服务器脚本变了会结束旧进程并用新代码重启', async () => {
  if (!REAL_SPAWN) {
    console.log('        （跳过：本会话无法创建真实子进程）')
    return
  }
  const dir = mkdtempSync(join(tmpdir(), 'dsh-h3clab-watch-'))
  try {
    const target = join(dir, 'mock-server.mjs')
    // mock 服务器是相对 import 的，必须把它的依赖一起复制过去
    for (const name of readdirSync(join(HERE, 'test'))) {
      if (name.endsWith('.mjs'))
        copyFileSync(join(HERE, 'test', name), join(dir, name))
    }
    copyFileSync(MOCK_SERVER, target)
    const watched = new McpStdioClient({
      command: process.execPath,
      args: [target],
      watchPath: target,
      timeoutMs: 15_000,
      log: () => {}
    })
    try {
      await watched.listTools()
      const firstPid = watched.child.pid
      assert.ok(firstPid, '应已启动子进程')

      // 只碰内容不碰指纹是不够的：必须让 (mtimeMs, size) 真的变化
      await delay(1100)
      writeFileSync(target, `${readFileSync(target, 'utf8')}\n// touched by selftest\n`)

      await watched.listTools()
      assert.notEqual(watched.child.pid, firstPid, '脚本变化后应换一个新的子进程')
    }
    finally {
      watched.close()
    }
  }
  finally {
    rmSync(dir, { recursive: true, force: true })
  }
})

await check('schemastery Config 能被 StandardSchema 校验并补默认值', async () => {
  const standard = plugin.Config['~standard']
  assert.ok(standard !== undefined, 'Config 应实现 StandardSchemaV1')
  const result = await standard.validate({})
  assert.equal(result.issues, undefined, `校验失败：${JSON.stringify(result.issues)}`)
  assert.equal(result.value.pythonCommand, 'python')
  assert.equal(result.value.toolCallTimeoutMs, 240000)
  const bad = await standard.validate({ toolCallTimeoutMs: -1 })
  assert.ok(Array.isArray(bad.issues) && bad.issues.length > 0, '非法配置应报 issue')
})

client.close()

// ---------------------------------------------------------------------------
console.log('---')
console.log(`总计：${results.length - failures} 通过 / ${failures} 失败（传输：${TRANSPORT}）`)
if (failures > 0) {
  console.log('失败项：')
  for (const line of results.filter(item => item.startsWith('[FAIL]')))
    console.log(line)
}
process.exitCode = failures === 0 ? 0 : 1
