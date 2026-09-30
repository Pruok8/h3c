/**
 * mock MCP 服务器的**协议实现**（不关心传输）。
 *
 * 被两个宿主复用：
 *   - test/mock-mcp-server.mjs  —— 真实子进程，用 process.stdin/stdout 做传输；
 *   - test/in-process-child.mjs —— 沙箱禁止命名管道时，用内存流做传输。
 *
 * 协议：JSON-RPC over stdio，换行分隔。
 */

/** mock 暴露的 8 个工具名，与真实 scripts/server.py 一致。 */
export const TOOL_NAMES = Object.freeze([
  'hcl_list_devices',
  'hcl_topology',
  'hcl_run_command',
  'hcl_get_facts',
  'hcl_verify',
  'hcl_link_watch',
  'hcl_search_memory',
  'hcl_apply_plan'
])

/** 统一的 tools/call 结果。 */
function textResult(text, isError = false) {
  return isError
    ? { isError: true, content: [{ type: 'text', text }] }
    : { content: [{ type: 'text', text }] }
}

/** 各工具的固定回显（不连任何真实设备）。 */
function callTool(name, args) {
  const a = args === null || typeof args !== 'object' ? {} : args
  switch (name) {
    case 'hcl_list_devices':
      return textResult(`devices: 2001=SW1(vlan1), 2002=SW2(vlan1), 2003=R1${a.ports === undefined ? '' : ` (filtered: ${JSON.stringify(a.ports)})`}`)
    case 'hcl_topology':
      return textResult(`topology: SW1 -- SW2 (file=${a.net_file ?? 'default.net'})`)
    case 'hcl_run_command':
      return textResult(`port ${a.port} <${a.command}> => ok (timeout=${a.timeout ?? 'default'}, max_chars=${a.max_chars ?? 'default'})`)
    case 'hcl_get_facts':
      return textResult(`facts port ${a.port}: Comware 7.1.070, 24 interfaces up`)
    case 'hcl_verify':
      return textResult(`verify: 2/2 passed (only=${a.only ?? 'all'}) checklist=${a.checklist_json}`)
    case 'hcl_link_watch':
      return textResult(`link_watch: ${Array.isArray(a.links) ? a.links.length : 0} link(s), all up`)
    case 'hcl_search_memory':
      return textResult(`memory: 1 hit for ${JSON.stringify(a.keywords ?? [])} (any=${a.any ?? false}, max=${a.max ?? 'default'})`)
    case 'hcl_apply_plan':
      return textResult(`apply_plan: dry_run=${a.dry_run === true} only=${a.only ?? 'all'} save=${a.save === true}`)
    // 以下两个是隐藏测试钩子，不出现在 tools/list 里。
    case 'hcl_fail':
      return textResult('device unreachable: connect timeout', true)
    default:
      return undefined
  }
}

/**
 * 构造一个 mock MCP 服务器。
 * @param {{ write: (message: object) => void, stderr?: (text: string) => void, slowMs?: number }} io
 * @returns {{ handleLine: (line: string) => void }}
 */
export function createMockServer(io) {
  const write = io.write
  const stderr = io.stderr ?? (() => {})
  const slowMs = io.slowMs ?? 5000
  const respond = (id, result) => write({ jsonrpc: '2.0', id, result })
  const respondError = (id, code, message) => write({ jsonrpc: '2.0', id, error: { code, message } })

  return {
    handleLine(line) {
      let message
      try {
        message = JSON.parse(line)
      }
      catch {
        stderr(`mock: 收到非 JSON 行：${line.slice(0, 80)}\n`)
        return
      }
      const { id, method, params } = message
      stderr(`mock: <- ${method}\n`)
      switch (method) {
        case 'initialize':
          respond(id, {
            protocolVersion: '2024-11-05',
            capabilities: { tools: { listChanged: false } },
            serverInfo: { name: 'h3c-lab-mock', version: '0.1.0' }
          })
          return
        case 'notifications/initialized':
          // 通知：不回复。
          return
        case 'tools/list':
          respond(id, {
            tools: TOOL_NAMES.map(name => ({ name, description: `mock ${name}`, inputSchema: { type: 'object' } }))
          })
          return
        case 'tools/call': {
          const name = params === null || typeof params !== 'object' ? undefined : params.name
          const args = params === null || typeof params !== 'object' ? {} : params.arguments
          if (name === 'hcl_slow') {
            // 故意不回复：客户端应在超时后 kill 本进程。
            stderr(`mock: hcl_slow sleeping ${slowMs} ms\n`)
            return
          }
          const result = callTool(name, args)
          if (result === undefined) {
            respondError(id, -32602, `unknown tool: ${name}`)
            return
          }
          respond(id, result)
          return
        }
        default:
          if (id !== undefined)
            respondError(id, -32601, `method not found: ${method}`)
      }
    }
  }
}
