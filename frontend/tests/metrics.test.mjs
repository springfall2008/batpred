import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import ts from 'typescript'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createRequire } from 'node:module'

const utilityPath = fileURLToPath(new URL('../src/utils/metrics.ts', import.meta.url))

function loadUtilities() {
  const source = fs.readFileSync(utilityPath, 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 }
  }).outputText
  const module = { exports: {} }
  vm.runInNewContext(compiled, { module, exports: module.exports, Date })
  return module.exports
}

test('metric helpers summarise labelled values and timestamps', () => {
  const { formatMetricAge, metricVersion, sumMetricValues } = loadUtilities()

  assert.equal(sumMetricValues({ "{'type': 'warning'}": 2, "{'type': 'error'}": 3 }), 5)
  assert.equal(metricVersion({ "{'version': 'v9.2.0'}": 1 }), 'v9.2.0')
  assert.equal(formatMetricAge(1000, 4665), '1h 1m ago')
  assert.equal(formatMetricAge(0, 4665), 'Never')
})


test('metric chart helpers split signed power and order energy totals', () => {
  const { metricEnergyValues, metricPowerValues } = loadUtilities()
  const metrics = {
    battery_power: -2.5,
    grid_power: 1.25,
    load_power: 0.8,
    pv_power: 3.1,
    load_today_kwh: 9.2,
    pv_today_kwh: 12.4,
    import_today_kwh: 2.3,
    export_today_kwh: 4.7
  }

  assert.deepEqual(Array.from(metricPowerValues(metrics)), [2.5, 0, 0.8, 3.1, 0, 1.25])
  assert.deepEqual(Array.from(metricEnergyValues(metrics)), [9.2, 12.4, 2.3, 4.7])
})

test('dashboard chart descriptions contain the values and units shown in each chart', () => {
  const source = fs.readFileSync(new URL('../src/components/MetricsCharts.tsx', import.meta.url), 'utf8')
  const module = { exports: {} }
  const require = createRequire(import.meta.url)
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } }).outputText, {
    module, exports: module.exports,
    require: name => name === 'chart.js' ? { Chart: { register() {} } } : name.endsWith('/metrics') ? loadUtilities() : require(name)
  })
  const metrics = { battery_soc_percent: 42, battery_soc_kwh: 4.2, battery_max_kwh: 10, battery_power: -2.5, grid_power: 1.25, load_power: 0.8, pv_power: 3.1, load_today_kwh: 9.2, pv_today_kwh: 12.4, import_today_kwh: 2.3, export_today_kwh: 4.7 }
  const html = renderToStaticMarkup(React.createElement(module.exports.default, { metrics, socBoundaries: { low: 20, medium: 50 } }))
  assert.match(html, /42%, 4\.2 of 10\.0 kWh/)
  assert.match(html, /battery charge 2\.50 kW.*grid export 1\.25 kW/)
  assert.match(html, /load 9\.2 kWh.*grid export 4\.7 kWh/)
})
