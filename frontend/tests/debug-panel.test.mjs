import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/components/DebugPanel.tsx', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 }
}).outputText
const module = { exports: {} }
vm.runInNewContext(compiled, {
  module, exports: module.exports,
  require(specifier) { return specifier.endsWith('.css') ? {} : require(specifier) }
})

test('debug history downloads recent snapshots from the registered backend route', () => {
  const html = renderToStaticMarkup(React.createElement(module.exports.default, { planData: {}, statusData: {}, powerFlowData: null }))
  const href = html.match(/href="([^"]*debug_history[^"]*)"/)?.[1]
  assert.equal(href, '../debug_history_download_recent')
  assert.match(html, /Download recent debug snapshots/)
  assert.match(html, /Download recent<\/a>/)
  assert.doesNotMatch(html, /Download all/)
  const backend = fs.readFileSync(new URL('../../apps/predbat/web.py', import.meta.url), 'utf8')
  for (const base of ['https://example.test/ui/dash', 'https://example.test/api/hassio_ingress/example/ui/dash']) {
    const url = new URL(href, base)
    assert.ok(url.pathname.endsWith('/debug_history_download_recent'))
    assert.ok(backend.includes('app.router.add_get("/debug_history_download_recent", self.html_debug_history_download_recent)'))
  }
})
