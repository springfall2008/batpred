import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/utils/log.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const module = { exports: {} }
vm.runInNewContext(compiled, { module, exports: module.exports })

const line = (line_number, raw_message) => ({ line_number, raw_message })
const merged = module.exports.mergeLogLines([line(2, 'old'), line(3, 'three')], [line(1, 'one'), line(2, 'new')], 3)

assert.equal(merged.map((item) => item.line_number).join(','), '1,2,3')
assert.equal(merged[1].raw_message, 'new')
