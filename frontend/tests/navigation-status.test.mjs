import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const app = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
const statusExpression = app.match(/const navigationPlanStatus = (.+)/)[1]
const freshnessModule = { exports: {} }
vm.runInNewContext(ts.transpileModule(fs.readFileSync(new URL('../src/utils/dataFreshness.ts', import.meta.url), 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS }
}).outputText, { module: freshnessModule, exports: freshnessModule.exports })
const source = fs.readFileSync(new URL('../src/components/AppNavigation.tsx', import.meta.url), 'utf8')
const module = { exports: {} }
vm.runInNewContext(ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 }
}).outputText, {
  module, exports: module.exports, URLSearchParams,
  window: { location: { pathname: '/ui/config', search: '' }, matchMedia: () => ({ matches: false }) },
  localStorage: { getItem: () => 'light' },
  require(specifier) {
    if (specifier === 'react') return { useState: initial => [typeof initial === 'function' ? initial() : initial, () => {}], useEffect: () => {} }
    if (/\.(css|png)$/.test(specifier)) return {}
    if (specifier === './theme') return { readThemePreference: value => value, resolveTheme: () => 'light', getNextThemePreference: () => 'dark' }
    return require(specifier)
  }
})

function indicator(state) {
  const planTimestamp = state.planData?.plan?.timestamp ? Date.parse(state.planData.plan.timestamp) : null
  const planFreshness = freshnessModule.exports.snapshotState(planTimestamp, Date.parse('2026-10-29T10:01:00Z'), 30 * 60_000, Boolean(state.apiErrors.plan))
  const planStatus = vm.runInNewContext(statusExpression, { ...state, planFreshness })
  const tree = module.exports.default({
    collapsed: false, layout: 'horizontal', onCollapsedChange() {}, onLayoutChange() {},
    planStatus, batterySoc: null, chatEnabled: false, version: ''
  })
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)]
  return nodes(tree).find(node => node.props?.className?.startsWith('navigation-plan-status'))
}

const ready = { apiErrors: {}, active: false, statusData: { calculating: false }, planData: { plan: { timestamp: '2026-10-29T10:00:00Z' } } }

for (const [name, overrides] of [
  ['initial data unavailable', { statusData: null, planData: null }],
  ['status unavailable', { statusData: null }],
  ['status missing calculating field', { statusData: {} }],
  ['plan unavailable', { planData: null }],
  ['plan timestamp unavailable', { planData: { plan: {} } }],
  ['status refresh failed', { apiErrors: { status: 'offline' } }],
  ['stale calculating status after failure', { apiErrors: { status: 'offline' }, active: true }]
]) {
  test(`navigation reports unavailable when ${name}`, () => {
    const node = indicator({ ...ready, ...overrides })
    assert.equal(node.props.title, 'Plan status unavailable')
    assert.equal(node.props['aria-label'], 'Plan status unavailable')
    assert.equal(node.props.className, 'navigation-plan-status is-unavailable')
    assert.equal(node.props.children.props.icon.iconName, 'circle-question')
    assert.equal(node.props.children.props.spin, false)
  })
}

test('navigation shows a generated plan only with plan data and an idle status', () => {
  const node = indicator(ready)
  assert.equal(node.props.title, 'Plan generated')
  assert.equal(node.props.children.props.icon.iconName, 'circle-check')
  assert.equal(node.props.className, 'navigation-plan-status is-ready')
})

test('navigation shows calculation in progress, including before the first plan exists', () => {
  const node = indicator({ ...ready, active: true, statusData: { calculating: true }, planData: null })
  assert.equal(node.props.title, 'Predbat is calculating')
  assert.equal(node.props.children.props.icon.iconName, 'circle-notch')
  assert.equal(node.props.children.props.spin, true)
})

test('unrelated power flow failure does not hide a generated plan', () => {
  assert.equal(indicator({ ...ready, apiErrors: { powerFlow: 'offline' } }).props.title, 'Plan generated')
})

test('old plans and failed plan refreshes are explicitly cached', () => {
  for (const state of [
    { ...ready, apiErrors: { plan: 'offline' } },
    { ...ready, planData: { plan: { timestamp: '2026-10-29T08:00:00Z' } } }
  ]) {
    const node = indicator(state)
    assert.equal(node.props.title, 'Showing a cached plan')
    assert.equal(node.props.className, 'navigation-plan-status is-stale')
    assert.equal(node.props.children.props.icon.iconName, 'circle-question')
  }
})
