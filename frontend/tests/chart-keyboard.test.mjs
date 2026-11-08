import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import ts from 'typescript'

test('chart keyboard controls zoom, pan and reset and are removed on destroy', () => {
  const source = fs.readFileSync(new URL('../src/utils/chartZoom.ts', import.meta.url), 'utf8')
  let plugin, handler
  const Chart = { defaults: { events: [], plugins: {} }, register(...plugins) { plugin = plugins[1] } }
  const module = { exports: {} }
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, {
    module, exports: module.exports, require: name => name === 'chart.js' ? { Chart } : {}
  })
  const calls = []
  const canvas = { addEventListener: (_, fn) => { handler = fn }, removeEventListener: (_, fn) => calls.push(fn === handler ? 'removed' : 'wrong handler') }
  const chart = { canvas, options: { plugins: {} }, zoom: value => calls.push(value), pan: value => calls.push(value.x), resetZoom: () => calls.push('reset'), isZoomedOrPanned: () => false }
  plugin.afterInit(chart)
  assert.equal(canvas.tabIndex, 0)
  for (const key of ['+', '-', 'ArrowLeft', 'ArrowRight', 'Home']) handler({ key, preventDefault: () => calls.push('prevented') })
  plugin.beforeDestroy(chart)
  assert.deepEqual(calls, [1.2, 'prevented', 1 / 1.2, 'prevented', 50, 'prevented', -50, 'prevented', 'reset', 'prevented', 'removed'])
  const staticCanvas = { addEventListener() { throw new Error('Static chart should not receive keyboard controls') } }
  plugin.afterInit({ canvas: staticCanvas, options: { plugins: { zoom: { pan: { enabled: false } } } } })
  assert.equal(staticCanvas.title, undefined)
})
