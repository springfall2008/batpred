import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/pages/ComparePage.tsx', import.meta.url), 'utf8')
const component = source.slice(source.indexOf('function ComparisonChart('), source.indexOf('/** Compare configured tariffs'))
const module = { exports: {} }
vm.runInNewContext(ts.transpileModule(`${component}\nmodule.exports = ComparisonChart`, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 }
}).outputText, {
  module, exports: module.exports, require,
  useRef: () => ({ current: null }), useEffect: () => {}
})

test('comparison canvases expose distinct accessible names and an image role', () => {
  const daily = module.exports({ data: {} }).props.children
  const rolling = module.exports({ data: {}, rolling: true }).props.children
  assert.equal(daily.type, 'canvas')
  assert.equal(daily.props.role, 'img')
  assert.equal(daily.props['aria-label'], 'Daily true cost comparison of tariffs and actual costs')
  assert.equal(rolling.props.role, 'img')
  assert.equal(rolling.props['aria-label'], '7-day rolling average tariff cost comparison')
})
