/**
 * dsh-h3clab —— 8 个原生 DSH 工具定义。
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
 *   h3c_apply_plan    -> hcl_apply_plan   （唯一有副作用的工具）
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
  h3c_apply_plan: 'hcl_apply_plan'
})

/** 8 个 DSH 工具名（顺序即注册顺序）。 */
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
    description: 'List H3C/HCL lab devices the MCP server can reach, with console ports, hostnames and models. Optional ports narrows the probe (default 30001-30010). Read-only.',
    parameters: {
      ports: { type: 'array', items: { type: 'integer' }, description: 'Only report these console ports.' }
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
    description: 'Read the HCL lab topology (devices and links) from its .net file. Optional net_file overrides the default topology path. Read-only.',
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
    description: 'Run one read-only Comware command on a device console port and return its output. The server accepts only display/show/ping/tracert/traceroute. timeout (seconds) and max_chars cap the wait and the returned text.',
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
    description: 'Collect baseline facts from one device console port (Comware version, clock, board status - a short summary, not raw output). Read-only.',
    parameters: {
      port: { type: 'integer', required: true, description: PORT_DESCRIPTION }
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
    description: 'Check the lab against a checklist JSON file and report PASS/FAIL per item. checklist_json is the PATH of that file; only runs check ids by comma-separated prefix. Read-only on devices.',
    parameters: {
      checklist_json: { type: 'string', required: true, description: 'Path to the checklist JSON file.' },
      only: { type: 'string', description: 'Comma-separated check id prefixes to run.' }
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
    description: 'Probe the listed links and report up/down state and interface counters. Read-only.',
    parameters: {
      links: { type: 'array', required: true, items: LINK_ITEM, description: 'Links to probe: {name?, port, intf, peer_port?, peer_intf?}.' }
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
    description: 'Search the lab memory/notes store for keywords and return matches. any=true ORs the keywords; max caps the hits. Read-only.',
    parameters: {
      keywords: { type: 'array', required: true, items: { type: 'string' }, description: 'Keywords to search for.' },
      any: { type: 'boolean', description: 'Match any keyword instead of all.' },
      max: { type: 'integer', description: 'Maximum number of hits.' }
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
    description: 'Push a config plan to lab devices. plan_json is the PATH of the plan JSON file. DEFAULTS TO dry_run=true: only a preview is produced and NOTHING is sent. A real push requires dry_run=false. save runs "save force"; only limits to comma-separated device names.',
    parameters: {
      plan_json: { type: 'string', required: true, description: 'Path to the plan JSON file.' },
      only: { type: 'string', description: 'Comma-separated device names to limit the push to.' },
      save: { type: 'boolean', description: 'Persist configuration to flash (save force).' },
      dry_run: { type: 'boolean', default: true, description: 'Default true: preview only. Set false to really push.' }
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
