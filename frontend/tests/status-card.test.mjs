import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/components/StatusCard.tsx', import.meta.url), 'utf8')
const module = { exports: {} }
vm.runInNewContext(ts.transpileModule(source, {
  compilerOptions: { esModuleInterop: true, jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS }
}).outputText, {
  module, exports: module.exports,
  require: (specifier) => specifier.endsWith('.css') ? {} : require(specifier)
})

test('operating-mode selector includes every backend mode and selects Control SOC only', () => {
  const constants = fs.readFileSync(new URL('../../apps/predbat/const.py', import.meta.url), 'utf8')
  const modes = JSON.parse(constants.match(/^PREDBAT_MODE_OPTIONS = (\[.*\])$/m)[1])
  const html = renderToStaticMarkup(React.createElement(module.exports.default, {
    status: 'Idle', mode: 'Control SOC only', version: 'v9.3.8', latestVersion: null,
    lastUpdated: null, configOk: true, active: true, readOnly: false, debugEnabled: false
  }))
  const options = [...html.matchAll(/<option value="([^"]+)"/g)].map((match) => match[1].replaceAll('&amp;', '&'))
  assert.deepEqual(options, modes)
  assert.match(html, /<option value="Control SOC only" selected="">Control SOC only<\/option>/)
})

test('unknown and starting status are neutral rather than healthy', () => {
  for (const status of ['Unknown', 'Unavailable', 'Starting', '']) {
    const html = renderToStaticMarkup(React.createElement(module.exports.default, {
      status, mode: 'Monitor', version: '', lastUpdated: null, configOk: true,
      active: false, readOnly: true, debugEnabled: false
    }))
    assert.match(html, /status-dot status-unknown/)
    assert.doesNotMatch(html, /status-dot status-ok/)
  }
})
