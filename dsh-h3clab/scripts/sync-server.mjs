#!/usr/bin/env node
/**
 * ⚠ 已废弃的旧脚本，只为兼容保留：请改用 `node scripts/sync-all.mjs`。
 *
 * 为什么废弃：它只会做「上游 server.py -> 插件 scripts/」这一条腿，
 * 而 server.py 与插件现在一共有三份副本（上游真源、插件快照、仓库里的插件副本），
 * 只同步一条腿仍然会静默漂移。`sync-all.mjs` 三条腿都会管，还支持 `--check`。
 *
 * 这里把参数原样转发给 sync-all.mjs，行为不变但覆盖面更大。
 */
import { spawnSync } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
process.stderr.write('[sync-server] 已废弃：转发到 sync-all.mjs（它还会同步仓库副本，并支持 --check）\n')

const result = spawnSync(process.execPath, [resolve(HERE, 'sync-all.mjs'), ...process.argv.slice(2)], {
  stdio: 'inherit'
})
process.exitCode = result.status ?? 1
