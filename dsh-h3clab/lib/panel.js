/**
 * dsh-h3clab —— 面板的 **host 侧** HTTP 路由。
 *
 * 为什么要有这一层：客户端半边跑在浏览器里，**没有** `ctx.tools`，不能直接调工具。
 * 官方做法（Typert / API Gateway）需要改 DSH 自身装配并跑代码生成器，profile bundle
 * 插件做不到。因此这里用 host 的 `webServer` 注册一条同源 prefix 路由，
 * 客户端 `fetch` 它，host 再用 `ctx.tools.execute(...)` 去跑 `h3c_*` 工具。
 * （本机已装的 `dsh-super-injector` 用的就是这个模式。）
 *
 * 路由：
 *   GET  /h3clab/api/state      面板启动时拉：可用工具名、生效配置、设置覆盖、路径
 *   POST /h3clab/api/call       { tool, arguments, confirm? } -> 执行一个 h3c_* 工具
 *   GET  /h3clab/api/settings   读面板管理的设置覆盖
 *   POST /h3clab/api/settings   { settings } -> 写覆盖并重启 python（下次调用生效）
 *
 * 安全边界：
 * - **只允许调用本插件的 `h3c_*` 工具**，不做通用工具代理。
 * - `h3c_apply_plan` 的真下发必须带 `confirm: "REAL"`，否则拒绝——防止面板上一个
 *   误点就把配置推到设备上。
 * - 请求体大小上限 1 MB。
 */

/** 面板 API 的挂载前缀。 */
export const PANEL_ROUTE = '/h3clab/api'
/** 真下发 h3c_apply_plan 时必须带上的确认串。 */
export const REAL_PUSH_CONFIRM = 'REAL'
/** 请求体上限。 */
const MAX_BODY_BYTES = 1024 * 1024

/** 读取请求体（Node IncomingMessage）。 */
function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(new Error(`请求体超过 ${MAX_BODY_BYTES} 字节`))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

/** 把工具执行结果里的文本抠出来（对几种可能的形状都容错）。 */
export function resultText(result) {
  if (result === null || result === undefined)
    return ''
  if (typeof result === 'string')
    return result
  if (typeof result !== 'object')
    return String(result)
  const value = result.value
  if (value !== null && typeof value === 'object' && typeof value.text === 'string')
    return value.text
  if (Array.isArray(result.content)) {
    return result.content
      .filter(block => block !== null && typeof block === 'object' && typeof block.text === 'string')
      .map(block => block.text)
      .join('\n')
  }
  try {
    return JSON.stringify(result)
  }
  catch {
    return String(result)
  }
}

/** 统一写 JSON 响应。 */
function sendJson(res, status, payload) {
  const body = `${JSON.stringify(payload)}\n`
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store'
  })
  res.end(body)
}

/**
 * 构造面板路由的 handler。
 * @param {object} deps 依赖注入（便于自测时换成假 ctx）。
 * @param {object} deps.ctx Cordis 上下文（用 ctx.tools.execute）。
 * @param {string[]} deps.toolNames 允许从面板调用的工具名白名单。
 * @param {() => object} deps.getResolved 取当前生效配置。
 * @param {() => object} deps.getSettings 取面板管理的设置覆盖。
 * @param {(settings: object) => object} deps.saveSettings 保存设置覆盖并生效。
 * @param {() => string} deps.getSettingsPath 设置文件路径。
 * @param {() => string|null} deps.getServerConfigPath 生成的服务器配置路径。
 * @param {(level: string, message: string) => void} deps.log 日志。
 * @returns {(req: object, res: object) => Promise<void>} HTTP handler。
 */
