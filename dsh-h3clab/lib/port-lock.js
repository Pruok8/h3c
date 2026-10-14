/**
 * dsh-h3clab —— 控制台端口 **per-port 进程内互斥**。
 *
 * 为什么必须有这一层（lab20 实测教训）：
 *   HCL 的控制台是**单条 TCP 流**，同一个端口上两个会话会互相看到对方的输出
 *   （见 lab20/CAPABILITIES.md D 节：“同一端口不能两个会话”）。本次并行作业
 *   完全靠人工在任务书里写“只碰这几个端口”来避让——人一多就一定会踩上：
 *   两个计划同时往 31008 下发，轻则回显串台、重则命令落进对方的视图里。
 *
 * 因此给每个 console port 一把**进程内互斥锁**：
 *   - 同一端口的 `h3c_apply_plan` / `h3c_run` / `h3c_verify` / … 串行执行；
 *   - 获取锁**带超时**，超时给出可读错误（“端口 31008 正被另一次下发占用”）；
 *   - 一次调用涉及多个端口时**按键排序后依次申请**，从根上避免 A 等 B、B 等 A 的死锁。
 *
 * 作用范围（必须说清楚，别把期望放错地方）：
 *   **同一个 DSH 宿主进程内的所有 agent 共享同一把锁**——本次并行作业的 6 个
 *   teammate 就跑在同一个 DSH 进程里，共用这一份插件实例，所以这把锁正好覆盖
 *   “两个 teammate 同时下发同一端口”这个真实故障模式。
 *   跨进程（另一个 DSH 窗口、或直接用 python 调 MCP 服务器）**不在**本锁范围内：
 *   MCP 服务器本身是 python 单进程串行处理请求，但两个宿主进程各有一个 python
 *   子进程，互相看不见。跨进程避让仍需人工约定端口归属（或后续加锁文件）。
 *
 * 键的形态（`inspect()` 与错误消息里会翻译成人话）：
 *   `port:31008`        某个控制台端口
 *   `device:HQ-PE1`     某个设备名（计划里没写 port 时按名字锁定）
 *   `plan:<文件路径>`   计划文件本身（连端口都解析不出来时的兜底）
 *   `checklist:<路径>`  验收清单文件本身
 */

import { readFileSync } from 'node:fs'

/** 默认的等锁超时：两分钟。超过就报错，而不是无限期挂住一个 agent。 */
export const DEFAULT_LOCK_WAIT_MS = 120_000
/** 已知的“控制台端口”下限：低于它的键不可能是 HCL 控制台。 */
const MIN_LOCK_PORT = 1

/** 端口键。 */
export function portKey(port) {
  const value = Number(port)
  return Number.isInteger(value) && value >= MIN_LOCK_PORT && value <= 65535 ? `port:${value}` : null
}

/** 设备名键。 */
export function deviceKey(name) {
  const text = typeof name === 'string' ? name.trim() : ''
  return text === '' ? null : `device:${text}`
}

/** 文件键（计划/清单）。 */
export function fileKey(kind, path) {
  const text = typeof path === 'string' ? path.trim() : ''
  return text === '' ? null : `${kind}:${text}`
}

/** 丢掉 null/空并去重排序——**排序**是避免死锁的关键。 */
export function normalizeLockKeys(keys) {
  const list = (Array.isArray(keys) ? keys : [keys])
    .filter(key => typeof key === 'string' && key.trim() !== '')
    .map(key => key.trim())
  return [...new Set(list)].sort()
}

/** 把锁键翻译成给人看的中文（错误消息/诊断用）。 */
export function describeLockKey(key) {
  const text = typeof key === 'string' ? key : String(key ?? '')
  const colon = text.indexOf(':')
  if (colon <= 0)
    return text
  const kind = text.slice(0, colon)
  const value = text.slice(colon + 1)
  if (kind === 'port')
    return `控制台端口 ${value}`
  if (kind === 'device')
    return `设备 ${value}`
  if (kind === 'plan')
    return `计划文件 ${value}`
  if (kind === 'checklist')
    return `验收清单 ${value}`
  return text
}

