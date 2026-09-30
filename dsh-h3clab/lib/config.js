/**
 * dsh-h3clab —— 配置解析。
 *
 * 配置分两层：
 *
 * 1. **桥接层**（本插件自己用）：`pythonCommand` / `serverPath` / `toolCallTimeoutMs`
 *    / `serverName` —— 决定怎么把 MCP 服务器拉起来。
 * 2. **实验层**（转交给 `scripts/server.py`）：`host` / `ports` / `devices` / `netFile`
 *    / `evidenceRoot` / `referencesDir` —— 决定连哪台设备、拓扑在哪、证据写哪里。
 *
 * 实验层不在命令行上传参，而是由 `buildServerConfig()` 生成一个 JSON，
 * 经环境变量 `H3C_MCP_CONFIG` 交给 python（server.py 的 `load_config()` 读它）。
 * 这样只有**一个**配置入口，不必再去插件包目录里手写 `h3c_lab_mcp.json`。
 *
 * `Config` 是交给 Cordis 插件加载器做校验的 schemastery schema；
 * `resolveConfig` 负责把（可能缺失的）配置补成完整可用的对象，
 * 因此即使绕过 Cordis（例如自测里的假 ctx）直接 apply，也能正常工作。
 */
import { homedir } from 'node:os'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import Schema from '@deepseek-ai/schemastery'

/** 本包根目录（lib/ 的上一级）。 */
export const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
/** 随包发布的 MCP 服务器副本；serverPath 留空时用它。 */
export const DEFAULT_SERVER_PATH = resolve(PACKAGE_ROOT, 'scripts', 'server.py')
/** 默认 python 命令。 */
export const DEFAULT_PYTHON_COMMAND = 'python'
/** 默认单次 MCP 工具调用超时（毫秒）。 */
export const DEFAULT_TOOL_CALL_TIMEOUT_MS = 240_000
/** 默认服务器名，仅用于日志。 */
export const DEFAULT_SERVER_NAME = 'h3clab'
/** 默认探测主机：只连本机，绝不扫局域网。 */
export const DEFAULT_HOST = '127.0.0.1'
/**
 * server.py 内置的兜底探测范围。
 *
 * 只有在**既没配 `ports`、也没配 `netFile`** 时才会用到；正常情况下端口由
 * server.py 从拓扑 `.net` 的 device_id 推导（控制台端口 = 30000 + device_id）。
 */
export const FALLBACK_PORTS = Object.freeze(Array.from({ length: 10 }, (_, index) => 30001 + index))

/** DSH 主目录：优先 `$DSH_HOME`，其次 `~/.dsh`。 */
export function dshHome() {
  const fromEnv = typeof process.env.DSH_HOME === 'string' ? process.env.DSH_HOME.trim() : ''
  return fromEnv === '' ? join(homedir(), '.dsh') : resolve(fromEnv)
}

/** 证据默认落盘根目录（`<DSH_HOME>/h3clab/evidence`），不再写进插件安装目录。 */
export function defaultEvidenceRoot() {
  return join(dshHome(), 'h3clab', 'evidence')
}

/** 生成的 server.py 配置 JSON 的默认路径（`<DSH_HOME>/h3clab/server-config.json`）。 */
export function defaultServerConfigPath() {
  return join(dshHome(), 'h3clab', 'server-config.json')
}

/** 状态目录默认值（`<DSH_HOME>/h3clab`）：配置快照与 lab 状态文件放这里。 */
export function defaultStateDir() {
  return join(dshHome(), 'h3clab')
}

/** skill 记忆库默认目录（`<DSH_HOME>/skills/h3c-lab-automation/references`）。 */
export function defaultReferencesDir() {
  return join(dshHome(), 'skills', 'h3c-lab-automation', 'references')
}

