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
import { defineTool } from '@deepseek-ai/dsh-tools'

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
    intf: { type: 'string', required: true, description: 'Local interface name, e.g. GE1/0/1.' },
    peer_port: { type: 'integer', description: 'Console port of the peer device.' },
    peer_intf: { type: 'string', description: 'Peer interface name.' }
  }
}

/** 端口参数的通用描述。 */
const PORT_DESCRIPTION = 'Console/monitor port of the target device, e.g. 2001.'

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
      model: { type: 'boolean', description: 'Read each model with "display version" (default true); false is much faster.' },
      prompt_timeout: { type: 'number', description: 'Seconds to wait for the console prompt per device (default 25).' },
      workers: { type: 'integer', description: 'Parallel probe threads, 1-10 (default 10; capped by the port count).' }
    },
    presentCall: args => ({
      card: 'generic',
      title: 'List H3C lab devices',
      kind: 'read',
      rawInput: args.ports === undefined ? undefined : args.ports.join(', ')
    })
  },
  {
    name: 'h3c_topology',
    kind: 'read',
    description: 'Parse the HCL .net topology into a device table and a link table. Read-only. Errors when no topology is configured (it never guesses).',
    parameters: {
      net_file: { type: 'string', description: 'Path to a .net topology file; default is the lab topology.' }
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
      max_chars: { type: 'integer', description: 'Cap on returned characters.' }
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
      timeout: { type: 'number', description: 'Seconds per console command (default 30).' }
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
    description: 'Run a checklist of assertions across devices and report PASS/FAIL per check. Read-only. Devices run concurrently (workers, default 8); checks on the same port share one connection.',
    parameters: {
      checklist_json: { type: 'string', required: true, description: 'Path to the checklist JSON file.' },
      only: { type: 'string', description: 'Comma-separated check id prefixes to run.' },
      timeout: { type: 'number', description: 'Seconds per check command (default 15).' },
      workers: { type: 'integer', description: 'Concurrent devices, 1-8 (default 8).' }
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
      links: { type: 'array', required: true, items: LINK_ITEM, description: 'Links to probe: {name?, port, intf, peer_port?, peer_intf?}.' },
      timeout: { type: 'number', description: 'Seconds per interface command (default 25).' }
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
      max_lines: { type: 'integer', description: 'Max matched lines shown per section (default 20).' }
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
    description: 'Push a config plan to devices. plan_json is a FILE PATH. DEFAULTS TO dry_run=true: preview only, nothing is sent. A real push needs dry_run=false. save runs "save force"; only limits by device name. Devices are pushed concurrently (workers, default 4).',
    parameters: {
      plan_json: { type: 'string', required: true, description: 'Path to the plan JSON file.' },
      only: { type: 'string', description: 'Comma-separated device names to limit the push to.' },
      save: { type: 'boolean', description: 'Persist configuration to flash (save force).' },
      dry_run: { type: 'boolean', default: true, description: 'Default true: preview only. Set false to really push.' },
      timeout: { type: 'number', description: 'Seconds per console command during apply (default 60).' },
      workers: { type: 'integer', description: 'Parallel devices, 1-5 (default 4; capped by the device count). Ignored for a dry run.' }
    },
    /** dry_run 缺省即预演；只有显式 false 才真下发。 */
    buildArguments: args => ({
      plan_json: args.plan_json,
      ...args.only === undefined ? {} : { only: args.only },
      ...args.save === undefined ? {} : { save: args.save },
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
      probe: { type: 'boolean', description: 'Actually connect to the consoles (default true); false skips section 5.' },
      prompt_timeout: { type: 'number', description: 'Seconds to wait for the console prompt (default 8).' },
      workers: { type: 'integer', description: 'Parallel probe threads, 1-10 (default 10).' }
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
      ports: { type: 'array', items: { type: 'integer' }, description: 'Batch: console ports, handled concurrently.' },
      names: { type: 'array', items: { type: 'string' }, description: 'Batch: device names, parallel to ports.' },
      against: { type: 'string', description: 'Baseline snapshot filename or absolute path.' },
      timeout: { type: 'number', description: 'Seconds per console command (default 60).' },
      max_chars: { type: 'integer', description: 'Cap on captured characters (default 200000).' },
      workers: { type: 'integer', description: 'Concurrent devices in batch mode, 1-8 (default 8).' },
      max_lines: { type: 'integer', description: 'Max diff lines printed (default 200; 0 = unlimited).' }
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
        description: 'Key(s) to delete; a comma-separated string or a string array.'
      },
      note: { type: 'string', description: 'Optional free-text note stored in _note.' }
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
      sections: { type: 'string', description: 'Comma-separated: topology,devices,links,verify,state.' },
      net_file: { type: 'string', description: 'Path to a .net topology file.' },
      ports: { type: 'array', items: { type: 'integer' }, description: 'Ports to probe for the devices section.' },
      links: { type: 'array', items: { type: 'object', additionalProperties: true }, description: 'Links for the links section.' },
      checklist_json: { type: 'string', description: 'Checklist file path for the verify section.' },
      only: { type: 'string', description: 'Comma-separated check id prefixes.' },
      state: { type: 'string', description: 'Lab state file path.' },
      model: { type: 'boolean', description: 'Read models in the devices section (default false, faster).' },
      prompt_timeout: { type: 'number', description: 'Seconds to wait for the console prompt (default 8).' },
      timeout: { type: 'number', description: 'Seconds per console command.' },
      inline: { type: 'boolean', description: 'true = also return the full report text (default false, preview only).' },
      preview_lines: { type: 'integer', description: 'Preview lines when inline is false (default 40).' }
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
      id: { type: 'string', description: 'Entry id; cases auto-assign C-00N.' },
      date: { type: 'string', description: 'Date (default today).' },
      tags: { type: 'string', description: 'Comma-separated keywords for the cases index row.' },
      one_line: { type: 'string', description: 'One-line symptom for the cases index row.' },
      section: { type: 'string', description: "The ## section a gotcha belongs to." },
      file: { type: 'string', description: 'Explicit target file.' },
      dry_run: { type: 'boolean', default: true, description: 'Default true: preview only. Set false to really write.' }
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
 * 构造 8 个工具定义。
 * @param {import('./mcp-client.js').McpStdioClient} client 共享的 stdio MCP 客户端。
 * @param {{toolCallTimeoutMs: number}} config 已解析配置。
 * @returns {Array<object>} 可直接交给 ctx.tools.register 的工具定义。
 */
export function createH3cTools(client, config) {
  const mcpTools = TOOL_MAPPING
  return TOOL_SPECS.map((spec) => {
    const mcpName = mcpTools[spec.name]
    return defineTool({
      name: spec.name,
      description: spec.description,
      parameters: spec.parameters,
      // 外层预算比客户端内部超时略大：正常情况下先由客户端超时给出可读错误。
      timeoutMs: config.toolCallTimeoutMs + 15_000,
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
          const mcpArguments = typeof spec.buildArguments === 'function' ? spec.buildArguments(args) : args
          const text = await client.callToolText(mcpName, mcpArguments, config.toolCallTimeoutMs, signal)
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
