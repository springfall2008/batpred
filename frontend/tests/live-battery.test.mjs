import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const require = createRequire(import.meta.url)
function load(path) {
  const source = fs.readFileSync(new URL(path, import.meta.url), 'utf8')
  const module = { exports: {} }
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 }
  }).outputText, {
    module, exports: module.exports, URLSearchParams,
    window: { location: { pathname: '/ui', search: '' }, matchMedia: () => ({ matches: false }) },
    localStorage: { getItem: () => 'light' },
    require(specifier) {
      if (/\.(css|png)$/.test(specifier)) return {}
      if (specifier === '../utils/currency') return load('../src/utils/currency.ts')
      if (specifier === '../utils/plan') return load('../src/utils/plan.ts')
      if (specifier === './theme') return { readThemePreference: value => value, resolveTheme: () => 'light', getNextThemePreference: () => 'dark' }
      if (specifier === 'react') return { ...React, useState: initial => [typeof initial === 'function' ? initial() : initial, () => {}], useEffect: () => {} }
      return require(specifier)
    }
  })
  return module.exports
}

const PlanSummary = load('../src/components/PlanSummary.tsx').default
const Navigation = load('../src/components/AppNavigation.tsx').default
const plan = JSON.parse(fs.readFileSync(new URL('./fixtures/plan-data-split-actions.json', import.meta.url), 'utf8')).plan

test('plan reason is available through a named native disclosure', () => {
  const withReason = { ...plan, reason_templates: { test_reason: 'A readable explanation' }, rows: plan.rows.map(row => ({ ...row, reasons: [{ code: 'test_reason', params: {} }] })) }
  const html = renderToStaticMarkup(React.createElement(PlanSummary, { plan: withReason, batterySoc: 42, batteryStale: false, socBoundaries: { low: 20, medium: 50 } }))
  assert.match(html, /<details class="reason-tooltip"><summary aria-label="Why this action\? A readable explanation"/)
  assert.match(html, /A readable explanation/)
})

test('plan summary shows live, cached and unavailable battery readings instead of the plan snapshot', () => {
  const render = (batterySoc, batteryStale = false) => renderToStaticMarkup(React.createElement(PlanSummary, { plan, batterySoc, batteryStale, socBoundaries: { low: 20, medium: 50 } }))
  const live = render(0)
  assert.match(live, /Live battery SOC/)
  assert.match(live, />0%/)
  assert.doesNotMatch(live, />87%/)
  assert.match(render(42, true), /Battery SOC \(cached\).*42%/)
  assert.match(render(null), /Battery reading unavailable/)
})

test('menu battery icon follows the summary colour thresholds', () => {
  for (const [soc, colour] of [[0, 'low'], [20, 'low'], [21, 'medium'], [50, 'medium'], [51, 'high'], [100, 'high']]) {
    const tree = Navigation({ collapsed: false, layout: 'horizontal', onCollapsedChange() {}, onLayoutChange() {}, planStatus: 'ready', batterySoc: soc, socBoundaries: { low: 20, medium: 50 }, chatEnabled: false, version: '' })
    const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)]
    const battery = nodes(tree).find(node => node.props?.className?.startsWith('navigation-battery-status'))
    assert.equal(battery.props.className, `navigation-battery-status is-${colour}`)
  }
})

test('custom SOC boundaries are used by the menu and plan summary', () => {
  const socBoundaries = { low: 30, medium: 70 }
  const summary = renderToStaticMarkup(React.createElement(PlanSummary, { plan, batterySoc: 25, batteryStale: false, socBoundaries }))
  assert.match(summary, /battery-bar-fill battery-low/)
  const tree = Navigation({ collapsed: false, layout: 'horizontal', onCollapsedChange() {}, onLayoutChange() {}, planStatus: 'ready', batterySoc: 65, socBoundaries, chatEnabled: false, version: '' })
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)]
  assert.equal(nodes(tree).find(node => node.props?.className?.startsWith('navigation-battery-status')).props.className, 'navigation-battery-status is-medium')
})