/** Cordis 插件加载器读取的配置 schema。 */
export const Config = Schema.object({
  // ---- 桥接层 ----
  // python 可执行文件；需要指定解释器时改这里（例如 'py' 或绝对路径）。
  pythonCommand: Schema.string().min(1).default(DEFAULT_PYTHON_COMMAND),
  // MCP 服务器脚本路径；留空表示使用包内 scripts/server.py。
  // 相对路径按【包根目录】解析，以免受 DSH 进程 cwd 影响。
  serverPath: Schema.string().default(''),
  // 单次工具调用超时；超时会结束 python 子进程，下次调用重新启动。
  toolCallTimeoutMs: Schema.number().step(1).min(1_000).max(1_800_000).default(DEFAULT_TOOL_CALL_TIMEOUT_MS),
  // 日志里显示的服务器名。
  serverName: Schema.string().min(1).default(DEFAULT_SERVER_NAME),

  // ---- 实验层（转交 server.py）----
  // 设备控制台主机；默认只连本机。
  host: Schema.string().min(1).default(DEFAULT_HOST),
  // 探测/扫描的端口列表。留空 => 由 server.py 从 netFile 拓扑推导；
  // 连 netFile 也没配时才回落到内置兜底范围。
  ports: Schema.array(Schema.natural()).default([]),
  // 设备名 -> 控制台端口 的映射；供按主机名寻址的工具（verify/apply_plan）使用。
  devices: Schema.dict(Schema.natural()).default({}),
  // HCL 拓扑 .net 文件路径。**刻意不提供默认值**：D:\NET 下有十几个不同实验的
  // .net，猜错拓扑比报错更危险。留空时 h3c_topology 会直接报错并提示怎么配。
  netFile: Schema.string().default(''),
  // 证据（apply_plan/verify 的回显）落盘根目录；留空 => <DSH_HOME>/h3clab/evidence。
  evidenceRoot: Schema.string().default(''),
  // skill 记忆库 references 目录；留空 => <DSH_HOME>/skills/h3c-lab-automation/references。
  referencesDir: Schema.string().default(''),
  // 生成的 server.py 配置 JSON 写到哪里；留空 => <DSH_HOME>/h3clab/server-config.json。
  serverConfigPath: Schema.string().default(''),
  // 状态目录：h3c_cfgdiff 的配置快照、h3c_lab_state 的状态文件都放这里。
  // 留空 => <DSH_HOME>/h3clab。
  stateDir: Schema.string().default(''),
  // 额外注入给 python 子进程的环境变量（例如 H3C_MCP_DEBUG_DUMP）。
  env: Schema.dict(Schema.string()).default({})
})

/** 把端口列表规整成去重、升序、合法的整数数组。 */
function normalizePorts(raw) {
  if (!Array.isArray(raw))
    return []
  const seen = new Set()
  for (const item of raw) {
    const value = Number(item)
    if (!Number.isFinite(value))
      continue
    const port = Math.floor(value)
    if (port < 1 || port > 65535)
      continue
    seen.add(port)
  }
  return [...seen].sort((left, right) => left - right)
}

/** 把设备表规整成 {名字: 端口}，丢弃非法项。 */
function normalizeDevices(raw) {
  const out = {}
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw))
    return out
  for (const [key, value] of Object.entries(raw)) {
    const name = String(key).trim()
    const port = Number(value)
    if (name === '' || !Number.isFinite(port))
      continue
    const portNumber = Math.floor(port)
    if (portNumber < 1 || portNumber > 65535)
      continue
    out[name] = portNumber
  }
  return out
}

/** 把环境变量表规整成 {名字: 字符串}。 */
function normalizeEnv(raw) {
  const out = {}
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw))
    return out
  for (const [key, value] of Object.entries(raw)) {
    const name = String(key).trim()
    if (name === '' || value === null || value === undefined)
      continue
    out[name] = String(value)
  }
  return out
}

