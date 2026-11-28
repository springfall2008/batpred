import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/components/PlanIconKey.tsx', import.meta.url), 'utf8')
const module = { exports: {} }
vm.runInNewContext(ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS }
}).outputText, {
  module, exports: module.exports,
  require: (name) => name === './NordPoolIcon' ? () => React.createElement('svg', { 'data-icon': 'nord-pool' }) : require(name)
})

test('plan icon key explains every symbol and includes the red warning flag', () => {
  const html = renderToStaticMarkup(React.createElement(module.exports.default))
  assert.equal((html.match(/<dt>/g) ?? []).length, 26)
  assert.deepEqual([...html.matchAll(/<h3>(.*?)<\/h3>/g)].map((match) => match[1]), ['Columns', 'Controls', 'Actions', 'Indicators'])
  assert.match(html, /style="color:var\(--color-charge\)"/)
  assert.match(html, /style="color:var\(--color-export\)"/)
  assert.match(html, /Freeze charge/)
  assert.match(html, /Freeze export/)
  assert.match(html, /plan-warning-flag/)
  assert.match(html, /data-icon="flag"/)
  assert.match(html, /Warning \/ error/)
  assert.match(html, /Nord Pool/)
  assert.match(html, /not guaranteed/)
  const page = fs.readFileSync(new URL('../src/pages/PlanPage.tsx', import.meta.url), 'utf8')
  assert.ok(page.indexOf('<PlanIconKey />') > page.indexOf('<PlanTable'))
})
