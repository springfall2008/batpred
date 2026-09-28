import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = readFileSync(new URL('../src/components/PlanVisual.tsx', import.meta.url), 'utf8')
const currencySource = readFileSync(new URL('../src/utils/currency.ts', import.meta.url), 'utf8')

function compile(value) {
  return ts.transpileModule(value, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 } }).outputText
}

const currencyModule = { exports: {} }
vm.runInNewContext(compile(currencySource), { module: currencyModule, exports: currencyModule.exports })

const module = { exports: {} }
vm.runInNewContext(compile(source), {
  module,
  exports: module.exports,
  require(specifier) {
    if (specifier.endsWith('.css')) return {}
    if (specifier.endsWith('/utils/currency')) return currencyModule.exports
    return require(specifier)
  }
})

test('timeline uses separate date labels and three-hour time ticks', () => {
  const start = new Date(2026, 0, 1, 0, 0).getTime()
  const ticks = module.exports.buildTimeTicks(start, start + 6 * 60 * 60 * 1000)

  assert.equal(ticks.length, 3)
  assert.equal(ticks[0].label, '00:00')
  assert.match(ticks[0].dateLabel, /1 Jan/)
  assert.equal(ticks[1].label, '03:00')
  assert.equal(ticks[1].dateLabel, undefined)
  assert.equal(ticks[2].label, '06:00')
})

test('SOC hover interpolates the plotted value at the pointer time', () => {
  const points = [{ time: 0, soc: 20 }, { time: 30, soc: 50 }, { time: 60, soc: 40 }]

  assert.equal(module.exports.interpolateSoc(points, 15), 35)
  assert.equal(module.exports.interpolateSoc(points, 45), 45)
  assert.equal(module.exports.interpolateSoc(points, 90), 40)
})
