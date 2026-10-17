import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

import ts from 'typescript'

const utilityPath = fileURLToPath(new URL('../src/utils/currency.ts', import.meta.url))

function loadCurrencyUtilities() {
  const source = fs.readFileSync(utilityPath, 'utf8')
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2023
    }
  }).outputText
  const module = { exports: {} }

  vm.runInNewContext(compiled, { module, exports: module.exports })
  return module.exports
}

test('currency symbols support configured arrays and legacy strings', () => {
  const { resolveCurrencySymbols } = loadCurrencyUtilities()

  assert.deepEqual({ ...resolveCurrencySymbols(['€', 'c']) }, { major: '€', minor: 'c' })
  assert.deepEqual({ ...resolveCurrencySymbols('$c') }, { major: '$', minor: 'c' })
  assert.deepEqual({ ...resolveCurrencySymbols(undefined) }, { major: '£', minor: 'p' })
})

test('currency values and rates use their configured symbols', () => {
  const { formatMajorCurrency, formatRate } = loadCurrencyUtilities()

  assert.equal(formatMajorCurrency(1.25, '€'), '€1.25')
  assert.equal(formatMajorCurrency(-0.5, '$'), '-$0.50')
  assert.equal(formatRate(12.345, 'c'), '12.35c/kWh')
})
