import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'

import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/utils/planData.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 }
}).outputText
const module = { exports: {} }
vm.runInNewContext(compiled, { module, exports: module.exports })

const { resolvePlanData } = module.exports

test('an unchanged response keeps the last complete plan', () => {
  const cached = { unchanged: false, plan: { num_cars: 1 }, overrides_hash: 'old' }
  assert.equal(resolvePlanData({ unchanged: true, overrides_hash: 'same' }, cached), cached)
})

test('an unchanged response is rejected when no complete plan has loaded', () => {
  assert.equal(resolvePlanData({ unchanged: true, overrides_hash: 'empty' }, null), null)
})

test('a full response replaces the cached plan', () => {
  const response = { unchanged: false, plan: { num_cars: 2 }, overrides_hash: 'new' }
  assert.equal(resolvePlanData(response, null), response)
})
