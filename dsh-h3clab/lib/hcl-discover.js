/**
 * dsh-h3clab —— HCL 工程 / 控制台端口**自动发现**（Windows 专用，只读）。
 *
 * 为什么必须有这一层：
 *   HCL **每个工程窗口分配一个不同的端口基址**。实测（本机 2026-10-14）：
 *   先开的 `lab4_ts` 是 30001+，后开的 `mylab20` 是 **31001+**。而既有的
 *   “控制台端口 = 30000 + device_id”惯例会**连到另一个工程的设备上**——
 *   看起来一切正常（能连上、能敲命令），实际改的是别人的实验。
 *   本次为定位这一点花了 20 分钟人肉排查，所以做成自动探测。
 *
 * 探测原理（两步，全部只读）：
 *   1. `Get-Process SimwareClient` 拿到每个工程窗口：
 *      `MainWindowTitle` 形如 `HCL - mylab20 [d:\dsh\net\lab20]`。
 *   2. `netstat -ano -p TCP` 找出**该 PID 正在 LISTEN 的 TCP 端口**：
 *      其中成段的连续端口就是该工程的控制台端口（31001…31020）。
 *      （HCL 自己 GUI 用的 18601 之类的孤立端口会被启发式规则排除。）
 *
 * 设计约束：
 *   - **只依赖 node 内置模块**，不引入第三方依赖；
 *   - 非 Windows 或探测失败时**绝不抛异常**，一律返回 `{ ok: false, reason }`，
 *     调用方（lib/tools.js）据此回退到原有配置逻辑；
 *   - 结果带 TTL 缓存，并在日志里留痕（`discover()` 每次刷新都记一行）。
 */

import { execFileSync } from 'node:child_process'

/** 承载 HCL 工程窗口的进程名。 */
export const HCL_PROCESS_NAME = 'SimwareClient'
/** 探测结果缓存时长（毫秒）。短一些：工程开关随时会变，但也不必每次调用都探。 */
export const DEFAULT_DISCOVERY_TTL_MS = 15_000
/** 单次外部命令（powershell / netstat）超时。探测失败要快速回退，不能拖住工具调用。 */
export const DEFAULT_PROBE_TIMEOUT_MS = 8_000
/**
 * 控制台端口的下限。
 * HCL 的工程基址是 30000/31000 这类整千值，端口一定 ≥ 30001；
 * 而 HCL 客户端内部 GUI 通道（实测 18601）和系统端口都在这之下，直接排除。
 */
export const MIN_CONSOLE_PORT = 2000
/** 外部命令输出的最大缓冲（netstat 全表在 Windows 上可到几百 KB）。 */
const MAX_COMMAND_BUFFER = 16 * 1024 * 1024

/** 把任意抛出物转成一行可读文本（探测层绝不把异常抛给调用方）。 */
function describeError(error) {
  if (error instanceof Error)
    return error.message
  if (typeof error === 'string')
    return error
  try {
    return JSON.stringify(error)
  }
  catch {
    return String(error)
  }
}

/**
 * 解析 HCL 工程窗口标题。
 *
 * 两种形态都要认（标题里工程名可能带空格，路径可能不存在）：
 *   `HCL - mylab20 [d:\dsh\net\lab20]`
 *   `HCL - mylab20`
 * @param {unknown} title 窗口标题。
 * @returns {{project: string, path: string, title: string} | null} 解析结果；不是 HCL 工程窗口返回 null。
 */
export function parseHclWindowTitle(title) {
  if (typeof title !== 'string')
    return null
  const text = title.trim()
  if (text === '')
    return null
  // 必须能以 HCL 开头（HCL 5.x 的标题前缀是 "HCL - "）。
  const match = /^HCL\s*[-–—]\s*(.+)$/i.exec(text)
  if (match === null)
    return null
  let rest = match[1].trim()
  let path = ''
  // 取**最后一组**方括号当路径：工程名自身理论上可能含方括号。
  const bracket = /\[([^\]]*)\]\s*$/.exec(rest)
  if (bracket !== null) {
    path = bracket[1].trim()
    rest = rest.slice(0, bracket.index).trim()
  }
  if (rest === '')
    return null
  return { project: rest, path, title: text }
}

/**
 * 从 `netstat -ano` 输出里挑出**指定 PID 正在 LISTEN 的 TCP 端口**。
 *
 * 只认 `LISTENING` 行；`0.0.0.0:31001` / `[::]:31001` / `127.0.0.1:31001`
 * 会去重成一个 31001。
 * @param {string} text netstat 输出。
 * @param {number|string} pid 目标进程号。
 * @returns {number[]} 升序去重后的端口。
 */
export function parseNetstatListeningPorts(text, pid) {
  const wanted = String(pid).trim()
  const ports = new Set()
  if (typeof text !== 'string' || wanted === '')
    return []
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (line === '' || !/LISTENING/i.test(line))
      continue
    // 协议 本地地址 外部地址 状态 PID
    const columns = line.split(/\s+/)
    if (columns.length < 5)
      continue
    if (columns[columns.length - 1] !== wanted)
      continue
    const local = columns[1]
    const colon = local.lastIndexOf(':')
    if (colon < 0)
      continue
    const port = Number(local.slice(colon + 1))
    if (Number.isInteger(port) && port >= 1 && port <= 65535)
      ports.add(port)
  }
  return [...ports].sort((left, right) => left - right)
}