/** 解析一个路径配置：留空用默认值；相对路径按包根目录解析。 */
function resolvePath(raw, fallback) {
  const text = typeof raw === 'string' ? raw.trim() : ''
  if (text === '')
    return fallback
  return isAbsolute(text) ? resolve(text) : resolve(PACKAGE_ROOT, text)
}

/**
 * 把原始配置补全成完整对象。
 * @param {object} [config] Cordis 传入的（已校验或未校验的）配置。
 * @returns {{
 *   pythonCommand: string, serverPath: string, toolCallTimeoutMs: number,
 *   serverName: string, host: string, ports: number[], devices: Record<string, number>,
 *   netFile: string, evidenceRoot: string, referencesDir: string,
 *   serverConfigPath: string, env: Record<string, string>, packageRoot: string
 * }}
 */
export function resolveConfig(config = {}) {
  const raw = config === null || typeof config !== 'object' ? {} : config
  const pythonCommand = typeof raw.pythonCommand === 'string' && raw.pythonCommand.trim() !== ''
    ? raw.pythonCommand.trim()
    : DEFAULT_PYTHON_COMMAND
  const rawServerPath = typeof raw.serverPath === 'string' ? raw.serverPath.trim() : ''
  const serverPath = rawServerPath === ''
    ? DEFAULT_SERVER_PATH
    : (isAbsolute(rawServerPath) ? resolve(rawServerPath) : resolve(PACKAGE_ROOT, rawServerPath))
  const timeout = Number(raw.toolCallTimeoutMs)
  const toolCallTimeoutMs = Number.isFinite(timeout) && timeout > 0 ? Math.floor(timeout) : DEFAULT_TOOL_CALL_TIMEOUT_MS
  const serverName = typeof raw.serverName === 'string' && raw.serverName.trim() !== ''
    ? raw.serverName.trim()
    : DEFAULT_SERVER_NAME

  const host = typeof raw.host === 'string' && raw.host.trim() !== '' ? raw.host.trim() : DEFAULT_HOST
  const ports = normalizePorts(raw.ports)
  const devices = normalizeDevices(raw.devices)
  const netFile = resolvePath(raw.netFile, '')
  const evidenceRoot = resolvePath(raw.evidenceRoot, defaultEvidenceRoot())
  const referencesDir = resolvePath(raw.referencesDir, defaultReferencesDir())
  const serverConfigPath = resolvePath(raw.serverConfigPath, defaultServerConfigPath())
  const stateDir = resolvePath(raw.stateDir, defaultStateDir())
  const env = normalizeEnv(raw.env)

  return {
    pythonCommand,
    serverPath,
    toolCallTimeoutMs,
    serverName,
    host,
    ports,
    devices,
    netFile,
    evidenceRoot,
    referencesDir,
    serverConfigPath,
    stateDir,
    env,
    packageRoot: PACKAGE_ROOT
  }
}

/**
 * 生成交给 `server.py` 的配置对象（将被写成 JSON，经 `H3C_MCP_CONFIG` 传入）。
 *
 * 约定：
 * - `ports` 为空数组时**不写**该键 —— 让 server.py 自己决定是"从拓扑推导"还是"用兜底范围"；
 * - `net_file` 为空时**不写**该键 —— server.py 便不会去猜拓扑，而是明确报错。
 * @param {ReturnType<typeof resolveConfig>} resolved 已解析配置。
 * @returns {Record<string, unknown>} 可直接 JSON.stringify 的对象。
 */
export function buildServerConfig(resolved) {
  const payload = {
    host: resolved.host,
    evidence_root: resolved.evidenceRoot,
    references_dir: resolved.referencesDir,
    state_dir: resolved.stateDir
  }
  if (resolved.ports.length > 0)
    payload.ports = resolved.ports
  if (Object.keys(resolved.devices).length > 0)
    payload.devices = resolved.devices
  if (resolved.netFile !== '')
    payload.net_file = resolved.netFile
  return payload
}
