import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'

import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/utils/tariffHelper.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const module = { exports: {} }
vm.runInNewContext(compiled, { module, exports: module.exports })

const settings = [
  { name: 'combine_charge_slots', value: true },
  { name: 'set_charge_low_power', value: true },
  { name: 'combine_export_slots', value: true },
  { name: 'set_export_low_power', value: true }
]

test('tariff helper detects dynamic pricing and suggests preserving individual price peaks', () => {
  const start = Date.parse('2026-09-24T12:00:00Z')
  const series = (values) => Object.fromEntries(values.map((value, index) => [new Date(start + index * 1800000).toISOString(), value]))
  const analysis = module.exports.analyseTariff(settings, {
    generated_at: new Date(start).toISOString(),
    series: { import: series([-2, 4, 11, 18, 25, 31, 16, 7]), export: series([4, 4, 5, 14]) }
  })

  assert.equal(analysis.profile, 'Dynamic')
  assert.deepEqual(Array.from(analysis.suggestions, ({ name, value }) => ({ name, value })), [
    { name: 'combine_charge_slots', value: false },
    { name: 'set_charge_low_power', value: false },
    { name: 'combine_export_slots', value: false },
    { name: 'set_export_low_power', value: false }
  ])
})

test('tariff helper does not make export suggestions without export-rate data', () => {
  const stamp = '2026-09-24T12:00:00Z'
  const analysis = module.exports.analyseTariff(settings, {
    generated_at: stamp,
    series: { import: { [stamp]: 10, '2026-09-24T12:30:00Z': 30 }, export: {} }
  })

  assert.deepEqual(Array.from(analysis.suggestions, ({ name }) => name), [])
})

test('tariff helper carries a flat export rate into the analysis window', () => {
  const analysis = module.exports.analyseTariff(settings, {
    generated_at: '2026-09-27T08:55:00+01:00',
    currency_unit: 'p',
    series: {
      import: { '2026-09-27T08:30:00+01:00': 32.32 },
      export: {
        '2026-09-26T00:00:00+01:00': 7.64,
        '2026-09-30T08:30:00+01:00': 7.64
      }
    }
  })

  assert.equal(analysis.exportMin, 7.64)
  assert.equal(analysis.exportMax, 7.64)
})