/**
 * 把端口列表切成**连续段**（后一个 = 前一个 + 1）。
 * @param {number[]} rawPorts 端口（不必有序）。
 * @returns {number[][]} 每段升序；按段内首端口升序返回。
 */
export function contiguousRuns(rawPorts) {
  const ports = [...new Set(
    (Array.isArray(rawPorts) ? rawPorts : [])
      .map(item => Number(item))
      .filter(item => Number.isInteger(item) && item >= 1 && item <= 65535)
  )].sort((left, right) => left - right)
  const runs = []
  for (const port of ports) {
    const last = runs.at(-1)
    if (last !== undefined && port === last.at(-1) + 1)
      last.push(port)
    else
      runs.push([port])
  }
  return runs
}

/**
 * 从一个 PID 的全部监听端口里挑出“最像工程控制台端口”的那一段。
 *
 * 启发式（按优先级）：
 *   1. 丢掉 < MIN_CONSOLE_PORT 的端口（HCL 内部 GUI 通道、系统服务）；
 *   2. **段长优先**（≥2 的段优先于单端口段）——控制台端口一定成段；
 *   3. 段首减一（或段首本身）是整千 → 优先（30000/31000 这类工程基址）；
 *   4. 基址大的优先（后开的工程基址更大，也更可能是当前在用的）。
 *
 * 实测样本（mylab20）：`{31001…31020, 18601}` → 返回 `31001…31020`。
 * @param {number[]} rawPorts 该进程 LISTEN 的端口。
 * @param {object} [options]
 * @param {number} [options.minPort] 覆盖端口下限。
 * @returns {{ports: number[], base: number|null, reason: string}} 选中的端口段与理由。
 */
export function pickConsolePorts(rawPorts, options = {}) {
  const minPort = Number.isFinite(options.minPort) ? Number(options.minPort) : MIN_CONSOLE_PORT
  const candidates = (Array.isArray(rawPorts) ? rawPorts : [])
    .map(item => Number(item))
    .filter(item => Number.isInteger(item) && item >= 1 && item <= 65535)
  const usable = candidates.filter(item => item >= minPort)
  const allRuns = contiguousRuns(usable).map(run => ({
    run,
    length: run.length,
    base: run[0] - 1,
    roundBase: (run[0] - 1) % 1000 === 0 || run[0] % 1000 === 0
  }))
  // 形态过滤：控制台端口一定“成段”或“基址是整千”。
  // 实测里 HCL 客户端自己的内部 GUI 通道就是 18601 这种**孤立且非整千**的端口，
  // 必须排除；宁可报“没发现控制台端口段”，也不要把内部通道当成设备控制台。
  const runs = allRuns.filter(item => item.length >= 2 || item.roundBase)
  if (runs.length === 0) {
    const lonely = allRuns.flatMap(item => item.run)
    return {
      ports: [],
      base: null,
      reason: usable.length === 0
        ? '该进程没有监听端口'
        : `只看到孤立的非整千端口（${lonely.join('、')}），判定为 HCL 客户端内部通道，不是控制台端口段`
    }
  }
  runs.sort((left, right) => {
    const leftMulti = left.length >= 2 ? 1 : 0
    const rightMulti = right.length >= 2 ? 1 : 0
    if (leftMulti !== rightMulti)
      return rightMulti - leftMulti
    if (left.length !== right.length)
      return right.length - left.length
    if (left.roundBase !== right.roundBase)
      return Number(right.roundBase) - Number(left.roundBase)
    return right.base - left.base
  })
  const best = runs[0]
  const dropped = usable.filter(port => !best.run.includes(port))
  const reason = dropped.length === 0
    ? `唯一端口段 ${best.run[0]}-${best.run.at(-1)}`
    : `取最长端口段 ${best.run[0]}-${best.run.at(-1)}（${best.length} 个）；忽略 ${dropped.sort((a, b) => a - b).join('、')}`
  return { ports: best.run, base: best.run[0] - 1, reason }
}

/** 默认外部命令执行器（可注入以便测试）。 */
export function defaultCommandRunner(command, args, options = {}) {
  const text = execFileSync(command, args, {
    encoding: 'utf8',
    windowsHide: true,
    timeout: options.timeoutMs ?? DEFAULT_PROBE_TIMEOUT_MS,
    maxBuffer: MAX_COMMAND_BUFFER,
    stdio: ['ignore', 'pipe', 'pipe']
  })
  return typeof text === 'string' ? text : String(text ?? '')
}

/** 列出工程窗口的 PowerShell 脚本（输出 `pid|标题`，一行一个）。 */
const LIST_WINDOWS_SCRIPT = [
  "$ErrorActionPreference='SilentlyContinue';",
  'try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {};',
  'Get-Process -Name ' + HCL_PROCESS_NAME,
  '| Where-Object { $_.MainWindowTitle }',
  '| ForEach-Object { "$($_.Id)|$($_.MainWindowTitle)" }'
].join(' ')

