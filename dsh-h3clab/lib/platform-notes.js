/**
 * dsh-h3clab —— **内置已知平台回显特性**（写验收断言前先看这个）。
 *
 * 为什么要有这份常量：
 *   写验收清单时，`display vrrp brief`、`O_INTRA`、`undo port m-lag group <n>` 这几件事
 *   是**各自试错一整轮**才发现的（本次两个人重复踩了同一批坑，浪费掉好几个子代理轮次）。
 *   所以把它们固化成**可引用的常量 + 可生成断言的辅助函数**，并在
 *   `references/platform-echo.md` 里给出证据来源。
 *
 * 用法：
 *   import { ROUTE_PROTO, DISPLAY_COMMANDS, routeProtoRegex } from './platform-notes.js'
 *   ROUTE_PROTO.OSPF_INTRA            // 'O_INTRA'
 *   DISPLAY_COMMANDS.VRRP             // 'display vrrp'
 *   routeProtoRegex('ospf', 'isis')   // 'O(?:_INTRA|_INTER|_ASE2?)|IS_L[12]'
 *
 * 全部条目都带 `evidence`（证据来源）与 `verified`（是否在本平台实测过）——
 * 没实测的宁可标 false，也不要把“听说的”写成结论。
 */

/** 路由表 `Proto` 列的取值（本平台实测）。 */
export const ROUTE_PROTO = Object.freeze({
  /** OSPF 域内路由（**注意：不是 `OSPF`**，本次断言写错就栽在这）。 */
  OSPF_INTRA: 'O_INTRA',
  /** OSPF 域间路由。 */
  OSPF_INTER: 'O_INTER',
  /** OSPF 外部路由（ASBR 引入）；本平台实测到的是 `O_ASE2`。 */
  OSPF_ASE: 'O_ASE',
  OSPF_ASE2: 'O_ASE2',
  /** IS-IS Level-1 / Level-2。 */
  ISIS_L1: 'IS_L1',
  ISIS_L2: 'IS_L2',
  BGP: 'BGP',
  DIRECT: 'Direct',
  STATIC: 'Static',
  RIP: 'RIP'
})

/**
 * 每个 Proto 取值对应的“人话”与证据。
 * `verified: true` = 在本平台（Microsoft HCL + Comware 7.1.064）实测过。
 */
export const ROUTE_PROTO_NOTES = Object.freeze([
  { value: 'O_INTRA', means: 'OSPF 域内（intra-area）', verified: true, evidence: 'lab20/t2-report.md、verify-final.json（A 段整表实跑）' },
  { value: 'O_INTER', means: 'OSPF 域间（inter-area）', verified: false, evidence: 'Comware 通用取值；本实验未出现域间路由' },
  { value: 'O_ASE', means: 'OSPF 外部（Type-1）', verified: false, evidence: 'Comware 通用取值；本平台实测到的是 O_ASE2' },
  { value: 'O_ASE2', means: 'OSPF 外部（Type-2）', verified: true, evidence: 'lab20/t2-report.md：172.17.10.0/24 O_ASE2 150 1' },
  { value: 'IS_L1', means: 'IS-IS Level-1', verified: false, evidence: 'Comware 通用取值' },
  { value: 'IS_L2', means: 'IS-IS Level-2', verified: true, evidence: 'lab20/t3-report.md：1.1.1.4/32 IS_L2 15 20' },
  { value: 'BGP', means: 'BGP 学到的路由', verified: true, evidence: 'lab20/t2-report.md（BGP 引入后整表出现 BGP 行）' },
  { value: 'Direct', means: '直连', verified: true, evidence: '本平台所有设备的 display ip routing-table' },
  { value: 'Static', means: '静态', verified: true, evidence: '本平台所有设备的 display ip routing-table' }
])

/**
 * 本平台可用的 `display` 命令（同名命令的“不能用”变体在 note 里）。
 */
