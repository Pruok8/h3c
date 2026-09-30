/**
 * dsh-h3clab —— 配置解析。
 *
 * Config 是交给 Cordis 插件加载器做校验的 schemastery schema；
 * resolveConfig 负责把（可能缺失的）配置补成完整可用的对象，
 * 因此即使绕过 Cordis（例如自测里的假 ctx）直接 apply，也能正常工作。
 */
import { dirname, isAbsolute, resolve } from 'node:path'
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

/** Cordis 插件加载器读取的配置 schema。 */
export const Config = Schema.object({
  // python 可执行文件；需要指定解释器时改这里（例如 'py' 或绝对路径）。
  pythonCommand: Schema.string().min(1).default(DEFAULT_PYTHON_COMMAND),
  // MCP 服务器脚本路径；留空表示使用包内 scripts/server.py。
  // 相对路径按【包根目录】解析，以免受 DSH 进程 cwd 影响。
  serverPath: Schema.string().default(''),
  // 单次工具调用超时；超时会结束 python 子进程，下次调用重新启动。
  toolCallTimeoutMs: Schema.number().step(1).min(1_000).max(1_800_000).default(DEFAULT_TOOL_CALL_TIMEOUT_MS),
  // 日志里显示的服务器名。
  serverName: Schema.string().min(1).default(DEFAULT_SERVER_NAME)
})

/**
 * 把原始配置补全成完整对象。
 * @param {object} [config] Cordis 传入的（已校验或未校验的）配置。
 * @returns {{pythonCommand: string, serverPath: string, toolCallTimeoutMs: number, serverName: string, packageRoot: string}}
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
  return { pythonCommand, serverPath, toolCallTimeoutMs, serverName, packageRoot: PACKAGE_ROOT }
}
