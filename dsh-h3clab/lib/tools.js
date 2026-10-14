/**
 * dsh-h3clab —— 13 个原生 DSH 工具定义。
 *
 * 每个工具都【显式声明参数】（不从 MCP 动态拉 schema），execute 里把参数
 * 透传成 MCP `tools/call` 的 arguments，并把返回的 content[].text 拼起来。
 *
 * 命名映射（DSH 工具名 -> MCP 原始工具名）：
 *   h3c_devices       -> hcl_list_devices
 *   h3c_topology      -> hcl_topology
 *   h3c_run           -> hcl_run_command
 *   h3c_facts         -> hcl_get_facts
 *   h3c_verify        -> hcl_verify
 *   h3c_link_watch    -> hcl_link_watch
 *   h3c_memory_search -> hcl_search_memory
 *   h3c_apply_plan    -> hcl_apply_plan      （改设备配置，默认 dry_run）
 *   h3c_doctor        -> hcl_doctor          （环境自检，只读）
 *   h3c_cfgdiff       -> hcl_cfgdiff         （配置快照/对比，设备侧只读）
 *   h3c_lab_state     -> hcl_lab_state       （本地状态文件，写本地）
 *   h3c_report        -> hcl_report          （汇总报告，设备侧只读）
 *   h3c_memory_write  -> hcl_memory_write    （写记忆库，默认 dry_run）
 */
import { readFileSync } from 'node:fs'
import { defineTool } from '@deepseek-ai/dsh-tools'
import {
  PORT_SOURCES,
  PORT_SOURCE_LABEL,
  createHclDiscovery,
  expectedHostnamesFor,
  formatProjects,
  parseNetTopology,
  planDevicePorts,
  verifyDeviceProbe
} from './hcl-discover.js'
import { DEFAULT_LOCK_WAIT_MS, PortLockRegistry, formatLockLabel, lockKeysForTool } from './port-lock.js'

/** 工具 -> MCP 原始工具名映射，导出以便自测与文档核对。 */
export const TOOL_MAPPING = Object.freeze({
  h3c_devices: 'hcl_list_devices',
  h3c_topology: 'hcl_topology',
  h3c_run: 'hcl_run_command',
  h3c_facts: 'hcl_get_facts',
  h3c_verify: 'hcl_verify',
  h3c_link_watch: 'hcl_link_watch',
  h3c_memory_search: 'hcl_search_memory',
  h3c_apply_plan: 'hcl_apply_plan',
  h3c_doctor: 'hcl_doctor',
  h3c_cfgdiff: 'hcl_cfgdiff',
  h3c_lab_state: 'hcl_lab_state',
  h3c_report: 'hcl_report',
  h3c_memory_write: 'hcl_memory_write'
})

/** 全部 DSH 工具名（顺序即注册顺序）。 */
export const TOOL_NAMES = Object.freeze(Object.keys(TOOL_MAPPING))

/** 链路探测参数的单个元素结构。 */
const LINK_ITEM = {
  type: 'object',
  additionalProperties: false,
  properties: {
    name: { type: 'string', description: 'Label for this link.' },
    port: { type: 'integer', required: true, description: 'Console port of the local device.' },
    intf: { type: 'string', required: true, description: 'Local interface, e.g. GE1/0/1.' },
    peer_port: { type: 'integer', description: 'Console port of the peer device.' },
    peer_intf: { type: 'string', description: 'Peer interface name.' }
  }
}

/** 端口参数的通用描述。 */
const PORT_DESCRIPTION = 'Console port, e.g. 2001.'

/**
 * 每个工具的静态定义。
 * kind 取自 @deepseek-ai/dsh-tools 的 ToolCallKind：
 * 'read' | 'edit' | 'delete' | 'move' | 'search' | 'execute' | 'fetch' | 'other'。
 */