export const DISPLAY_COMMANDS = Object.freeze({
  /** VRRP 状态：**必须用 `display vrrp`**。 */
  VRRP: 'display vrrp',
  /** 路由表（整表）。 */
  ROUTE_TABLE: 'display ip routing-table',
  /** VRF 内的路由表（`ping`/`tracert` 查 VPN 内地址也要显式带 -vpn-instance）。 */
  ROUTE_TABLE_VRF: 'display ip routing-table vpn-instance <vrf> <prefix>',
  /** M-LAG 概览（字段少但一定有 peer-link / keepalive 状态）。 */
  MLAG_SUMMARY: 'display m-lag summary',
  /** M-LAG 详情（含 Local/Peer M-LAG interface 的 LACP MAC，判断仿真器缺陷要看这里）。 */
  MLAG_VERBOSE: 'display m-lag verbose',
  /** IRF 成员表（MemberID / Role / Priority / CPU-Mac）。 */
  IRF: 'display irf',
  /** IRF 拓扑（每个成员两个 IRF-Port 的 Link/neighbor 状态）。 */
  IRF_TOPOLOGY: 'display irf topology',
  /** IRF 端口配置（MemberID / NewID / IRF-Port1 / IRF-Port2）。 */
  IRF_CONFIGURATION: 'display irf configuration',
  /** 地址族 peer（按需加 `vpnv4` / `ipv4` / `vpn-instance X`）。 */
  BGP_PEER: 'display bgp peer <af>',
  ISIS_PEER: 'display isis peer',
  LDP_PEER: 'display mpls ldp peer'
})

/**
 * 本平台**不能带编号**的 `undo` 命令。
 * 带编号一律 `% Too many parameters`（本次撞了 3 次）。
 */
export const UNDO_WITHOUT_NUMBER = Object.freeze([
  { command: 'undo port m-lag group', wrong: 'undo port m-lag group 1', verified: true, evidence: 'lab20 CAPABILITIES.md C 节 + 本次实测' },
  { command: 'undo port access vlan', wrong: 'undo port access vlan 10', verified: true, evidence: 'lab20 CAPABILITIES.md C 节 + 本次实测' },
  { command: 'undo port link-aggregation group', wrong: 'undo port link-aggregation group 1', verified: true, evidence: 'lab20 CAPABILITIES.md C 节 + 本次实测' }
])

/** 子接口封装：**必须**写 `vlan-type dot1q vid <n>`，写 `vlan-type dot1q <n>` 报 Unrecognized。 */
export const SUBINTERFACE_ENCAP = Object.freeze({
  template: 'vlan-type dot1q vid <n>',
  keyword: 'vid',
  wrong: 'vlan-type dot1q <n>（缺 vid，报 % Unrecognized）',
  verified: true,
  evidence: 'lab20 CAPABILITIES.md C 节：MSR36-20 子接口 + dot1q 实测'
})

/** 生成一条子接口封装命令。 */
export function subinterfaceEncap(vid) {
  const value = Number(vid)
  if (!Number.isInteger(value) || value < 1 || value > 4094)
    throw new Error(`vlan-type dot1q vid 需要 1..4094 的 VLAN ID，收到 ${JSON.stringify(vid)}`)
  return `vlan-type dot1q vid ${value}`
}

/** 不能用 / 不稳定要避开的命令。 */
export const AVOID_COMMANDS = Object.freeze([
  { command: 'display vrrp brief', why: '本版本报 `% Too many parameters`；改用 `display vrrp`', verified: true, evidence: '本次实测（端口 31001/31018 均复现）' },
  { command: 'tracert', why: '会把控制台卡住（连跑两次后提示符都认不出）；验证选路改用 display ip routing-table', verified: true, evidence: 'lab20 CAPABILITIES.md C 节' },
  { command: 'ping <VPN 内地址>（不带 -vpn-instance）', why: '默认查公网表，VPN 内地址一律 timeout，会被误判成“单向不通”', verified: true, evidence: 'lab20 CAPABILITIES.md C 节' },
  { command: 'peer label-route-capability', why: '地址族 vpnv4 下报 % Wrong parameter（Option C 用不到；Option B 不需要它）', verified: true, evidence: 'lab20 CAPABILITIES.md B 节' }
])

/**
 * M-LAG 相关：`display m-lag summary` / `display m-lag verbose` 里**可用与不可用**的字段。
 *
 * 关键结论：本仿真器**缺 “peer M-LAG interface TLV”** → M-LAG 三个组全部 DOWN(B)，
 * 但这**不代表配置错了**：peer-link、keepalive、角色选举都是好的。写断言时别把
 * `Local M-LAG interface ... Effective=0001-0001-0001` 当成“M-LAG 起来了”。
 */