/**
 * 真正去 Windows 上探测一次（不缓存）。
 *
 * 任何一步失败都返回 `{ ok: false, reason }`，绝不抛异常。
 * @param {object} [options]
 * @param {(command: string, args: string[], options?: object) => string} [options.runner] 命令执行器。
 * @param {number} [options.timeoutMs] 单条命令超时。
 * @param {string} [options.powershell] powershell 可执行文件名（默认 `powershell.exe`）。
 * @returns {{ok: boolean, reason: string, projects: Array<object>, probes: object}}
 */
export function probeHclProjects(options = {}) {
  const runner = typeof options.runner === 'function' ? options.runner : defaultCommandRunner
  const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : DEFAULT_PROBE_TIMEOUT_MS
  const shell = typeof options.powershell === 'string' && options.powershell !== '' ? options.powershell : 'powershell.exe'
  const probes = { windows: false, netstat: false }

  let windowText = ''
  try {
    windowText = runner(shell, ['-NoProfile', '-NonInteractive', '-Command', LIST_WINDOWS_SCRIPT], { timeoutMs })
    probes.windows = true
  }
  catch (error) {
    return { ok: false, reason: `列 HCL 工程窗口失败（${shell}）：${describeError(error)}`, projects: [], probes }
  }

  const windows = []
  for (const line of String(windowText).split(/\r?\n/)) {
    const text = line.trim()
    if (text === '')
      continue
    const bar = text.indexOf('|')
    if (bar <= 0)
      continue
    const pid = Number(text.slice(0, bar).trim())
    if (!Number.isInteger(pid) || pid <= 0)
      continue
    const parsed = parseHclWindowTitle(text.slice(bar + 1))
    if (parsed === null)
      continue
    windows.push({ pid, ...parsed })
  }
  if (windows.length === 0)
    return { ok: false, reason: `未发现 ${HCL_PROCESS_NAME} 工程窗口（HCL 可能没开，或只开了工程管理界面）`, projects: [], probes }

  let netstatText = ''
  try {
    netstatText = runner('netstat', ['-ano', '-p', 'TCP'], { timeoutMs })
    probes.netstat = true
  }
  catch (error) {
    return { ok: false, reason: `netstat 执行失败：${describeError(error)}`, projects: [], probes }
  }

  const projects = windows.map((item) => {
    const listening = parseNetstatListeningPorts(netstatText, item.pid)
    const picked = pickConsolePorts(listening, options)
    return {
      pid: item.pid,
      project: item.project,
      path: item.path,
      title: item.title,
      listening,
      ports: picked.ports,
      base: picked.base,
      portCount: picked.ports.length,
      note: picked.reason
    }
  })
  const withPorts = projects.filter(item => item.portCount > 0)
  if (withPorts.length === 0)
    return { ok: false, reason: '找到 HCL 工程窗口，但没有任何工程在监听控制台端口（设备可能没开机）', projects, probes }
  return { ok: true, reason: '', projects, probes }
}

/** 把探测结果整理成一行行的文本（日志与工具提示共用）。 */
export function formatProjects(projects) {
  if (!Array.isArray(projects) || projects.length === 0)
    return '（未发现 HCL 工程）'
  return projects.map((item) => {
    const span = item.portCount > 0 ? `${item.ports[0]}-${item.ports.at(-1)}（${item.portCount} 个）` : '无监听端口'
    const where = item.path === '' ? '' : `  路径 ${item.path}`
    return `${item.project}  pid=${item.pid}  端口 ${span}  基址 ${item.base ?? '?'}${where}`
  }).join('\n')
}

/** 规范化路径，便于跨分隔符/大小写比较。 */
function normalizePath(value) {
  if (typeof value !== 'string')
    return ''
  return value.trim().replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase()
}

/**
 * 按工程名匹配（大小写不敏感）。
 * 依次尝试：精确 → 唯一前缀 → 唯一包含。
 * @param {Array<object>} projects 探测结果。
 * @param {string} query 工程名。
 * @returns {{project: object|null, reason: string}}
 */
export function matchProjectByName(projects, query) {
  const wanted = typeof query === 'string' ? query.trim() : ''
  const list = Array.isArray(projects) ? projects : []
  if (wanted === '')
    return { project: null, reason: '工程名为空' }
  const needle = wanted.toLowerCase()
  const exact = list.filter(item => String(item.project).toLowerCase() === needle)
  if (exact.length === 1)
    return { project: exact[0], reason: '精确匹配工程名' }
  if (exact.length > 1)
    return { project: null, reason: `有 ${exact.length} 个同名工程窗口：${exact.map(item => `pid=${item.pid}`).join('、')}` }
  const prefix = list.filter(item => String(item.project).toLowerCase().startsWith(needle))
  if (prefix.length === 1)
    return { project: prefix[0], reason: '前缀唯一匹配' }
  const contains = list.filter(item => String(item.project).toLowerCase().includes(needle))
  if (contains.length === 1)
    return { project: contains[0], reason: '包含匹配' }
  const ambiguous = prefix.length > 1 ? prefix : contains
  if (ambiguous.length > 1)
    return { project: null, reason: `工程名 "${wanted}" 不唯一，匹配到：${ambiguous.map(item => item.project).join('、')}` }
  return { project: null, reason: `没有名为 "${wanted}" 的 HCL 工程；已发现：${list.map(item => item.project).join('、') || '（无）'}` }
}

