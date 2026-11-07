import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import ts from 'typescript'

const source = readFileSync(new URL('../src/utils/cards.ts', import.meta.url), 'utf8')
const output = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText
const module = { exports: {} }
vm.runInNewContext(output, { module, exports: module.exports })
const { buildGlanceSummaryYaml, buildPlanSummaryYaml, getPlanSummaryEntities } = module.exports

test('uses the entity IDs exposed by the running Predbat instance', () => {
  const entities = [
    { id: 'house.status', name: 'Status', group: 'Predbat Entities' },
    { id: 'house.soc_kw_h0', name: 'Current SoC', group: 'Predbat Entities' },
    { id: 'sensor.unrelated', name: 'Unrelated', group: 'Other' }
  ]

  assert.equal(JSON.stringify(getPlanSummaryEntities(entities).map((entity) => entity.id)), '["house.status","house.soc_kw_h0"]')
  assert.match(buildPlanSummaryYaml(entities), /entity: house\.status/)
  assert.doesNotMatch(buildPlanSummaryYaml(entities), /predbat\.status|sensor\.unrelated/)
  assert.match(buildGlanceSummaryYaml(entities), /^type: glance/)
  assert.match(buildGlanceSummaryYaml(entities), /entities:\n  - entity: house\.status/)
})
