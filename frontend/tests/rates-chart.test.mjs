import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import ts from 'typescript'

const utilityPath = fileURLToPath(new URL('../src/utils/ratesChart.ts', import.meta.url))

function loadRatesChartUtilities() {
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

test('rates helpers find the active and best upcoming tariffs', () => {
  const { currentRate, upcomingRateExtremes } = loadRatesChartUtilities()
  const now = Date.parse('2026-09-18T12:00:00Z')
  const end = Date.parse('2026-09-18T18:00:00Z')
  const imports = [
    { x: Date.parse('2026-09-18T11:30:00Z'), y: 24 },
    { x: Date.parse('2026-09-18T12:30:00Z'), y: -2 },
    { x: Date.parse('2026-09-18T15:00:00Z'), y: 12 }
  ]
  const exports = [
    { x: Date.parse('2026-09-18T11:30:00Z'), y: 5 },
    { x: Date.parse('2026-09-18T13:00:00Z'), y: 18 },
    { x: Date.parse('2026-09-18T17:00:00Z'), y: 10 }
  ]

  assert.equal(currentRate(imports, now), 24)
  assert.deepEqual(
    { ...upcomingRateExtremes(imports, exports, now, end) },
    { lowestImport: -2, highestExport: 18 }
  )
})
