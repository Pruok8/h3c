/**
 * dsh-h3clab —— stdio MCP 客户端。
 *
 * 设计要点（与需求一一对应）：
 * - 用 node:child_process 的 spawn(command, args, { stdio: ['pipe','pipe','pipe'] })
 *   启动 `python <serverPath>`。
 * - JSON-RPC over stdio，**换行分隔**（每行一个 JSON 对象）：写入用
 *   `JSON.stringify(msg) + '\n'`，读取按 '\n' 切分缓冲。这里刻意 **不** 实现
 *   Content-Length 分帧，因为 H3C 实验 MCP 服务器（scripts/server.py）用的就是
 *   换行分隔。
 * - 首次调用时懒启动 + 自动握手：initialize -> 收响应 ->
 *   notifications/initialized（通知不带 id，不等待响应）。
 * - stdout 只用于 JSON-RPC 解析；stderr 一律转发给 DSH 日志。
 * - 子进程退出/出错/超时/被取消 -> 结束子进程并让所有在途请求以可读错误失败；
 *   下一次调用会重新 spawn + 重新握手（懒重启）。
 *
 * 只依赖 Node 内置模块，不引入任何第三方运行时依赖。
 */
import { spawn } from 'node:child_process'

/** 握手使用的 MCP 协议版本。 */
export const PROTOCOL_VERSION = '2024-11-05'
/** 握手（含 python 启动）的最长等待时间，与工具调用的超时分开。 */
export const DEFAULT_HANDSHAKE_TIMEOUT_MS = 30_000
/** stdout 缓冲上限：服务器若一直不换行，说明协议不对，直接放弃并结束子进程。 */
const MAX_STDOUT_BUFFER_CHARS = 4 * 1024 * 1024
/** 单条日志的最大长度，避免把设备回显整段灌进日志。 */
const MAX_LOG_CHARS = 400
/** tools/list 分页保护。 */
const MAX_TOOL_LIST_PAGES = 20

/** 截断日志文本。 */
function truncate(text, max = MAX_LOG_CHARS) {
  const value = typeof text === 'string' ? text : String(text)
  return value.length > max ? `${value.slice(0, max)}…（已截断）` : value
}

