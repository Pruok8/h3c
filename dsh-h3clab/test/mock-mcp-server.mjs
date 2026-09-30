#!/usr/bin/env node
/**
 * dsh-h3clab 自测用的 **mock MCP 服务器**（不连接任何真实设备）。
 *
 * 传输：JSON-RPC over stdio，**换行分隔**（每行一个 JSON 对象，不使用
 * Content-Length 分帧），与 scripts/server.py 保持一致。
 * 协议实现复用 test/mock-protocol.mjs。
 *
 * 用法：node test/mock-mcp-server.mjs
 * 环境变量：MOCK_SLOW_MS 覆盖 hcl_slow 的 sleep 毫秒数（默认 5000）。
 */
import { createMockServer } from './mock-protocol.mjs'

const server = createMockServer({
  write: message => process.stdout.write(`${JSON.stringify(message)}\n`),
  stderr: text => process.stderr.write(text),
  slowMs: Number(process.env.MOCK_SLOW_MS ?? 5000)
})

let buffer = ''
process.stdin.setEncoding('utf8')
process.stdin.on('data', (chunk) => {
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
process.stdin.on('end', () => process.exit(0))
process.stderr.write('mock: 已启动，等待 JSON-RPC 请求\n')