/** 把一组键翻译成一行可读文本。 */
export function describeLockKeys(keys) {
  const list = normalizeLockKeys(keys)
  return list.length === 0 ? '（无）' : list.map(describeLockKey).join('、')
}

/** 相对时间，用在“已持有 3.2 秒”这类描述里。 */
function seconds(ms) {
  const value = Number(ms)
  if (!Number.isFinite(value) || value < 0)
    return '?'
  return `${(value / 1000).toFixed(1)} 秒`
}

/**
 * 从**计划文件**里解析要锁哪些键（`{devices:[{name, port, commands}]}`，见 README）。
 *
 * 计划里的设备可能**不写 port**（真实下发时由服务器按名字扫端口），
 * 所以名字也要锁：两份计划命中同一台设备时会串行，而不是并行去抢同一个控制台。
 * 文件读不了/格式不认识时回退成只锁文件本身，绝不抛异常。
 * @param {string} planPath 计划 JSON 路径。
 * @returns {{keys: string[], ports: number[], devices: string[], note: string}}
 */
export function lockKeysFromPlan(planPath) {
  const fallback = fileKey('plan', planPath)
  const out = { keys: fallback === null ? [] : [fallback], ports: [], devices: [], note: '' }
  const text = typeof planPath === 'string' ? planPath.trim() : ''
  if (text === '')
    return { ...out, note: '计划路径为空' }
  let data
  try {
    data = JSON.parse(readFileSync(text, 'utf8').replace(/^\uFEFF/, ''))
  }
  catch (error) {
    return { ...out, note: `计划文件读不出来（${error?.message ?? String(error)}），只锁计划文件本身` }
  }
  const devices = Array.isArray(data?.devices) ? data.devices : []
  const keys = []
  for (const item of devices) {
    if (item === null || typeof item !== 'object')
      continue
    const name = deviceKey(item.name ?? item.hostname)
    if (name !== null) {
      keys.push(name)
      out.devices.push(String(item.name ?? item.hostname).trim())
    }
    const port = portKey(item.port)
    if (port !== null) {
      keys.push(port)
      out.ports.push(Number(item.port))
    }
  }
  out.keys = normalizeLockKeys([...out.keys, ...keys])
  out.note = devices.length === 0 ? '计划里没有 devices 数组' : `计划含 ${devices.length} 台设备`
  return out
}

/**
 * 从**验收清单**里解析要锁哪些键（`{checks:[{id, port, ...}]}`）。
 * @param {string} checklistPath 清单 JSON 路径。
 */
export function lockKeysFromChecklist(checklistPath) {
  const fallback = fileKey('checklist', checklistPath)
  const out = { keys: fallback === null ? [] : [fallback], ports: [], devices: [], note: '' }
  const text = typeof checklistPath === 'string' ? checklistPath.trim() : ''
  if (text === '')
    return { ...out, note: '清单路径为空' }
  let data
  try {
    data = JSON.parse(readFileSync(text, 'utf8').replace(/^\uFEFF/, ''))
  }
  catch (error) {
    return { ...out, note: `清单文件读不出来（${error?.message ?? String(error)}），只锁清单文件本身` }
  }
  const checks = Array.isArray(data?.checks) ? data.checks : []
  const keys = []
  for (const item of checks) {
    if (item === null || typeof item !== 'object')
      continue
    // 清单里 `name` 只是标签，服务器按 `port` 连设备，所以只锁端口。
    const port = portKey(item.port)
    if (port !== null) {
      keys.push(port)
      out.ports.push(Number(item.port))
      if (typeof item.name === 'string' && item.name.trim() !== '')
        out.devices.push(item.name.trim())
    }
  }
  out.keys = normalizeLockKeys([...out.keys, ...keys])
  out.note = checks.length === 0 ? '清单里没有 checks 数组' : `清单含 ${checks.length} 项检查`
  return out
}

/**
 * 一次调用的锁需求：键 + 一句人话说明（用于错误消息与日志）。
 * 约定：`{ keys, label, detail }`。
 */
