/**
 * dsh-h3clab —— DeepSeek Harness 的 H3C 实验自动化工具桥。
 *
 * 本插件不实现任何设备驱动：它用 stdio 与一个 MCP 服务器
 * （scripts/server.py）通信，把 MCP 工具转成 DSH 原生工具。
 *
 * 除了 host 半边，本包还有一个**客户端半边**（`lib/client.js`，浏览器里跑的
 * 设置面板），两者通过一条同源 HTTP 路由（`lib/panel.js`）通信：
 *
 *   lib/client.js  --fetch /h3clab/api/call-->  lib/panel.js  --ctx.tools.execute-->  h3c_* 工具
 *
 * Cordis 入口约定：导出 name / inject / apply（以及给加载器用的 Config）。
 */
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { buildServerConfig, resolveConfig } from './config.js'
import { McpStdioClient } from './mcp-client.js'
import { PANEL_ROUTE, createPanelHandler } from './panel.js'
import { createH3cTools, TOOL_NAMES } from './tools.js'

/** 插件名，必须与 cordis.patch.yml 里的 name 一致。 */
export const name = 'dsh-h3clab'
/**
 * host 半边只依赖 tools。
 *
 * `webServer` **刻意不写进这里**：写了就变成硬依赖，没有 Web 宿主的 profile
 * （纯 CLI）会让整个插件不加载、连 13 个工具一起消失。面板改用
 * `ctx.inject(['webServer'], …)` 做条件注入——服务不在就只是没有面板。
 */
export const inject = ['tools']

/** 面板管理的设置覆盖文件名（与生成的 server-config.json 同目录）。 */
export const SETTINGS_FILE_NAME = 'settings.json'

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
export { PANEL_ROUTE, REAL_PUSH_CONFIRM, createPanelHandler, resultText } from './panel.js'
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
 * 读面板管理的设置覆盖（实验层）。读不到或坏了都只告警，返回 `{}`。
 * @param {string} path settings.json 路径。
 * @param {(level: string, message: string) => void} log 日志出口。
 * @returns {Record<string, unknown>} 覆盖对象（可能为空）。
 */
export function readSettings(path, log = () => {}) {
  try {
    const raw = readFileSync(path, 'utf8')
    const data = JSON.parse(raw)
    if (data === null || typeof data !== 'object' || Array.isArray(data)) {
      log('warn', `设置文件顶层不是对象，已忽略：${path}`)
      return {}
    }
    return data
  }
  catch (error) {
    if (error !== null && typeof error === 'object' && error.code === 'ENOENT')
      return {}
    log('warn', `设置文件读取失败（已忽略）：${path} -> ${error.message}`)
    return {}
  }
}

/**
 * 写面板管理的设置覆盖。
 * @param {string} path settings.json 路径。
 * @param {Record<string, unknown>} settings 覆盖对象。
 * @param {(level: string, message: string) => void} log 日志出口。
 * @returns {boolean} 是否写入成功。
 */
export function writeSettings(path, settings, log = () => {}) {
  try {
    mkdirSync(dirname(path), { recursive: true })
    writeFileSync(path, `${JSON.stringify(settings, null, 2)}\n`, 'utf8')
    return true
  }
  catch (error) {
    log('warn', `设置文件写入失败：${path} -> ${error.message}`)
    return false
  }
}

/**
 * Cordis 挂载点。
 * @param {object} ctx 插件上下文（提供 tools 服务、logger、effect、inject）。
 * @param {object} [config] cordis.patch.yml 中的 config。
 */
export function apply(ctx, config) {
  const baseConfig = config === null || typeof config !== 'object' ? {} : config
  const baseResolved = resolveConfig(baseConfig)
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

  // 面板管理的设置覆盖：与生成的 server-config.json 同目录。
  // 优先级：**面板设置 > profile 的 cordis 配置 > 内置默认值**。
  // 这样面板上的"配置编辑"改完即时生效，不必去改 YAML；而没被面板碰过的键
  // 仍然由 profile 配置决定。
  const settingsPath = join(dirname(baseResolved.serverConfigPath), SETTINGS_FILE_NAME)
  let settings = readSettings(settingsPath, log)
  let resolved = resolveConfig({ ...baseConfig, ...settings })

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

  /** 保存面板设置：重算配置、重写 JSON、重启 python（下次调用生效）。 */
  const saveSettings = (next) => {
    settings = next !== null && typeof next === 'object' ? next : {}
    resolved = resolveConfig({ ...baseConfig, ...settings })
    writeSettings(settingsPath, settings, log)
    writeServerConfig(resolved, log)
    client.restart('面板更新了实验层设置')
    return settings
  }

  // 面板路由：**条件注入**。没有 webServer 的 profile 只是没有面板，工具照常可用。
  if (typeof ctx.inject === 'function') {
    ctx.inject(['webServer'], (panelCtx) => {
      const handler = createPanelHandler({
        ctx: panelCtx,
        toolNames: TOOL_NAMES,
        getResolved: () => resolved,
        getSettings: () => settings,
        saveSettings,
        getSettingsPath: () => settingsPath,
        getServerConfigPath: () => serverConfigFile,
        log
      })
      panelCtx.effect(() => panelCtx.webServer.register({
        kind: 'prefix',
        path: PANEL_ROUTE,
        handler
      }), 'dsh-h3clab: panel-routes')
      log('info', `H3CLab 面板 API 已挂载：${PANEL_ROUTE}（可用工具 ${TOOL_NAMES.length} 个）`)
    })
  }
  else {
    log('info', '当前 Cordis 上下文没有 ctx.inject，跳过面板路由（工具不受影响）')
  }

  // 拓扑未配置是"会导致工具不可用"的配置缺口，必须显式吵醒用户，而不是静默猜一个。
  if (resolved.netFile === '') {
    log('warn', '未配置 netFile：h3c_topology 会直接报错；h3c_devices 也不得不退回内置兜底端口范围。'
      + '请在 profile 的 cordis.patch.yml 里给 dsh-h3clab 设置 netFile，'
      + '或打开设置里的 H3CLab 面板直接填。')
  }
  else {
    log('info', `拓扑：${resolved.netFile}；证据目录：${resolved.evidenceRoot}；记忆库：${resolved.referencesDir}`
      + `；状态目录：${resolved.stateDir}${serverConfigFile === null ? '' : `；服务器配置：${serverConfigFile}`}`)
  }
  return undefined
}
