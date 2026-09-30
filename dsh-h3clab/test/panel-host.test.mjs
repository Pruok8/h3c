/**
 * 面板 **host 侧**路由的离线测试。
 *
 * 不起真的 HTTP 服务器：直接拿 createPanelHandler 配假 ctx / 假 req / 假 res，
 * 验证路由语义与安全边界（工具白名单、真下发确认、设置落盘 + 重启）。
 */
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

/** 造一个最小的 IncomingMessage 替身。 */
function makeReq(method, url, body) {
  const handlers = {}
  const req = {
    method,
    url,
    on(event, fn) {
      (handlers[event] ??= []).push(fn)
      return this
    },
    off(event, fn) {
      if (handlers[event])
        handlers[event] = handlers[event].filter(item => item !== fn)
      return this
    },
    destroy() {}
  }
  setTimeout(() => {
    if (body !== undefined && handlers.data) {
      for (const fn of handlers.data)
        fn(Buffer.from(body, 'utf8'))
    }
    if (handlers.end) {
      for (const fn of handlers.end)
        fn()
    }
  }, 0)
  return req
}

/** 造一个最小的 ServerResponse 替身。 */
function makeRes() {
  const res = { status: 0, headers: null, body: '' }
  res.writeHead = (status, headers) => {
    res.status = status
    res.headers = headers
  }
  res.end = (text) => {
    res.body = text
  }
  return res
}

/** 发一次请求并解析 JSON 响应。 */
async function send(handler, method, path, body) {
  const res = makeRes()
  await handler(makeReq(method, `/h3clab/api${path}`, body === undefined ? undefined : JSON.stringify(body)), res)
  let json
  try {
    json = JSON.parse(res.body)
  }
  catch {
    json = undefined
  }
  return { status: res.status, json, raw: res.body }
}

/**
 * 跑全部 host 侧面板测试。
 * @param {(name: string, body: () => Promise<void>|void) => Promise<void>} check 断言记录器。
 * @param {object} deps 依赖：lib/panel.js 的导出。
 */
