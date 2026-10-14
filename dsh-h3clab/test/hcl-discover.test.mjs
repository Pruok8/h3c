/**
 * HCL 工程 / 控制台端口自动发现的离线测试。
 *
 * 用的都是**真机观察到的数据形状**（2026-10-14 本机）：
 *   - 窗口标题：`HCL - mylab20 [d:\dsh\net\lab20]`
 *   - netstat：`TCP 0.0.0.0:31001 ... LISTENING 17580`（同一端口在 0.0.0.0 与 127.0.0.1 各出现一次）
 *   - 控制台端口段 31001-31020，外加 HCL 自己的内部通道 18601（必须被启发式排除）
 *
 * 覆盖：标题解析 / netstat 解析 / 端口段选择 / 探测（假 runner）/ TTL 缓存 /
 *       多来源置信度排序 / 连错工程预警 / 反验（verified / suspect / unverified / unreachable）/
 *       以及经 lib/tools.js 的端到端接线。
 */
import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import {
  PORT_SOURCE_CONFIDENCE,
  PORT_SOURCES,
  createHclDiscovery,
  formatProjects,
  parseHclWindowTitle,
  parseNetTopology,
  parseNetstatListeningPorts,
  pickConsolePorts,
  planDevicePorts,
  probeHclProjects,
  verifyDeviceProbe
} from '../lib/hcl-discover.js'

/** 真机观察到的窗口标题。 */
const REAL_TITLE = 'HCL - mylab20 [d:\\dsh\\net\\lab20]'

/** 造一段 netstat 输出：同一端口在 0.0.0.0/127.0.0.1 各一行，另加别的 PID 与 ESTABLISHED。 */
function netstatFixture(pid, ports, extraPorts = [18601]) {
  const lines = []
  for (const port of [...ports, ...extraPorts]) {
    lines.push(`  TCP    0.0.0.0:${port}          0.0.0.0:0              LISTENING       ${pid}`)
    lines.push(`  TCP    127.0.0.1:${port}        0.0.0.0:0              LISTENING       ${pid}`)
  }
  lines.push('  TCP    127.0.0.1:18601        127.0.0.1:53722        ESTABLISHED     17580')
  lines.push('  TCP    0.0.0.0:30001          0.0.0.0:0              LISTENING       99999')
  return lines.join('\r\n')
}

/** 造两行窗口输出（两个工程）。 */
function windowsFixture() {
  return [
    '1234|HCL - lab4_ts [d:\\dsh\\net\\lab4]',
    '17580|HCL - mylab20 [d:\\dsh\\net\\lab20]'
  ].join('\r\n')
}

/** 假的外部命令执行器：powershell 给窗口，netstat 给端口表。 */
function fakeRunner({ windows = windowsFixture(), netstat = '' } = {}) {
  const calls = []
  const runner = (command, args) => {
    calls.push({ command, args })
    if (command === 'netstat')
      return netstat
    return windows
  }
  runner.calls = calls
  return runner
}

/** 两个工程的假探测结果。 */
function twoProjectDiscoveryProjects() {
  return [
    {
      pid: 1234,
      project: 'lab4_ts',
      path: 'd:\\dsh\\net\\lab4',
      title: 'HCL - lab4_ts [d:\\dsh\\net\\lab4]',
      listening: [18601, ...Array.from({ length: 10 }, (_, index) => 30001 + index)],
      ports: Array.from({ length: 10 }, (_, index) => 30001 + index),
      base: 30000,
      portCount: 10,
      note: '取最长端口段 30001-30010（10 个）'
    },
    {
      pid: 17580,
      project: 'mylab20',
      path: 'd:\\dsh\\net\\lab20',
      title: REAL_TITLE,
      listening: [18601, ...Array.from({ length: 20 }, (_, index) => 31001 + index)],
      ports: Array.from({ length: 20 }, (_, index) => 31001 + index),
      base: 31000,
      portCount: 20,
      note: '取最长端口段 31001-31020（20 个）'
    }
  ]
}

