import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/utils/entities.ts', import.meta.url), 'utf8')
const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
const module = { exports: {} }
vm.runInNewContext(output, { module, exports: module.exports })
const { filterEntities, formatEntityState } = module.exports

test('filters entities by metadata or live state and formats units', () => {
  const entities = [
    { id: 'sensor.predbat_soc', name: 'Battery state', group: 'Predbat Entities' },
    { id: 'switch.predbat_active', name: 'Predbat active', group: 'Config Settings' }
  ]
  const states = {
    'sensor.predbat_soc': { state: 83, attributes: { unit_of_measurement: '%' } },
    'switch.predbat_active': { state: 'on', attributes: {} }
  }

  assert.deepEqual(filterEntities(entities, states, '83').map((entity) => entity.id), ['sensor.predbat_soc'])
  assert.deepEqual(filterEntities(entities, states, 'config').map((entity) => entity.id), ['switch.predbat_active'])
  assert.equal(formatEntityState(states['sensor.predbat_soc']), '83 %')
  assert.equal(formatEntityState(), 'Unavailable')
})