export function createPanelHandler(deps) {
  const {
    ctx, toolNames, getResolved, getSettings, saveSettings,
    getSettingsPath, getServerConfigPath, log
  } = deps
  const allowed = new Set(toolNames)
  let callSeq = 0

  /** 面板可见的配置（只挑实验层，够面板展示与编辑用）。 */
  const publicConfig = () => {
    const resolved = getResolved()
    return {
      host: resolved.host,
      ports: resolved.ports,
      devices: resolved.devices,
      netFile: resolved.netFile,
      evidenceRoot: resolved.evidenceRoot,
      referencesDir: resolved.referencesDir,
      stateDir: resolved.stateDir,
      pythonCommand: resolved.pythonCommand,
      serverPath: resolved.serverPath,
      toolCallTimeoutMs: resolved.toolCallTimeoutMs,
      serverName: resolved.serverName
    }
  }

  return async function handlePanelRequest(req, res) {
    let url
    try {
      url = new URL(req.url ?? '/', 'http://dsh.invalid')
    }
    catch (error) {
      sendJson(res, 400, { ok: false, error: `URL 解析失败：${error.message}` })
      return
    }
    const route = url.pathname.slice(PANEL_ROUTE.length) || '/'
    const method = (req.method ?? 'GET').toUpperCase()
    try {
      if (method === 'GET' && (route === '/' || route === '/state')) {
        sendJson(res, 200, {
          ok: true,
          plugin: 'dsh-h3clab',
          tools: [...allowed],
          config: publicConfig(),
          settings: getSettings(),
          settingsPath: getSettingsPath(),
          serverConfigPath: getServerConfigPath(),
          realPushConfirm: REAL_PUSH_CONFIRM
        })
        return
      }

      if (method === 'GET' && route === '/settings') {
        sendJson(res, 200, { ok: true, settings: getSettings(), settingsPath: getSettingsPath() })
        return
      }

      if (method === 'POST' && route === '/settings') {
        const raw = await readBody(req)
        let payload
        try {
          payload = JSON.parse(raw === '' ? '{}' : raw)
        }
        catch (error) {
          sendJson(res, 400, { ok: false, error: `请求体不是合法 JSON：${error.message}` })
          return
        }
        const settings = payload === null || typeof payload !== 'object' ? {} : payload.settings
        if (settings === null || typeof settings !== 'object' || Array.isArray(settings)) {
          sendJson(res, 400, { ok: false, error: 'settings 必须是一个对象' })
          return
        }
        const applied = saveSettings(settings)
        log('info', `面板更新了设置：${Object.keys(settings).join(', ') || '(空)'}；python 子进程已重启，下次调用生效`)
        sendJson(res, 200, {
          ok: true,
          config: publicConfig(),
          settings: applied,
          settingsPath: getSettingsPath(),
          note: 'python 子进程已重启，下一次工具调用会用新配置。'
        })
        return
      }

      if (method === 'POST' && route === '/call') {
        const raw = await readBody(req)
        let payload
        try {
          payload = JSON.parse(raw === '' ? '{}' : raw)
        }
        catch (error) {
          sendJson(res, 400, { ok: false, error: `请求体不是合法 JSON：${error.message}` })
          return
        }
        const tool = typeof payload?.tool === 'string' ? payload.tool : ''
        if (!allowed.has(tool)) {
          sendJson(res, 403, { ok: false, error: `面板不允许调用工具 ${tool || '(空)'}；可用：${[...allowed].join(', ')}` })
          return
        }
        const args = payload?.arguments === null || typeof payload.arguments !== 'object' || Array.isArray(payload.arguments)
          ? {}
          : payload.arguments
        // 真下发必须显式确认：面板上的一个误点不该把配置推到设备上。
        if (tool === 'h3c_apply_plan' && args.dry_run === false && payload?.confirm !== REAL_PUSH_CONFIRM) {
          sendJson(res, 400, {
            ok: false,
            error: `真下发需要确认：把 confirm 设为 "${REAL_PUSH_CONFIRM}"。先用 dry_run=true 看一遍预演。`
          })
          return
        }
        const controller = new AbortController()
        const onAbort = () => controller.abort()
        req.on('aborted', onAbort)
        const started = Date.now()
        try {
          const result = await ctx.tools.execute({
            callId: `h3clab-panel-${++callSeq}`,
            name: tool,
            arguments: args,
            signal: controller.signal
          })
          const isError = result !== null && typeof result === 'object' && result.isError === true
          sendJson(res, 200, {
            ok: !isError,
            tool,
            ms: Date.now() - started,
            text: resultText(result),
            error: isError ? (typeof result.error === 'string' ? result.error : '工具报告失败') : undefined
          })
        }
        catch (error) {
          log('warn', `面板调用 ${tool} 失败：${error?.message ?? String(error)}`)
          sendJson(res, 200, { ok: false, tool, ms: Date.now() - started, error: `${error?.name ?? 'Error'}: ${error?.message ?? String(error)}` })
        }
        finally {
          req.off('aborted', onAbort)
        }
        return
      }

      sendJson(res, 404, { ok: false, error: `未知的面板路由：${method} ${route}` })
    }
    catch (error) {
      log('warn', `面板路由异常：${error?.stack ?? String(error)}`)
      try {
        sendJson(res, 500, { ok: false, error: `${error?.name ?? 'Error'}: ${error?.message ?? String(error)}` })
      }
      catch {
        /* 响应可能已经发出去一半了 */
      }
    }
  }
}
