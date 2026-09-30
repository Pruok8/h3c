/**
 * 客户端半边（`lib/client.js`）的离线测试。
 *
 * 浏览器 bundle 的真实运行环境（ModuleLoader + React 基线模块）在 Node 里没有，
 * 所以这里给一个**假的 ModuleLoader 和一个假的 React**（实现 useState/useEffect/
 * useMemo/useCallback），把 bundle 跑起来并渲染面板树。
 *
 * 能验证的：外壳格式与 id、导出契约（inject/apply）、注册到哪个 slot、
 * 5 个页签能否渲染、点击切页、以及设备页挂载时真的去 fetch 了 host 路由。
 * 不能验证的：真实 React 渲染与真实 Slot 挂载（那需要重启 DSH 看真身）。
 */
import assert from 'node:assert/strict'

/** factory 的返回值，跨 check 传递。 */
let clientExports = null

/**
 * 极简 React 替身：只实现面板用到的那几个 hook。
 *
 * hook 状态**按组件分桶**（像一个极简的 fiber）：否则切换页签后面板形状变了，
 * 全局下标会串到别的组件的值上（第一版就踩了这个，`confirmText` 拿到了对象）。
 */
function createFakeReact() {
  const stores = new WeakMap()
  const effects = []
  let current = null
  let cursor = 0

  function bucket() {
    const store = stores.get(current)
    if (store !== undefined)
      return store
    const created = []
    stores.set(current, created)
    return created
  }

  return {
    createElement(type, props, ...children) {
      // 真 React 会把 children 同时塞进 props.children —— 不这么做，
      // 所有形如 function Button(props) { return h('button', ..., props.children) }
      // 的包装组件都会渲染成空（第一版就踩了这个）。
      const flat = children.flat(Infinity)
      return { type, props: { ...(props ?? {}), children: flat }, children: flat }
    },
    useState(initial) {
      const store = bucket()
      const index = cursor++
      if (!(index in store))
        store[index] = typeof initial === 'function' ? initial() : initial
      return [store[index], (next) => {
        store[index] = typeof next === 'function' ? next(store[index]) : next
      }]
    },
    useEffect(fn) {
      effects.push(fn)
    },
    useMemo(fn) {
      return fn()
    },
    useCallback(fn) {
      return fn
    },
    useRef(initial) {
      const store = bucket()
      const index = cursor++
      if (!(index in store))
        store[index] = { current: initial }
      return store[index]
    },
    Fragment: Symbol('Fragment'),
    __reset() {
      effects.length = 0
    },
    __enter(component) {
      current = component
      cursor = 0
    },
    __effects: effects
  }
}

/** 渲染一次（含 effect 提交），把函数组件展开成普通元素树。 */
function render(component, React) {
  React.__reset()
  const tree = expandTree({ type: component, props: {}, children: [] }, React, 0)
  for (const fn of [...React.__effects])
    fn()
  return tree
}

/** 假 React 不渲染函数组件，这里手动展开（层级上限兜底防死循环）。 */
function expandTree(node, React, depth) {
  if (node === null || node === undefined || typeof node !== 'object')
    return node
  if (Array.isArray(node))
    return node.map(item => expandTree(item, React, depth))
  if (typeof node.type === 'function') {
    if (depth > 30)
      throw new Error('组件展开层数过深（可能是渲染死循环）')
    React.__enter(node.type)
    return expandTree(node.type(node.props ?? {}), React, depth + 1)
  }
  return { ...node, children: expandTree(node.children, React, depth) }
}

/** 把元素树里的文本拼起来，便于断言。 */
function textOf(node) {
  if (node === null || node === undefined || typeof node === 'boolean')
    return ''
  if (typeof node === 'string' || typeof node === 'number')
    return String(node)
  if (Array.isArray(node))
    return node.map(textOf).join(' ')
  if (typeof node === 'object' && 'children' in node)
    return textOf(node.children)
  return ''
}

/** 深度优先找出第一个满足条件的元素。 */
function findElement(node, predicate) {
  if (node === null || typeof node !== 'object')
    return null
  if (Array.isArray(node)) {
    for (const item of node) {
      const hit = findElement(item, predicate)
      if (hit !== null)
        return hit
    }
    return null
  }
  if (predicate(node))
    return node
  return findElement(node.children ?? null, predicate)
}

/** 按可见文本找按钮/可点击元素。 */
function findByText(node, text) {
  return findElement(node, item => typeof item.type === 'string'
    && item.props !== null && typeof item.props === 'object'
    && typeof item.props.onClick === 'function'
    && textOf(item) === text)
}

const settle = () => new Promise(resolvePromise => setTimeout(resolvePromise, 30))

/**
 * 跑全部客户端半边测试。
 * @param {(name: string, body: () => Promise<void>|void) => Promise<void>} check 断言记录器。
 * @param {string} clientBundlePath lib/client.js 的绝对路径。
 */
