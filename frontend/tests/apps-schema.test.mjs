import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/utils/appsSchema.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const module = { exports: {} }
vm.runInNewContext(compiled, { module, exports: module.exports })

const schema = module.exports.addEntitySuggestions(
  { $defs: {}, properties: { source: { type: 'string', 'x-ha-entity': true } } },
  [{ id: 'sensor.battery_soc', name: 'Battery state of charge (%)' }]
)

assert.equal(schema.properties.source.anyOf[0].type, 'string')
assert.equal(schema.properties.source.anyOf[1].$ref, '#/$defs/haEntitySuggestion')
assert.equal(schema.$defs.haEntitySuggestion.enum[0], 'sensor.battery_soc')
assert.equal(schema.$defs.haEntitySuggestion.markdownEnumDescriptions[0], 'Battery state of charge (%)')