/** 把任意抛出物转成可读字符串。 */
export function describeError(error) {
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
 * 把 MCP tools/call 结果的 content[] 拼成文本。
 * 只读取 text 块的 text 叶子字段，不对整个 MCP 对象做序列化。
 */
export function extractToolText(result) {
  if (result === null || typeof result !== 'object')
    return ''
  const content = Array.isArray(result.content) ? result.content : []
  const parts = []
  for (const block of content) {
    if (block === null || typeof block !== 'object')
      continue
    if (block.type === 'text' && typeof block.text === 'string') {
      parts.push(block.text)
      continue
    }
    if (typeof block.type === 'string')
      parts.push(`[${block.type} 内容]`)
  }
  return parts.join('\n').trim()
}

/** 从 JSON-RPC error 对象构造可读错误。 */
function errorFromResponse(response, label) {
  const raw = response.error
  const code = raw !== null && typeof raw === 'object' && raw.code !== undefined ? String(raw.code) : 'unknown'
  const message = raw !== null && typeof raw === 'object' && typeof raw.message === 'string'
    ? raw.message
    : truncate(JSON.stringify(raw ?? response))
  let detail = ''
  if (raw !== null && typeof raw === 'object' && raw.data !== undefined) {
    detail = `；data=${truncate(typeof raw.data === 'string' ? raw.data : JSON.stringify(raw.data))}`
  }
  return new Error(`MCP ${label} 失败（code=${code}）：${message}${detail}`)
}

/**
 * 单个长生命周期 stdio MCP 客户端。
 * 一次 apply 创建一个实例，所有工具共享同一个 python 子进程。
 */
export class McpStdioClient {
  /**
   * @param {object} options
   * @param {string} options.command 可执行文件，例如 'python'。
   * @param {string[]} [options.args] 参数，例如 [serverPath]。
   * @param {string} [options.cwd] 子进程工作目录。
   * @param {Record<string, string>} [options.env] 子进程环境变量。
   * @param {number} [options.timeoutMs] 工具调用默认超时。
   * @param {string} [options.serverName] 用于日志的服务器名。
   * @param {(level: 'debug'|'info'|'warn', message: string) => void} [options.log] 日志出口。
   * @param {(options: object) => object} [options.createChild] 子进程工厂注入点。
   *   默认（也是唯一的生产路径）是
   *   `spawn(command, args, { stdio: ['pipe','pipe','pipe'] })`。
   *   仅用于自测/受限沙箱：当宿主沙箱禁止创建命名管道时，可以在自测里换成
   *   内存流 child（见 test/in-process-child.mjs）。注入的 child 需要提供
   *   stdin/stdout/stderr、pid/exitCode/signalCode/killed、on('error'|'exit')、kill()。
   */
  constructor(options) {
    this.command = options.command
    this.args = Array.isArray(options.args) ? options.args : []
    this.cwd = options.cwd
    this.env = options.env ?? {}
    this.timeoutMs = typeof options.timeoutMs === 'number' ? options.timeoutMs : 240_000
    this.handshakeTimeoutMs = typeof options.handshakeTimeoutMs === 'number'
      ? options.handshakeTimeoutMs
      : Math.min(this.timeoutMs, DEFAULT_HANDSHAKE_TIMEOUT_MS)
    this.serverName = options.serverName ?? 'h3clab'
    this.log = typeof options.log === 'function' ? options.log : () => {}
    this.createChild = typeof options.createChild === 'function' ? options.createChild : null
    /** 当前子进程；null 表示未启动或已结束。 */
    this.child = null
    /** stdout 行缓冲。 */
    this.buffer = ''
    /** id -> { resolve, reject, timer, abortListener, signal, label } */
    this.pending = new Map()
    this.nextId = 0
    /** 正在进行的启动+握手 promise，避免并发重复 spawn。 */
    this.starting = null
    /** 插件卸载后置 true，不再允许启动。 */
    this.disposed = false
    /** 握手结果快照，仅用于日志/诊断。 */
    this.serverInfo = null
  }

  /** 子进程是否还活着。 */
  alive() {
    return this.child !== null
      && this.child.exitCode === null
      && this.child.signalCode === null
  }

  /** 统一日志出口；level 是否存在由注入的 log 实现决定（见 lib/index.js）。 */
  writeLog(level, message) {
    try {
      this.log(level, `[${this.serverName}] ${message}`)
    }
    catch {
      /* 日志本身绝不影响调用 */
    }
  }

  /** 启动子进程（不做握手）。 */
  spawnChild() {
    const child = this.createChild !== null
      ? this.createChild({ command: this.command, args: this.args, cwd: this.cwd, env: this.env })
      : spawn(this.command, this.args, {
          stdio: ['pipe', 'pipe', 'pipe'],
          cwd: this.cwd,
          env: this.env,
          windowsHide: true
        })
    this.child = child
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', chunk => this.onStdout(child, chunk))
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', chunk => this.onStderr(child, chunk))
    child.on('error', (error) => {
      this.writeLog('warn', `无法启动 ${this.command}：${describeError(error)}`)
      this.failChild(child, `无法启动 ${this.command}：${describeError(error)}`)
    })
    child.on('exit', (code, signal) => this.onExit(child, code, signal))
    this.writeLog('info', `已启动 ${this.command} ${this.args.join(' ')}（pid=${child.pid ?? '未知'}）`)
    return child
  }

  /** stdout 只用于 JSON-RPC 解析。 */
  onStdout(child, chunk) {
    // 已被替换/结束的旧子进程，其后到的数据一律丢弃，避免污染共享缓冲。
    if (this.child !== child)
      return
    this.buffer += chunk
    let index = this.buffer.indexOf('\n')
    while (index >= 0) {
      const line = this.buffer.slice(0, index).replace(/\r$/, '')
      this.buffer = this.buffer.slice(index + 1)
      if (line.trim() !== '')
        this.handleLine(line)
      index = this.buffer.indexOf('\n')
    }
    if (this.buffer.length > MAX_STDOUT_BUFFER_CHARS) {
      this.writeLog('warn', 'stdout 缓冲超过上限，判定协议不匹配（期望换行分隔的 JSON-RPC），结束子进程')
      this.killChild('stdout 缓冲溢出')
    }
  }

  /** stderr 全部转发到 DSH 日志（逐行，debug 级）。 */
  onStderr(child, chunk) {
    if (this.child !== child)
      return
    const lines = String(chunk).split(/\r?\n/)
    for (const line of lines) {
      if (line.trim() === '')
        continue
      this.writeLog('debug', `[stderr] ${line}`)
    }
  }

  /** 处理一行 JSON-RPC 消息。 */
  handleLine(line) {
    // 容忍 BOM（例如服务器 stdout 被 UTF-8-BOM 包装时的首行）。
    const text = line.charCodeAt(0) === 0xFEFF ? line.slice(1) : line
    let message
    try {
      message = JSON.parse(text)
    }
    catch {
      this.writeLog('debug', `忽略非 JSON 的 stdout 行：${truncate(line)}`)
      return
    }
    if (message === null || typeof message !== 'object')
      return
    const hasId = message.id !== undefined && message.id !== null
    if (hasId && this.pending.has(message.id)) {
      const entry = this.pending.get(message.id)
      if (message.error !== undefined)
        this.settle(message.id, { error: errorFromResponse(message, entry.label) })
      else
        this.settle(message.id, { result: message.result })
      return
    }
    if (hasId && (message.result !== undefined || message.error !== undefined)) {
      this.writeLog('debug', `收到未知 id=${truncate(String(message.id), 40)} 的响应，已忽略`)
      return
    }
    if (hasId && typeof message.method === 'string') {
      // 服务器反向请求：本桥接不提供任何能力，统一回 method not found。
      this.write({ jsonrpc: '2.0', id: message.id, error: { code: -32601, message: `dsh-h3clab 不支持服务器发起的 ${message.method} 请求` } })
      return
    }
    if (typeof message.method === 'string')
      this.writeLog('debug', `忽略服务器通知 ${message.method}`)
  }

  /** 子进程退出：清空在途请求，等待下一次调用懒重启。 */
  onExit(child, code, signal) {
    if (this.child !== child) {
      this.writeLog('debug', `旧子进程已退出（code=${code ?? 'null'}）`)
      return
    }
    const reason = `python 子进程已退出（code=${code ?? 'null'}${signal === null || signal === undefined ? '' : `，signal=${signal}`}）`
    this.writeLog('warn', `${reason}；下次调用会重新启动并重新握手`)
    this.failChild(child, reason)
  }

  /** 结束子进程，并让所有在途请求失败。 */
  failChild(child, reason) {
    if (this.child === child)
      this.child = null
    this.buffer = ''
    const entries = [...this.pending.entries()]
    this.pending.clear()
    for (const [, entry] of entries) {
      if (entry.abortListener !== undefined && entry.signal !== undefined)
        entry.signal.removeEventListener('abort', entry.abortListener)
      clearTimeout(entry.timer)
      entry.reject(new Error(`MCP ${entry.label} 中断：${reason}`))
    }
    if (child !== null && child.exitCode === null && child.signalCode === null) {
      try {
        child.kill()
      }
      catch (error) {
        this.writeLog('debug', `结束子进程失败：${describeError(error)}`)
      }
    }
  }

  /** 主动结束当前子进程（超时、取消、卸载）。 */
  killChild(reason) {
    const child = this.child
    this.failChild(child, reason)
    return child
  }

  /** 写入一行 JSON-RPC 消息；管道不可用时返回 false。 */
  write(message) {
    const child = this.child
    if (child === null || child.stdin === null || child.stdin.destroyed || !child.stdin.writable)
      return false
    try {
      child.stdin.write(`${JSON.stringify(message)}\n`)
      return true
    }
    catch (error) {
      this.writeLog('debug', `写入 stdin 失败：${describeError(error)}`)
      return false
    }
  }

  /** 结算一个在途请求。 */
  settle(id, outcome) {
    const entry = this.pending.get(id)
    if (entry === undefined)
      return
    this.pending.delete(id)
    clearTimeout(entry.timer)
    if (entry.abortListener !== undefined && entry.signal !== undefined)
      entry.signal.removeEventListener('abort', entry.abortListener)
    if (outcome.error !== undefined)
      entry.reject(outcome.error)
    else
      entry.resolve(outcome.result)
  }

  /**
   * 登记一个请求并写入。id 自增，超时/取消都会结束子进程。
   */
  writeRequest(method, params, timeoutMs, label, signal) {
    const id = this.nextId++
    return new Promise((resolve, reject) => {
      if (signal !== undefined && signal.aborted) {
        reject(new Error(`MCP ${label} 在发起前已被取消`))
        return
      }
      const timer = setTimeout(() => {
        this.settle(id, { error: new Error(`MCP ${label} 超时（${timeoutMs} ms），已结束 python 子进程；下次调用会重新启动`) })
        this.killChild(`MCP ${label} 超时`)
      }, timeoutMs)
      // 注意：不要 unref。在途请求必须把事件循环留住（真实场景下子进程句柄也会留住），
      // 否则无人等待时超时定时器可能永远不触发。请求结算/失败时都会 clearTimeout。
      const entry = { resolve, reject, timer, label, signal, abortListener: undefined }
      if (signal !== undefined) {
        entry.abortListener = () => {
          this.settle(id, { error: new Error(`MCP ${label} 已被调用方取消，已结束 python 子进程`) })
          this.killChild(`MCP ${label} 被取消`)
        }
        signal.addEventListener('abort', entry.abortListener, { once: true })
      }
      this.pending.set(id, entry)
      const written = this.write({ jsonrpc: '2.0', id, method, params: params ?? {} })
      if (!written) {
        this.settle(id, { error: new Error(`MCP ${label} 无法写入请求：python 子进程的 stdin 不可用`) })
      }
    })
  }

  /** 懒启动：没有存活子进程时 spawn + 握手。 */
  async ensureStarted() {
    if (this.disposed)
      throw new Error('MCP 客户端已随插件卸载而关闭')
    if (this.alive())
      return
    if (this.starting !== null)
      return this.starting
    this.starting = this.startAndHandshake().finally(() => {
      this.starting = null
    })
    return this.starting
  }

  /** spawn + initialize + notifications/initialized。 */
  async startAndHandshake() {
    this.buffer = ''
    let child = null
    try {
      // spawnChild 可能同步抛出（例如受限沙箱下的 EPERM），一并包成可读错误。
      child = this.spawnChild()
      const result = await this.writeRequest('initialize', {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: {},
        clientInfo: { name: 'dsh-h3clab', version: '0.1.0' }
      }, this.handshakeTimeoutMs, 'initialize')
      // 通知不带 id，不等待响应。
      this.write({ jsonrpc: '2.0', method: 'notifications/initialized' })
      this.serverInfo = result !== null && typeof result === 'object' ? result.serverInfo ?? null : null
      const version = result !== null && typeof result === 'object' && typeof result.protocolVersion === 'string'
        ? result.protocolVersion
        : '未知'
      const name = this.serverInfo !== null && typeof this.serverInfo.name === 'string' ? this.serverInfo.name : '未知'
      this.writeLog('info', `握手完成：server=${name}，protocolVersion=${version}`)
      return result
    }
    catch (error) {
      this.failChild(child, `握手失败：${describeError(error)}`)
      throw new Error(`无法连接 H3C 实验 MCP 服务器（${this.command} ${this.args.join(' ')}）：${describeError(error)}`)
    }
  }

  /** 发一个带 id 的请求。 */
  async request(method, params, timeoutMs = this.timeoutMs, label = method, signal) {
    await this.ensureStarted()
    return this.writeRequest(method, params, timeoutMs, label, signal)
  }

  /**
   * 拉取 MCP 工具清单（自测与诊断用；工具定义本身是静态的，不依赖它）。
   */
  async listTools(timeoutMs = this.timeoutMs) {
    const collected = []
    let cursor
    for (let page = 0; page < MAX_TOOL_LIST_PAGES; page++) {
      const result = await this.request('tools/list', cursor === undefined ? {} : { cursor }, timeoutMs, 'tools/list')
      const tools = result !== null && typeof result === 'object' && Array.isArray(result.tools) ? result.tools : []
      collected.push(...tools)
      const next = result !== null && typeof result === 'object' && typeof result.nextCursor === 'string' ? result.nextCursor : undefined
      if (next === undefined || next === '')
        break
      cursor = next
    }
    return collected
  }

  /** 调用一个 MCP 工具，返回原始 result。 */
  async callTool(name, args = {}, timeoutMs = this.timeoutMs, signal) {
    return this.request('tools/call', { name, arguments: args ?? {} }, timeoutMs, `tools/call ${name}`, signal)
  }

  /**
   * 调用一个 MCP 工具并把 content[].text 拼成文本。
   * isError === true 时抛出可读错误（DSH 会渲染成失败结果）。
   */
  async callToolText(name, args = {}, timeoutMs = this.timeoutMs, signal) {
    const result = await this.callTool(name, args, timeoutMs, signal)
    const text = extractToolText(result)
    if (result !== null && typeof result === 'object' && result.isError === true) {
      throw new Error(`MCP 工具 ${name} 执行失败：${text === '' ? '（服务器未返回错误文本）' : text}`)
    }
    return text === '' ? `（MCP 工具 ${name} 未返回文本内容）` : text
  }

  /** 结束子进程并禁止再启动；插件卸载时调用。 */
  close() {
    this.disposed = true
    this.killChild('插件已卸载')
  }
}