export async function runPanelHostTests(check, deps) {
  const { createPanelHandler, PANEL_ROUTE, REAL_PUSH_CONFIRM, resultText } = deps

  const tmp = mkdtempSync(join(tmpdir(), 'dsh-h3clab-panel-'))
  const settingsPath = join(tmp, 'settings.json')
  const calls = []
  const logs = []
  let settings = {}
  let resolved = { host: '127.0.0.1', ports: [], devices: {}, netFile: '', evidenceRoot: 'D:/ev', referencesDir: 'D:/refs', stateDir: 'D:/state', pythonCommand: 'python', serverPath: 'D:/srv.py', toolCallTimeoutMs: 1000, serverName: 'h3clab' }

  const handler = createPanelHandler({
    ctx: {
      tools: {
        async execute(input) {
          calls.push(input)
          if (input.name === 'h3c_boom')
            throw new Error('炸了')
          return { value: { text: `ran ${input.name} with ${JSON.stringify(input.arguments)}` }, isError: false }
        }
      }
    },
    toolNames: ['h3c_devices', 'h3c_apply_plan', 'h3c_boom'],
    getResolved: () => resolved,
    getSettings: () => settings,
    getServerConfigPath: () => join(tmp, 'server-config.json'),
    getSettingsPath: () => settingsPath,
    saveSettings: (next) => {
      settings = next
      resolved = { ...resolved, ...next }
      // 真实实现是写到 settingsPath（见 lib/index.js 的 saveSettings）；
      // 这里照做，才能验证 handler 确实把 settings 透传下来了。
      writeFileSync(settingsPath, `${JSON.stringify(settings, null, 2)}\n`, 'utf8')
      return settings
    },
    log: (level, message) => logs.push(`${level}: ${message}`)
  })

  try {
    await check('面板：常量与 host 侧一致（前缀 / 真下发确认串）', async () => {
      assert.equal(PANEL_ROUTE, '/h3clab/api')
      assert.equal(REAL_PUSH_CONFIRM, 'REAL')
    })

    await check('面板：GET /state 返回工具白名单、生效配置与设置路径', async () => {
      const { status, json } = await send(handler, 'GET', '/state')
      assert.equal(status, 200)
      assert.equal(json.ok, true)
      assert.deepEqual(json.tools, ['h3c_devices', 'h3c_apply_plan', 'h3c_boom'])
      assert.equal(json.config.host, '127.0.0.1')
      assert.equal(json.settingsPath, settingsPath)
      assert.equal(json.realPushConfirm, 'REAL')
    })

    await check('面板：POST /call 拒绝白名单外的工具（不做通用代理）', async () => {
      const { status, json } = await send(handler, 'POST', '/call', { tool: 'h3c_evil', arguments: {} })
      assert.equal(status, 403)
      assert.equal(json.ok, false)
      assert.match(json.error, /不允许调用工具/)
      assert.equal(calls.length, 0, '不该真的执行')
    })

    await check('面板：POST /call 拒绝缺 confirm 的真下发', async () => {
      const { status, json } = await send(handler, 'POST', '/call', {
        tool: 'h3c_apply_plan', arguments: { plan_json: 'x.json', dry_run: false }
      })
      assert.equal(status, 400)
      assert.match(json.error, /真下发需要确认/)
      assert.equal(calls.length, 0, '不该真的执行')
    })

    await check('面板：POST /call 允许预演（dry_run=true）', async () => {
      const { json } = await send(handler, 'POST', '/call', {
        tool: 'h3c_apply_plan', arguments: { plan_json: 'x.json', dry_run: true }
      })
      assert.equal(json.ok, true)
      assert.match(json.text, /ran h3c_apply_plan/)
      assert.equal(calls.length, 1)
    })

    await check('面板：POST /call 带 confirm 才放行真下发', async () => {
      const { json } = await send(handler, 'POST', '/call', {
        tool: 'h3c_apply_plan', arguments: { plan_json: 'x.json', dry_run: false }, confirm: 'REAL'
      })
      assert.equal(json.ok, true)
      assert.equal(calls.length, 2)
      assert.equal(calls[1].arguments.dry_run, false)
      assert.equal(calls[1].name, 'h3c_apply_plan')
      assert.ok(typeof calls[1].callId === 'string' && calls[1].callId !== '')
      assert.ok(calls[1].signal !== undefined, '应传 AbortSignal')
    })

    await check('面板：工具抛异常 -> 结构化失败（不 500、不崩）', async () => {
      const { status, json } = await send(handler, 'POST', '/call', { tool: 'h3c_boom', arguments: {} })
      assert.equal(status, 200)
      assert.equal(json.ok, false)
      assert.match(json.error, /炸了/)
    })

    await check('面板：POST /settings 写覆盖并回执“已重启”', async () => {
      const { json } = await send(handler, 'POST', '/settings', {
        settings: { netFile: 'D:/lab/x.net', ports: [30001, 30002] }
      })
      assert.equal(json.ok, true)
      assert.equal(json.settings.netFile, 'D:/lab/x.net')
      assert.match(json.note, /重启/)
      const written = JSON.parse(readFileSync(settingsPath, 'utf8'))
      assert.equal(written.netFile, 'D:/lab/x.net')
      assert.deepEqual(written.ports, [30001, 30002])
    })

    await check('面板：POST /settings 拒绝非对象 settings', async () => {
      const { status, json } = await send(handler, 'POST', '/settings', { settings: [1, 2] })
      assert.equal(status, 400)
      assert.match(json.error, /settings 必须是一个对象/)
    })

    await check('面板：POST /call 非法 JSON 被拒', async () => {
      const res = makeRes()
      await handler(makeReq('POST', '/h3clab/api/call', 'not json'), res)
      assert.equal(res.status, 400)
      assert.match(JSON.parse(res.body).error, /不是合法 JSON/)
    })

    await check('面板：未知路由 404', async () => {
      const { status, json } = await send(handler, 'GET', '/nope')
      assert.equal(status, 404)
      assert.match(json.error, /未知的面板路由/)
    })

    await check('面板：resultText 对多种结果形状都容错', async () => {
      assert.equal(resultText({ value: { text: 'A' } }), 'A')
      assert.equal(resultText({ content: [{ type: 'text', text: 'B' }, { type: 'image' }] }), 'B')
      assert.equal(resultText('C'), 'C')
      assert.equal(resultText(null), '')
      assert.equal(resultText(undefined), '')
    })
  }
  finally {
    rmSync(tmp, { recursive: true, force: true })
  }
}
