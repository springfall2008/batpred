import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import ts from 'typescript'

const utilityPath = fileURLToPath(new URL('../src/utils/inDayChart.ts', import.meta.url))

function loadInDayChartUtilities() {
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

test('in-day helpers preserve the daily span and cumulative summary values', () => {
  const { finalValue, getInDayWindow, latestValueAt } = loadInDayChartUtilities()
  const morning = Date.parse('2026-09-18T00:00:00Z')
  const now = Date.parse('2026-09-18T12:00:00Z')
  const evening = Date.parse('2026-09-18T23:55:00Z')
  const actual = [{ x: morning, y: 0 }, { x: now, y: 4.5 }, { x: evening, y: 9.2 }]
  const predicted = [{ x: morning, y: 0 }, { x: evening, y: 8.7 }]

  assert.deepEqual({ ...getInDayWindow([actual, predicted], now) }, { start: morning, end: evening })
  assert.equal(latestValueAt(actual, now), 4.5)
  assert.equal(finalValue(predicted), 8.7)
})
