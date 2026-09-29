import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const componentPath = fileURLToPath(new URL('../src/components/PlanTable.tsx', import.meta.url))
const currencyUtilityPath = fileURLToPath(new URL('../src/utils/currency.ts', import.meta.url))
const planUtilityPath = fileURLToPath(new URL('../src/utils/plan.ts', import.meta.url))

function loadTypeScriptModule(path) {
  const source = fs.readFileSync(path, 'utf8')
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

function loadPlanTable() {
  const source = fs.readFileSync(componentPath, 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2023
    }
  }).outputText

  const module = { exports: {} }
  vm.runInNewContext(compiled, {
    module,
    exports: module.exports,
    FormData,
    console,
    require(specifier) {
      if (specifier.endsWith('.css')) {
        return {}
      }

      if (specifier.endsWith('/utils/currency')) {
        return loadTypeScriptModule(currencyUtilityPath)
      }

      if (specifier === './NordPoolIcon') {
        return (props) => React.createElement('svg', { ...props, 'data-icon': 'nord-pool' })
      }

      return require(specifier)
    }
  })

  return module.exports.default
}

const row = {
  time: '2099-01-01T12:00:00Z',
  slot_minute: 720,
  import_rate: 10,
  export_rate: 5,
  import_rate_adjusted: 12.34,
  export_rate_adjusted: 4.25,
  rate_color_import: '#3AEE85',
  state: 'Demand',
  state_target: '60',
  show_limit: '60 (55)',
  state_override: '',
  reasons: [],
  pv_forecast: 1.2,
  pv_forecast10: 0.8,
  load_forecast: 0.5,
  load_forecast10: 0.3,
  load_color: '#F18261',
  clipped: 0.15,
  extra_load: '0.10, 0.05',
  soc_percent: 60,
  soc_change: 0,
  cost_change: 0.12,
  total_cost: 1.23,
  cost_color: '#FFFF00',
  description: ''
}

const plan = {
  rows: [row],
  reason_templates: {},
  currency_symbols: ['€', 'c'],
  soc: 6,
  soc_max: 10,
  mode: 'Control charge & discharge',
  num_cars: 0,
  car_charging_from_battery: true,
  car_energy_reported_load: false,
  totals: {
    total_cost: 2.34,
    pv_forecast: 1.2,
    load_forecast: 2.4,
    soc_percent: 55
  }
}

const overrides = {
  manual_charge_times: [],
  manual_export_times: [],
  manual_freeze_charge_times: [],
  manual_freeze_export_times: [],
  manual_demand_times: [],
  manual_import_rates: [],
  manual_export_rates: [],
  manual_soc: []
}

function renderPlanTable(debugEnabled, selectedPlan = plan) {
  const PlanTable = loadPlanTable()

  return renderToStaticMarkup(
    React.createElement(PlanTable, {
      plan: selectedPlan,
      overrides,
      debugEnabled,
      onOverrideSubmitted() {}
    })
  )
}

test('debug mode exposes the diagnostic plan values supplied by Predbat', () => {
  const html = renderPlanTable(true)

  assert.match(html, /Debug details are enabled/)
  assert.match(html, /Plan colour display/)
  assert.match(html, />Dots</)
  assert.match(html, />Cells</)
  assert.match(html, /PV \(10%\)/)
  assert.match(html, /Clip/)
  assert.match(html, /XLoad/)
  assert.match(html, /12\.34c with loss/)
  assert.match(html, /10\.00c/)
  assert.match(html, /c per kWh/)
  assert.match(html, /€1\.23/)
  assert.match(html, /0\.80/)
  assert.match(html, /0\.15/)
  assert.match(html, /0\.10, 0\.05/)
  assert.match(html, /Internal optimiser limit/)
  assert.match(html, />\(55\)</)
})

test('future rates show a prediction indicator', () => {
  const html = renderPlanTable(false, {
    ...plan,
    rows: [{ ...row, import_rate_adjust_type: 'future' }]
  })

  assert.match(html, /aria-label="Predicted rate"/)
  assert.match(html, /Predbat is using Nord Pool data/)
  assert.match(html, /data-icon="nord-pool"/)
})

test('iBoost column appears when iBoost is enabled', () => {
  const enabledHtml = renderPlanTable(false, {
    ...plan,
    iboost_enable: true,
    rows: [{
      ...row,
      iboost: 1.25,
      iboost_change: 0.5,
      iboost_color: '#FFFF00'
    }],
    totals: {
      ...plan.totals,
      iboost: 1.75
    }
  })
  const disabledHtml = renderPlanTable(false)

  assert.match(enabledHtml, />iBoost</)
  assert.match(enabledHtml, /Cumulative energy diverted to iBoost/)
  assert.match(enabledHtml, /1\.25 \(\+0\.50\)/)
  assert.match(enabledHtml, /1\.75/)
  assert.match(enabledHtml, /background-color:#FFFF00/)
  assert.doesNotMatch(disabledHtml, />iBoost</)
})

test('normal mode keeps diagnostic plan values hidden', () => {
  const html = renderPlanTable(false)

  assert.doesNotMatch(html, /Debug details are enabled/)
  assert.match(html, /Plan colour display/)
  assert.match(html, />Dots</)
  assert.match(html, />Cells</)
  assert.doesNotMatch(html, /PV \(10%\)/)
  assert.doesNotMatch(html, /12\.34c with loss/)
  assert.doesNotMatch(html, /0\.10, 0\.05/)
  assert.doesNotMatch(html, /Internal optimiser limit/)
  assert.match(html, /plan-pv-value is-generating/)
  assert.match(html, /background-color:#3AEE85/)
  assert.match(html, /background-color:#F18261/)
  assert.match(html, /background-color:#FFFF00/)
  assert.match(html, /\+€0\.12/)
  assert.match(html, /<span>Total<\/span>/)
  assert.match(html, /Plan total/)
  assert.match(html, /€2\.34/)
})

test('carbon columns appear only when carbon planning is enabled', () => {
  const carbonPlan = {
    ...plan,
    carbon_enable: true,
    rows: [{
      ...row,
      carbon_intensity: 123,
      carbon_change: 25,
      total_carbon: 0.42,
      carbon_intensity_color: '#90EE90',
      carbon_color: '#FFAA00'
    }],
    totals: {
      ...plan.totals,
      carbon_intensity: 123,
      total_carbon: 0.67
    }
  }

  const enabledHtml = renderPlanTable(false, carbonPlan)
  const disabledHtml = renderPlanTable(false)

  assert.match(enabledHtml, /CO₂ intensity/)
  assert.match(enabledHtml, /CO₂ total/)
  assert.match(enabledHtml, /0\.42/)
  assert.match(enabledHtml, /0\.67/)
  assert.match(enabledHtml, /background-color:#90EE90/)
  assert.doesNotMatch(disabledHtml, /CO₂ intensity/)
})

test('plan view selection maps history and baseline data', () => {
  const { selectPlanView, shouldShowPlanDebug } = loadTypeScriptModule(planUtilityPath)
  const history = { rows: ['history'] }
  const baseline = { rows: ['baseline'] }

  assert.equal(selectPlanView('plan', plan, history, baseline), plan)
  assert.equal(selectPlanView('yesterday', plan, history, baseline), history)
  assert.equal(selectPlanView('baseline', plan, history, baseline), baseline)

  assert.equal(shouldShowPlanDebug('plan', { ...plan, plan_debug: true }, false), true)
  assert.equal(shouldShowPlanDebug('plan', plan, true), true)
  assert.equal(shouldShowPlanDebug('yesterday', { ...plan, plan_debug: true }, true), false)
})
