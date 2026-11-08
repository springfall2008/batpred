import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import ts from 'typescript'

const utilityPath = fileURLToPath(new URL('../src/utils/batteryChart.ts', import.meta.url))
const powerUtilityPath = fileURLToPath(new URL('../src/utils/powerChart.ts', import.meta.url))
const costUtilityPath = fileURLToPath(new URL('../src/utils/costChart.ts', import.meta.url))

function loadBatteryChartUtilities() {
  const source = fs.readFileSync(utilityPath, 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2023
    }
  }).outputText
  const module = { exports: {} }

  vm.runInNewContext(compiled, {
    module,
    exports: module.exports
  })

  return module.exports
}

function loadPowerChartUtilities() {
  const source = fs.readFileSync(powerUtilityPath, 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2023
    }
  }).outputText
  const module = { exports: {} }

  vm.runInNewContext(compiled, {
    module,
    exports: module.exports,
    require() {
      return {}
    }
  })

  return module.exports
}

function loadCostChartUtilities() {
  const source = fs.readFileSync(costUtilityPath, 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2023
    }
  }).outputText
  const module = { exports: {} }

  vm.runInNewContext(compiled, {
    module,
    exports: module.exports
  })

  return module.exports
}

test('battery series conversion sorts points and drops invalid values', () => {
  const { toChartPoints } = loadBatteryChartUtilities()
  const points = toChartPoints({
    '2026-09-18T12:00:00Z': 7.5,
    invalid: 10,
    '2026-09-18T10:00:00Z': 6.25
  })

  assert.deepEqual(Array.from(points, (point) => point.y), [6.25, 7.5])
})

test('plan action lookup returns the action in force at the tooltip time', () => {
  const { actionAtTime } = loadBatteryChartUtilities()
  const rows = [
    { time: '2026-09-18T10:00:00Z', state: 'Demand' },
    { time: '2026-09-18T10:30:00Z', state: 'Charge' },
    { time: '2026-09-18T11:00:00Z', state: 'Demand' }
  ]

  assert.equal(actionAtTime(rows, Date.parse('2026-09-18T10:45:00Z')), 'Charge')
  assert.equal(actionAtTime(rows, Date.parse('2026-09-18T09:45:00Z')), null)
})

test('compact Predbat plan actions are expanded for display', () => {
  const { formatPlanAction } = loadBatteryChartUtilities()

  assert.equal(formatPlanAction('Chrg'), 'Charge')
  assert.equal(formatPlanAction('HoldChrg'), 'Hold Charge')
  assert.equal(formatPlanAction('FrzExp'), 'Freeze Export')
  assert.equal(formatPlanAction('Demand'), 'Demand')
})

test('comparison data helpers identify real charge targets and the prediction end', () => {
  const { getChartWindow, hasSeriesData, predictionEndTime } = loadBatteryChartUtilities()
  const window = getChartWindow('2026-09-18T12:00:00Z', 12)
  const noChargeWindow = {
    '2026-09-18T12:00:00Z': 0,
    '2026-09-18T18:00:00Z': 0
  }
  const chargeWindow = {
    ...noChargeWindow,
    '2026-09-18T14:00:00Z': 8.5
  }
  const predictionWindow = {
    '2026-09-18T12:00:00Z': 0,
    '2026-09-18T20:00:00Z': 10
  }

  assert.equal(hasSeriesData(noChargeWindow, window, true), false)
  assert.equal(hasSeriesData(chargeWindow, window, true), true)
  assert.equal(predictionEndTime(predictionWindow), Date.parse('2026-09-18T20:00:00Z'))
})

test('cumulative iBoost energy is converted to interval power', () => {
  const { cumulativeEnergyToPower, getPowerChartWindow, gridPowerPeaks } = loadPowerChartUtilities()
  const points = cumulativeEnergyToPower({
    '2026-09-18T12:00:00Z': 0,
    '2026-09-18T12:30:00Z': 0.75,
    '2026-09-18T13:00:00Z': 1.25
  })

  assert.deepEqual(Array.from(points, (point) => point.y), [1.5, 1])
  assert.equal(
    getPowerChartWindow('2026-09-18T12:00:00Z', 12).start,
    Date.parse('2026-09-18T11:30:00Z')
  )
  assert.deepEqual(
    { ...gridPowerPeaks([{ x: 1, y: -10.5 }, { x: 2, y: 0.01 }]) },
    { import: 10.5, export: 0.01 }
  )
})

test('cost helpers retain today history and format minor currency units', () => {
  const { formatMinorCurrency, getCostChartWindow, latestCostValue, plannedSaving } = loadCostChartUtilities()
  const points = [
    { x: Date.parse('2026-09-18T00:00:00Z'), y: 0 },
    { x: Date.parse('2026-09-18T12:00:00Z'), y: 125 }
  ]
  const window = getCostChartWindow(points, '2026-09-18T12:00:00Z', 24)

  assert.equal(window.start, Date.parse('2026-09-18T00:00:00Z'))
  assert.equal(window.end, Date.parse('2026-09-19T12:00:00Z'))
  assert.equal(latestCostValue(points, Date.parse('2026-09-18T11:00:00Z')), 0)
  assert.equal(formatMinorCurrency(125, '£'), '£1.25')
  assert.equal(formatMinorCurrency(-50, '£'), '-£0.50')
  assert.equal(formatMinorCurrency(125, '€'), '€1.25')
  assert.equal(plannedSaving(240, 180), 60)
})
