import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import ts from 'typescript'

const utilityPath = fileURLToPath(new URL('../src/utils/metrics.ts', import.meta.url))

function loadUtilities() {
  const source = fs.readFileSync(utilityPath, 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 }
  }).outputText
  const module = { exports: {} }
  vm.runInNewContext(compiled, { module, exports: module.exports, Date })
  return module.exports
}

test('metric helpers summarise labelled values and timestamps', () => {
  const { formatMetricAge, metricVersion, sumMetricValues } = loadUtilities()

  assert.equal(sumMetricValues({ "{'type': 'warning'}": 2, "{'type': 'error'}": 3 }), 5)
  assert.equal(metricVersion({ "{'version': 'v9.2.0'}": 1 }), 'v9.2.0')
  assert.equal(formatMetricAge(1000, 4665), '1h 1m ago')
  assert.equal(formatMetricAge(0, 4665), 'Never')
})
