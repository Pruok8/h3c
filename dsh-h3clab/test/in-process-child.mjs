/**
 * 内存流版的「mock MCP 子进程」，只用于自测。
 *
 * 为什么需要它：DSH 的 Windows 文件沙箱禁止被托管的进程创建命名管道，
 * 因此 `spawn(..., { stdio: ['pipe','pipe','pipe'] })` 会同步抛 EPERM
 * （真实生产环境 / 普通终端里不存在的限制）。为了让协议层断言在受限会话里
 * 也能真实执行，自测会注入这个 child：接口与 node:child_process 的 child
 * 子集一致（stdin/stdout/stderr/pid/exitCode/signalCode/killed/on/kill），
 * 协议实现与真实 mock 服务器共用 test/mock-protocol.mjs。
 */
import { EventEmitter } from 'node:events'
import { PassThrough } from 'node:stream'
import { createMockServer } from './mock-protocol.mjs'

/**
 * 造一个 child-like 对象。
 * @param {{ slowMs?: number }} [options]
 * @returns {object} 可被 McpStdioClient 当作子进程使用。
 */
export function createInProcessMockChild(options = {}) {
  const child = new EventEmitter()
  // client 的 stdin -> mock 读取
  const stdin = new PassThrough()
  // mock 写出 -> client 的 stdout
  const stdout = new PassThrough()
  const stderr = new PassThrough()
  child.stdin = stdin
  child.stdout = stdout
  child.stderr = stderr
  child.pid = 0
  child.exitCode = null
  child.signalCode = null
  child.killed = false

  const server = createMockServer({
    write: message => stdout.write(`${JSON.stringify(message)}\n`),
    stderr: text => stderr.write(text),
    slowMs: options.slowMs ?? 5000
  })

  let buffer = ''
  stdin.setEncoding('utf8')
  stdin.on('data', (chunk) => {
    buffer += chunk
    let index = buffer.indexOf('\n')
    while (index >= 0) {
      const line = buffer.slice(0, index).replace(/\r$/, '')
      buffer = buffer.slice(index + 1)
      if (line.trim() !== '')
        server.handleLine(line)
      index = buffer.indexOf('\n')
    }
  })

  /** 模拟进程结束：置状态、销毁管道、异步派发 exit。 */
  child.kill = () => {
    if (child.exitCode !== null)
      return false
    setImmediate(() => {
      if (child.exitCode !== null)
        return
      child.killed = true
      child.exitCode = 0
      child.signalCode = null
      stdin.destroy()
      stdout.destroy()
      stderr.destroy()
      child.emit('exit', 0, null)
    })
    return true
  }

  return child
}
