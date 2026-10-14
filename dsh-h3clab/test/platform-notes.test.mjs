/**
 * 内置已知平台回显（lib/platform-notes.js）的离线测试。
 *
 * 断言的样本行都取自**真机回显**（本次 31001-31020 实测）：
 *   `1.1.1.4/32         IS_L2   15  10          100.1.11.2      GE0/1`
 *   `172.16.100.0/24    O_INTRA 10  2           10.0.31.1       RAGG1.301`
 *   `172.17.10.0/24   O_ASE2  150 1   10.0.31.2  Vlan301`
 */
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
import {
  AVOID_COMMANDS,
  DISPLAY_COMMANDS,
  IRF_COMMANDS,
  MLAG_FIELDS,
  PLATFORM_ECHO,
  ROUTE_PROTO,
  ROUTE_PROTO_NOTES,
  SUBINTERFACE_ENCAP,
  UNDO_WITHOUT_NUMBER,
  platformEchoNotes,
  routeProtoRegex,
  subinterfaceEncap
} from '../lib/platform-notes.js'

/**
 * 跑全部平台回显常量测试。
 * @param {(name: string, body: () => Promise<void>|void) => Promise<void>} check 断言记录器。
 */
export async function runPlatformNotesTests(check) {
  await check('平台回显：路由 Proto 取值常量（O_INTRA 不是 OSPF）', async () => {
    assert.equal(ROUTE_PROTO.OSPF_INTRA, 'O_INTRA')
    assert.equal(ROUTE_PROTO.OSPF_INTER, 'O_INTER')
    assert.equal(ROUTE_PROTO.OSPF_ASE2, 'O_ASE2')
    assert.equal(ROUTE_PROTO.ISIS_L2, 'IS_L2')
    assert.equal(ROUTE_PROTO.BGP, 'BGP')
    assert.equal(ROUTE_PROTO.DIRECT, 'Direct')
    assert.equal(ROUTE_PROTO.STATIC, 'Static')
    assert.equal(Object.isFrozen(ROUTE_PROTO), true)
    for (const item of ROUTE_PROTO_NOTES) {
      assert.ok(typeof item.evidence === 'string' && item.evidence !== '', `${item.value} 缺证据来源`)
      assert.ok(typeof item.verified === 'boolean')
    }
    const intra = ROUTE_PROTO_NOTES.find(item => item.value === 'O_INTRA')
    assert.equal(intra.verified, true, 'O_INTRA 是本平台实测结论，必须标 verified')
  })

  await check('平台回显：routeProtoRegex 生成的断言正则（这是最容易写错的地方）', async () => {
    const ospf = new RegExp(routeProtoRegex('ospf'))
    assert.ok(ospf.test('172.16.100.0/24    O_INTRA 10  2   10.0.31.1   RAGG1.301'))
    assert.ok(ospf.test('172.17.10.0/24   O_ASE2  150 1   10.0.31.2  Vlan301'))
    assert.ok(!ospf.test('1.1.1.4/32         IS_L2   15  10'), 'ospf 模式不该命中 IS-IS')
    // 写死 OSPF 是当初的错：本平台根本没有这个取值
    assert.ok(!/OSPF/.test(routeProtoRegex('ospf')))
    assert.ok(routeProtoRegex('ospf').includes('_INTRA'), '必须容忍 O_INTRA（域内的实际取值）')
    // 正则不能松到“光秃秃一个 O 也算命中”，否则断言等于没写。
    assert.ok(!ospf.test('something O something'), 'ospf 模式不能只靠一个字母 O 命中')

    const isis = new RegExp(routeProtoRegex('isis'))
    assert.ok(isis.test('1.1.1.4/32         IS_L2   15  10          100.1.11.2      GE0/1'))
    assert.ok(isis.test('1.1.1.1/32         IS_L1   15  10'))

    const both = new RegExp(routeProtoRegex('ospf', 'isis'))
    assert.ok(both.test('IS_L2') && both.test('O_INTRA') && !both.test('OSPF'))

    const all = routeProtoRegex()
    assert.ok(all.includes('O_INTRA') && all.includes('IS_L2') && all.includes('BGP'))
    assert.equal(routeProtoRegex(new Set()), routeProtoRegex())
  })

  await check('平台回显：display 命令（vrrp 必须不带 brief）', async () => {
    assert.equal(DISPLAY_COMMANDS.VRRP, 'display vrrp')
    assert.ok(!DISPLAY_COMMANDS.VRRP.includes('brief'))
    assert.equal(DISPLAY_COMMANDS.IRF, 'display irf')
    assert.equal(DISPLAY_COMMANDS.IRF_TOPOLOGY, 'display irf topology')
    assert.equal(DISPLAY_COMMANDS.IRF_CONFIGURATION, 'display irf configuration')
    assert.equal(DISPLAY_COMMANDS.MLAG_SUMMARY, 'display m-lag summary')
    assert.equal(DISPLAY_COMMANDS.MLAG_VERBOSE, 'display m-lag verbose')
    assert.equal(DISPLAY_COMMANDS.ROUTE_TABLE, 'display ip routing-table')
    assert.match(DISPLAY_COMMANDS.ROUTE_TABLE_VRF, /vpn-instance/)
    const avoid = AVOID_COMMANDS.find(item => item.command === 'display vrrp brief')
    assert.ok(avoid !== undefined, '必须把 display vrrp brief 标成不可用')
    assert.match(avoid.why, /Too many parameters/)
    assert.ok(AVOID_COMMANDS.some(item => item.command === 'tracert'))
  })

  await check('平台回显：不能带编号的 undo（三条）', async () => {
    const commands = UNDO_WITHOUT_NUMBER.map(item => item.command)
    for (const expected of ['undo port m-lag group', 'undo port access vlan', 'undo port link-aggregation group'])
      assert.ok(commands.includes(expected), `缺少 ${expected}`)
    for (const item of UNDO_WITHOUT_NUMBER) {
      assert.ok(item.wrong.startsWith(`${item.command} `), `wrong 字段应是带编号的写法：${item.wrong}`)
      assert.match(item.evidence, /./)
    }
  })

  await check('平台回显：子接口必须 vlan-type dot1q vid <n>', async () => {
    assert.equal(SUBINTERFACE_ENCAP.template, 'vlan-type dot1q vid <n>')
    assert.equal(SUBINTERFACE_ENCAP.keyword, 'vid')
    assert.equal(subinterfaceEncap(110), 'vlan-type dot1q vid 110')
    assert.equal(subinterfaceEncap('301'), 'vlan-type dot1q vid 301')
    assert.throws(() => subinterfaceEncap(0), /1\.\.4094/)
    assert.throws(() => subinterfaceEncap(4095), /1\.\.4094/)
    assert.throws(() => subinterfaceEncap('x'), /1\.\.4094/)
  })

  await check('平台回显：M-LAG 可用/不可用字段（Peer M-LAG interface 是 N/A）', async () => {
    const peer = MLAG_FIELDS.verbose.find(item => item.field.includes('Peer M-LAG interface'))
    assert.ok(peer !== undefined)
    assert.equal(peer.usable, false, '仿真器缺 peer M-LAG interface TLV，必须标不可用')
    assert.match(peer.note, /N\/A/)
    assert.ok(MLAG_FIELDS.summary.every(item => item.usable === true))
    assert.match(MLAG_FIELDS.verdict, /不可用/)
    assert.match(MLAG_FIELDS.verdict, /DOWN\(B\)/)
  })

  await check('平台回显：IRF 三个命令的取舍', async () => {
    assert.equal(IRF_COMMANDS.membership, 'display irf')
    assert.equal(IRF_COMMANDS.topology, 'display irf topology')
    assert.equal(IRF_COMMANDS.configuration, 'display irf configuration')
    assert.match(IRF_COMMANDS.choice, /先 display irf 确认成员角色/)
    assert.ok(IRF_COMMANDS.facts.some(item => item.fact.includes('save force')))
    assert.ok(IRF_COMMANDS.facts.some(item => /Master/.test(item.detail)))
  })

  await check('平台回显：platformEchoNotes 能整体/按话题渲染 markdown', async () => {
    const all = platformEchoNotes()
    for (const heading of ['路由表 Proto 取值', '可用的 display 命令', '不能带编号的 undo',
                           '子接口封装', 'M-LAG 回显字段', 'IRF 命令取舍', '要避开的命令'])
      assert.ok(all.includes(heading), `整体渲染缺少小节：${heading}`)
    assert.ok(all.includes('O_INTRA'))

    const mlag = platformEchoNotes('mlag')
    assert.ok(mlag.includes('M-LAG 回显字段'))
    assert.ok(!mlag.includes('IRF 命令取舍'))
    assert.ok(platformEchoNotes('irf').includes('IRF 命令取舍'))
    assert.ok(platformEchoNotes('route').includes('O_INTRA'))
    assert.ok(platformEchoNotes('undo').includes('undo port m-lag group'))
    assert.ok(platformEchoNotes('vlan').includes('vlan-type dot1q vid 110'))

    assert.equal(PLATFORM_ECHO.routeProto, ROUTE_PROTO_NOTES)
    assert.equal(PLATFORM_ECHO.irf, IRF_COMMANDS)
    assert.deepEqual(platformEchoNotes('nosuchtopic'), '')
  })
}

/** 单独跑（node test/platform-notes.test.mjs）时的极简 runner。 */
if (process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const results = []
  let failures = 0
  const check = async (name, body) => {
    try {
      await body()
      results.push(`[PASS] ${name}`)
    }
    catch (error) {
      failures++
      results.push(`[FAIL] ${name} :: ${error instanceof Error ? error.message : String(error)}`)
    }
    console.log(results.at(-1))
  }
  await runPlatformNotesTests(check)
  console.log(`---\n${results.length - failures} 通过 / ${failures} 失败`)
  process.exitCode = failures === 0 ? 0 : 1
}