export function lockRequest(keys, label, detail = '') {
  return { keys: normalizeLockKeys(keys), label: typeof label === 'string' ? label : '工具调用', detail }
}

/**
 * 进程内 per-key 互斥锁。
 *
 * 语义：
 *   - FIFO 公平：先到先得，释放时把锁交给队首；
 *   - **一次只申请一个键**；多键由 `acquire()` 排序后逐个申请；
 *   - 超时 / 取消都会把自己从队列里摘掉，绝不留下“幽灵等待者”；
 *   - `release()` 可重复调用（幂等），但只有真正的持有者能释放。
 */
export class PortLockRegistry {
  /**
   * @param {object} [options]
   * @param {number} [options.waitMs] 默认等锁超时。
   * @param {(level: string, message: string) => void} [options.log] 日志出口。
   * @param {() => number} [options.now] 时钟（测试注入点）。
   * @param {Function} [options.setTimer] setTimeout（测试注入点）。
   * @param {Function} [options.clearTimer] clearTimeout（测试注入点）。
   */
  constructor(options = {}) {
    this.waitMs = Number.isFinite(options.waitMs) && Number(options.waitMs) >= 0
      ? Number(options.waitMs)
      : DEFAULT_LOCK_WAIT_MS
    this.log = typeof options.log === 'function' ? options.log : () => {}
    this.now = typeof options.now === 'function' ? options.now : Date.now
    this.setTimer = typeof options.setTimer === 'function' ? options.setTimer : setTimeout
    this.clearTimer = typeof options.clearTimer === 'function' ? options.clearTimer : clearTimeout
    /** key -> { locked, holder, queue } */
    this.entries = new Map()
    /** 累计统计，便于诊断（不是权威状态）。 */
    this.stats = { acquired: 0, timeouts: 0, aborted: 0, handedOver: 0 }
  }

  writeLog(level, message) {
    try {
      this.log(level, message)
    }
    catch {
      /* 日志绝不影响锁 */
    }
  }

  entry(key) {
    let item = this.entries.get(key)
    if (item === undefined) {
      item = { locked: false, holder: null, queue: [] }
      this.entries.set(key, item)
    }
    return item
  }

  /** 诊断快照：谁持有（持了多久）、谁在等。 */
  inspect() {
    const held = []
    const queued = []
    const now = this.now()
    for (const [key, item] of this.entries) {
      if (item.locked && item.holder !== null)
        held.push({ key, label: item.holder.label, heldMs: now - (item.holder.grantedAt ?? item.holder.startedAt) })
      for (const waiter of item.queue)
        queued.push({ key, label: waiter.label, waitedMs: now - waiter.startedAt })
    }
    return { waitMs: this.waitMs, held, queued, stats: { ...this.stats } }
  }

  /** 给错误消息用：当前谁占着这个键。 */
  holderOf(key) {
    const item = this.entries.get(key)
    if (item === undefined || !item.locked || item.holder === null)
      return null
    return { label: item.holder.label, heldMs: this.now() - (item.holder.grantedAt ?? item.holder.startedAt) }
  }

  /** 是否有人在等 / 有锁未放（自测与面板诊断用）。 */
  busy() {
    for (const item of this.entries.values()) {
      if (item.locked || item.queue.length > 0)
        return true
    }
    return false
  }

  /** 构造可读的超时错误。 */
  timeoutError(key, waitedMs) {
    const holder = this.holderOf(key)
    const occupied = holder === null
      ? '（占用者信息不可用）'
      : `${holder.label} 已持有 ${seconds(holder.heldMs)}`
    return new Error(`等锁超时：${describeLockKey(key)} 正被另一次调用占用（占用者：${occupied}；已等待 ${seconds(waitedMs)}）。`
      + '请等它结束再重试，或改用其它端口——同一控制台端口不能有两个会话同时下发。')
  }

