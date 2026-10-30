import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const compile = source => ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 }
}).outputText
const utility = { exports: {} }
vm.runInNewContext(compile(fs.readFileSync(new URL('../src/utils/dataFreshness.ts', import.meta.url), 'utf8')), {
  module: utility, exports: utility.exports
})
const { snapshotState, snapshotAge } = utility.exports
const now = Date.parse('2026-10-29T10:00:00Z')

test('missing, invalid and future source times are unavailable, not fresh zero readings', () => {
  for (const time of [null, 0, NaN, Infinity, now + 120_000]) assert.equal(snapshotState(time, now, 600_000), 'unavailable')
  assert.equal(snapshotAge(NaN, now), 'time unknown')
})

test('snapshot age advances without a request; refresh failures do not reset source time', () => {
  assert.equal(snapshotState(now, now + 300_000, 600_000), 'current')
  assert.equal(snapshotState(now, now + 601_000, 600_000), 'stale')
  assert.equal(snapshotState(now, now + 1_000, 600_000, true), 'stale')
  assert.equal(snapshotAge(now, now + 3_600_000), '1h 0m ago')
})

const app = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
const declarations = app.slice(app.indexOf('  const planTimestamp ='), app.indexOf('  const activityOverlay ='))
const module = { exports: {} }
vm.runInNewContext(compile(`export function inspect({planData, powerFlowData, powerFlowUpdatedAt, freshnessNow, apiErrors, statusData, active = false}) {${declarations}; return {livePowerFlow, navigationPlanStatus, powerFreshness};}`), {
  module, exports: module.exports, require, snapshotAge, snapshotState
})
const state = { planData: { plan: { timestamp: '2026-10-29T10:00:00Z' } }, powerFlowData: { soc_percent: 0 }, powerFlowUpdatedAt: now, freshnessNow: now, apiErrors: {}, statusData: { calculating: false } }

test('live zero is preserved after a completed source refresh; startup defaults are withheld', () => {
  assert.equal(module.exports.inspect(state).livePowerFlow.soc_percent, 0)
  const startup = module.exports.inspect({ ...state, powerFlowUpdatedAt: null })
  assert.equal(startup.livePowerFlow, null)
  assert.equal(startup.powerFreshness, 'unavailable')
})

test('old snapshots and failed refreshes retain stale or unavailable status', () => {
  const cached = module.exports.inspect({ ...state, freshnessNow: now + 3_600_000 })
  assert.equal(cached.navigationPlanStatus, 'stale')
  assert.equal(cached.powerFreshness, 'stale')
  const failed = module.exports.inspect({ ...state, apiErrors: { plan: 'offline', powerFlow: 'offline', status: 'offline' } })
  assert.equal(failed.powerFreshness, 'stale')
  assert.equal(failed.navigationPlanStatus, 'unavailable')
})

test('recalculating retains calculation status', () => {
  const result = module.exports.inspect({ ...state, active: true, calculating: true, freshnessNow: now + 120_000 })
  assert.equal(result.navigationPlanStatus, 'calculating')
})

test('a missing or invalid plan timestamp has unavailable status', () => {
  for (const timestamp of [null, 'invalid', '2026-10-29T10:10:00Z']) {
    const result = module.exports.inspect({ ...state, planData: { plan: { timestamp } } })
    assert.equal(result.navigationPlanStatus, 'unavailable')
  }
})

test('metrics startup defaults are hidden and retained snapshots keep ageing after errors', () => {
  const metricsSource = fs.readFileSync(new URL('../src/components/MetricsPanel.tsx', import.meta.url), 'utf8')
  const helpers = { exports: {} }
  vm.runInNewContext(compile(fs.readFileSync(new URL('../src/utils/metrics.ts', import.meta.url), 'utf8')), { module: helpers, exports: helpers.exports })
  function render(timestamp, time, failed) {
    const data = new Proxy({ last_update_timestamp: timestamp, up: {}, errors_total: {}, api_services: {}, control_conflicts_events: [], control_conflicts_sustained_controls: [], config_errors: {} }, {
      get: (target, key) => key in target ? target[key] : 0
    })
    let slot = 0
    const component = { exports: {} }
    vm.runInNewContext(compile(metricsSource), {
      module: component, exports: component.exports,
      require(specifier) {
        if (specifier === 'react') return { useState: () => [[data, failed][slot++], () => {}], useEffect: () => {} }
        if (specifier.endsWith('.css')) return {}
        if (specifier === './MetricsCharts') return { __esModule: true, default: () => null }
        if (specifier.endsWith('/metrics')) return helpers.exports
        if (specifier.endsWith('/currency')) return { formatMajorCurrency: value => String(value) }
        return require(specifier)
      }
    })
    return renderToStaticMarkup(React.createElement(component.exports.default, { lastStarted: null, now: time }))
  }
  const startup = render(0, now, false)
  assert.match(startup, /Awaiting the first completed Predbat update/)
  assert.doesNotMatch(startup, /Stopped|Running|Stale|0\.00/)
  const old = render(now / 1000, now + 120_000, true)
  assert.match(old, /Refresh failed.*previous metrics.*2m ago/)
  assert.match(render(now / 1000, now + 300_000, true), /5m ago/)
})
