/**
 * 控制台 per-port 互斥锁的离线测试。
 *
 * 覆盖：锁键解析（工具入参 / 计划文件 / 验收清单）、同键串行、异键并行、
 *       等锁超时的可读错误、超时者不占队（不留幽灵等待者）、多键半途失败要还锁、
 *       取消、诊断快照，以及经 lib/tools.js 的端到端接线。
 */
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import {
  DEFAULT_LOCK_WAIT_MS,
  PortLockRegistry,
  createPortLock,
  describeLockKey,
  describeLockKeys,
  deviceKey,
  fileKey,
  formatLockLabel,
  lockKeysForTool,
  lockKeysFromChecklist,
  lockKeysFromPlan,
  normalizeLockKeys,
  portKey
} from '../lib/port-lock.js'

/** 等一小会儿（测试里只用很短的时间，避免拖慢自测）。 */
const delay = ms => new Promise(resolve => setTimeout(resolve, ms))

/**
 * 跑全部锁相关测试。
 * @param {(name: string, body: () => Promise<void>|void) => Promise<void>} check 断言记录器。
 * @param {object} deps `{ plugin }`。
 */
export async function runPortLockTests(check, deps) {
  const { plugin } = deps
  const { createH3cTools } = plugin

  await check('互斥锁：锁键构造与规范化（去空、去重、排序 —— 排序是防死锁的关键）', async () => {
    assert.equal(portKey(31008), 'port:31008')
    assert.equal(portKey('31008'), 'port:31008')
    assert.equal(portKey(0), null)
    assert.equal(portKey(70000), null)
    assert.equal(portKey('x'), null)
    assert.equal(deviceKey(' HQ-PE1 '), 'device:HQ-PE1')
    assert.equal(deviceKey(''), null)
    assert.equal(fileKey('plan', ' D:\\a.json '), 'plan:D:\\a.json')
    assert.equal(fileKey('plan', ''), null)
    assert.deepEqual(normalizeLockKeys(['port:2', '', 'port:1', 'port:2', null]), ['port:1', 'port:2'])
    assert.equal(describeLockKey('port:31008'), '控制台端口 31008')
    assert.equal(describeLockKey('device:HQ-PE1'), '设备 HQ-PE1')
    assert.equal(describeLockKey('plan:D:\\x.json'), '计划文件 D:\\x.json')
    assert.equal(describeLockKey('checklist:D:\\c.json'), '验收清单 D:\\c.json')
    assert.equal(describeLockKeys(['port:1', 'device:A']), '设备 A、控制台端口 1', '键是排序过的（防死锁）')
    assert.match(formatLockLabel('h3c_run', ['port:31008'], [31008]), /h3c_run 端口 31008 锁\[port:31008\]/)
  })

  await check('互斥锁：按工具入参算锁键（run/facts/devices/cfgdiff/link_watch）', async () => {
    assert.deepEqual(lockKeysForTool('h3c_run', { port: 31008 }).keys, ['port:31008'])
    assert.deepEqual(lockKeysForTool('h3c_facts', { port: 31008 }).keys, ['port:31008'])
    assert.deepEqual(lockKeysForTool('h3c_devices', { ports: [31002, 31001] }).keys, ['port:31001', 'port:31002'])
    assert.deepEqual(lockKeysForTool('h3c_cfgdiff', { ports: [31003], port: 31001 }).keys, ['port:31001', 'port:31003'])
    assert.deepEqual(lockKeysForTool('h3c_link_watch', { links: [{ port: 31001, peer_port: 31002 }] }).keys,
      ['port:31001', 'port:31002'])
    // 没有端口信息的工具（拓扑/记忆/状态）不加锁
    assert.deepEqual(lockKeysForTool('h3c_topology', {}).keys, [])
    assert.deepEqual(lockKeysForTool('h3c_memory_search', { keywords: ['vlan'] }).keys, [])
    assert.deepEqual(lockKeysForTool('h3c_lab_state', { action: 'get' }).keys, [])
  })

  await check('互斥锁：计划文件解析成 端口 + 设备名 + 文件本身', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'dsh-h3clab-lock-'))
    try {
      const planFile = join(dir, 'plan.json')
      writeFileSync(planFile, JSON.stringify({
        devices: [
          { name: 'HQ-PE1', port: 31008, commands: ['sysname HQ-PE1'] },
          { name: 'HQ-PE2', commands: ['sysname HQ-PE2'] }
        ]
      }), 'utf8')
      const detail = lockKeysFromPlan(planFile)
      assert.deepEqual(detail.ports, [31008])
      assert.deepEqual(detail.devices, ['HQ-PE1', 'HQ-PE2'])
      // 没写 port 的设备也要锁名字：两份计划命中同一台设备时不能并行
      assert.ok(detail.keys.includes('device:HQ-PE2'))
      assert.ok(detail.keys.includes(`plan:${planFile}`))
      assert.match(detail.note, /计划含 2 台设备/)

      const keys = lockKeysForTool('h3c_apply_plan', { plan_json: planFile })
      assert.equal(keys.keys.length, 4, JSON.stringify(keys.keys))
      assert.deepEqual(keys.ports, [31008])

      // 文件读不了 / 不是 JSON / 路径为空：只能锁文件本身，绝不抛异常
      const missing = lockKeysFromPlan(join(dir, 'nope.json'))
      assert.deepEqual(missing.keys, [`plan:${join(dir, 'nope.json')}`])
      assert.match(missing.note, /读不出来/)
      const broken = join(dir, 'broken.json')
      writeFileSync(broken, '{ not json', 'utf8')
      assert.deepEqual(lockKeysFromPlan(broken).keys, [`plan:${broken}`])
      assert.deepEqual(lockKeysFromPlan('').keys, [])
      writeFileSync(join(dir, 'empty.json'), JSON.stringify({ devices: [] }), 'utf8')
      assert.match(lockKeysFromPlan(join(dir, 'empty.json')).note, /没有 devices 数组/)
    }
    finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  await check('互斥锁：验收清单解析成端口（同一端口不能两个会话）', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'dsh-h3clab-lock-check-'))
    try {
      const file = join(dir, 'checklist.json')
      writeFileSync(file, JSON.stringify({
        checks: [
          { id: 'C1', port: 31005, cmd: 'display isis peer' },
          { id: 'C2', port: 31006, cmd: 'display mpls ldp peer' },
          { id: 'C3', name: 'timeout check' }
        ]
      }), 'utf8')
      const detail = lockKeysFromChecklist(file)
      assert.deepEqual(detail.ports, [31005, 31006])
      assert.deepEqual(detail.devices, [], '清单里的 name 只是标签，不参与加锁')
      assert.ok(detail.keys.includes(`checklist:${file}`))
      const keys = lockKeysForTool('h3c_verify', { checklist_json: file })
      assert.deepEqual(keys.keys, [`checklist:${file}`, 'port:31005', 'port:31006'])
      assert.deepEqual(lockKeysFromChecklist('').keys, [])
    }
    finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  await check('互斥锁：同一端口串行执行（并发两个调用不会交叠）', async () => {
    const lock = createPortLock()
    const events = []
    let active = 0
    let maxActive = 0
    const job = (name) => lock.run(['port:31008'], async () => {
      active++
      maxActive = Math.max(maxActive, active)
      events.push(`start ${name}`)
      await delay(20)
      events.push(`end ${name}`)
      active--
    }, { label: name })
    await Promise.all([job('A'), job('B'), job('C')])
    assert.equal(maxActive, 1, `同一端口最多只能有一个调用在跑，实到 ${maxActive}`)
    assert.deepEqual(events, ['start A', 'end A', 'start B', 'end B', 'start C', 'end C'], '应严格 FIFO 串行')
    assert.equal(lock.busy(), false, '跑完必须全部释放')
  })

  await check('互斥锁：不同端口互不阻塞（并行作业靠端口归属而不是全局串行）', async () => {
    const lock = createPortLock()
    let active = 0
    let maxActive = 0
    const job = (port) => lock.run([`port:${port}`], async () => {
      active++
      maxActive = Math.max(maxActive, active)
      await delay(20)
      active--
    }, { label: `h3c_run p${port}` })
    await Promise.all([job(31001), job(31002), job(31003)])
    assert.equal(maxActive, 3, '不同端口应当并行')
  })

  await check('互斥锁：等锁超时给出可读错误（含端口号、占用者、已等待时长）', async () => {
    const lock = createPortLock({ waitMs: 40 })
    const release = await lock.acquire(['port:31008'], { label: 'h3c_apply_plan 端口 31008' })
    const started = Date.now()
    await assert.rejects(
      () => lock.acquire(['port:31008'], { label: 'h3c_run' }),
      (error) => {
        assert.match(error.message, /等锁超时/)
        assert.match(error.message, /控制台端口 31008 正被另一次调用占用/)
        assert.match(error.message, /h3c_apply_plan 端口 31008/)
        assert.match(error.message, /已等待/)
        return true
      }
    )
    assert.ok(Date.now() - started >= 30, '应真的等满超时时间')
    assert.equal(lock.stats.timeouts, 1)
    release()
    assert.equal(lock.busy(), false)
  })

  await check('互斥锁：超时者要出队（不留幽灵等待者把后来的人挡住）', async () => {
    const lock = createPortLock({ waitMs: 30 })
    const releaseFirst = await lock.acquire(['port:31009'], { label: 'first' })
    // A 等超时
    await assert.rejects(() => lock.acquire(['port:31009'], { label: 'timeout-waiter', waitMs: 20 }))
    // B 用长等待排在后面；等 first 释放后 B 必须立刻拿到锁
    const pending = lock.acquire(['port:31009'], { label: 'B', waitMs: 5_000 })
    await delay(50)
    assert.equal(lock.inspect().queued.length, 1, `队列里只应有 B：${JSON.stringify(lock.inspect())}`)
    releaseFirst()
    const releaseB = await pending
    assert.equal(lock.inspect().held[0].label, 'B')
    releaseB()
    assert.equal(lock.inspect().held.length, 0)
    assert.equal(lock.inspect().queued.length, 0)
  })

  await check('互斥锁：release 幂等；重复释放不会放走别人的锁', async () => {
    const lock = createPortLock()
    const release = await lock.acquire(['port:31010'], { label: 'A' })
    release()
    release()
    assert.equal(lock.busy(), false)
    const releaseB = await lock.acquire(['port:31010'], { label: 'B' })
    release() // A 的重复释放不得影响 B
    assert.equal(lock.inspect().held[0].label, 'B', 'B 的锁不能被 A 的陈旧释放函数放掉')
    releaseB()
  })

  await check('互斥锁：多键申请半途失败要把已拿到的锁还回去（否则死锁）', async () => {
    const lock = createPortLock({ waitMs: 25 })
    const hold = await lock.acquire(['port:31001'], { label: '占用 31001 的人' })
    // 申请 [31001, 31002]：31001 拿不到 → 整体失败，31002 必须没有被占住
    await assert.rejects(() => lock.acquire(['port:31001', 'port:31002'], { label: '多人申请' }), /等锁超时/)
    const inspect = lock.inspect()
    assert.deepEqual(inspect.held.map(item => item.key), ['port:31001'], JSON.stringify(inspect.held))
    assert.equal(inspect.queued.length, 0)
    hold()
    // 现在 31002 应该是空闲的：能立刻拿到
    const releaseOther = await lock.acquire(['port:31002'], { label: 'later', waitMs: 10 })
    releaseOther()
  })

  await check('互斥锁：等待中取消（AbortSignal）→ 立刻失败且出队', async () => {
    const lock = createPortLock({ waitMs: 5_000 })
    const hold = await lock.acquire(['port:31011'], { label: 'holder' })
    const controller = new AbortController()
    const pending = lock.acquire(['port:31011'], { label: '被取消的人', signal: controller.signal })
    await delay(10)
    controller.abort()
    await assert.rejects(() => pending, /调用已被取消/)
    assert.equal(lock.inspect().queued.length, 0, '取消后不能留在队列里')
    assert.equal(lock.stats.aborted, 1)
    hold()
    assert.equal(lock.busy(), false)
  })

  await check('互斥锁：waitMs=0 视为不加锁；waitMs 默认值来自常量', async () => {
    const lock = createPortLock({ waitMs: 0 })
    const releaser = await lock.acquire(['port:31012'], { label: 'no-wait' })
    assert.equal(typeof releaser, 'function')
    assert.equal(lock.busy(), false, 'waitMs=0 不应真的占锁')
    assert.ok(DEFAULT_LOCK_WAIT_MS >= 60_000, '默认等锁超时至少要够一次下发起步')
    assert.equal(new PortLockRegistry().waitMs, DEFAULT_LOCK_WAIT_MS)
  })

  // -------------------------------------------------------------------------
  // 端到端：经 lib/tools.js 的接线
  // -------------------------------------------------------------------------

  /** 造一个会记录“并发进入次数”的假 MCP 客户端。 */
  function slowClient(workMs = 25) {
    const state = { active: 0, maxActive: 0, calls: [] }
    return {
      state,
      client: {
        async callToolText(name, args) {
          state.active++
          state.maxActive = Math.max(state.maxActive, state.active)
          state.calls.push({ name, args })
          await delay(workMs)
          state.active--
          return `${args.port ?? (args.ports ?? []).join(',')}  SW1  S5820V2  UP`
        }
      }
    }
  }

  await check('接线：同一端口的两个 h3c_run 在工具层被串行（不会交叠）', async () => {
    const { client, state } = slowClient()
    const tools = createH3cTools(client, { toolCallTimeoutMs: 1000, discovery: null, portLockEnabled: true })
    const run = tools.find(item => item.name === 'h3c_run')
    const signal = new AbortController().signal
    await Promise.all([
      run.execute({ port: 31008, command: 'display vrrp' }, { signal }),
      run.execute({ port: 31008, command: 'display clock' }, { signal })
    ])
    assert.equal(state.maxActive, 1, `同一端口必须串行，实到并发 ${state.maxActive}`)
    assert.equal(state.calls.length, 2)
  })

  await check('接线：不同端口的 h3c_run 仍然并行（别把并行作业一刀砍掉）', async () => {
    const { client, state } = slowClient()
    const tools = createH3cTools(client, { toolCallTimeoutMs: 1000, discovery: null })
    const run = tools.find(item => item.name === 'h3c_run')
    const signal = new AbortController().signal
    await Promise.all([
      run.execute({ port: 31005, command: 'display clock' }, { signal }),
      run.execute({ port: 31006, command: 'display clock' }, { signal }),
      run.execute({ port: 31007, command: 'display clock' }, { signal })
    ])
    assert.equal(state.maxActive, 3, `不同端口应并行，实到 ${state.maxActive}`)
  })

  await check('接线：等锁超时 → 工具给出可读失败（端口 31008 正被另一次下发占用）', async () => {
    const { client } = slowClient(300)
    const tools = createH3cTools(client, {
      toolCallTimeoutMs: 1000,
      discovery: null,
      portLockWaitMs: 50
    })
    const run = tools.find(item => item.name === 'h3c_run')
    const signal = new AbortController().signal
    const first = run.execute({ port: 31008, command: 'display vrrp' }, { signal })
    await delay(10)
    await assert.rejects(
      () => run.execute({ port: 31008, command: 'display clock' }, { signal }),
      /h3c_run 调用失败：等锁超时：控制台端口 31008 正被另一次调用占用/
    )
    await first
  })

  await check('接线：h3c_apply_plan 锁住计划文件里的端口（与 h3c_run 争同一端口）', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'dsh-h3clab-lock-plan-'))
    try {
      const planFile = join(dir, 'plan.json')
      writeFileSync(planFile, JSON.stringify({
        devices: [{ name: 'HQ-PE1', port: 31008, commands: ['sysname HQ-PE1'] }]
      }), 'utf8')
      const { client, state } = slowClient(300)
      const tools = createH3cTools(client, {
        toolCallTimeoutMs: 1000,
        discovery: null,
        portLockWaitMs: 50
      })
      const apply = tools.find(item => item.name === 'h3c_apply_plan')
      const run = tools.find(item => item.name === 'h3c_run')
      const signal = new AbortController().signal
      const pushing = apply.execute({ plan_json: planFile, dry_run: false }, { signal })
      await delay(10)
      await assert.rejects(
        () => run.execute({ port: 31008, command: 'display clock' }, { signal }),
        /等锁超时：控制台端口 31008 正被另一次调用占用/
      )
      await pushing
      assert.equal(state.maxActive, 1)
    }
    finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  await check('接线：portLockEnabled=false 可关掉锁（调试“锁是不是卡住了”时用）', async () => {
    const { client, state } = slowClient()
    const tools = createH3cTools(client, { toolCallTimeoutMs: 1000, discovery: null, portLockEnabled: false })
    const run = tools.find(item => item.name === 'h3c_run')
    const signal = new AbortController().signal
    await Promise.all([
      run.execute({ port: 31008, command: 'display clock' }, { signal }),
      run.execute({ port: 31008, command: 'display clock' }, { signal })
    ])
    assert.equal(state.maxActive, 2, '关掉锁后应当并行（确认开关真的生效）')
  })

  await check('接线：h3c_verify 按清单端口加锁', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'dsh-h3clab-lock-verify-'))
    try {
      const file = join(dir, 'checklist.json')
      writeFileSync(file, JSON.stringify({ checks: [{ id: 'C1', port: 31005, cmd: 'display clock' }] }), 'utf8')
      const { client, state } = slowClient(300)
      const tools = createH3cTools(client, { toolCallTimeoutMs: 1000, discovery: null, portLockWaitMs: 50 })
      const verify = tools.find(item => item.name === 'h3c_verify')
      const run = tools.find(item => item.name === 'h3c_run')
      const signal = new AbortController().signal
      const verifying = verify.execute({ checklist_json: file }, { signal })
      await delay(10)
      await assert.rejects(
        () => run.execute({ port: 31005, command: 'display clock' }, { signal }),
        /等锁超时：控制台端口 31005/
      )
      await verifying
      assert.equal(state.maxActive, 1)
    }
    finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })
}

/** 单独跑（node test/port-lock.test.mjs）时的极简 runner。 */
if (process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const results = []
  let failures = 0
  const check = async (name, body) => {
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
  const plugin = await import('../lib/index.js')
  await runPortLockTests(check, { plugin })
  console.log(`---\n${results.length - failures} 通过 / ${failures} 失败`)
  process.exitCode = failures === 0 ? 0 : 1
}