export const MLAG_FIELDS = Object.freeze({
  summary: Object.freeze([
    { field: 'Peer-link interface', usable: true, note: '本端 peer-link 聚合口名（如 BAGG10）' },
    { field: 'Peer-link interface state (cause)', usable: true, note: 'UP/DOWN 与原因' },
    { field: 'Keepalive link state (cause)', usable: true, note: 'UP/DOWN 与原因；keepalive 要起来必须先 undo stp enable' },
    { field: 'Flags（A/B/C）', usable: true, note: 'B = No peer M-LAG interface configured —— 本平台常态，不是配置错误' }
  ]),
  verbose: Object.freeze([
    { field: 'Local M-LAG interface LACP MAC: Effective', usable: true, note: '本机算得对（0001-0001-0001）' },
    { field: 'Peer M-LAG interface LACP MAC: Effective', usable: false, note: '**N/A**：仿真器不在 DRCPDU 里带该 TLV —— 因此 M-LAG 组永远起不来' }
  ]),
  verdict: 'M-LAG 在本平台**不可用**：DRCP 通道通、配置一致，但缺 peer M-LAG interface TLV。验收断言请按“配置已下发 + 状态 DOWN(B)”写，不要断言 UP。',
  evidence: 'lab20/CAPABILITIES.md A 节（含 diff 干净、干净重启、同时加组三次排除实验）'
})

/**
 * IRF：`display irf` / `display irf topology` / `display irf configuration` 怎么选。
 */
export const IRF_COMMANDS = Object.freeze({
  /** 判“合并成功没有”只看这个：MemberID / Role(Master/Standby) / Priority / CPU-Mac。 */
  membership: 'display irf',
  /** 判“堆叠口 up 没有、邻居对不对”看这个：每个成员的 IRF-Port1/IRF-Port2 的 Link 与 neighbor。 */
  topology: 'display irf topology',
  /** 判“预期配置与当前是否一致、要不要 renumber”看这个：MemberID / NewID / 端口成员。 */
  configuration: 'display irf configuration',
  /** 取舍一句话。 */
  choice: '先 display irf 确认成员角色；再看 display irf topology 确认堆叠口与邻居；'
    + '只有在准备 renumber / 对账“预期配置”时才需要 display irf configuration。',
  facts: Object.freeze([
    { fact: '成员合并成功的标志', detail: '`*+1 Master` / `+2 Standby`（* = master，+ = 当前登录的成员）', verified: true },
    { fact: '桥 MAC', detail: '`The bridge MAC of the IRF is: ...`，Domain ID 也在 display irf 末尾', verified: true },
    { fact: '堆叠口 up 的判据', detail: '`display irf topology` 里 Link=UP 且 neighbor 指向期望成员', verified: true },
    { fact: 'undo shutdown 后必须 save force', detail: '否则合并会死循环（备机重载 startup-config 后端口又变 ADM，只有 display saved-configuration 看得出来）', verified: true }
  ]),
  evidence: 'lab20/CAPABILITIES.md A/D 节 + 本次实测（端口 31010：*+1 Master / +2 Standby，IRF-Port1 2→UP、1→DIS）'
})

/**
 * 整个“已知回显”知识库。`platformEchoNotes()` 会把它渲染成 markdown。
 */
export const PLATFORM_ECHO = Object.freeze({
  routeProto: ROUTE_PROTO_NOTES,
  display: DISPLAY_COMMANDS,
  undoWithoutNumber: UNDO_WITHOUT_NUMBER,
  subinterfaceEncap: SUBINTERFACE_ENCAP,
  avoid: AVOID_COMMANDS,
  mlag: MLAG_FIELDS,
  irf: IRF_COMMANDS
})

/**
 * 生成**路由 Proto 断言用**的正则源码片段，避免再写错 `O_INTRA`。
 *
 * @param {...string} families `'ospf' | 'isis' | 'bgp' | 'direct' | 'static'`，可传数组或变参。
 * @returns {string} 可直接放进清单 `expect` 的正则源码（不含锚点）。
 * @example routeProtoRegex('ospf') // 'O(?:_INTRA|_INTER|_ASE2?)'
 */