/**
 * 按拓扑文件路径匹配工程（用 `.net` 所在目录去比工程窗口标题里的路径）。
 * 互相包含都算命中，取匹配最长的那个（更具体）。
 * @param {Array<object>} projects 探测结果。
 * @param {string} netFile 拓扑文件路径。
 * @returns {{project: object|null, reason: string}}
 */
export function matchProjectByPath(projects, netFile) {
  const target = normalizePath(netFile)
  const list = Array.isArray(projects) ? projects : []
  if (target === '')
    return { project: null, reason: '未配置 netFile' }
  const dir = target.replace(/\\[^\\]*$/, '')
  let best = null
  for (const item of list) {
    const projectPath = normalizePath(item.path)
    if (projectPath === '')
      continue
    const hit = dir === projectPath || dir.startsWith(`${projectPath}\\`) || projectPath.startsWith(`${dir}\\`)
    if (!hit)
      continue
    if (best === null || projectPath.length > normalizePath(best.path).length)
      best = item
  }
  if (best === null)
    return { project: null, reason: `没有工程的路径与 ${netFile} 相符；已发现：${list.map(item => `${item.project}${item.path === '' ? '' : `（${item.path}）`}`).join('、') || '（无）'}` }
  return { project: best, reason: `按 netFile 目录匹配到工程 ${best.project}` }
}

/**
 * 解析 `project` 参数：`mylab20`（整段端口）或 `mylab20#3`（第 3 个端口）。
 * @param {Array<object>} projects 探测结果。
 * @param {string} query 参数值。
 * @returns {{ok: boolean, reason: string, project: object|null, ports: number[], port: number|null}}
 */
export function resolveProjectQuery(projects, query) {
  const text = typeof query === 'string' ? query.trim() : ''
  const hash = text.lastIndexOf('#')
  const name = hash >= 0 ? text.slice(0, hash).trim() : text
  const indexText = hash >= 0 ? text.slice(hash + 1).trim() : ''
  const matched = matchProjectByName(projects, name)
  if (matched.project === null)
    return { ok: false, reason: matched.reason, project: null, ports: [], port: null }
  const project = matched.project
  if (indexText === '')
    return { ok: true, reason: matched.reason, project, ports: [...project.ports], port: project.ports[0] ?? null }
  const index = Number(indexText)
  if (!Number.isInteger(index) || index < 1) {
    return { ok: false, reason: `"#${indexText}" 不是合法的设备序号（应为 1..${project.ports.length || 0}）`, project, ports: [], port: null }
  }
  if (index > project.ports.length) {
    return {
      ok: false,
      reason: `工程 ${project.project} 只发现 ${project.ports.length} 个控制台端口（${project.ports.join('、') || '无'}），没有第 ${index} 个`,
      project,
      ports: [],
      port: null
    }
  }
  return { ok: true, reason: `${matched.reason}，第 ${index} 个端口`, project, ports: [project.ports[index - 1]], port: project.ports[index - 1] }
}

/**
 * 造一个带 TTL 缓存、带日志的工程发现器。
 *
 * `discover()` 永不抛异常：不是 Windows、命令不存在、HCL 没开、输出格式变了……
 * 一律 `{ ok: false, reason }`，调用方回退到配置逻辑。
 * @param {object} [options]
 * @param {string} [options.platform] 平台（默认 `process.platform`）。
 * @param {number} [options.ttlMs] 缓存时长。
 * @param {number} [options.timeoutMs] 单条外部命令超时。
 * @param {(level: string, message: string) => void} [options.log] 日志出口。
 * @param {(options: object) => object} [options.probe] 探测实现（测试注入点）。
 * @param {() => number} [options.now] 时钟（测试注入点）。
 */
