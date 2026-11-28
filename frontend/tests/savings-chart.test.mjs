import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import ts from 'typescript'

const utilityPath = fileURLToPath(new URL('../src/utils/savingsChart.ts', import.meta.url))

function loadSavingsChartUtilities() {
  const source = fs.readFileSync(utilityPath, 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2023
    }
  }).outputText
  const module = { exports: {} }

  vm.runInNewContext(compiled, { module, exports: module.exports })
  return module.exports
}

function zeroPosition(bounds) {
  return bounds.max / (bounds.max - bounds.min)
}

test('savings axes place zero at the same height with independent scales', () => {
  const { alignedSavingsAxisBounds } = loadSavingsChartUtilities()
  const bounds = alignedSavingsAxisBounds([-2, 1, 6], [0, 3, 20])

  assert.ok(bounds.total.min < 0)
  assert.ok(Math.abs(zeroPosition(bounds.daily) - zeroPosition(bounds.total)) < 1e-12)
  assert.ok(bounds.daily.max >= 6)
  assert.ok(bounds.total.max >= 20)
})

test('positive savings axes share a bottom baseline', () => {
  const { alignedSavingsAxisBounds } = loadSavingsChartUtilities()
  const bounds = alignedSavingsAxisBounds([1, 2], [10, 20])

  assert.equal(bounds.daily.min, 0)
  assert.equal(bounds.total.min, 0)
})