export async function runPanelClientTests(check, clientBundlePath) {
  // bundle 是"浏览器脚本"：顶层直接写 window.__ModuleLoader__，先把它挂出来。
  const loaded = []
  globalThis.window = { __ModuleLoader__: { load: definition => loaded.push(definition) } }

  const originalFetch = globalThis.fetch
  const fetchCalls = []
  globalThis.fetch = async (url, init) => {
    fetchCalls.push({ url, init })
    return {
      status: 200,
      async json() {
        if (String(url).includes('/call')) {
          return {
            ok: true,
            tool: 'h3c_devices',
            ms: 7,
            text: '30001  H3C  S6850  UP\n30006  H3C  S6850  UP\n\n合计 2 台可达 / 探测 2 个端口（30001-30006）\n端口来源：配置项 ports'
          }
        }
        return { ok: true, tools: ['h3c_devices'], config: {}, settings: {}, settingsPath: 'x', serverConfigPath: 'y' }
      }
    }
  }

  try {
    await import(clientBundlePath)
    await settle()

    await check('客户端：bundle 通过 window.__ModuleLoader__.load 注册，且 id 等于包名', async () => {
      assert.equal(loaded.length, 1, `期望注册 1 次，实到 ${loaded.length}`)
      assert.equal(loaded[0].id, 'dsh-h3clab')
      assert.equal(typeof loaded[0].factory, 'function')
    })

    const React = createFakeReact()

    await check('客户端：factory 只 require 基线模块（react）', async () => {
      const required = []
      clientExports = loaded[0].factory((spec) => {
        required.push(spec)
        if (spec === 'react')
          return React
        throw new Error(`bundle 不应 require 非基线模块：${spec}`)
      })
      assert.deepEqual(required, ['react'])
    })

    await check('客户端：导出契约与 host 半边同形（inject + apply）', async () => {
      assert.deepEqual(clientExports.inject, ['slots'])
      assert.equal(typeof clientExports.apply, 'function')
      assert.equal(typeof clientExports.Panel, 'function')
    })

    let injectKey = null
    let registered = null
    await check('客户端：apply 把面板注册进 settings.section', async () => {
      clientExports.apply({
        slots: {
          inject(key, callback) {
            injectKey = key
            callback()
            return () => {}
          },
          register(options, Component) {
            registered = { options, Component }
            return () => {}
          }
        }
      })
      assert.equal(injectKey, 'settings.section')
      assert.ok(registered !== null, 'register 未被调用')
      assert.equal(registered.options.name, 'settings.section', 'name 必须是 slot 键本身')
      assert.equal(registered.options.id, 'h3clab')
      assert.equal(typeof registered.Component, 'function', '第二个参数必须是 React 组件')
      assert.equal(registered.options.label(), 'H3CLab')
      assert.equal(typeof registered.options.order, 'number')
    })

    let tree = null
    await check('客户端：面板能渲染出标题与 5 个页签', async () => {
      tree = render(registered.Component, React)
      const text = textOf(tree)
      for (const label of ['H3CLab', '设备', '链路', '计划下发', '记忆库', '配置'])
        assert.ok(text.includes(label), `面板文本里缺少「${label}」`)
    })

    await check('客户端：设备页挂载时就去 fetch 面板 API（走 host 代理）', async () => {
      await settle()
      const call = fetchCalls.find(item => String(item.url).includes('/call'))
      assert.ok(call !== undefined, `没有发出 /call 请求；实到 ${JSON.stringify(fetchCalls.map(item => item.url))}`)
      const body = JSON.parse(call.init.body)
      assert.equal(body.tool, 'h3c_devices')
      assert.equal(call.init.credentials, 'same-origin')
      assert.equal(call.init.method, 'POST')
    })

    await check('客户端：拿到结果后渲染成设备表（端口/主机名/型号/状态）', async () => {
      tree = render(registered.Component, React)
      const text = textOf(tree)
      assert.ok(text.includes('30001'), `表格里应出现端口 30001：${text.slice(0, 300)}`)
      assert.ok(text.includes('S6850'), '表格里应出现型号')
      assert.ok(text.includes('2 / 2 台可达'), `应显示可达合计：${text.slice(0, 300)}`)
      assert.ok(text.includes('端口来源'), '应显示端口来源')
    })

    await check('客户端：点击页签能切到别的面板', async () => {
      const tab = findByText(tree, '配置')
      assert.ok(tab !== null, '找不到「配置」页签')
      tab.props.onClick()
      // 配置页的初始 state 来自异步的 /state，要等它回来再渲染一次
      render(registered.Component, React)
      await settle()
      const next = render(registered.Component, React)
      const text = textOf(next)
      assert.ok(text.includes('netFile'), `配置页应出现 netFile 字段：${text.slice(0, 400)}`)
      assert.ok(text.includes('保存并生效'), '配置页应有保存按钮')
    })

    await check('客户端：计划页要求先预演、真下发需手输 REAL', async () => {
      const tab = findByText(tree, '计划下发')
      assert.ok(tab !== null, '找不到「计划下发」页签')
      tab.props.onClick()
      const next = render(registered.Component, React)
      const text = textOf(next)
      assert.ok(text.includes('预演'), '计划页应有预演按钮')
      assert.ok(text.includes('REAL'), '计划页应要求手输 REAL 才允许真下发')
      assert.ok(text.includes('计划文件是') && text.includes('路径'), '计划页应说明 plan_json 是路径')
    })

    await check('客户端：不直接调设备（只跟 /h3clab/api 说话）', async () => {
      for (const call of fetchCalls)
        assert.ok(String(call.url).startsWith('/h3clab/api'), `越权请求：${call.url}`)
    })
  }
  finally {
    globalThis.fetch = originalFetch
    delete globalThis.window
  }
}