/** 造一个假发现器（不发任何外部命令）。 */
function fakeDiscovery(projects, options = {}) {
  let calls = 0
  const instance = createHclDiscovery({
    platform: 'win32',
    ...options,
    probe: () => {
      calls++
      return { ok: projects.length > 0, reason: projects.length > 0 ? '' : '没有工程', projects, probes: {} }
    }
  })
  instance.probeCallCount = () => calls
  return instance
}

/** 真机拓扑 fixture（存在才用，缺失就跳过相关断言）。 */
function realNetFile() {
  const candidate = resolve('D:\\DSH\\NET\\lab20\\mylab20.net')
  return existsSync(candidate) ? candidate : null
}

/**
 * 跑全部发现相关测试。
 * @param {(name: string, body: () => Promise<void>|void) => Promise<void>} check 断言记录器。
 * @param {object} deps `{ plugin }`。
 */
export async function runHclDiscoverTests(check, deps) {
  const { plugin } = deps

  await check('HCL 工程：窗口标题解析（含路径 / 无路径 / 非 HCL）', async () => {
    const parsed = parseHclWindowTitle(REAL_TITLE)
    assert.equal(parsed.project, 'mylab20')
    assert.equal(parsed.path, 'd:\\dsh\\net\\lab20')
    assert.equal(parseHclWindowTitle('HCL - lab4_ts').project, 'lab4_ts')
    assert.equal(parseHclWindowTitle('HCL - lab4_ts').path, '')
    assert.equal(parseHclWindowTitle('HCL - 我的 工程 [d:\\x y]').project, '我的 工程')
    assert.equal(parseHclWindowTitle('记事本'), null)
    assert.equal(parseHclWindowTitle(''), null)
    assert.equal(parseHclWindowTitle(undefined), null)
  })

  await check('HCL 工程：netstat 输出解析（去重 / 只认 LISTENING / 只认该 PID）', async () => {
    const text = netstatFixture(17580, [31001, 31002, 31003])
    assert.deepEqual(parseNetstatListeningPorts(text, 17580), [18601, 31001, 31002, 31003])
    assert.deepEqual(parseNetstatListeningPorts(text, 99999), [30001])
    assert.deepEqual(parseNetstatListeningPorts(text, 55555), [])
    assert.deepEqual(parseNetstatListeningPorts('', 17580), [])
  })

  await check('HCL 工程：端口段选择把 HCL 内部通道 18601 排除掉', async () => {
    const listening = [18601, ...Array.from({ length: 20 }, (_, index) => 31001 + index)]
    const picked = pickConsolePorts(listening)
    assert.equal(picked.ports.length, 20)
    assert.equal(picked.ports[0], 31001)
    assert.equal(picked.ports.at(-1), 31020)
    assert.equal(picked.base, 31000)
    assert.match(picked.reason, /忽略 18601/)
  })

  await check('HCL 工程：单设备工程也能选中（整千基址优先于内部端口）', async () => {
    assert.deepEqual(pickConsolePorts([18601, 35001]).ports, [35001])
    // 只剩孤立且非整千的端口时，判定为 HCL 内部通道，不硬凑一个“控制台端口”。
    const lonely = pickConsolePorts([18601])
    assert.deepEqual(lonely.ports, [])
    assert.equal(lonely.base, null)
    assert.match(lonely.reason, /内部通道/)
    assert.deepEqual(pickConsolePorts([]).ports, [])
  })

  await check('HCL 工程：探测两个工程各自的端口段（powershell + netstat）', async () => {
    const runner = fakeRunner({
      netstat: [
        netstatFixture(1234, Array.from({ length: 10 }, (_, index) => 30001 + index)),
        netstatFixture(17580, Array.from({ length: 20 }, (_, index) => 31001 + index))
      ].join('\r\n')
    })
    const result = probeHclProjects({ runner })
    assert.equal(result.ok, true, result.reason)
    assert.equal(result.projects.length, 2)
    const lab4 = result.projects.find(item => item.project === 'lab4_ts')
    const lab20 = result.projects.find(item => item.project === 'mylab20')
    assert.equal(lab4.base, 30000)
    assert.equal(lab4.ports[0], 30001)
    assert.equal(lab20.base, 31000)
    assert.equal(lab20.ports[0], 31001)
    assert.equal(lab20.ports.at(-1), 31020)
    assert.equal(lab20.pid, 17580)
    assert.equal(result.probes.windows, true)
    assert.equal(result.probes.netstat, true)
  })

  await check('HCL 工程：探测失败一律返回可读原因，绝不抛异常（非 Windows / 没开 HCL / 命令失败）', async () => {
    const boom = probeHclProjects({ runner: () => { throw new Error('powershell 不存在') } })
    assert.equal(boom.ok, false)
    assert.match(boom.reason, /列 HCL 工程窗口失败.*powershell 不存在/)
    assert.deepEqual(boom.projects, [])

    const noWindow = probeHclProjects({ runner: () => '' })
    assert.equal(noWindow.ok, false)
    assert.match(noWindow.reason, /未发现 SimwareClient 工程窗口/)

    const noPorts = probeHclProjects({ runner: (command) => (command === 'netstat' ? '' : windowsFixture()) })
    assert.equal(noPorts.ok, false)
    assert.match(noPorts.reason, /没有任何工程在监听控制台端口/)

    const netstatBoom = probeHclProjects({
      runner: (command) => {
        if (command === 'netstat')
          throw new Error('netstat 被禁用')
        return windowsFixture()
      }
    })
    assert.equal(netstatBoom.ok, false)
    assert.match(netstatBoom.reason, /netstat 执行失败/)

    const nonWindows = createHclDiscovery({ platform: 'linux', probe: () => { throw new Error('不该被调用') } })
    const skipped = nonWindows.discover()
    assert.equal(skipped.ok, false)
    assert.equal(skipped.enabled, false)
    assert.match(skipped.reason, /只在 Windows 可用/)
  })

  await check('HCL 工程：TTL 缓存（命中不重复探测；force / invalidate 能刷新）', async () => {
    let clock = 1_000
    const projects = twoProjectDiscoveryProjects()
    const discovery = createHclDiscovery({
      platform: 'win32',
      ttlMs: 10_000,
      now: () => clock,
      log: () => {},
      probe: () => ({ ok: true, reason: '', projects, probes: {} })
    })
    assert.equal(discovery.discover().cached, false)
    assert.equal(discovery.discover().cached, true, 'TTL 内应命中缓存')
    assert.equal(discovery.probeCount, 1)
    clock += 11_000
    assert.equal(discovery.discover().cached, false, 'TTL 过后应重新探测')
    assert.equal(discovery.probeCount, 2)
    discovery.discover({ force: true })
    assert.equal(discovery.probeCount, 3)
    discovery.invalidate()
    discovery.discover()
    assert.equal(discovery.probeCount, 4)
  })

  await check('HCL 工程：按工程名选端口（含 #N 取第 N 台；未命中会强制刷新一次）', async () => {
    const projects = twoProjectDiscoveryProjects()
    const discovery = fakeDiscovery(projects)
    const all = discovery.select('mylab20')
    assert.equal(all.ok, true)
    assert.deepEqual(all.ports.slice(0, 3), [31001, 31002, 31003])
    assert.equal(all.ports.length, 20)

    const third = discovery.select('mylab20#3')
    assert.equal(third.ok, true)
    assert.deepEqual(third.ports, [31003])
    assert.equal(third.port, 31003)

    const prefix = discovery.select('mylab')
    assert.equal(prefix.ok, true)
    assert.equal(prefix.project.project, 'mylab20')
    assert.match(prefix.reason, /前缀唯一匹配/)

    const tooMany = discovery.select('mylab20#99')
    assert.equal(tooMany.ok, false)
    assert.match(tooMany.reason, /只发现 20 个控制台端口/)

    const before = discovery.probeCallCount()
    const missing = discovery.select('nosuchlab')
    assert.equal(missing.ok, false)
    assert.match(missing.reason, /没有名为 "nosuchlab" 的 HCL 工程/)
    assert.equal(discovery.probeCallCount(), before + 1, '未命中应强制刷新一次（工程可能是刚打开的）')
  })

  await check('HCL 工程：按 netFile 目录匹配工程；不匹配时不乱认', async () => {
    const projects = twoProjectDiscoveryProjects()
    const discovery = fakeDiscovery(projects)
    const hit = discovery.selectByNetFile('D:\\DSH\\NET\\lab20\\mylab20.net')
    assert.equal(hit.project.project, 'mylab20')
    const miss = discovery.selectByNetFile('D:\\DSH\\NET\\lab9\\x.net')
    assert.equal(miss.project, null)
    assert.match(miss.reason, /没有工程的路径与/)
  })

  await check('HCL 工程：.net 拓扑解析与 server.py 同构（[[型号 名字]] + device_id）', async () => {
    const text = [
      'version = 5.10.3',
      '[vbox 127.0.0.1:16600]',
      '    udp = 10000',
      '    [[MSR36-20 BR-PE]]',
      '        device_id = 1',
      '        GE_0/1 = ASBR GE_0/2',
      '    [[NOTE 说明]]',
      '        device_id = 99',
      '    [[S5820V2-54QS-GE BR-SW1]]',
      '        device_id = 2'
    ].join('\n')
    const parsed = parseNetTopology(text)
    assert.equal(parsed.base, 30000)
    assert.deepEqual(parsed.devices.map(item => item.name), ['BR-PE', 'BR-SW1'])
    assert.equal(parsed.devices[0].port, 30001)
    assert.equal(parsed.devices[0].model, 'MSR36-20')
    assert.equal(parsed.devices[1].port, 30002)

    const netFile = realNetFile()
    if (netFile === null) {
      console.log('        （跳过真机 .net 断言：找不到 lab20\\mylab20.net）')
      return
    }
    const real = parseNetTopology(readFileSync(netFile, 'utf8'))
    assert.equal(real.devices.length, 20, `真机 mylab20.net 应解析出 20 台设备，实到 ${real.devices.length}`)
    assert.equal(real.devices[0].name, 'BR-PE')
    assert.equal(real.devices[0].port, 30001)
  })

  await check('HCL 工程：端口来源置信度排序（显式 > netstat 实测 > device_id 推算 > 兜底）', async () => {
    assert.ok(PORT_SOURCE_CONFIDENCE[PORT_SOURCES.EXPLICIT] > PORT_SOURCE_CONFIDENCE[PORT_SOURCES.NETSTAT])
    assert.ok(PORT_SOURCE_CONFIDENCE[PORT_SOURCES.NETSTAT] > PORT_SOURCE_CONFIDENCE[PORT_SOURCES.INFERRED])
    assert.ok(PORT_SOURCE_CONFIDENCE[PORT_SOURCES.INFERRED] > PORT_SOURCE_CONFIDENCE[PORT_SOURCES.FALLBACK])

    const discovery = fakeDiscovery(twoProjectDiscoveryProjects())
    const topology = parseNetTopology([
      '[[MSR36-20 BR-PE]]',
      '    device_id = 1',
      '[[MSR36-20 ASBR]]',
      '    device_id = 4'
    ].join('\n'))
    const expected = {}
    for (const item of topology.devices)
      expected[String(item.port)] = item.name

    const explicit = planDevicePorts({ explicitPorts: [2001], discovery, netFile: 'D:\\other\\x.net', topology: { ...topology, expected } })
    assert.equal(explicit.source, PORT_SOURCES.EXPLICIT)
    assert.deepEqual(explicit.ports, [2001])

    // netFile 指向 mylab20 → netstat 实测（31001+）压过 30000+id 推算。
    const byNetstat = planDevicePorts({ discovery, netFile: 'D:\\DSH\\NET\\lab20\\mylab20.net', topology: { ...topology, expected } })
    assert.equal(byNetstat.source, PORT_SOURCES.NETSTAT)
    assert.equal(byNetstat.ports[0], 31001)
    assert.equal(byNetstat.ports.length, 20)
    assert.equal(byNetstat.project.project, 'mylab20')
    assert.equal(byNetstat.expected['31001'], 'BR-PE', '反验期望值要跟着实际端口（工程基址 31000）走')

    // 没有发现器 → 退回 device_id 推算（并标出这是候选）。
    const inferred = planDevicePorts({ discovery: null, netFile: 'D:\\x\\x.net', topology: { ...topology, expected } })
    assert.equal(inferred.source, PORT_SOURCES.INFERRED)
    assert.deepEqual(inferred.ports, [30001, 30004])
    assert.match(inferred.candidates[0].reason, /只是候选/)

    // netFile 与任何已打开工程都不符 → 明确警告，不拿别人的端口顶替。
    // 注意：只有在**推算出来的端口也不属于任何已打开工程**时才判“疑似连错工程”
    // （这里是 mylab20 的 31001+，而推算得到 30001/30004 → 真的不在里面）。
    const onlyLab20 = fakeDiscovery(twoProjectDiscoveryProjects().filter(item => item.project === 'mylab20'))
    const mismatch = planDevicePorts({ discovery: onlyLab20, netFile: 'D:\\DSH\\NET\\lab9\\x.net', topology: { ...topology, expected } })
    assert.equal(mismatch.source, PORT_SOURCES.INFERRED)
    assert.equal(mismatch.suspectedWrongProject, true, '推算端口不在任何已发现工程段内 → 疑似连错工程')
    assert.match(mismatch.note, /路径都不符/)
    assert.match(mismatch.note, /很可能连到另一个工程/)

    // 推算端口恰好落在**另一个**已打开工程（lab4_ts 30001+）里时不算“疑似”，
    // 但仍要提示 netFile 与已打开工程路径不符 —— 让人自己确认到底该连哪个工程。
    const both = planDevicePorts({ discovery, netFile: 'D:\\DSH\\NET\\lab9\\x.net', topology: { ...topology, expected } })
    assert.equal(both.suspectedWrongProject, false)
    assert.match(both.note, /路径都不符/)
  })

  await check('HCL 工程：反验（verified / suspect / unverified / unreachable）', async () => {
    const plan = {
      source: PORT_SOURCES.NETSTAT,
      project: { project: 'mylab20' },
      expected: { 31001: 'BR-PE', 31002: 'BR-SW1' },
      suspectedWrongProject: false,
      note: ''
    }
    const ok = verifyDeviceProbe('31001  BR-PE  MSR36-20  UP\n31002  BR-SW1  S5820V2-54QS-GE  UP', plan)
    assert.equal(ok.status, 'verified')
    assert.equal(ok.up, 2)
    assert.match(ok.line, /^\[verified\]/)
    assert.match(ok.line, /工程 mylab20/)

    const wrong = verifyDeviceProbe('31001  HQ-PE1  MSR36-20  UP', plan)
    assert.equal(wrong.status, 'suspect')
    assert.equal(wrong.mismatches.length, 1)
    assert.match(wrong.line, /疑似连错工程/)
    assert.match(wrong.line, /31001 实测 HQ-PE1 ≠ 拓扑 BR-PE/)

    const unknown = verifyDeviceProbe('31001  BR-PE  MSR36-20  UP', { ...plan, expected: null })
    assert.equal(unknown.status, 'unverified')
    assert.match(unknown.line, /连上了”不等于“连对了/)

    const dead = verifyDeviceProbe('连不上的端口（1 个）：\n  31001: timeout', plan)
    assert.equal(dead.status, 'unreachable')
    assert.match(dead.line, /没有一台设备响应/)

    const suspectByPort = verifyDeviceProbe('30001  BR-PE  MSR36-20  UP', {
      ...plan,
      source: PORT_SOURCES.INFERRED,
      suspectedWrongProject: true,
      note: '⚠ 端口 30001-30010 是按「30000+device_id」推算的'
    })
    assert.equal(suspectByPort.status, 'suspect')
    assert.match(suspectByPort.line, /不在任何已发现 HCL 工程的端口段内/)
  })

  await check('HCL 工程：formatProjects 一行一个工程（含端口段与路径）', async () => {
    const text = formatProjects(twoProjectDiscoveryProjects())
    assert.match(text, /lab4_ts\s+pid=1234\s+端口 30001-30010（10 个）/m)
    assert.match(text, /mylab20\s+pid=17580\s+端口 31001-31020（20 个）/m)
    assert.match(text, /路径 d:\\dsh\\net\\lab20/)
    assert.equal(formatProjects([]), '（未发现 HCL 工程）')
  })

  // -------------------------------------------------------------------------
  // 端到端：经 lib/tools.js 的接线
  // -------------------------------------------------------------------------
  const { createH3cTools } = plugin

  /** 造一个记录参数的假 MCP 客户端。 */
  function probeTools(responseText = '31003  BR-SW2  S5820V2-54QS-GE  UP') {
    const seen = []
    const client = {
      async callToolText(name, args) {
        seen.push({ name, args })
        return responseText
      }
    }
    return { client, seen }
  }

  await check('h3c_devices：project="mylab20#3" 变成明确 ports，且 project 不会漏给服务器', async () => {
    const { client, seen } = probeTools()
    const tools = createH3cTools(client, {
      toolCallTimeoutMs: 1000,
      discovery: fakeDiscovery(twoProjectDiscoveryProjects())
    })
    const devices = tools.find(item => item.name === 'h3c_devices')
    const value = await devices.execute({ project: 'mylab20#3' }, { signal: new AbortController().signal })
    assert.equal(seen.length, 1)
    assert.deepEqual(seen[0].args.ports, [31003], `实到 ${JSON.stringify(seen[0].args)}`)
    assert.ok(!('project' in seen[0].args), 'project 是插件自己的参数，不能漏给 MCP 服务器')
    assert.match(value.text, /HCL 工程 mylab20/)
    const schema = devices.parameters ?? devices.inputSchema
    assert.ok(Object.keys(schema.properties ?? schema).includes('project'))
  })

  await check('h3c_devices：配了 netFile 时自动按工程端口段探测 + 反验主机名（连上了≠连对了）', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'dsh-h3clab-hclnet-'))
    try {
      const netFile = join(dir, 'mylab20.net')
      writeFileSync(netFile, [
        '[[MSR36-20 BR-PE]]',
        '    device_id = 1',
        '[[S5820V2-54QS-GE BR-SW1]]',
        '    device_id = 2'
      ].join('\n'), 'utf8')
      // 工程窗口的路径 = 这个临时目录，才能验证“按 netFile 目录匹配工程”这条路。
      const projects = twoProjectDiscoveryProjects().map(item =>
        item.project === 'mylab20' ? { ...item, path: dir } : item)

      const good = probeTools('31001  BR-PE  MSR36-20  UP\n31002  BR-SW1  S5820V2-54QS-GE  UP')
      const tools = createH3cTools(good.client, {
        toolCallTimeoutMs: 1000,
        netFile,
        discovery: fakeDiscovery(projects)
      })
      const devices = tools.find(item => item.name === 'h3c_devices')
      const value = await devices.execute({}, { signal: new AbortController().signal })
      assert.equal(good.seen[0].args.ports.length, 20)
      assert.equal(good.seen[0].args.ports[0], 31001)
      assert.match(value.text, /端口来源：netstat 实测/)
      assert.match(value.text, /工程 mylab20/)
      assert.match(value.text, /\[verified\]/, `主机名与拓扑一致应判 verified：${value.text}`)

      // 反验失败：连上的设备主机名和拓扑对不上 → 明确报“疑似连错工程”。
      const bad = probeTools('31001  HQ-CORE1  S6850  UP')
      const badTools = createH3cTools(bad.client, {
        toolCallTimeoutMs: 1000,
        netFile,
        discovery: fakeDiscovery(projects)
      })
      const badValue = await badTools.find(item => item.name === 'h3c_devices')
        .execute({}, { signal: new AbortController().signal })
      assert.match(badValue.text, /\[suspect\]/)
      assert.match(badValue.text, /疑似连错工程/)
      assert.match(badValue.text, /31001 实测 HQ-CORE1 ≠ 拓扑 BR-PE/)
    }
    finally {
      rmSync(dir, { recursive: true, force: true })
    }
  })

  await check('h3c_devices：工程名识别不了 → 可读错误（不崩、不静默猜端口）', async () => {
    const { client, seen } = probeTools()
    const tools = createH3cTools(client, {
      toolCallTimeoutMs: 1000,
      discovery: fakeDiscovery(twoProjectDiscoveryProjects())
    })
    const devices = tools.find(item => item.name === 'h3c_devices')
    await assert.rejects(
      () => devices.execute({ project: 'nosuch' }, { signal: new AbortController().signal }),
      /h3c_devices 调用失败：.*没有名为 "nosuch" 的 HCL 工程/
    )
    assert.equal(seen.length, 0, '识别不了就不该去打设备')
  })

  await check('h3c_devices：拿不到工程信息（HCL 没开）时仍能按显式 ports 工作', async () => {
    const { client, seen } = probeTools('2001  SW1  S5820V2  UP')
    const tools = createH3cTools(client, {
      toolCallTimeoutMs: 1000,
      discovery: fakeDiscovery([])
    })
    const devices = tools.find(item => item.name === 'h3c_devices')
    const value = await devices.execute({ ports: [2001] }, { signal: new AbortController().signal })
    assert.deepEqual(seen[0].args.ports, [2001])
    // 发现不到工程时不能编造工程名，也不能冒出“来源：netstat 实测”这类假结论。
    assert.doesNotMatch(value.text, /工程\s+\S+（pid|端口来源：netstat|已发现 HCL 工程/)
    assert.match(value.text, /\[unverified\]/)
  })

  await check('h3c_devices：未指定 ports 且没有 netFile 时，提示“会按 30000+id 推导”并列出可用工程', async () => {
    const { client, seen } = probeTools('30001  BR-PE  MSR36-20  UP')
    const tools = createH3cTools(client, {
      toolCallTimeoutMs: 1000,
      discovery: fakeDiscovery(twoProjectDiscoveryProjects())
    })
    const devices = tools.find(item => item.name === 'h3c_devices')
    const value = await devices.execute({}, { signal: new AbortController().signal })
    assert.equal(seen[0].args.ports, undefined, '不确定时不要替服务器猜端口')
    assert.match(value.text, /未能确定端口/)
    assert.match(value.text, /mylab20\s+pid=17580\s+端口 31001-31020/)
    assert.match(value.text, /project: "<工程名>"/)
  })
}

/** 单独跑（node test/hcl-discover.test.mjs）时的极简 runner。 */
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
  await runHclDiscoverTests(check, { plugin })
  console.log(`---\n${results.length - failures} 通过 / ${failures} 失败`)
  process.exitCode = failures === 0 ? 0 : 1
}
