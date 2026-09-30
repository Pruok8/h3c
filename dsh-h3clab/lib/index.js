/**
 * dsh-h3clab —— DeepSeek Harness 的 H3C 实验自动化工具桥。
 *
 * 本插件不实现任何设备驱动：它用 stdio 与一个 MCP 服务器
 * （scripts/server.py）通信，把 MCP 工具转成 8 个 DSH 原生工具。
 *
 * Cordis 入口约定：导出 name / inject / apply（以及给加载器用的 Config）。
 */
import { dirname } from 'node:path'
import { resolveConfig } from './config.js'
import { McpStdioClient } from './mcp-client.js'
import { createH3cTools } from './tools.js'

/** 插件名，必须与 cordis.patch.yml 里的 name 一致。 */
export const name = 'dsh-h3clab'
/** 依赖 tools 服务（工具注册表）。 */
export const inject = ['tools']

export { Config, resolveConfig, DEFAULT_SERVER_PATH } from './config.js'
export { McpStdioClient } from './mcp-client.js'
export { TOOL_MAPPING, TOOL_NAMES, createH3cTools } from './tools.js'

/**
 * Cordis 挂载点。
 * @param {object} ctx 插件上下文（提供 tools 服务、logger、effect）。
 * @param {object} [config] cordis.patch.yml 中的 config。
 */
export function apply(ctx, config) {
  const resolved = resolveConfig(config)
  const logger = ctx.logger('dsh-h3clab')
  /** 日志出口：level 缺失时忽略，日志异常绝不影响运行。 */
  const log = (level, message) => {
    try {
      const write = logger === undefined || logger === null ? undefined : logger[level]
      if (typeof write === 'function')
        write.call(logger, message)
    }
    catch {
      /* 忽略 */
    }
  }

  const client = new McpStdioClient({
    command: resolved.pythonCommand,
    args: [resolved.serverPath],
    cwd: dirname(resolved.serverPath),
    // 强制 UTF-8，避免 Windows 上 python 以 cp936 输出导致中文乱码。
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    timeoutMs: resolved.toolCallTimeoutMs,
    serverName: resolved.serverName,
    log
  })

  // 插件卸载时回收 python 子进程。
  // Cordis 4 的 ctx.effect(body) 里 body 立即执行并返回 disposer，disposer 在
  // fiber 卸载时按后进先出执行；ctx.on('dispose') 在 cordis 4 中并不是公开事件，
  // 因此只作为 effect 不可用时的兜底。
  const disposeClient = () => {
    client.close()
  }
  if (typeof ctx.effect === 'function')
    ctx.effect(() => disposeClient, 'dsh-h3clab: mcp-stdio-client')
  else if (typeof ctx.on === 'function')
    ctx.on('dispose', disposeClient)

  const tools = createH3cTools(client, resolved)
  for (const tool of tools)
    ctx.tools.register(tool)

  log('info', `已注册 ${tools.length} 个 H3C 工具（python=${resolved.pythonCommand}，server=${resolved.serverPath}）；首次调用时才启动 MCP 服务器`)
  return undefined
}