export function routeProtoRegex(...families) {
  const wanted = new Set(
    families
      .flat()
      .filter(item => typeof item === 'string' && item.trim() !== '')
      .map(item => item.trim().toLowerCase())
  )
  if (wanted.size === 0)
    return Object.values(ROUTE_PROTO).join('|')
  const parts = []
  if (wanted.has('ospf')) {
    // `O_INTRA` 是本平台的实际取值；顺带容忍 `O_INTER` / `O_ASE(2)` / `O_SPF`。
    // ★ 注意别把整组写成可选的（`(?:…)?`）—— 那样连光秃秃一个字母 `O` 都会命中，
    //   断言会变成“永远通过”。
    parts.push('O(?:_INTRA|_INTER|_ASE2?|_SPF)')
  }
  if (wanted.has('isis'))
    parts.push('IS_L[12]')
  if (wanted.has('bgp'))
    parts.push('BGP')
  if (wanted.has('direct'))
    parts.push('Direct')
  if (wanted.has('static'))
    parts.push('Static')
  if (wanted.has('rip'))
    parts.push('RIP')
  return parts.length === 0 ? Object.values(ROUTE_PROTO).join('|') : parts.join('|')
}

/**
 * 渲染“已知回显”markdown（可指定子话题）。
 * @param {string} [topic] `route|display|undo|vlan|mlag|irf|avoid`，留空=全部。
 * @returns {string} markdown 文本。
 */
export function platformEchoNotes(topic = '') {
  const wanted = typeof topic === 'string' ? topic.trim().toLowerCase() : ''
  const sections = []
  const want = name => wanted === '' || wanted === name

  if (want('route')) {
    sections.push('## 路由表 Proto 取值（本平台实测）',
      '',
      '| Proto | 含义 | 实测 | 证据 |',
      '|---|---|---|---|',
      ...ROUTE_PROTO_NOTES.map(item => `| \`${item.value}\` | ${item.means} | ${item.verified ? '✅' : '—'} | ${item.evidence} |`),
      '',
      `断言别写死 \`OSPF\`：用 \`${routeProtoRegex('ospf')}\`（O_INTRA 才是域内的实际取值）。`)
  }
  if (want('display')) {
    sections.push('## 可用的 display 命令',
      '',
      ...Object.entries(DISPLAY_COMMANDS).map(([key, cmd]) => `- \`${key}\` → \`${cmd}\``))
  }
  if (want('undo')) {
    sections.push('## 不能带编号的 undo',
      '',
      ...UNDO_WITHOUT_NUMBER.map(item => `- ✅ \`${item.command}\`　❌ \`${item.wrong}\`（${item.evidence}）`))
  }
  if (want('vlan')) {
    sections.push('## 子接口封装',
      '',
      `- ✅ \`${SUBINTERFACE_ENCAP.template}\``,
      `- ❌ ${SUBINTERFACE_ENCAP.wrong}`,
      '',
      `- 例：\`${subinterfaceEncap(110)}\``)
  }
  if (want('mlag')) {
    sections.push('## M-LAG 回显字段',
      '',
      `**结论**：${MLAG_FIELDS.verdict}`,
      '',
      '- `display m-lag summary`：',
      ...MLAG_FIELDS.summary.map(item => `  - ${item.usable ? '✅' : '❌'} \`${item.field}\` —— ${item.note}`),
      '- `display m-lag verbose`：',
      ...MLAG_FIELDS.verbose.map(item => `  - ${item.usable ? '✅' : '❌'} \`${item.field}\` —— ${item.note}`),
      '',
      `证据：${MLAG_FIELDS.evidence}`)
  }
  if (want('irf')) {
    sections.push('## IRF 命令取舍',
      '',
      `- 成员角色 → \`${IRF_COMMANDS.membership}\``,
      `- 堆叠口/邻居 → \`${IRF_COMMANDS.topology}\``,
      `- 预期配置对账 → \`${IRF_COMMANDS.configuration}\``,
      '',
      IRF_COMMANDS.choice,
      '',
      ...IRF_COMMANDS.facts.map(item => `- **${item.fact}**：${item.detail}`),
      '',
      `证据：${IRF_COMMANDS.evidence}`)
  }
  if (want('avoid')) {
    sections.push('## 要避开的命令 / 写法',
      '',
      ...AVOID_COMMANDS.map(item => `- ❌ \`${item.command}\`：${item.why}（${item.evidence}）`))
  }
  return sections.join('\n')
}
