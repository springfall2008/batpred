import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = readFileSync(new URL('../src/pages/OverviewPage.tsx', import.meta.url), 'utf8')
const tree = ts.createSourceFile('OverviewPage.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
let timestamp
function visit(node) {
  if (ts.isJsxElement(node) && node.openingElement.tagName.getText(tree) === 'time') timestamp = node
  ts.forEachChild(node, visit)
}
visit(tree)
assert.ok(timestamp)
const compile = source => ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS }
}).outputText
const metrics = { exports: {} }
vm.runInNewContext(compile(readFileSync(new URL('../src/utils/metrics.ts', import.meta.url), 'utf8')), {
  module: metrics, exports: metrics.exports
})
const module = { exports: {} }
vm.runInNewContext(compile(`export function Timestamp({ updatedAt, now }) { return updatedAt === null ? null : (${timestamp.getText(tree)}); }`), {
  module, exports: module.exports, require, isValidElement: React.isValidElement,
  FontAwesomeIcon: () => null, formatMetricAge: metrics.exports.formatMetricAge
})

function render(updatedAt, now) {
  return renderToStaticMarkup(React.createElement(module.exports.Timestamp, {
    updatedAt, now: new Date(now)
  }))
}

test('Overview page shows the age of their last successful refresh', () => {
  const updatedAt = Date.parse('2026-10-22T10:00:00Z')
  assert.match(render(updatedAt, updatedAt + 20_000), /Updated 20s ago/)
  const stale = render(updatedAt, updatedAt + 120_000)
  assert.match(stale, /Updated 2m ago/)
  assert.match(stale, /datetime="2026-10-22T10:00:00.000Z"/i)
  assert.match(stale, /title="Last completed power-data refresh"/)
  assert.doesNotMatch(render(null, updatedAt), /<time/)
})

test('the shared refresh timestamp appears once, outside the cards', () => {
  assert.equal((source.match(/className="overview-updated"/g) ?? []).length, 1)
  assert.ok(source.lastIndexOf('className="overview-updated"') > source.lastIndexOf('</section>'))
})

test('Overview clock refreshes every second and clears its timers', () => {
  const hook = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'useCurrentTime')
  assert.ok(hook)
  let startInterval
  let tick
  let intervalDelay
  let timeoutDelay
  let cleanup
  let updates = 0
  const cleared = []
  const hookModule = { exports: {} }
  vm.runInNewContext(compile(`export ${hook.getText(tree)}`), {
    module: hookModule, exports: hookModule.exports,
    useState: initial => [initial(), () => { updates++ }],
    useEffect: effect => { cleanup = effect() },
    window: {
      setTimeout: (callback, delay) => { startInterval = callback; timeoutDelay = delay; return 1 },
      setInterval: (callback, delay) => { tick = callback; intervalDelay = delay; return 2 },
      clearTimeout: id => cleared.push(id),
      clearInterval: id => cleared.push(id)
    }
  })
  hookModule.exports.useCurrentTime()
  assert.ok(timeoutDelay > 0 && timeoutDelay <= 1000)
  startInterval()
  assert.equal(intervalDelay, 1000)
  tick()
  tick()
  assert.equal(updates, 3)
  cleanup()
  assert.deepEqual(cleared, [1, 2])
})

test('successful polling uses the backend snapshot time rather than the request time', async () => {
  const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')
  const appTree = ts.createSourceFile('App.tsx', appSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  let fetchFunction
  function find(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'fetchPowerFlow') fetchFunction = node
    ts.forEachChild(node, find)
  }
  find(appTree)
  assert.ok(fetchFunction)
  let response = { updated_at: 1792663200 }
  let updatedAt
  const appModule = { exports: {} }
  vm.runInNewContext(compile(`export ${fetchFunction.getText(appTree)}`), {
    module: appModule, exports: appModule.exports,
    fetchJson: async () => response,
    setPowerFlowData: () => {},
    setPowerFlowUpdatedAt: value => { updatedAt = value }
  })
  await appModule.exports.fetchPowerFlow()
  assert.equal(updatedAt, 1792663200000)
  await appModule.exports.fetchPowerFlow()
  assert.equal(updatedAt, 1792663200000)
  response = { updated_at: null }
  await appModule.exports.fetchPowerFlow()
  assert.equal(updatedAt, null)
  response = {}
  await appModule.exports.fetchPowerFlow()
  assert.equal(updatedAt, null)
})
