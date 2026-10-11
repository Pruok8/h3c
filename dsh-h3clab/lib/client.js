/**
 * dsh-h3clab —— 客户端半边（浏览器里跑的 H3CLab 设置面板）。
 *
 * 外壳格式：官方 client bundle 唯一合法的形状是 lazy-CJS + ModuleLoader 注册
 * （`window.__ModuleLoader__.load({ id, factory })`，id 必须等于包名）。
 * `factory` 里只能 `require(spec)`；`react` / `react-dom` / `cordis` /
 * `@deepseek-ai/dsh-client-ui-slots` / `@deepseek-ai/dsh-client-ui-primitives`
 * 是宿主静态模块表里的基线模块，免声明即可 require。
 *
 * 导出约定与 host 半边一样：`export const inject` + `export function apply(ctx)`，
 * 只不过跑在浏览器里的 cordis Context 上。这里用 CJS 对应写法（exports.apply /
 * exports.inject），因为产物必须是 lazy-CJS。
 *
 * 面板不做任何设备访问：所有动作都 POST 到 host 的 `/h3clab/api/call`，
 * 由 host 用 ctx.tools.execute 去跑 h3c_* 工具（客户端 ctx 上没有 tools 服务）。
 */
window.__ModuleLoader__.load({
  id: 'dsh-h3clab',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports

    const React = require('react')
    const h = React.createElement

    /** 面板 API 前缀，必须与 host 侧 lib/panel.js 的 PANEL_ROUTE 一致。 */
    const API = '/h3clab/api'

    // ------------------------------------------------------------------
    // 与 host 通信
    // ------------------------------------------------------------------

    /** 统一的 fetch 包装：同源请求，带会话 Cookie，失败也返回结构化结果。 */
    async function api(path, init) {
      try {
        const response = await fetch(`${API}${path}`, {
          credentials: 'same-origin',
          headers: init !== undefined && init.body !== undefined
            ? { 'content-type': 'application/json' }
            : undefined,
          ...init
        })
        const payload = await response.json().catch(() => undefined)
        if (payload === undefined)
          return { ok: false, error: `HTTP ${response.status}：响应不是 JSON` }
        return payload
      }
      catch (error) {
        return { ok: false, error: `请求失败：${error?.message ?? String(error)}` }
      }
    }

    /** 调一个 h3c_* 工具。 */
    const callTool = (tool, args, extra) =>
      api('/call', { method: 'POST', body: JSON.stringify({ tool, arguments: args ?? {}, ...(extra ?? {}) }) })

    // ------------------------------------------------------------------
    // 样式：全部用主题令牌，浅色/深色都跟着走
    // ------------------------------------------------------------------

    const S = {
      wrap: { padding: '14px 16px', color: 'var(--dsw-alias-label-primary)', fontFamily: 'inherit', fontSize: '13px' },
      tabs: { display: 'flex', gap: '4px', borderBottom: '1px solid var(--dsw-alias-border-l1)', marginBottom: '12px', flexWrap: 'wrap' },
      tab: { padding: '6px 12px', cursor: 'pointer', border: 'none', background: 'transparent', color: 'var(--dsw-alias-label-secondary)', borderBottom: '2px solid transparent', fontSize: '13px' },
      tabActive: { color: 'var(--dsw-alias-brand-primary)', borderBottom: '2px solid var(--dsw-alias-brand-primary)' },
      row: { display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap', marginBottom: '10px' },
      field: { display: 'flex', flexDirection: 'column', gap: '3px', marginBottom: '8px' },
      label: { fontSize: '12px', color: 'var(--dsw-alias-label-secondary)' },
      input: { padding: '5px 8px', borderRadius: '5px', border: '1px solid var(--dsw-alias-border-l2)', background: 'var(--dsw-alias-bg-layer-1)', color: 'var(--dsw-alias-label-primary)', fontSize: '12px', fontFamily: 'ui-monospace, Consolas, monospace' },
      button: { padding: '5px 12px', borderRadius: '5px', border: '1px solid var(--dsw-alias-border-l2)', background: 'var(--dsw-alias-bg-layer-2)', color: 'var(--dsw-alias-label-primary)', cursor: 'pointer', fontSize: '12px' },
      buttonPrimary: { background: 'var(--dsw-alias-brand-primary)', borderColor: 'var(--dsw-alias-brand-primary)', color: '#fff' },
      buttonDanger: { background: 'var(--dsw-alias-state-error-primary)', borderColor: 'var(--dsw-alias-state-error-primary)', color: '#fff' },
      pre: { margin: 0, padding: '10px', borderRadius: '6px', background: 'var(--dsw-alias-bg-layer-2)', border: '1px solid var(--dsw-alias-border-l1)', overflow: 'auto', maxHeight: '420px', fontSize: '12px', lineHeight: '1.5', whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'ui-monospace, Consolas, monospace' },
      hint: { fontSize: '12px', color: 'var(--dsw-alias-label-secondary)', marginBottom: '10px', lineHeight: '1.6' },
      th: { textAlign: 'left', padding: '5px 10px', borderBottom: '1px solid var(--dsw-alias-border-l1)', color: 'var(--dsw-alias-label-secondary)', fontWeight: 'normal', fontSize: '12px' },
      td: { padding: '5px 10px', borderBottom: '1px solid var(--dsw-alias-border-l1)', fontSize: '12px', fontFamily: 'ui-monospace, Consolas, monospace' },
      ok: { color: 'var(--dsw-alias-state-success-primary)' },
      bad: { color: 'var(--dsw-alias-state-error-primary)' },
      warn: { color: 'var(--dsw-alias-state-warn-primary)' },
      chip: { padding: '1px 7px', borderRadius: '10px', border: '1px solid var(--dsw-alias-border-l2)', fontSize: '11px', color: 'var(--dsw-alias-label-secondary)' }
    }

    // ------------------------------------------------------------------
    // 小部件
    // ------------------------------------------------------------------

    function Button(props) {
      const style = { ...S.button, ...(props.variant === 'primary' ? S.buttonPrimary : {}), ...(props.variant === 'danger' ? S.buttonDanger : {}), ...(props.disabled ? { opacity: 0.5, cursor: 'default' } : {}), ...(props.style ?? {}) }
      return h('button', { type: 'button', style, disabled: props.disabled, onClick: props.onClick, title: props.title }, props.children)
    }

    function Field(props) {
      return h('label', { style: { ...S.field, ...(props.style ?? {}) } },
        h('span', { style: S.label }, props.label),
        props.children)
    }

    function TextInput(props) {
      return h('input', {
        style: { ...S.input, ...(props.style ?? {}) },
        value: props.value,
        placeholder: props.placeholder,
        spellCheck: false,
        onChange: event => props.onChange(event.target.value)
      })
    }

    function Check(props) {
      return h('label', { style: { ...S.row, marginBottom: 0, fontSize: '12px', cursor: 'pointer' } },
        h('input', { type: 'checkbox', checked: Boolean(props.checked), onChange: event => props.onChange(event.target.checked) }),
        h('span', null, props.label))
    }

    /** 结果区：成功显示文本，失败显示错误原因。 */
    function Output(props) {
      const result = props.result
      if (result === null || result === undefined)
        return null
      if (result.ok === false)
        return h('div', null,
          h('div', { style: { ...S.warn, marginBottom: '6px' } }, `❌ ${result.error ?? '调用失败'}`),
          result.text ? h('pre', { style: S.pre }, result.text) : null)
      return h('div', null,
        h('div', { style: { ...S.label, marginBottom: '6px' } },
          `✅ ${result.tool} 用时 ${result.ms ?? '?'} ms`),
        h('pre', { style: S.pre }, result.text ?? ''))
    }

    /** 通用「跑一次工具」状态机。 */
    function useToolCall() {
      const [busy, setBusy] = React.useState(false)
      const [result, setResult] = React.useState(null)
      const run = React.useCallback(async (tool, args, extra) => {
        setBusy(true)
        setResult({ ok: true, tool, text: '执行中…' })
        const payload = await callTool(tool, args, extra)
        setResult(payload)
        setBusy(false)
        return payload
      }, [])
      return { busy, result, run, clear: () => setResult(null) }
    }

    // ------------------------------------------------------------------
    // 面板 1：设备列表 + 可达状态
    // ------------------------------------------------------------------

    function DevicesPanel() {
      const [ports, setPorts] = React.useState('')
      const [model, setModel] = React.useState(false)
      const { busy, result, run } = useToolCall()

      const refresh = () => {
        const list = ports.split(/[\s,]+/).map(item => Number(item.trim())).filter(item => Number.isFinite(item) && item > 0)
        run('h3c_devices', { ...(list.length > 0 ? { ports: list } : {}), model })
      }
      React.useEffect(() => { refresh() }, [])

      const rows = React.useMemo(() => {
        if (result === null || result.ok !== true || typeof result.text !== 'string')
          return null
        const out = []
        for (const line of result.text.split('\n')) {
          const match = /^(\d{4,5})\s+(\S+)\s+(\S+)\s+(UP|DOWN|ADM)\s*$/.exec(line.trim())
          if (match !== null)
            out.push({ port: match[1], name: match[2], model: match[3], state: match[4] })
        }
        return out.length > 0 ? out : null
      }, [result])

      const summary = React.useMemo(() => {
        if (result === null || typeof result.text !== 'string')
          return null
        const match = /合计\s+(\d+)\s+台可达\s*\/\s*探测\s+(\d+)\s+个端口/.exec(result.text)
        const origin = /端口来源：(.+)/.exec(result.text)
        return {
          up: match === null ? null : Number(match[1]),
          total: match === null ? null : Number(match[2]),
          origin: origin === null ? null : origin[1].trim()
        }
      }, [result])

      return h('div', null,
        h('div', { style: S.row },
          h('span', { style: S.label }, '端口（留空 = 按配置/拓扑推导）'),
          h('input', { style: { ...S.input, width: '320px' }, value: ports, placeholder: '30001,30002 或留空', onChange: event => setPorts(event.target.value) }),
          h(Check, { label: '读型号（慢）', checked: model, onChange: setModel }),
          h(Button, { variant: 'primary', disabled: busy, onClick: refresh }, busy ? '探测中…' : '刷新')),
        summary !== null && summary.total !== null
          ? h('div', { style: { ...S.row, marginBottom: '8px' } },
              h('span', { style: summary.up > 0 ? S.ok : S.bad }, `${summary.up} / ${summary.total} 台可达`),
              summary.origin === null ? null : h('span', { style: S.label }, `端口来源：${summary.origin}`))
          : null,
        rows !== null
          ? h('table', { style: { width: '100%', borderCollapse: 'collapse', marginBottom: '10px' } },
              h('thead', null, h('tr', null,
                h('th', { style: S.th }, '端口'), h('th', { style: S.th }, '主机名'),
                h('th', { style: S.th }, '型号'), h('th', { style: S.th }, '状态'))),
              h('tbody', null, rows.map(row => h('tr', { key: row.port },
                h('td', { style: S.td }, row.port),
                h('td', { style: S.td }, row.name),
                h('td', { style: S.td }, row.model),
                h('td', { style: { ...S.td, ...(row.state === 'UP' ? S.ok : S.bad) } }, row.state)))))
          : null,
        h(Output, { result }))
    }

    // ------------------------------------------------------------------
    // 面板 2：链路状态表
    // ------------------------------------------------------------------

    const EMPTY_LINK = { port: '', intf: '', peer_port: '', peer_intf: '' }

    function LinksPanel() {
      const [links, setLinks] = React.useState([{ ...EMPTY_LINK }])
      const { busy, result, run } = useToolCall()

      const update = (index, key, value) => {
        setLinks(current => current.map((item, i) => (i === index ? { ...item, [key]: value } : item)))
      }
      const probe = () => {
        const payload = links
          .filter(item => item.port !== '' && item.intf !== '')
          .map(item => ({
            port: Number(item.port),
            intf: item.intf,
            ...(item.peer_port === '' ? {} : { peer_port: Number(item.peer_port) }),
            ...(item.peer_intf === '' ? {} : { peer_intf: item.peer_intf })
          }))
        if (payload.length === 0) {
          setLinks(current => current)
          run('h3c_link_watch', { links: [] })
          return
        }
        run('h3c_link_watch', { links: payload })
      }

      const rows = React.useMemo(() => {
        if (result === null || result.ok !== true || typeof result.text !== 'string')
          return null
        const out = []
        for (const line of result.text.split('\n')) {
          const match = /^(\S+)\s+(\S+)\s+本端\s+(\S+)\s+\|\s+对端\s+(.+?)\s+\[phy=(\S+)\s+proto=(\S+)\]$/.exec(line.trim())
          if (match !== null)
            out.push({ name: match[1], intf: match[2], local: match[3], peer: match[4], phy: match[5], proto: match[6] })
        }
        return out.length > 0 ? out : null
      }, [result])

      return h('div', null,
        h('div', { style: S.hint }, '填本端端口/接口，可选填对端。接口名用 Comware 写法（如 GE1/0/1）。'),
        links.map((item, index) => h('div', { key: index, style: S.row },
          h(TextInput, { style: { width: '90px' }, value: item.port, placeholder: '端口', onChange: value => update(index, 'port', value) }),
          h(TextInput, { style: { width: '110px' }, value: item.intf, placeholder: 'GE1/0/1', onChange: value => update(index, 'intf', value) }),
          h('span', { style: S.label }, '↔'),
          h(TextInput, { style: { width: '90px' }, value: item.peer_port, placeholder: '对端端口', onChange: value => update(index, 'peer_port', value) }),
          h(TextInput, { style: { width: '110px' }, value: item.peer_intf, placeholder: 'GE1/0/1', onChange: value => update(index, 'peer_intf', value) }),
          h(Button, { onClick: () => setLinks(current => current.filter((_item, i) => i !== index)) }, '删除'))),
        h('div', { style: S.row },
          h(Button, { onClick: () => setLinks(current => [...current, { ...EMPTY_LINK }]) }, '＋ 加一条'),
          h(Button, { variant: 'primary', disabled: busy, onClick: probe }, busy ? '探测中…' : '探测链路')),
        rows !== null
          ? h('table', { style: { width: '100%', borderCollapse: 'collapse', marginBottom: '10px' } },
              h('thead', null, h('tr', null,
                h('th', { style: S.th }, '本端'), h('th', { style: S.th }, '接口'),
                h('th', { style: S.th }, '本端状态'), h('th', { style: S.th }, '对端'),
                h('th', { style: S.th }, 'phy/proto'))),
              h('tbody', null, rows.map((row, index) => h('tr', { key: index },
                h('td', { style: S.td }, row.name),
                h('td', { style: S.td }, row.intf),
                h('td', { style: { ...S.td, ...(row.local === 'UP' ? S.ok : row.local === 'ADM' ? S.warn : S.bad) } }, row.local),
                h('td', { style: S.td }, row.peer),
                h('td', { style: S.td }, `${row.phy}/${row.proto}`)))))
          : null,
        h(Output, { result }))
    }

    // ------------------------------------------------------------------
    // 面板 3：计划预览 + 一键下发
    // ------------------------------------------------------------------

    function PlanPanel() {
      const [plan, setPlan] = React.useState('')
      const [only, setOnly] = React.useState('')
      const [save, setSave] = React.useState(false)
      const [confirmText, setConfirmText] = React.useState('')
      const { busy, result, run } = useToolCall()

      const args = () => ({
        plan_json: plan,
        ...(only.trim() === '' ? {} : { only: only.trim() }),
        save,
        dry_run: true
      })
      const preview = () => run('h3c_apply_plan', args())
      const push = () => run('h3c_apply_plan',
        { ...args(), dry_run: false },
        { confirm: 'REAL' })

      return h('div', null,
        h('div', { style: S.hint }, '计划文件是**路径**（不是内联 JSON）。先预演，确认无误后再真下发。'),
        h(Field, { label: '计划文件路径' },
          h(TextInput, { style: { width: '520px' }, value: plan, placeholder: 'D:\\...\\plan.json', onChange: setPlan })),
        h('div', { style: S.row },
          h(TextInput, { style: { width: '200px' }, value: only, placeholder: 'only（设备名，可选）', onChange: setOnly }),
          h(Check, { label: 'save force', checked: save, onChange: setSave }),
          h(Button, { variant: 'primary', disabled: busy || plan === '', onClick: preview }, busy ? '执行中…' : '预演（dry-run）')),
        h('div', { style: { ...S.row, borderTop: '1px solid var(--dsw-alias-border-l1)', paddingTop: '10px', marginTop: '4px' } },
          h('span', { style: { ...S.label, color: 'var(--dsw-alias-state-error-primary)' } }, '真下发会改设备配置，需手输 REAL 解锁：'),
          h(TextInput, { style: { width: '120px' }, value: confirmText, placeholder: 'REAL', onChange: setConfirmText }),
          h(Button, {
            variant: 'danger',
            disabled: busy || plan === '' || confirmText.trim().toUpperCase() !== 'REAL',
            onClick: push
          }, '真下发')),
        h(Output, { result }))
    }

    // ------------------------------------------------------------------
    // 面板 4：记忆库搜索
    // ------------------------------------------------------------------

    function MemoryPanel() {
      const [keywords, setKeywords] = React.useState('')
      const [any, setAny] = React.useState(false)
      const [max, setMax] = React.useState('5')
      const { busy, result, run } = useToolCall()

      const search = () => {
        const list = keywords.split(/[\s,]+/).map(item => item.trim()).filter(Boolean)
        if (list.length === 0)
          return
        run('h3c_memory_search', { keywords: list, any, max: Number(max) || 5 })
      }

      return h('div', null,
        h('div', { style: S.hint }, '在 skill 的 cases.md / gotchas.md / aliases.md 里检索（带同义词扩展）。'),
        h('div', { style: S.row },
          h('input', {
            style: { ...S.input, width: '360px' },
            value: keywords,
            placeholder: '关键词，空格或逗号分隔',
            onChange: event => setKeywords(event.target.value),
            onKeyDown: event => { if (event.key === 'Enter') search() }
          }),
          h(Check, { label: '任一命中（any）', checked: any, onChange: setAny }),
          h(TextInput, { style: { width: '60px' }, value: max, placeholder: '5', onChange: setMax }),
          h(Button, { variant: 'primary', disabled: busy || keywords.trim() === '', onClick: search }, busy ? '搜索中…' : '搜索')),
        h(Output, { result }))
    }

    // ------------------------------------------------------------------
    // 面板 5：配置编辑
    // ------------------------------------------------------------------

    function ConfigPanel() {
      const [state, setState] = React.useState(null)
      const [draft, setDraft] = React.useState(null)
      const [message, setMessage] = React.useState(null)
      const [busy, setBusy] = React.useState(false)

      const load = React.useCallback(async () => {
        const payload = await api('/state')
        if (payload.ok !== true) {
          setMessage({ ok: false, text: payload.error ?? '读取失败' })
          return
        }
        setState(payload)
        setDraft({
          netFile: payload.config.netFile ?? '',
          ports: (payload.config.ports ?? []).join(','),
          devices: JSON.stringify(payload.config.devices ?? {}, null, 2),
          evidenceRoot: payload.config.evidenceRoot ?? '',
          referencesDir: payload.config.referencesDir ?? '',
          stateDir: payload.config.stateDir ?? ''
        })
      }, [])
      React.useEffect(() => { load() }, [load])

      const save = async () => {
        if (draft === null)
          return
        setBusy(true)
        let devices
        try {
          devices = draft.devices.trim() === '' ? {} : JSON.parse(draft.devices)
        }
        catch (error) {
          setMessage({ ok: false, text: `设备名映射不是合法 JSON：${error.message}` })
          setBusy(false)
          return
        }
        const ports = draft.ports.split(/[\s,]+/).map(item => Number(item.trim())).filter(item => Number.isFinite(item) && item > 0)
        const payload = await api('/settings', {
          method: 'POST',
          body: JSON.stringify({
            settings: {
              netFile: draft.netFile.trim(),
              ports,
              devices,
              evidenceRoot: draft.evidenceRoot.trim(),
              referencesDir: draft.referencesDir.trim(),
              stateDir: draft.stateDir.trim()
            }
          })
        })
        setBusy(false)
        if (payload.ok !== true) {
          setMessage({ ok: false, text: payload.error ?? '保存失败' })
          return
        }
        setState(current => ({ ...(current ?? {}), ...payload }))
        setMessage({ ok: true, text: payload.note ?? '已保存' })
      }

      const clearOverride = async () => {
        setBusy(true)
        const payload = await api('/settings', { method: 'POST', body: JSON.stringify({ settings: {} }) })
        setBusy(false)
        if (payload.ok !== true) {
          setMessage({ ok: false, text: payload.error ?? '清除失败' })
          return
        }
        setMessage({ ok: true, text: '已清除面板覆盖，回到 profile 的 cordis 配置。' })
        await load()
      }

      if (draft === null)
        return h('div', { style: S.wrap }, '读取配置中…')

      return h('div', null,
        h('div', { style: S.hint },
          '这里保存的是**面板覆盖**，优先级高于 profile 的 cordis.patch.yml。保存后会重写服务器配置并重启 python 子进程，下一次工具调用生效（不必重启 DSH）。'),
        h(Field, { label: 'netFile（HCL 拓扑 .net 绝对路径；留空则 h3c_topology 会报错）' },
          h(TextInput, { style: { width: '620px' }, value: draft.netFile, placeholder: 'D:\\NET\\...\\xxx.net', onChange: value => setDraft({ ...draft, netFile: value }) })),
        h(Field, { label: 'ports（逗号分隔；留空则从拓扑推导，再兜底 30001-30010）' },
          h(TextInput, { style: { width: '620px' }, value: draft.ports, placeholder: '30001,30002,...,30022', onChange: value => setDraft({ ...draft, ports: value }) })),
        h(Field, { label: 'devices（JSON：{"设备名": 端口}；按名寻址用）' },
          h('textarea', {
            style: { ...S.input, width: '620px', height: '90px' },
            value: draft.devices,
            spellCheck: false,
            onChange: event => setDraft({ ...draft, devices: event.target.value })
          })),
        h(Field, { label: 'evidenceRoot（证据目录）' },
          h(TextInput, { style: { width: '620px' }, value: draft.evidenceRoot, onChange: value => setDraft({ ...draft, evidenceRoot: value }) })),
        h(Field, { label: 'referencesDir（skill 记忆库 references 目录）' },
          h(TextInput, { style: { width: '620px' }, value: draft.referencesDir, onChange: value => setDraft({ ...draft, referencesDir: value }) })),
        h(Field, { label: 'stateDir（配置快照 / lab 状态文件目录）' },
          h(TextInput, { style: { width: '620px' }, value: draft.stateDir, onChange: value => setDraft({ ...draft, stateDir: value }) })),
        h('div', { style: S.row },
          h(Button, { variant: 'primary', disabled: busy, onClick: save }, busy ? '保存中…' : '保存并生效'),
          h(Button, { disabled: busy, onClick: clearOverride }, '清除面板覆盖'),
          h(Button, { disabled: busy, onClick: load }, '重新读取')),
        message !== null
          ? h('div', { style: { ...(message.ok ? S.ok : S.bad), margin: '8px 0', fontSize: '12px' } }, message.text)
          : null,
        state !== null
          ? h('div', { style: S.hint },
              h('div', null, `设置文件：${state.settingsPath ?? '-'}`),
              h('div', null, `服务器配置：${state.serverConfigPath ?? '-'}`),
              h('div', null, `可用工具（${(state.tools ?? []).length}）：${(state.tools ?? []).join(', ')}`),
              h('div', null, `当前覆盖键：${Object.keys(state.settings ?? {}).join(', ') || '（无）'}`))
          : null)
    }

    // ------------------------------------------------------------------
    // 实时操作面板：侧边栏图标（sidebar.panellist）+ 中央视图（main，keyed）
    //
    // 为什么另开一个座位：settings.section 藏在「设置」里面，想看 agent 在设备上
    // 干什么得点三层。DSH 的侧边栏有 `sidebar.panellist`（全局面板图标）+ `main`
    // （按侧边栏条目 id 选中的中央面板），注册这两处才是"一眼可见、一点就开"。
    // ------------------------------------------------------------------

    const L = {
      grid: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', alignItems: 'start' },
      col: { border: '1px solid var(--dsw-alias-border-l1)', borderRadius: '8px', background: 'var(--dsw-alias-bg-layer-1)', overflow: 'hidden' },
      colHead: { padding: '7px 10px', borderBottom: '1px solid var(--dsw-alias-border-l1)', fontSize: '12px', color: 'var(--dsw-alias-label-secondary)', display: 'flex', gap: '8px', alignItems: 'center' },
      body: { maxHeight: '58vh', overflow: 'auto', padding: '8px 10px', fontSize: '12px', lineHeight: '1.55', fontFamily: 'ui-monospace, Consolas, monospace' },
      item: { padding: '3px 0', borderBottom: '1px dashed var(--dsw-alias-border-l1)', whiteSpace: 'pre-wrap', wordBreak: 'break-all' },
      dim: { color: 'var(--dsw-alias-label-secondary)' },
      acc: { color: 'var(--dsw-alias-brand-primary)' }
    }

    /** 侧边栏图标：内联 SVG（不引外部资源），跟随主题色。 */
    function SidebarIcon() {
      return h('svg', {
        width: 16, height: 16, viewBox: '0 0 16 16', fill: 'none',
        stroke: 'currentColor', strokeWidth: 1.3, strokeLinecap: 'round',
        style: { display: 'block' }
      },
      h('rect', { x: 1.5, y: 2.5, width: 13, height: 4.5, rx: 1 }),
      h('rect', { x: 1.5, y: 9, width: 13, height: 4.5, rx: 1 }),
      h('path', { d: 'M4 4.75h.02M4 11.25h.02' }))
    }

    /** 实时操作：左边 agent 的每次工具调用，右边最新一次下发的设备回显。 */
    function LivePanel() {
      const [data, setData] = React.useState({ activity: [], evidence: {} })
      const [error, setError] = React.useState('')
      const [live, setLive] = React.useState(true)
      const [stamp, setStamp] = React.useState('读取中…')
      const feedRef = React.useRef(null)
      const evRef = React.useRef(null)

      React.useEffect(() => {
        if (!live) return undefined
        let alive = true
        const tick = async () => {
          const r = await api('/activity?n=400')
          if (!alive) return
          if (r.ok === false) { setError(r.error ?? '读取失败'); return }
          setError('')
          setData({ activity: r.activity ?? [], evidence: r.evidence ?? {} })
          setStamp(new Date().toLocaleTimeString())
        }
        tick()
        const timer = setInterval(tick, 1500)
        return () => { alive = false; clearInterval(timer) }
      }, [live])

      const stick = ref => { const el = ref.current; if (el) el.scrollTop = el.scrollHeight }
      React.useEffect(() => { stick(feedRef) }, [data.activity.length])
      React.useEffect(() => { stick(evRef) }, [data.evidence.file, data.evidence.lines?.length])

      const rows = data.activity.map((r, i) => {
        const t = h('span', { style: L.dim }, String(r.ts ?? '').slice(11) + ' ')
        switch (r.kind) {
          case 'call':
            return h('div', { key: i, style: L.item }, t,
              h('span', { style: L.acc }, '▶ ' + (r.tool ?? '?')), ' ',
              h('span', { style: L.dim }, r.args ?? ''))
          case 'done':
            return h('div', { key: i, style: L.item }, t,
              h('span', { style: S.ok }, '✔ ' + (r.tool ?? '?')), ' ',
              h('span', { style: L.dim }, `${r.seconds ?? '?'}s / ${r.chars ?? '?'} 字符`), '\n',
              h('span', { style: L.dim }, r.head ?? ''))
          case 'error':
            return h('div', { key: i, style: L.item }, t,
              h('span', { style: S.bad }, '✘ ' + (r.tool ?? '?')), ' ',
              h('span', { style: S.bad }, r.error ?? ''))
          default:
            return h('div', { key: i, style: L.item }, t, r.text ?? r.kind ?? '')
        }
      })

      const evLines = (data.evidence.lines ?? []).map((line, i) => h('div', {
        key: i,
        style: /^\s*% (Unrecognized|Wrong|Too many|Incomplete|Ambiguous)/.test(line)
          ? { ...L.item, ...S.bad }
          : line.startsWith('$ ') ? { ...L.item, ...L.acc } : L.item
      }, line))

      return h('div', { style: S.wrap },
        h('div', { style: S.row },
          h('strong', null, 'H3CLab 实时操作'),
          h('span', { style: S.chip }, live ? `每 1.5s 刷新 · ${stamp}` : '已暂停'),
          h('span', { style: { flex: 1 } }),
          h(Button, { onClick: () => setLive(v => !v) }, live ? '暂停' : '继续'),
          h(Button, { onClick: async () => {
            const r = await api('/activity?n=400')
            if (r.ok) {
              setData({ activity: r.activity ?? [], evidence: r.evidence ?? {} })
              setStamp(new Date().toLocaleTimeString())
            }
            else setError(r.error ?? '读取失败')
          } }, '立即刷新')),
        h('div', { style: S.hint },
          'Agent 每调一次 h3c_* 工具都会在这里出现一条；右边是最近一次下发/查询的逐条命令与设备回显（', 
          h('code', null, 'activity.jsonl'), ' + 证据文件，都在 ', h('code', null, 'stateDir'), ' 下）。'),
        error !== '' ? h('div', { style: { ...S.hint, ...S.bad } }, error) : null,
        h('div', { style: L.grid },
          h('div', { style: L.col },
            h('div', { style: L.colHead }, '操作流水（每次工具调用）',
              h('span', { style: S.chip }, `${data.activity.length} 条`)),
            h('div', { ref: feedRef, style: L.body },
              rows.length ? rows : h('div', { style: L.dim }, '暂无记录 —— agent 还没调用过 h3c_* 工具。'))),
          h('div', { style: L.col },
            h('div', { style: L.colHead }, '最新设备回显',
              h('span', { style: S.chip }, data.evidence.file ?? '—')),
            h('div', { ref: evRef, style: L.body },
              evLines.length ? evLines : h('div', { style: L.dim }, '暂无证据文件。')))))
    }

    // ------------------------------------------------------------------
    // 外壳：一个设置页 section，里面 5 个页签
    // ------------------------------------------------------------------

    const TABS = [
      { id: 'devices', label: '设备', component: DevicesPanel },
      { id: 'links', label: '链路', component: LinksPanel },
      { id: 'plan', label: '计划下发', component: PlanPanel },
      { id: 'memory', label: '记忆库', component: MemoryPanel },
      { id: 'config', label: '配置', component: ConfigPanel }
    ]

    function Panel() {
      const [active, setActive] = React.useState('devices')
      const current = TABS.find(tab => tab.id === active) ?? TABS[0]
      return h('div', { style: S.wrap },
        h('div', { style: { ...S.row, marginBottom: '8px' } },
          h('strong', null, 'H3CLab'),
          h('span', { style: S.chip }, 'H3C / HCL 实验自动化'),
          h('span', { style: { ...S.label, flex: 1 } }, '设备 · 链路 · 计划下发 · 记忆库 · 配置')),
        h('div', { style: S.tabs }, TABS.map(tab => h('button', {
          key: tab.id,
          type: 'button',
          style: { ...S.tab, ...(tab.id === active ? S.tabActive : {}) },
          onClick: () => setActive(tab.id)
        }, tab.label))),
        h(current.component, null))
    }

    // ------------------------------------------------------------------
    // 插件入口（与 host 半边同形：inject + apply）
    // ------------------------------------------------------------------

    const inject = ['slots']

    function apply(ctx) {
      // settings.section 已被官方与本地插件实证可用；inject 与 register 自带
      // effect 生命周期，不需要再外套一层 ctx.effect。
      ctx.slots.inject('settings.section', () => ctx.slots.register({
        name: 'settings.section',
        id: 'h3clab',
        order: 60,
        label: () => 'H3CLab'
      }, Panel))

      // 侧边栏图标：id 与下面 main 的 key 必须一致 —— 官方对 main 的说明是
      // "Central panel selected by sidebar entry id"。
      ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
        name: 'sidebar.panellist',
        id: 'h3clab',
        order: 60,
        label: () => 'H3CLab 实时操作'
      }, SidebarIcon))

      // 中央面板（keyed）：点侧边栏图标后渲染。
      ctx.slots.inject('main', () => ctx.slots.register({
        name: 'main',
        key: 'h3clab'
      }, LivePanel))
    }

    exports.apply = apply
    exports.inject = inject
    exports.Panel = Panel
    exports.LivePanel = LivePanel
    exports.SidebarIcon = SidebarIcon
    return module.exports
  }
})
