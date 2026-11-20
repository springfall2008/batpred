import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/hooks/useStoredState.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const module = { exports: {} }

vm.runInNewContext(compiled, {
  module,
  exports: module.exports,
  require(name) {
    assert.equal(name, 'react')
    return { useEffect() {}, useState() {} }
  }
})

const { resolveStoredValue } = module.exports

assert.equal(resolveStoredValue('48', 24, [12, 24, 48]), 48)
assert.equal(resolveStoredValue('unknown', 'battery', ['battery', 'solar']), 'battery')

console.log('stored state tests passed')