export function createHclDiscovery(options = {}) {
  const platform = typeof options.platform === 'string' && options.platform !== '' ? options.platform : process.platform
  const ttlMs = Number.isFinite(options.ttlMs) ? Math.max(0, Number(options.ttlMs)) : DEFAULT_DISCOVERY_TTL_MS
  const timeoutMs = Number.isFinite(options.timeoutMs) ? Number(options.timeoutMs) : DEFAULT_PROBE_TIMEOUT_MS
  const log = typeof options.log === 'function' ? options.log : () => {}
  const probe = typeof options.probe === 'function' ? options.probe : probeHclProjects
  const now = typeof options.now === 'function' ? options.now : Date.now
  const enabled = platform === 'win32'

  let cache = null
  let probeCount = 0

  const writeLog = (level, message) => {
    try {
      log(level, message)
    }
    catch {
      /* 日志绝不影响探测 */
    }
  }

  /** 跑一次探测（或命中缓存），结果里额外带 at / cached / enabled。 */
  const discover = (runOptions = {}) => {
    if (!enabled) {
      return {
        ok: false,
        reason: `HCL 工程探测只在 Windows 可用（当前平台 ${platform}）`,
        projects: [],
        probes: { windows: false, netstat: false },
        at: now(),
        cached: false,
        enabled: false
      }
    }
    const force = runOptions.force === true
    if (!force && cache !== null && now() - cache.at < ttlMs) {
      writeLog('debug', `HCL 工程探测命中缓存（${((now() - cache.at) / 1000).toFixed(1)}s 前，TTL ${ttlMs / 1000}s）`)
      return { ...cache, cached: true, enabled: true }
    }
    const started = now()
    let result
    try {
      result = probe({ timeoutMs, platform })
    }
    catch (error) {
      result = { ok: false, reason: `HCL 工程探测异常：${describeError(error)}`, projects: [], probes: {} }
    }
    probeCount++
    const snapshot = {
      ok: result?.ok === true,
      reason: typeof result?.reason === 'string' ? result.reason : '',
      projects: Array.isArray(result?.projects) ? result.projects : [],
      probes: result?.probes ?? {},
      at: now(),
      cached: false,
      enabled: true
    }
    cache = snapshot
    const took = snapshot.at - started
    if (snapshot.ok) {
      writeLog('info', `HCL 工程探测（第 ${probeCount} 次，${took}ms）：`
        + snapshot.projects.map(item => `${item.project}=${item.portCount > 0 ? `${item.ports[0]}-${item.ports.at(-1)}` : '无'}`).join('、'))
    }
    else {
      writeLog('debug', `HCL 工程探测未成功（${took}ms）：${snapshot.reason}`)
    }
    return { ...snapshot, enabled: true }
  }

  /**
   * 按工程名解析端口。**首次未命中会强制刷新一次**（工程可能是刚打开的）。
   * @param {string} query 工程名（可带 `#N`）。
   */
  const select = (query) => {
    const first = resolveProjectQuery(discover().projects, query)
    if (first.ok)
      return { ...first, discovery: discover() }
    const refreshed = discover({ force: true })
    const second = resolveProjectQuery(refreshed.projects, query)
    return { ...second, discovery: refreshed }
  }

  /** 按 netFile 自动匹配工程（无显式端口时用）。 */
  const selectByNetFile = (netFile) => {
    const first = matchProjectByPath(discover().projects, netFile)
    if (first.project !== null)
      return { ...first, discovery: discover() }
    const refreshed = discover({ force: true })
    const second = matchProjectByPath(refreshed.projects, netFile)
    return { ...second, discovery: refreshed }
  }

  return {
    enabled,
    ttlMs,
    discover,
    select,
    selectByNetFile,
    /** 丢弃缓存（面板改配置、或调用方怀疑端口变了时用）。 */
    invalidate() {
      cache = null
    },
    /** 只读快照，便于诊断/面板展示。 */
    get snapshot() {
      return cache === null ? null : { ...cache }
    },
    get probeCount() {
      return probeCount
    }
  }
}

// ---------------------------------------------------------------------------
// 端口来源的分层置信度 + ”连上了不等于连对了“的反验
//
// 这套模型的教训来自 lab4_ts/mylab20：按 `30000 + device_id` 去连，**连成功了**，
// 但那台设备属于**另一个工程**（它的端口段是 31001+）。所以端口来源必须分置信度，
// 而且必须在连上之后**反证**：拿回显里的主机名/型号跟期望比对，不一致就报“疑似连错工程”。
// （感谢 lead 提供的同类开源项目 Sun3-3/HCL-Lab_mcp 的 design.md 思路。）
// ---------------------------------------------------------------------------

/** 端口来源枚举。 */
export const PORT_SOURCES = Object.freeze({
  /** 用户/配置**显式**给的端口：最高置信度，绝不改写。 */
  EXPLICIT: 'explicit',
  /** `netstat` 实测该工程 PID 正在监听的端口段：实测数据。 */
  NETSTAT: 'netstat',
  /** 拓扑 `device_id` 推算（`30000 + id`）：**只是候选**，必须反验。 */
  INFERRED: 'inferred',
  /** 服务器内置兜底范围（既没 ports 也没 netFile）：最低置信度。 */
  FALLBACK: 'fallback'
})

/** 各来源的置信度（数字越大越可信）。 */
export const PORT_SOURCE_CONFIDENCE = Object.freeze({
  [PORT_SOURCES.EXPLICIT]: 100,
  [PORT_SOURCES.NETSTAT]: 80,
  [PORT_SOURCES.INFERRED]: 40,
  [PORT_SOURCES.FALLBACK]: 10
})

/** 来源的中文名（日志/提示用）。 */
export const PORT_SOURCE_LABEL = Object.freeze({
  [PORT_SOURCES.EXPLICIT]: '显式配置',
  [PORT_SOURCES.NETSTAT]: 'netstat 实测',
  [PORT_SOURCES.INFERRED]: '拓扑 device_id 推算',
  [PORT_SOURCES.FALLBACK]: '内置兜底范围'
})

/** 按置信度排序候选（同分时端口少的优先，代价更小）。 */
export function rankPortCandidates(candidates) {
  const list = (Array.isArray(candidates) ? candidates : [])
    .filter(item => item !== null && typeof item === 'object' && Array.isArray(item.ports) && item.ports.length > 0)
  return [...list].sort((left, right) => {
    const lc = PORT_SOURCE_CONFIDENCE[left.source] ?? 0
    const rc = PORT_SOURCE_CONFIDENCE[right.source] ?? 0
    if (lc !== rc)
      return rc - lc
    return left.ports.length - right.ports.length
  })
}