const TOOL_SPECS = [
  {
    name: 'h3c_devices',
    kind: 'read',
    description: 'List reachable H3C/HCL lab consoles (port, hostname, model, state). Read-only. Omit ports to use the configured ports, or the ports derived from the topology.',
    parameters: {
      ports: { type: 'array', items: { type: 'integer' }, description: 'Only report these console ports.' },
      project: { type: 'string', description: 'HCL 工程名：自动发现该工程的控制台端口段（例如 "mylab20"，或 "mylab20#3" 取第 3 个端口）。比写死 "30000+device_id" 可靠 —— 每个工程窗口的端口基址不同，写死会连到另一个工程的设备上。省略则优先按 netFile 自动匹配工程。' },
      model: { type: 'boolean', description: 'Read model via display version (default true); false = faster.' },
      prompt_timeout: { type: 'number', description: 'Prompt wait sec/device (default 25).' },
      workers: { type: 'integer', description: 'Threads 1-10 (default 10).' }
    },
    /**
     * 参数预处理：把 `project` / `netFile` 变成**明确的 ports**，并带回反验上下文。
     * ★ 服务器（server.py）不认 `project` 参数，必须在这里摘掉再往下传。
     */
    async prepareArguments(args, deps) {
      return prepareDeviceArguments(args, deps)
    },
    /** 探完设备后**反验**：回显里的主机名与拓扑是否一致（“连上了 ≠ 连对了”）。 */
    verifyResult(text, prepared) {
      if (prepared?.verification === undefined)
        return text
      const check = verifyDeviceProbe(text, prepared.verification)
      return `${text}\n\n${check.line}`
    },
    presentCall: args => ({
      card: 'generic',
      title: 'List H3C lab devices',
      kind: 'read',
      rawInput: args.project ?? (args.ports === undefined ? undefined : args.ports.join(', '))
    })
  },
  {
    name: 'h3c_topology',
    kind: 'read',
    description: 'Parse the HCL .net topology into a device table and a link table. Read-only. Errors when no topology is configured (it never guesses).',
    parameters: {
      net_file: { type: 'string', description: 'Path to a .net topology file; default = configured.' }
    },
    presentCall: args => ({
      card: 'generic',
      title: 'Read H3C lab topology',
      kind: 'read',
      rawInput: args.net_file
    })
  },
  {
    name: 'h3c_run',
    kind: 'read',
    description: 'Run ONE read-only Comware command on a console port and return its output. Only display/show/ping/tracert/traceroute are accepted.',
    parameters: {
      port: { type: 'integer', required: true, description: PORT_DESCRIPTION },
      command: { type: 'string', required: true, description: 'Single Comware command line.' },
      timeout: { type: 'number', description: 'Seconds to wait for output.' },
      max_chars: { type: 'integer', description: 'Cap on returned chars (default 4000; the rest goes to a file).' }
    },
    presentCall: args => ({
      card: 'generic',
      title: `Run "${args.command}" on port ${args.port}`,
      kind: 'read',
      rawInput: args.command
    })
  },
  {
    name: 'h3c_facts',
    kind: 'read',
    description: 'Collect a short baseline summary from one device: model, software version, uptime, clock, boards. Read-only.',
    parameters: {
      port: { type: 'integer', required: true, description: PORT_DESCRIPTION },
      timeout: { type: 'number', description: 'Sec/command (default 30).' }
    },
    presentCall: args => ({
      card: 'generic',
      title: `Collect facts from port ${args.port}`,
      kind: 'read',
      rawInput: args.port
    })
  },
  {
    name: 'h3c_verify',
    kind: 'read',
    // 跨设备并发跑整份清单，设备多时也需要更宽的预算
    minTimeoutMs: 600_000,
    description: 'Run a checklist of assertions across devices and report PASS/FAIL per check. Read-only. Devices run concurrently (workers, default 8); checks on the same port share one connection.',
    parameters: {
      checklist_json: { type: 'string', required: true, description: 'Path to the checklist JSON file.' },
      only: { type: 'string', description: 'Check id prefixes to run (CSV).' },
      timeout: { type: 'number', description: 'Sec/check cmd (default 15).' },
      workers: { type: 'integer', description: 'Concurrency 1-8 (default 8).' }
    },
    presentCall: args => ({
      card: 'generic',
      title: args.only === undefined ? 'Verify H3C lab checks' : `Verify check ${args.only}`,
      kind: 'read',
      rawInput: args.checklist_json
    })
  },
  {
    name: 'h3c_link_watch',
    kind: 'read',
    description: 'Probe the up/down state of the listed links (local and peer). Read-only, never resets an interface. Peer names are measured from the console, not taken from config.',
    parameters: {
      links: { type: 'array', required: true, items: LINK_ITEM, description: 'Links: {name?, port, intf, peer_port?, peer_intf?}.' },
      timeout: { type: 'number', description: 'Sec/interface cmd (default 25).' }
    },
    presentCall: args => ({
      card: 'generic',
      title: `Watch ${Array.isArray(args.links) ? args.links.length : 0} link(s)`,
      kind: 'read',
      rawInput: undefined
    })
  },
  {
    name: 'h3c_memory_search',
    kind: 'read',
    description: 'Search the lab memory (cases.md / gotchas.md / aliases.md) with synonym expansion. Read-only.',
    parameters: {
      keywords: { type: 'array', required: true, items: { type: 'string' }, description: 'Keywords to search for.' },
      any: { type: 'boolean', description: 'Match any keyword instead of all.' },
      max: { type: 'integer', description: 'Maximum number of hits.' },
      max_lines: { type: 'integer', description: 'Max lines per section (default 20).' }
    },
    presentCall: args => ({
      card: 'generic',
      title: `Search lab memory for ${Array.isArray(args.keywords) ? args.keywords.join(', ') : ''}`,
      kind: 'search',
      rawInput: Array.isArray(args.keywords) ? args.keywords.join(' ') : undefined
    })
  },
  {
    name: 'h3c_apply_plan',
    kind: 'edit',
    // 批量写：真机上一次多台下发要好几分钟，绝不能按读命令的预算砍掉
    minTimeoutMs: 900_000,
    description: 'Push a config plan to devices. plan_json is a FILE PATH. DEFAULTS TO dry_run=true: preview only, nothing is sent. A real push needs dry_run=false. save runs "save force"; only limits by device name. Devices are pushed concurrently (workers, default 4). The dry-run prints per-device counts, a 3-command sample and pre-flight checks (destructive commands, auto-answered [Y/N], devices without a port) instead of echoing the whole plan you just wrote; pass echo_commands=true to see every command.',
    parameters: {
      plan_json: { type: 'string', required: true, description: 'Path to the plan JSON file.' },
      only: { type: 'string', description: 'Device names to limit to (CSV).' },
      save: { type: 'boolean', description: 'Persist to flash (save force).' },
      dry_run: { type: 'boolean', default: true, description: 'Default true = preview; false = push.' },
      timeout: { type: 'number', description: 'Sec/command during apply (default 60).' },
      workers: { type: 'integer', description: 'Parallel devices, 1-5 (default 4).' },
      echo_commands: { type: 'boolean', description: 'Dry-run: echo every command (default false = counts + sample + pre-flight).' },
      verify_changed: { type: 'boolean', description: 'Read back current-configuration before/after the push and flag commands that changed nothing, plus "changed but not saved" (default false; slower).' },
      allow_destructive: { type: 'boolean', description: 'Default false: reboot / reset saved-configuration / format are REFUSED. Pass true only when you really mean it; every allowed command is acknowledged in the result.' }
    },
    /**
     * dry_run 缺省即预演；只有显式 false 才真下发。
     * ★ 其余参数必须**原样透传**：这里以前只列了 4 个字段，把 timeout / workers
     *   静默丢掉了（工具 schema 里声明了、调用方也传了，但到不了服务器）。
     */
    buildArguments: args => ({
      ...args,
      dry_run: args.dry_run !== false
    }),
    presentCall: args => ({
      card: 'generic',
      title: args.dry_run === false
        ? 'Apply H3C plan (REAL push)'
        : 'Preview H3C plan (dry run)',
      kind: 'edit',
      rawInput: args.only ?? undefined
    })
  },
  {
    name: 'h3c_doctor',
    kind: 'read',
    description: 'Environment self-check: where the config came from, how ports were chosen, topology and directories, and console reachability. Read-only.',
    parameters: {
      ports: { type: 'array', items: { type: 'integer' }, description: 'Override the ports to probe.' },
      net_file: { type: 'string', description: 'Path to a .net topology file.' },
      probe: { type: 'boolean', description: 'Connect to consoles (default true); false skips it.' },
      prompt_timeout: { type: 'number', description: 'Prompt wait sec (default 8).' },
      workers: { type: 'integer', description: 'Threads 1-10 (default 10).' }
    },
    presentCall: () => ({
      card: 'generic',
      title: 'H3C lab environment self-check',
      kind: 'read',
      rawInput: undefined
    })
  },
  {
    name: 'h3c_cfgdiff',
    kind: 'read',
    description: 'Snapshot running-config ("display current-configuration") and diff it against a baseline. One device (port) or many at once (ports, run concurrently). Devices are read-only. The printed diff is capped by max_lines (default 200); the full diff is written to a file.',
    parameters: {
      action: { type: 'string', enum: ['snapshot', 'diff', 'list'], description: 'Default diff.' },
      port: { type: 'integer', description: 'Console port (single device).' },
      ports: { type: 'array', items: { type: 'integer' }, description: 'Batch: console ports (concurrent).' },
      names: { type: 'array', items: { type: 'string' }, description: 'Batch: names, parallel to ports.' },
      against: { type: 'string', description: 'Baseline snapshot name or path.' },
      timeout: { type: 'number', description: 'Sec/command (default 60).' },
      max_chars: { type: 'integer', description: 'Cap on captured chars (default 200000).' },
      workers: { type: 'integer', description: 'Batch concurrency 1-8 (default 8).' },
      max_lines: { type: 'integer', description: 'Max diff lines per device (default 200; 0 = all).' },
      detail: { type: 'string', enum: ['auto', 'summary', 'changed', 'full'], description: 'Diff granularity; auto = unified diff for one device, summary for a batch.' },
      grep: { type: 'string', description: 'Regex filtering changed lines, e.g. isis|bgp.' }
    },
    presentCall: args => ({
      card: 'generic',
      title: `Config ${args.action ?? 'diff'}${args.port === undefined ? (Array.isArray(args.ports) ? ` on ${args.ports.length} device(s)` : '') : ` on port ${args.port}`}`,
      kind: 'read',
      rawInput: args.name ?? undefined
    })
  },
  {
    name: 'h3c_lab_state',
    kind: 'edit',
    description: 'Read/write the lab state JSON to remember progress across calls. Every write backs up to <file>.bak-<timestamp> first.',
    parameters: {
      action: { type: 'string', enum: ['get', 'set', 'merge', 'delete', 'history'], description: 'Default get.' },
      path: { type: 'string', description: 'Alternative state file path.' },
      data: { type: 'object', additionalProperties: true, description: 'Object to write (set/merge).' },
      key: {
        oneOf: [{ type: 'string' }, { type: 'array', items: { type: 'string' } }],
        description: 'Key(s) to delete: CSV string or array.'
      },
      note: { type: 'string', description: 'Free-text note stored in _note.' }
    },
    presentCall: args => ({
      card: 'generic',
      title: `Lab state ${args.action ?? 'get'}`,
      kind: 'edit',
      rawInput: args.path ?? undefined
    })
  },
  {
    name: 'h3c_report',
    kind: 'read',
    description: 'Compose topology + devices + links + verify + state into a Markdown report and save it. Devices are read-only. Returns the file path plus the report body.',
    parameters: {
      title: { type: 'string', description: 'Report title.' },
      out: { type: 'string', description: 'Output path for the report.' },
      sections: { type: 'string', description: 'CSV: topology,devices,links,verify,state.' },
      net_file: { type: 'string', description: 'Path to a .net topology file.' },
      ports: { type: 'array', items: { type: 'integer' }, description: 'Ports for the devices section.' },
      links: { type: 'array', items: { type: 'object', additionalProperties: true }, description: 'Links for the links section.' },
      checklist_json: { type: 'string', description: 'Checklist path (verify section).' },
      only: { type: 'string', description: 'Check id prefixes (CSV).' },
      state: { type: 'string', description: 'Lab state file path.' },
      model: { type: 'boolean', description: 'Read models (default false, faster).' },
      prompt_timeout: { type: 'number', description: 'Prompt wait sec (default 8).' },
      timeout: { type: 'number', description: 'Seconds per console command.' },
      inline: { type: 'boolean', description: 'true = return full report text (default false).' },
      preview_lines: { type: 'integer', description: 'Preview lines (default 40).' }
    },
    presentCall: args => ({
      card: 'generic',
      title: args.title ?? 'H3C lab report',
      kind: 'read',
      rawInput: args.out ?? undefined
    })
  },
  {
    name: 'h3c_memory_write',
    kind: 'edit',
    description: 'Append a lesson to the lab memory (cases.md or gotchas.md). DEFAULTS TO dry_run=true: preview only, nothing is written. A real write needs dry_run=false. Backs the file up first and only appends to an existing file.',
    parameters: {
      kind: { type: 'string', enum: ['case', 'gotcha'], description: 'Default case.' },
      title: { type: 'string', required: true, description: 'Entry title.' },
      body: { type: 'string', required: true, description: 'Markdown body.' },
      id: { type: 'string', description: 'Entry id (cases: auto C-00N).' },
      date: { type: 'string', description: 'Date (default today).' },
      tags: { type: 'string', description: 'Index-row keywords (CSV).' },
      one_line: { type: 'string', description: 'One-line symptom for index row.' },
      section: { type: 'string', description: "The ## section it belongs to." },
      file: { type: 'string', description: 'Explicit target file.' },
      dry_run: { type: 'boolean', default: true, description: 'Default true = preview; false = write.' }
    },
    /** dry_run 缺省即预演；只有显式 false 才真写。 */
    buildArguments: args => ({
      kind: args.kind ?? 'case',
      title: args.title,
      body: args.body,
      ...args.id === undefined ? {} : { id: args.id },
      ...args.date === undefined ? {} : { date: args.date },
      ...args.tags === undefined ? {} : { tags: args.tags },
      ...args.one_line === undefined ? {} : { one_line: args.one_line },
      ...args.section === undefined ? {} : { section: args.section },
      ...args.file === undefined ? {} : { file: args.file },
      dry_run: args.dry_run !== false
    }),
    presentCall: args => ({
      card: 'generic',
      title: args.dry_run === false
        ? `Write memory (REAL) ${args.title ?? ''}`.trim()
        : `Preview memory entry ${args.title ?? ''}`.trim(),
      kind: 'edit',
      rawInput: args.kind ?? undefined
    })
  }
]