  /** 把锁交给一个等待者（调用方必须保证 waiter 还没超时/取消）。 */
  grant(entry, waiter) {
    if (waiter.done)
      return
    waiter.done = true
    waiter.grantedAt = this.now()
    if (waiter.timer !== null)
      this.clearTimer(waiter.timer)
    if (waiter.abortListener !== null && waiter.signal !== undefined)
      waiter.signal.removeEventListener('abort', waiter.abortListener)
    entry.locked = true
    entry.holder = waiter
    this.stats.acquired++
    waiter.resolve(this.makeRelease(entry, waiter))
  }

  makeRelease(entry, waiter) {
    let released = false
    return () => {
      if (released)
        return
      released = true
      if (entry.holder !== waiter)
        return
      entry.holder = null
      entry.locked = false
      // 交棒：跳过已经超时/取消的等待者（它们只会 `done` 而不会自己出队）。
      for (;;) {
        const next = entry.queue.shift()
        if (next === undefined)
          break
        if (next.done)
          continue
        this.stats.handedOver++
        this.grant(entry, next)
        break
      }
    }
  }

  /** 申请**一个**键；成功时 resolve 释放函数。 */
  acquireOne(key, options = {}) {
    const entry = this.entry(key)
    const label = typeof options.label === 'string' && options.label !== '' ? options.label : '工具调用'
    const signal = options.signal
    const deadline = Number.isFinite(options.deadline) ? Number(options.deadline) : this.now() + options.waitMs
    return new Promise((resolve, reject) => {
      const waiter = {
        key,
        label,
        startedAt: this.now(),
        resolve,
        reject,
        signal,
        abortListener: null,
        timer: null,
        done: false
      }
      const remaining = deadline - this.now()
      if (remaining <= 0) {
        this.stats.timeouts++
        reject(this.timeoutError(key, 0))
        return
      }
      // 已经在排队的人：先到先得，但超时一律按自己的 deadline 算。
      waiter.timer = this.setTimer(() => {
        if (waiter.done)
          return
        waiter.done = true
        const index = entry.queue.indexOf(waiter)
        if (index >= 0)
          entry.queue.splice(index, 1)
        if (waiter.abortListener !== null && signal !== undefined)
          signal.removeEventListener('abort', waiter.abortListener)
        this.stats.timeouts++
        this.writeLog('warn', `等锁超时：${describeLockKey(key)}（${label} 等待 ${seconds(this.now() - waiter.startedAt)}）`)
        reject(this.timeoutError(key, this.now() - waiter.startedAt))
      }, remaining)
      if (signal !== undefined) {
        waiter.abortListener = () => {
          if (waiter.done)
            return
          waiter.done = true
          if (waiter.timer !== null)
            this.clearTimer(waiter.timer)
          const index = entry.queue.indexOf(waiter)
          if (index >= 0)
            entry.queue.splice(index, 1)
          this.stats.aborted++
          reject(new Error('调用已被取消'))
        }
        signal.addEventListener('abort', waiter.abortListener, { once: true })
      }
      if (!entry.locked && entry.queue.length === 0) {
        this.grant(entry, waiter)
        return
      }
      entry.queue.push(waiter)
      this.writeLog('debug', `${describeLockKey(key)} 已被占用，${label} 进入等待队列（队列 ${entry.queue.length}）`)
    })
  }

  /**
   * 申请一组键（排序后逐个申请，全部拿到才算成功）。
   * @param {string[]} keys 锁键。
   * @param {object} [options]
   * @param {string} [options.label] 占用者标签（工具名 + 端口）。
   * @param {number} [options.waitMs] 覆盖默认等锁超时。
   * @param {AbortSignal} [options.signal] 取消信号。
   * @returns {Promise<() => void>} 释放函数（幂等）。
   */
  async acquire(keys, options = {}) {
    const list = normalizeLockKeys(keys)
    if (list.length === 0)
      return () => {}
    const waitMs = Number.isFinite(options.waitMs) ? Math.max(0, Number(options.waitMs)) : this.waitMs
    if (waitMs === 0)
      return () => {}
    const label = typeof options.label === 'string' && options.label !== '' ? options.label : '工具调用'
    const deadline = this.now() + waitMs
    const held = []
    try {
      for (const key of list) {
        const release = await this.acquireOne(key, { label, signal: options.signal, deadline, waitMs })
        held.push(release)
      }
    }
    catch (error) {
      // 半途失败：把已经拿到的锁原样还回去，绝不留悬挂锁。
      for (const release of held.reverse())
        release()
      throw error
    }
    let allReleased = false
    return () => {
      if (allReleased)
        return
      allReleased = true
      for (const release of held.reverse())
        release()
    }
  }