/**
 * 解析 HCL 的 `.net` 拓扑（INI 风格），取每台设备的 `device_id` 与名字。
 *
 * 规则与 `scripts/server.py` 的 `parse_net()` **保持同构**：只认 `[[型号 名字]]`，
 * 跳过 `NOTE`/`SHAPE` 这类非设备节，端口 = `30000 + device_id`。
 * @param {string} text .net 文件内容。
 * @param {number} [base] 端口基址（默认 30000，与服务器一致）。
 * @returns {{devices: Array<{name: string, model: string, deviceId: number|null, port: number|null}>, base: number}}
 */
export function parseNetTopology(text, base = 30000) {
  const devices = []
  if (typeof text !== 'string')
    return { devices, base }
  const skip = new Set(['NOTE', 'SHAPE'])
  let current = null
  for (const rawLine of text.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const line = rawLine.trim()
    const section = /^\[\[(.+?)\]\]\s*$/.exec(line)
    if (section !== null) {
      const body = section[1]
      const space = body.indexOf(' ')
      const model = space < 0 ? body : body.slice(0, space)
      const name = space < 0 ? '' : body.slice(space + 1).trim()
      current = null
      if (name !== '' && !skip.has(model.toUpperCase())) {
        current = { name, model, deviceId: null, port: null }
        devices.push(current)
      }
      continue
    }
    if (current === null || line === '' || line.startsWith('#') || !line.includes('='))
      continue
    const eq = line.indexOf('=')
    const key = line.slice(0, eq).trim()
    const value = line.slice(eq + 1).trim()
    if (key === 'device_id') {
      const id = Number(value)
      if (Number.isInteger(id)) {
        current.deviceId = id
        current.port = base + id
      }
    }
  }
  return { devices, base }
}

/**
 * 决定 `h3c_devices` 该探哪些端口（多来源候选 + 置信度排序 + 连错工程预警）。
 *
 * 优先级：**显式 ports > netstat 实测 > 拓扑 device_id 推算 > 兜底**。
 * @param {object} options
 * @param {number[]} [options.explicitPorts] 用户显式传入的 ports。
 * @param {object} [options.discovery] createHclDiscovery() 的实例。
 * @param {string} [options.netFile] 拓扑文件路径。
 * @param {{devices: Array<object>, base: number}|null} [options.topology] 已解析的拓扑。
 * @returns {{
 *   source: string, ports: number[], chosen: object|null, candidates: Array<object>,
 *   expected: Record<string, string>|null, base: number|null, project: object|null,
 *   suspectedWrongProject: boolean, projects: Array<object>, note: string
 * }}
 */
