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
const componentPath = fileURLToPath(new URL('../src/components/PowerFlow.tsx', import.meta.url))

function loadPowerFlow() {
  const source = fs.readFileSync(componentPath, 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      esModuleInterop: true,
      jsx: ts.JsxEmit.ReactJSX,
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2023
    }
  }).outputText

  const module = { exports: {} }

  vm.runInNewContext(compiled, {
    module,
    exports: module.exports,
    console,
    localStorage: {
      getItem() {
        return 'detailed'
      },
      setItem() {}
    },
    require(specifier) {
      if (specifier.endsWith('.css')) {
        return {}
      }

      if (specifier === './SimplePowerFlow') {
        return () => React.createElement('div', { 'data-view': 'simple' })
      }

      if (specifier === './DetailedPowerFlow') {
        return ({ showCar }) =>
          React.createElement('div', {
            'data-view': 'detailed',
            'data-show-car': String(showCar)
          })
      }

      return require(specifier)
    }
  })

  return module.exports.default
}

const data = {
  grid_power: 0,
  battery_power: 0,
  pv_power: 0,
  load_power: 0,
  house_power: 0,
  soc_percent: 50,
  grid_importing: false,
  battery_charging: false,
  battery_discharging: false,
  pv_generating: false,
  sun_state: 'above_horizon',
  car: {
    configured: false,
    power: 0,
    inside_clamp: true,
    charging: false
  }
}

test('detailed flow shows a car that Predbat is planning even without live power monitoring', () => {
  const PowerFlow = loadPowerFlow()
  const html = renderToStaticMarkup(React.createElement(PowerFlow, { data, numCars: 1 }))

  assert.match(html, /data-view="detailed"/)
  assert.match(html, /data-show-car="true"/)
})

test('detailed flow still hides the car when it is neither planned nor monitored', () => {
  const PowerFlow = loadPowerFlow()
  const html = renderToStaticMarkup(React.createElement(PowerFlow, { data, numCars: 0 }))

  assert.match(html, /data-show-car="false"/)
})
