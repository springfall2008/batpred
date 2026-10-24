import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import ts from 'typescript'

const utilityPath = fileURLToPath(new URL('../src/utils/solarChart.ts', import.meta.url))

function loadSolarChartUtilities() {
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

test('solar helpers build historical and forecast ranges and join forecasts at Now', () => {
  const { combineSolarForecast, getSolarChartWindow, getSolarPowerWindow, peakSolarPower, solarAccuracy, sumDailyCumulative } = loadSolarChartUtilities()
  const now = Date.parse('2026-09-18T12:00:00Z')
  const before = Date.parse('2026-09-18T11:30:00Z')
  const after = Date.parse('2026-09-18T12:30:00Z')
  const combined = combineSolarForecast(
    [{ x: before, y: 2.5 }, { x: after, y: 3 }],
    [{ x: before, y: 2.7 }, { x: after, y: 3.4 }],
    now
  )

  assert.deepEqual(Array.from(combined, (point) => ({ ...point })), [
    { x: before, y: 2.5 },
    { x: after, y: 3.4 }
  ])
  assert.equal(peakSolarPower(combined), 3.4)

  const oneDay = getSolarPowerWindow('2026-09-18T12:00:00', 1, Date.parse('2026-09-21T18:00:00'))
  const threeDays = getSolarPowerWindow('2026-09-18T12:00:00', 3, Date.parse('2026-09-19T18:00:00'))
  const sevenDays = getSolarPowerWindow('2026-09-18T12:00:00', 7, Date.parse('2026-09-21T18:00:00'))
  assert.deepEqual({ start: new Date(oneDay.start), end: new Date(oneDay.end) }, {
    start: new Date('2026-09-18T12:00:00'),
    end: new Date('2026-09-19T12:00:00')
  })
  assert.deepEqual({ start: new Date(threeDays.start), end: new Date(threeDays.end) }, {
    start: new Date('2026-09-17T00:00:00'),
    end: new Date('2026-09-20T00:00:00')
  })
  assert.equal(threeDays.forecastDays, 2)
  assert.deepEqual({ start: new Date(sevenDays.start), end: new Date(sevenDays.end) }, {
    start: new Date('2026-09-15T00:00:00'),
    end: new Date('2026-09-22T00:00:00')
  })
  assert.equal(sevenDays.forecastDays, 4)
  assert.equal((getSolarChartWindow('2026-09-18T12:00:00', 7).end - getSolarChartWindow('2026-09-18T12:00:00', 7).start) / 86400000, 7)
  assert.deepEqual({ ...solarAccuracy(8.5, 10) }, { difference: -1.5, achieved: 85 })
  assert.deepEqual({ ...solarAccuracy(null, 10) }, { difference: null, achieved: null })
  assert.equal(sumDailyCumulative([
    { x: Date.parse('2026-09-17T10:00:00Z'), y: 3 },
    { x: Date.parse('2026-09-17T18:00:00Z'), y: 8 },
    { x: Date.parse('2026-09-18T10:00:00Z'), y: 2 },
    { x: Date.parse('2026-09-18T18:00:00Z'), y: 5 }
  ]), 13)
})