export function planDevicePorts(options = {}) {
  const explicitPorts = (Array.isArray(options.explicitPorts) ? options.explicitPorts : [])
    .map(item => Number(item))
    .filter(item => Number.isInteger(item) && item >= 1 && item <= 65535)
  const discovery = options.discovery ?? null
  const netFile = typeof options.netFile === 'string' ? options.netFile : ''
  const topology = options.topology ?? null
  const snapshot = discovery === null ? { ok: false, projects: [] } : discovery.discover()
  const projects = snapshot.ok ? snapshot.projects : []
  const candidates = []

  if (explicitPorts.length > 0) {
    candidates.push({
      source: PORT_SOURCES.EXPLICIT,
      ports: [...new Set(explicitPorts)].sort((a, b) => a - b),
      reason: `调用方显式传入 ports（${explicitPorts.length} 个）`
    })
  }

  // netstat 候选的取用规则（**保守**，宁可用低置信度候选再反验，也不要"连上了但连错了"）：
  //   - 配了 netFile：只认**路径匹配**的工程；匹配不上就不用 netstat
  //     （否则用户拿着 lab4 的 netFile、只开了 mylab20 的窗口时，会"成功"连到别的工程）；
  //   - 没配 netFile：只有在**唯一**一个工程窗口时才敢用（并在提示里写明这是按唯一工程取的）。
  let matched = null
  let unmatchedNote = ''
  if (projects.length > 0) {
    if (netFile !== '') {
      matched = matchProjectByPath(projects, netFile).project
      if (matched === null) {
        unmatchedNote = `⚠ 已配置的 netFile（${netFile}）与任何已打开工程的路径都不符。已打开：\n`
          + `${formatProjects(projects)}\n`
          + '不会拿它们的端口去顶替（那正是“连上了却连到另一个工程”的来路）；'
          + '本次改用「30000+device_id」推算，**连上前请核对工程**。'
      }
    }
    else if (projects.length === 1) {
      matched = projects[0]
      unmatchedNote = `ℹ 未配置 netFile：按**唯一**打开的工程 ${matched.project} 取端口（置信度低于路径匹配）。`
    }
  }
  if (matched !== null && matched.ports.length > 0) {
    candidates.push({
      source: PORT_SOURCES.NETSTAT,
      ports: [...matched.ports],
      project: matched,
      reason: `netstat 实测：工程 ${matched.project}（pid=${matched.pid}）监听 ${matched.ports[0]}-${matched.ports.at(-1)}（${matched.portCount} 个）`,
      note: unmatchedNote
    })
  }

  // 拓扑 device_id 推算（30000+id）——只是候选。
  let base = null
  if (topology !== null && Array.isArray(topology.devices)) {
    const mapped = topology.devices.filter(item => item.port !== null)
    if (mapped.length > 0) {
      base = topology.base ?? 30000
      candidates.push({
        source: PORT_SOURCES.INFERRED,
        ports: mapped.map(item => item.port).sort((a, b) => a - b),
        reason: `拓扑 device_id 推算（${base}+id，共 ${mapped.length} 台）——**只是候选，必须反验**`,
        base
      })
    }
  }

  const chosen = candidates[0] ?? null
  let suspectedWrongProject = false
  const notes = []
  // 候选自带的说明（例如“netFile 与已打开工程不符”“未配置 netFile，按唯一工程取”）。
  // ★ 注意：这些说明**不能挂在候选上就算了**——候选没被选中时它恰恰是最该说的话。
  if (typeof chosen?.note === 'string' && chosen.note !== '')
    notes.push(chosen.note)
  else if (unmatchedNote !== '')
    notes.push(unmatchedNote)
  if (chosen !== null && chosen.source === PORT_SOURCES.INFERRED && projects.length > 0) {
    const known = new Set(projects.flatMap(item => item.ports))
    const inside = chosen.ports.filter(port => known.has(port))
    if (inside.length === 0) {
      suspectedWrongProject = true
      notes.push(`⚠ 端口 ${chosen.ports[0]}-${chosen.ports.at(-1)} 是按「${base}+device_id」推算的，`
        + `但已发现工程都不在这个段里：\n${formatProjects(projects)}\n`
        + `按惯例写死的 30000+id **很可能连到另一个工程的设备上**（连得上 ≠ 连对了）。`
        + `请改用 project: "${projects[0].project}"，或显式传 ports。`)
    }
  }
  if (chosen === null && projects.length > 0) {
    notes.push(`⚠ 未指定 ports：服务器会按「30000 + device_id」或内置兜底范围推导；已发现工程：\n`
      + `${formatProjects(projects)}\n如果目标不是 30000+ 段，请用 project: "<工程名>" 或显式传 ports。`)
  }

  // 反验期望值：**按最终选中的端口**从拓扑推出“这个端口上应该是哪台设备”。
  // 不能直接用 device_id+30000 —— 工程基址可能是 31000（本次实测），
  // 期望值必须跟着实际端口走，否则反验会因为“端口对不上号”而永远判 verified。
  const expected = topology === null
    ? null
    : expectedHostnamesFor(topology, chosen?.ports ?? [], [chosen?.base, chosen?.project?.base])

  return {
    source: chosen?.source ?? PORT_SOURCES.FALLBACK,
    ports: chosen?.ports ?? [],
    chosen,
    candidates: rankPortCandidates(candidates),
    expected,
    base,
    project: chosen?.project ?? null,
    suspectedWrongProject,
    projects,
    note: notes.join('\n')
  }
}

/**
 * 按**实际要探的端口**从拓扑推出期望主机名（反验基准）。
 *
 * HCL 的端口 = 工程基址 + device_id，但**工程基址每个工程不同**（30000 / 31000 / …），
 * 所以不能直接拿 `30000 + device_id` 当基准。这里依次试几个基址：
 *   1. 调用方给的候选基址（例如 netstat 发现的 31000）；
 *   2. 拓扑自己的基址（默认 30000）；
 *   3. `最小端口 - 最小 device_id` 推出的基址（例如 [31001,31002] 与 id {1,2} → 31000）。
 * 取**覆盖端口最多**的那个；一个都对不上就返回 null（只能“未反验”，不能假称“对”）。
 * @param {{devices: Array<{name: string, deviceId: number|null}>}|null} topology 已解析拓扑。
 * @param {number[]} ports 实际要探的端口。
 * @param {Array<number|undefined|null>} [preferredBases] 优先尝试的基址。
 * @returns {Record<string, string>|null} 端口 -> 期望主机名。
 */
export function expectedHostnamesFor(topology, ports, preferredBases = []) {
  const list = (Array.isArray(ports) ? ports : [])
    .map(item => Number(item))
    .filter(item => Number.isInteger(item))
  const devices = Array.isArray(topology?.devices)
    ? topology.devices.filter(item => item !== null && typeof item === 'object'
      && Number.isInteger(item.deviceId) && typeof item.name === 'string' && item.name !== '')
    : []
  if (list.length === 0 || devices.length === 0)
    return null
  const portSet = new Set(list)
  const ids = devices.map(item => item.deviceId)
  const bases = []
  const pushBase = (value) => {
    const number = Number(value)
    if (Number.isInteger(number) && !bases.includes(number))
      bases.push(number)
  }
  for (const value of Array.isArray(preferredBases) ? preferredBases : [])
    pushBase(value)
  pushBase(topology?.base)
  pushBase(30000)
  pushBase(Math.min(...list) - Math.min(...ids))

  let best = null
  for (const baseValue of bases) {
    const map = {}
    for (const device of devices) {
      const port = baseValue + device.deviceId
      if (portSet.has(port))
        map[String(port)] = device.name
    }
    if (best === null || Object.keys(map).length > Object.keys(best).length)
      best = map
  }
  return best !== null && Object.keys(best).length > 0 ? best : null
}

