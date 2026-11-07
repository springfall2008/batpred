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
    require(specifier) {
      if (specifier.endsWith('.css')) return {}
      if (specifier === './SimplePowerFlow') {
        return () => React.createElement('div', { 'data-view': 'simple' })
      }
      return require(specifier)
    }
  })

  return module.exports.default
}

test('power flow always renders the basic view without a view selector', () => {
  const PowerFlow = loadPowerFlow()
  const html = renderToStaticMarkup(React.createElement(PowerFlow, { data: {} }))

  assert.match(html, /data-view="simple"/)
  assert.doesNotMatch(html, /Detailed/)
  assert.doesNotMatch(html, /power-flow-view-toggle/)
})
