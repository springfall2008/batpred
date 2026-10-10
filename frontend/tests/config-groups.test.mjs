import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/utils/configGroups.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const module = { exports: {} }
vm.runInNewContext(compiled, { module, exports: module.exports })

const { configGroup } = module.exports
assert.equal(configGroup('pv_scaling'), 'Solar')
assert.equal(configGroup('battery_loss'), 'Battery')
assert.equal(configGroup('car_charging_rate'), 'Car')
assert.equal(configGroup('car_charging_manual_soc'), 'Car')
assert.equal(configGroup('inverter_soc_reset'), 'Inverter')
assert.equal(configGroup('rate_low_threshold'), 'Tariffs')
assert.equal(configGroup('predheat_enable'), 'Heating')
assert.equal(configGroup('ashp_power'), 'Heating')
assert.equal(configGroup('web_ui'), 'System')
assert.equal(configGroup('timezone'), 'System')
assert.equal(configGroup('unknown_setting'), 'Other')