/**
 * 解析 `hcl_list_devices` 的输出表格（由 python 生成，格式见 server.py）：
 *
 *   31001  BR-PE  MSR36-20  UP
 *   31002  -      -         DOWN
 *
 * @param {string} text 工具回显。
 * @returns {{measured: Array<{port: number, hostname: string, model: string, state: string}>, unreachable: number[]}}
 */
export function parseDeviceProbeText(text) {
  const measured = []
  const unreachable = []
  if (typeof text !== 'string')
    return { measured, unreachable }
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim()
    if (line === '')
      continue
    const row = /^(\d{1,5})\s+(\S+)\s+(\S+)\s+(UP|DOWN)$/.exec(line)
    if (row !== null) {
      measured.push({
        port: Number(row[1]),
        hostname: row[2],
        model: row[3],
        state: row[4]
      })
      continue
    }
    const bad = /^(\d{1,5}):\s*(.+)$/.exec(line)
    if (bad !== null)
      unreachable.push(Number(bad[1]))
  }
  return { measured, unreachable }
}

/**
 * **反验**：把回显里实测到的主机名/型号跟期望比对，给出 `[verified]` 之类的结论。
 *
 * 这是“连上了不等于连对了”的兜底。`expected` 来自拓扑（端口 -> 期望主机名）；
 * 没有期望值时只能说“未反验”，不能说“对”。
 * @param {string} text `hcl_list_devices` 回显。
 * @param {object} plan `planDevicePorts()` 的结果。
 * @returns {{status: string, line: string, measured: Array<object>, mismatches: Array<object>, up: number}}
 */
export function verifyDeviceProbe(text, plan) {
  const { measured, unreachable } = parseDeviceProbeText(text)
  const up = measured.filter(item => item.state === 'UP')
  const expected = plan?.expected ?? null
  const mismatches = []
  if (expected !== null) {
    for (const item of up) {
      const want = expected[String(item.port)]
      if (want !== undefined && want !== '-' && item.hostname !== want)
        mismatches.push({ port: item.port, expected: want, measured: item.hostname })
    }
  }
  const sourceLabel = PORT_SOURCE_LABEL[plan?.source ?? ''] ?? plan?.source ?? '未知来源'
  const projectText = plan?.project === null || plan?.project === undefined ? '' : `，工程 ${plan.project.project}`
  const noAnswer = unreachable.length > 0 ? `；${unreachable.length} 个端口连不上（${unreachable.slice(0, 8).join('、')}${unreachable.length > 8 ? '…' : ''}）` : ''
  const down = measured.filter(item => item.state !== 'UP')
  const downText = down.length > 0 ? `；${down.length} 个端口 DOWN（${down.slice(0, 8).map(item => item.port).join('、')}${down.length > 8 ? '…' : ''}）` : ''

  if (up.length === 0) {
    const line = `[unreachable] 端口来源 ${sourceLabel}${projectText}；没有一台设备响应（共探测 ${measured.length + unreachable.length} 个端口）${noAnswer}`
      + (plan?.suspectedWrongProject === true ? `\n[怀疑连错工程] ${String(plan?.note ?? '').split('\n')[0]}` : '')
    return { status: 'unreachable', line, measured: up, mismatches, up: 0 }
  }
  if (mismatches.length > 0) {
    const line = `[suspect] 端口来源 ${sourceLabel}${projectText}；${up.length} 台响应，但 ${mismatches.length} 台主机名与拓扑不符 → **疑似连错工程**：`
      + mismatches.slice(0, 8).map(item => `${item.port} 实测 ${item.measured} ≠ 拓扑 ${item.expected}`).join('；')
    return { status: 'suspect', line, measured: up, mismatches, up: up.length }
  }
  if (plan?.suspectedWrongProject === true) {
    return {
      status: 'suspect',
      line: `[suspect] 端口来源 ${sourceLabel}；${up.length} 台响应，但这些端口不在任何已发现 HCL 工程的端口段内 → **疑似连错工程**。\n${plan.note}`,
      measured: up,
      mismatches,
      up: up.length
    }
  }
  if (expected === null) {
    return {
      status: 'unverified',
      line: `[unverified] 端口来源 ${sourceLabel}${projectText}；${up.length} 台响应${noAnswer}${downText}。`
        + '没有拓扑期望值可比对（未配 netFile 或拓扑里没有 device_id）——**“连上了”不等于“连对了”**，建议配好 netFile 或用 project: 指定工程后重跑。',
      measured: up,
      mismatches,
      up: up.length
    }
  }
  return {
    status: 'verified',
    line: `[verified] 端口来源 ${sourceLabel}${projectText}；${up.length} 台响应，主机名与拓扑一致${noAnswer}${downText}。`,
    measured: up,
    mismatches,
    up: up.length
  }
}