/**
 * h3c_devices 的参数预处理：把 `project` / `netFile` 变成**明确的 ports**。
 *
 * 端口来源的分层置信度（见 lib/hcl-discover.js 的 planDevicePorts）：
 *   显式 ports > netstat 实测（该工程 PID 正在监听的端口段）> 拓扑 device_id 推算 > 兜底。
 *
 * 返回 `{ arguments, note, verification }`：
 *   - `arguments` 是要真正发给 MCP 服务器的参数（**不含** `project`）；
 *   - `note` 是要拼在回显前面的解释（命中了哪个工程 / 为什么警告）；
 *   - `verification` 交给 `verifyResult()` 做连接后的反验。
 * @param {Record<string, unknown>} args 工具入参。
 * @param {object} deps `{ discovery, netFile, topologyText, log }`。
 */
async function prepareDeviceArguments(args, deps) {
  const safeArgs = args === null || typeof args !== 'object' ? {} : args
  const out = { ...safeArgs }
  delete out.project
  const discovery = deps?.discovery ?? null
  const netFile = typeof deps?.netFile === 'string' ? deps.netFile : ''
  const explicitPorts = Array.isArray(safeArgs.ports) ? safeArgs.ports : []
  const project = typeof safeArgs.project === 'string' ? safeArgs.project.trim() : ''

  /** 显式指名工程：直接按名字解析，识别不了就报可读错误（不静默猜）。 */
  if (project !== '' && discovery !== null) {
    const selected = discovery.select(project)
    if (!selected.ok) {
      throw new Error(`${selected.reason}。可用 h3c_devices（不带 project）先看配置端口，`
        + '并确认 HCL 已打开该工程、设备已启动。')
    }
    const plan = {
      source: PORT_SOURCES.NETSTAT,
      ports: [...selected.ports],
      project: selected.project,
      base: selected.project.base ?? null,
      expected: expectedHostnamesFor(deps?.topology, selected.ports, [selected.project.base, 30000]),
      suspectedWrongProject: false,
      projects: selected.discovery?.projects ?? [],
      note: ''
    }
    out.ports = [...selected.ports]
    return {
      arguments: out,
      note: `✔ HCL 工程 ${selected.project.project}（pid=${selected.project.pid}，${selected.project.path || '路径未知'}）：`
        + `使用控制台端口 ${selected.ports.join('、')}（${selected.reason}）`,
      verification: plan
    }
  }

  // 没指名工程：按置信度选端口来源（显式 > netstat 实测 > device_id 推算 > 兜底）。
  const discoveredProjects = discovery === null ? [] : (discovery.discover().projects ?? [])
  const plan = planDevicePorts({
    explicitPorts,
    discovery,
    netFile,
    topology: deps?.topology ?? null
  })
  const notes = []
  if (plan.ports.length > 0) {
    out.ports = [...plan.ports]
    const label = PORT_SOURCE_LABEL[plan.source] ?? plan.source
    const projectText = plan.project === null ? '' : `（工程 ${plan.project.project}：${plan.project.ports[0]}-${plan.project.ports.at(-1)}）`
    notes.push(`✔ 端口来源：${label}${projectText} → ${plan.ports.length} 个端口`)
  }
  else if (discoveredProjects.length > 0) {
    // 既没显式端口、也没法推算：仍然给出“有哪些工程可用”的提示，避免 20 分钟人肉排查。
    notes.push(`⚠ 未能确定端口：服务器会按「30000 + device_id」或内置兜底范围推导。已发现 HCL 工程：\n`
      + `${formatProjects(discoveredProjects)}\n如果目标不是 30000+ 段，请加 project: "<工程名>"（或显式传 ports）。`)
  }
  if (plan.note !== '')
    notes.push(plan.note)
  return {
    arguments: out,
    note: notes.filter(item => item !== '').join('\n'),
    verification: {
      ...plan,
      // 反验用的期望值已由 planDevicePorts 按**最终端口**从拓扑推好（含工程基址）。
      expected: plan.expected ?? null,
      projects: discoveredProjects
    }
  }
}