  /**
   * 便捷包装：申请 → 执行 → 释放（异常也释放）。
   * @template T
   * @param {string[]} keys 锁键。
   * @param {() => Promise<T>|T} body 受保护的调用。
   * @param {object} [options] 同 acquire。
   * @returns {Promise<T>}
   */
  async run(keys, body, options = {}) {
    const release = await this.acquire(keys, options)
    try {
      return await body()
    }
    finally {
      release()
    }
  }
}

/** 造一把锁注册表（与 `new PortLockRegistry` 等价，命名与 createHclDiscovery 对齐）。 */
export function createPortLock(options = {}) {
  return new PortLockRegistry(options)
}

/**
 * 从工具入参算出要锁的键——与工具实现解耦，便于单测与复用。
 *
 * @param {string} toolName DSH 工具名（`h3c_apply_plan` 等）。
 * @param {Record<string, unknown>} args 已解析/已补全的入参（h3c_devices 应传补全后的 ports）。
 * @param {object} [options]
 * @param {string} [options.planField] 计划文件字段名（默认 `plan_json`）。
 * @returns {{keys: string[], label: string, detail: string}}
 */
export function lockKeysForTool(toolName, args, options = {}) {
  const safe = args === null || typeof args !== 'object' ? {} : args
  const label = typeof toolName === 'string' && toolName !== '' ? toolName : '工具调用'
  const ports = []
  const keys = []
  const pushPort = (value) => {
    const key = portKey(value)
    if (key !== null) {
      keys.push(key)
      ports.push(Number(value))
    }
  }

  switch (label) {
    case 'h3c_run':
    case 'h3c_facts':
      pushPort(safe.port)
      break
    case 'h3c_devices':
    case 'h3c_doctor':
    case 'h3c_report':
    case 'h3c_cfgdiff': {
      if (Array.isArray(safe.ports)) {
        for (const item of safe.ports)
          pushPort(item)
      }
      pushPort(safe.port)
      break
    }
    case 'h3c_link_watch': {
      const links = Array.isArray(safe.links) ? safe.links : []
      for (const item of links) {
        if (item === null || typeof item !== 'object')
          continue
        pushPort(item.port)
        pushPort(item.peer_port)
      }
      break
    }
    case 'h3c_apply_plan': {
      const detail = lockKeysFromPlan(safe.plan_json)
      keys.push(...detail.keys)
      ports.push(...detail.ports)
      return { keys: normalizeLockKeys(keys), ports: [...new Set(ports)].sort((a, b) => a - b), label, detail: detail.note }
    }
    case 'h3c_verify': {
      const detail = lockKeysFromChecklist(safe.checklist_json)
      keys.push(...detail.keys)
      ports.push(...detail.ports)
      return { keys: normalizeLockKeys(keys), ports: [...new Set(ports)].sort((a, b) => a - b), label, detail: detail.note }
    }
    default:
      break
  }
  return {
    keys: normalizeLockKeys(keys),
    ports: [...new Set(ports)].sort((a, b) => a - b),
    label,
    detail: ports.length > 0 ? `端口 ${[...new Set(ports)].sort((a, b) => a - b).join('、')}` : ''
  }
}

/** 给一行日志用：`h3c_apply_plan 锁[port:31008、device:HQ-PE1]`。 */
export function formatLockLabel(toolName, keys, ports = []) {
  const list = normalizeLockKeys(keys)
  if (list.length === 0)
    return `${toolName}（无需加锁）`
  const portText = ports.length > 0 ? `端口 ${ports.join('、')}` : ''
  return `${toolName} ${portText}${portText === '' ? '' : ' '}锁[${list.join('、')}]`
}
