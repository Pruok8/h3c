/**
 * dsh-h3clab —— DeepSeek Harness 的 H3C 实验自动化工具桥。
 *
 * 本插件不实现任何设备驱动：它用 stdio 与一个 MCP 服务器
 * （scripts/server.py）通信，把 MCP 工具转成 DSH 原生工具。
 *
 * Cordis 入口约定：导出 name / inject / apply（以及给加载器用的 Config）。
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { buildServerConfig, resolveConfig } from './config.js'
import { McpStdioClient } from './mcp-client.js'
import { createH3cTools } from './tools.js'

/** 插件名，必须与 cordis.patch.yml 里的 name 一致。 */
export const name = 'dsh-h3clab'
/** 依赖 tools 服务（工具注册表）。 */
export const inject = ['tools']

export {
  Config,
  DEFAULT_SERVER_PATH,
  buildServerConfig,
  defaultEvidenceRoot,
  defaultReferencesDir,
  defaultServerConfigPath,
  defaultStateDir,
  dshHome,
  resolveConfig
} from './config.js'
export { McpStdioClient } from './mcp-client.js'
export { TOOL_MAPPING, TOOL_NAMES, createH3cTools } from './tools.js'

/**
 * 把实验层配置写成 JSON，供 server.py 通过 `H3C_MCP_CONFIG` 读取。
 *
 * 内容不变时不重写，避免每次加载都刷新文件时间戳。
 * @param {ReturnType<typeof resolveConfig>} resolved 已解析配置。
 * @param {(level: string, message: string) => void} log 日志出口。
 * @returns {string | null} 成功时返回配置文件的绝对路径；失败返回 null。
 */
export function writeServerConfig(resolved, log = () => {}) {
  const payload = buildServerConfig(resolved)
  const text = `${JSON.stringify(payload, null, 2)}\n`
  const target = resolved.serverConfigPath
  try {
    mkdirSync(dirname(target), { recursive: true })
  }
  catch (error) {
    log('warn', `无法创建配置目录 ${dirname(target)}：${error.message}；将改用服务器内置默认配置`)
    return null
  }
  try {
    const current = readFileSync(target, 'utf8')
    if (current === text)
      return target
  }
  catch {
    /* 文件不存在或读不了，继续写 */
  }
  try {
    writeFileSync(target, text, 'utf8')
    return target
  }
  catch (error) {
    log('warn', `无法写入服务器配置 ${target}：${error.message}；将改用服务器内置默认配置`)
    return null
  }
}

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

  // 实验层配置落到 JSON，再让 python 自己读；只有写成功时才注入 H3C_MCP_CONFIG，
  // 否则让 server.py 回落到它自己的默认值/手工 h3c_lab_mcp.json，而不是读一个空文件。
  const serverConfigFile = writeServerConfig(resolved, log)
  const childEnv = {
    ...process.env,
    // 强制 UTF-8，避免 Windows 上 python 以 cp936 输出导致中文乱码。
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    ...resolved.env
  }
  if (serverConfigFile !== null)
    childEnv.H3C_MCP_CONFIG = serverConfigFile

  const client = new McpStdioClient({
    command: resolved.pythonCommand,
    args: [resolved.serverPath],
    cwd: dirname(resolved.serverPath),
    env: childEnv,
    timeoutMs: resolved.toolCallTimeoutMs,
    serverName: resolved.serverName,
    // 改了 server.py 但进程还是旧的 = "改了没生效"，最难查的一类问题。
    watchPath: resolved.serverPath,
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

  // 拓扑未配置是"会导致工具不可用"的配置缺口，必须显式吵醒用户，而不是静默猜一个。
  if (resolved.netFile === '') {
    log('warn', '未配置 netFile：h3c_topology 会直接报错；h3c_devices 也不得不退回内置兜底端口范围。'
      + '请在 profile 的 cordis.patch.yml 里给 dsh-h3clab 设置 netFile（指向 HCL 的 .net 拓扑）。')
  }
  else {
    log('info', `拓扑：${resolved.netFile}；证据目录：${resolved.evidenceRoot}；记忆库：${resolved.referencesDir}`
      + `${serverConfigFile === null ? '' : `；服务器配置：${serverConfigFile}`}`)
  }
  return undefined
}