/** 把任意抛出物包成带工具名的可读错误，绝不把原始异常直接抛崩插件。 */
function toReadableError(toolName, error) {
  if (error instanceof Error)
    return new Error(`${toolName} 调用失败：${error.message}`)
  if (typeof error === 'string')
    return new Error(`${toolName} 调用失败：${error}`)
  try {
    return new Error(`${toolName} 调用失败：${JSON.stringify(error)}`)
  }
  catch {
    return new Error(`${toolName} 调用失败：无法序列化的异常`)
  }
}

/**
 * 构造 13 个工具定义。
 * @param {import('./mcp-client.js').McpStdioClient} client 共享的 stdio MCP 客户端。
 * @param {object} config 已解析配置（resolveConfig 的结果）外加可选注入点：
 *   `log`（日志出口）、`discovery`（HCL 工程发现器，测试用）、`portLock`（锁注册表，测试用）。
 * @returns {Array<object>} 可直接交给 ctx.tools.register 的工具定义。
 */
export function createH3cTools(client, config) {
  const settings = config === null || typeof config !== 'object' ? {} : config
  const mcpTools = TOOL_MAPPING
  const log = typeof settings.log === 'function' ? settings.log : () => {}

  // HCL 工程发现器：懒探测 + TTL 缓存；非 Windows 直接不可用（回退到配置逻辑）。
  const discovery = settings.discovery ?? createHclDiscovery({ log })
  // per-port 互斥锁：同一 console port 上的 h3c_* 调用串行执行。
  const lockWaitMs = Number.isFinite(settings.portLockWaitMs) ? Number(settings.portLockWaitMs) : DEFAULT_LOCK_WAIT_MS
  const lockEnabled = settings.portLockEnabled !== false
  const portLock = settings.portLock instanceof PortLockRegistry
    ? settings.portLock
    : new PortLockRegistry({ waitMs: lockWaitMs, log })

  /**
   * 拓扑只读一次（netFile 不变就复用），用于 `device_id` 候选与反验期望值。
   * ★ 这里**不**预计算“端口 -> 期望主机名”：期望值必须跟着**实际选中的端口**
   *   （工程基址可能是 31000）走，由 `expectedHostnamesFor()` 现算，见 lib/hcl-discover.js。
   */
  let topologyCache = null
  const topologyOf = () => {
    if (topologyCache !== null)
      return topologyCache.value
    let value = null
    const netFile = typeof settings.netFile === 'string' ? settings.netFile : ''
    if (netFile !== '') {
      try {
        // 与 server.py 同构：utf-8-sig 容忍 BOM（\ufeff 不是空白字符，会静默少一台设备）。
        const text = readFileSyncSafe(netFile)
        if (typeof text === 'string' && text !== '')
          value = parseNetTopology(text)
      }
      catch {
        /* 拓扑读不了就只影响反验与推算，不影响原有行为 */
      }
    }
    topologyCache = { value }
    return value
  }

  const deps = {
    discovery,
    netFile: typeof settings.netFile === 'string' ? settings.netFile : '',
    get topology() {
      return topologyOf()
    },
    log,
    portLock
  }

  return TOOL_SPECS.map((spec) => {
    const mcpName = mcpTools[spec.name]
    // ★ 每个工具可以有**自己的超时下限**。批量写工具（计划下发）天然比读命令慢得多：
    //   一次 9 台 714 条命令在真机上要好几分钟。以前所有工具共用 toolCallTimeoutMs
    //   （默认 4 分钟），于是下发到一半被客户端砍掉 —— 设备留在半配置状态、控制台停在
    //   某个子视图、结果全丢（lab4_ts 实测）。这里给写类工具一个更宽的下限。
    const callTimeoutMs = Math.max(
      Number.isFinite(settings.toolCallTimeoutMs) ? settings.toolCallTimeoutMs : 240_000,
      spec.minTimeoutMs ?? 0
    )
    return defineTool({
      name: spec.name,
      description: spec.description,
      parameters: spec.parameters,
      // 外层预算比客户端内部超时略大：正常情况下先由客户端超时给出可读错误。
      timeoutMs: callTimeoutMs + 15_000,
      output: {
        schema: {
          type: 'object',
          additionalProperties: false,
          properties: {
            text: { type: 'string', required: true }
          }
        },
        render: (_args, value) => [{ type: 'text', text: value.text }]
      },
      async execute(args, exec) {
        try {
          const signal = exec === undefined || exec === null ? undefined : exec.signal
          if (signal !== undefined && signal.aborted)
            throw new Error('调用已被取消')
          // 1) 参数预处理（可能要去探 HCL 工程 / 读拓扑）——这一步不加锁：
          //    它不碰设备，加锁只会把等待时间拉长。
          const prepared = typeof spec.prepareArguments === 'function'
            ? (await spec.prepareArguments(args === null || typeof args !== 'object' ? {} : args, deps)) ?? {}
            : {}
          const baseArgs = prepared.arguments ?? args ?? {}
          const mcpArguments = typeof spec.buildArguments === 'function' ? spec.buildArguments(baseArgs) : baseArgs
          // 2) per-port 互斥：**按最终参数**算锁键（h3c_devices 的 ports 可能刚被工程发现补全）。
          const need = lockEnabled ? lockKeysForTool(spec.name, mcpArguments) : { keys: [], ports: [], label: spec.name, detail: '' }
          const release = need.keys.length > 0
            ? await portLock.acquire(need.keys, {
                label: formatLockLabel(spec.name, need.keys, need.ports),
                waitMs: lockWaitMs,
                signal
              })
            : null
          // 3) 真正的 MCP 调用；无论成败都必须把锁还回去。
          let text
          try {
            text = await client.callToolText(mcpName, mcpArguments, callTimeoutMs, signal)
          }
          finally {
            if (release !== null)
              release()
          }
          // 4) 可选的反验（“连上了 ≠ 连对了”）与解释性说明。
          if (typeof spec.verifyResult === 'function')
            text = spec.verifyResult(text, prepared)
          if (typeof prepared.note === 'string' && prepared.note !== '')
            text = `${prepared.note}\n\n${text}`
          return { text }
        }
        catch (error) {
          throw toReadableError(spec.name, error)
        }
      },
      presentCall: spec.presentCall
    })
  })
}

/** 读一个文本文件；读不到返回 null（调用方自行决定回退）。 */
function readFileSyncSafe(path) {
  try {
    return readFileSync(path, 'utf8')
  }
  catch {
    return null
  }
}
