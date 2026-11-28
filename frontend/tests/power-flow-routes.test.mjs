import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import ts from 'typescript'
import path from 'node:path'
import { createRequire } from 'node:module'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const source = fs.readFileSync(new URL('../src/utils/powerFlow.ts', import.meta.url), 'utf8')
const module = { exports: {} }
vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, { module, exports: module.exports })
const { resolvePowerFlow, getFlowDuration } = module.exports
const data = {
  load_power: 1600, house_power: 1100, pv_power: 3000, battery_power: 1000, grid_power: 400,
  battery_charging: true, battery_discharging: false, grid_importing: false,
  car: { configured: true, inside_clamp: true, power: 500 }, ashp: { power: 200 }
}

test('subtraction uses inclusive load and never subtracts the EV twice', () => {
  assert.equal(resolvePowerFlow(data, false).home, 1600)
  assert.equal(resolvePowerFlow(data, true).home, 900)
  const outside = { ...data, car: { ...data.car, inside_clamp: false } }
  assert.equal(resolvePowerFlow(outside, false).home, 2100)
  assert.equal(resolvePowerFlow(outside, true).home, 1400)
  assert.equal(resolvePowerFlow({ ...data, load_power: 10 }, true).home, 0)
})
test('solar supplies household, battery and export without double-counting', () => {
  const flow = resolvePowerFlow(data, false)
  assert.equal(flow.solarHome, 1600)
  assert.equal(flow.solarBattery, 1000)
  assert.equal(flow.solarGrid, 400)
  assert.equal(flow.gridHome, 0)
  assert.equal(flow.batteryHome, 0)
})
test('grid charging and battery export use correct route powers', () => {
  const imported = resolvePowerFlow({ ...data, pv_power: 0, grid_power: 2600, grid_importing: true }, false)
  assert.equal(imported.gridBattery, 1000)
  assert.equal(imported.gridHome, 1600)
  const exported = resolvePowerFlow({ ...data, pv_power: 0, battery_power: 2000, battery_charging: false, battery_discharging: true }, false)
  assert.equal(exported.batteryHome, 1600)
  assert.equal(exported.batteryGrid, 400)
})
test('optional devices and idle routes work without readings', () => {
  const flow = resolvePowerFlow({ ...data, load_power: 0, pv_power: 0, grid_power: 0, battery_power: 0, car: { configured: false }, ashp: null }, true)
  assert.equal(flow.home, 0)
  assert.equal(flow.car, 0)
  assert.equal(flow.heatPump, 0)
  assert.equal(flow.solarBattery, 0)
})
test('dot speed increases with power and stays capped', () => {
  assert.ok(parseFloat(getFlowDuration(100)) > parseFloat(getFlowDuration(1000)))
  assert.equal(getFlowDuration(3000), '0.80s')
  assert.equal(getFlowDuration(9000), '0.80s')
})

const require = createRequire(import.meta.url)
function loadComponent(file, preference = 'off') {
  const module = { exports: {} }
  const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true }
  }).outputText
  vm.runInNewContext(compiled, {
    module, exports: module.exports,
    require(specifier) {
      if (specifier.endsWith('.css')) return {}
      if (specifier === '../hooks/useStoredState') return { useStoredState: () => [preference, () => {}] }
      if (specifier.startsWith('.')) {
        const base = path.resolve(path.dirname(file), specifier)
        return loadComponent([`${base}.tsx`, `${base}.ts`].find((candidate) => fs.existsSync(candidate)), preference)
      }
      return require(specifier)
    }
  })
  return module.exports
}
const diagramFile = new URL('../src/components/SimplePowerFlow.tsx', import.meta.url).pathname
function renderDiagram(data, preference, socBoundaries = { low: 20, medium: 50 }) {
  const Component = loadComponent(diagramFile, preference).default
  return renderToStaticMarkup(React.createElement(Component, { data: { ...data, soc_percent: 55 }, socBoundaries }))
}
test('battery icon uses configured SOC boundaries', () => {
  assert.match(renderDiagram(data, 'off', { low: 30, medium: 70 }), /simple-node-icon soc-medium/)
  assert.match(renderDiagram(data, 'off', { low: 60, medium: 80 }), /simple-node-icon soc-low/)
})
test('configured devices stay visible below 100 W and their dots stop', () => {
  const html = renderDiagram({ ...data, car: { ...data.car, power: 99 }, ashp: { power: 99 } })
  assert.match(html, /Car:  Idle/)
  assert.match(html, /ASHP:  Idle/)
  assert.match(html, /flow-car"><path[^>]+is-idle/)
  assert.match(html, /flow-heat-pump"><path[^>]+is-idle/)
  assert.doesNotMatch(html, /type="checkbox"[^>]*checked/)
  const active = renderDiagram({ ...data, car: { ...data.car, power: 100 }, ashp: { power: 100 } })
  assert.match(active, /Car:  100 W/)
  assert.match(active, /flow-car"><path[^>]+is-active/)
})
test('optional nodes disappear and saved subtraction changes the home reading', () => {
  const minimal = renderDiagram({ ...data, car: { ...data.car, configured: false }, ashp: null })
  assert.doesNotMatch(minimal, /aria-label="Car:|aria-label="ASHP:|Subtract devices/)
  const html = renderDiagram(data, 'on')
  assert.match(html, /Home:  900 W/)
  assert.match(html, /type="checkbox" role="switch" checked/)
  assert.doesNotMatch(html, /simple-node-label">Home/)
  assert.match(html, /Grid: Exporting 400 W/)
  assert.match(html, /Battery: Charging 1.0 kW, 55% full/)
})
