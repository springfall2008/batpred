import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const require = createRequire(import.meta.url)

function loadComponent(name, stored = null) {
  const source = fs.readFileSync(new URL(`../src/${name}.tsx`, import.meta.url), 'utf8')
  const module = { exports: {} }
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS }
  }).outputText, {
    module, exports: module.exports,
    require(specifier) {
      if (specifier.endsWith('.css')) return {}
      if (specifier.endsWith('/useStoredState')) return { useStoredState: (key, fallback) => [stored ?? fallback, () => {}] }
      if (specifier === 'chart.js') return { Chart: { register() {} } }
      if (specifier.endsWith('/batteryChart')) return { getChartWindow: () => ({ start: 0, end: 1 }) }
      if (specifier.includes('/utils/')) return {}
      if (specifier.includes('/components/')) return { default: () => null }
      return require(specifier)
    }
  })
  return module.exports.default
}

test('PredHeat tab is shown only when enabled, including with an old saved selection', () => {
  const ChartsPage = loadComponent('pages/ChartsPage', 'predheat')
  const render = (enabled) => renderToStaticMarkup(React.createElement(ChartsPage, { plan: {}, loadMlEnabled: false, predheatEnabled: enabled }))
  assert.doesNotMatch(render(false), />PredHeat<\/button>/)
  assert.match(render(false), /Loading battery chart/)
  assert.match(render(true), />PredHeat<\/button>/)
  assert.match(render(true), /Loading predheat chart/)
})

test('PredHeat chart labels temperatures, predictions and forecast controls', () => {
  const PredheatChart = loadComponent('components/PredheatChart')
  const html = renderToStaticMarkup(React.createElement(PredheatChart, { data: { generated_at: '2026-10-24T12:00:00Z', series: {} } }))
  for (const label of ['Indoor temperature', 'Indoor forecast', 'Outdoor temperature', 'Outdoor forecast', 'Target temperature', 'Heating target', '12h', '24h', '48h']) {
    assert.ok(html.includes(label), label)
  }
  assert.match(html, /Dashed lines show predictions/)
  assert.match(html, /<canvas[^>]*role="img"[^>]*aria-label=/)
})
