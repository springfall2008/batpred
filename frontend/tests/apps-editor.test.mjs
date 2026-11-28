import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { test } from 'node:test'
import { build } from 'vite'
import { EditorState } from '@codemirror/state'
import { yaml } from '@codemirror/lang-yaml'
import { CompletionContext } from '@codemirror/autocomplete'

// Bundle the browser-only schema package, whose ESM imports omit file extensions.
const directory = await mkdtemp(fileURLToPath(new URL('../node_modules/.tmp-apps-editor-', import.meta.url)))
let support
try {
  const entry = `${directory}/entry.mjs`
  await writeFile(entry, `
    export * from ${JSON.stringify(fileURLToPath(new URL('../src/utils/appsEditor.ts', import.meta.url)))};
    export * from ${JSON.stringify(fileURLToPath(new URL('../src/utils/appsSchema.ts', import.meta.url)))};
    export { stateExtensions } from '@invopop/codemirror-json-schema';
    export * from ${JSON.stringify(fileURLToPath(new URL('../src/utils/editorMarkdown.ts', import.meta.url)))};
    export { yamlSchemaHover } from '@invopop/codemirror-json-schema/yaml';
  `)
  const result = await build({
    configFile: fileURLToPath(new URL('../vite.config.ts', import.meta.url)),
    logLevel: 'silent',
    build: {
      write: false,
      lib: { entry, formats: ['es'] },
      rolldownOptions: {
        external: [/^@codemirror\//],
        output: { codeSplitting: false },
      },
    },
  })
  assert.ok(!result[0].output.some(item => item.type === 'chunk' && Object.keys(item.modules).some(id => /[\\/]node_modules[\\/](?:shiki|@shikijs)[\\/]/.test(id))), 'schema help must not bundle Shiki')
  const output = `${directory}/support.mjs`
  await writeFile(output, result[0].output.find(item => item.type === 'chunk').code)
  support = await import(pathToFileURL(output).href)
} finally {
  await rm(directory, { recursive: true, force: true })
}

// Exercise the actual backend mapping without importing the Home Assistant runtime.
const backendDefinitions = JSON.parse(execFileSync('python3', ['-c', `
import ast, copy, json
from pathlib import Path
root = Path(${JSON.stringify(fileURLToPath(new URL('../../', import.meta.url)))})
namespace = {'copy': copy, 'THIS_VERSION': 'test'}
exec(compile(ast.parse((root / 'apps/predbat/const.py').read_text()), 'const.py', 'exec'), namespace)
config = ast.parse((root / 'apps/predbat/config.py').read_text())
config.body = [node for node in config.body if not isinstance(node, (ast.Import, ast.ImportFrom))]
exec(compile(config, 'config.py', 'exec'), namespace)
web = ast.parse((root / 'apps/predbat/web.py').read_text())
names = {'_apps_schema_value', '_apps_schema_description', 'build_apps_json_schema', 'APPS_SCHEMA_DESCRIPTIONS', 'INTEGRATION_NAMES', 'INTEGRATION_SETTING_DESCRIPTIONS'}
selected = [node for node in web.body if isinstance(node, ast.FunctionDef) and node.name in names or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)]
exec(compile(ast.Module(body=selected, type_ignores=[]), 'web.py', 'exec'), namespace)
print(json.dumps({'schema': namespace['build_apps_json_schema'](), 'specs': namespace['APPS_SCHEMA'], 'config_items': namespace['CONFIG_ITEMS']}))
`], { encoding: 'utf8' }))
const backendSchema = backendDefinitions.schema.$defs.predbatApp.properties

const schema = support.addEntitySuggestions({
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  type: 'object',
  properties: { pred_bat: { $ref: '#/$defs/predbatApp' } },
  $defs: {
    predbatApp: {
      type: 'object',
      properties: {
        threads: { type: 'integer', minimum: 0 },
        prefix: { type: 'string', pattern: '^[a-z][a-z0-9_]*$', description: 'Prefix added to every Home Assistant entity Predbat creates.' },
        ...backendSchema,
        module: { type: 'string', const: 'predbat' },
        ha_key: { type: 'string' },
        mode: { type: 'string', enum: ['auto', 'manual'] },
        grid_power: { type: 'string', 'x-ha-entity': true, description: 'Grid power sensor' },
        sensors: { type: 'array', items: { type: 'string', 'x-ha-entity': true } },
      },
    },
  },
}, [{ id: 'sensor.grid_power', name: 'Grid power' }])
const stateFor = doc => EditorState.create({ doc, extensions: [yaml(), support.stateExtensions(schema)] })
const diagnostics = doc => support.createAppsYamlLinter()({ state: stateFor(doc) })

test('YAML syntax, duplicate keys and schema errors block saving; corrected values pass', () => {
  assert.ok(diagnostics('pred_bat:\n  threads: [').length)
  assert.ok(diagnostics('pred_bat:\n  threads: 1\n  threads: 2').length)
  assert.ok(diagnostics('pred_bat:\n  threads: invalid').length)
  assert.ok(diagnostics('pred_bat:\n  mode: invalid').length)
  assert.ok(diagnostics('pred_bat:\n  module: wrong').length)
  assert.ok(diagnostics('pred_bat:\n  threads: -1').length)
  assert.deepEqual(diagnostics('pred_bat:\n  threads: 2\n  mode: auto'), [])
})

test('secret tags, anchors and arbitrary entity values remain valid and unchanged', () => {
  const doc = 'pred_bat:\n  ha_key: !secret ha_token\n  grid_power: sensor.custom_grid\n  sensors: &sensors [sensor.grid_power]\n  other: *sensors\n'
  const state = stateFor(doc)
  assert.deepEqual(diagnostics(doc), [])
  assert.equal(support.parseAppsYamlState(state).data.pred_bat.ha_key, 'ha_token')
  assert.equal(state.doc.toString(), doc)
})

test('schema keys, enums and entity values have contextual completion', () => {
  for (const [doc, expected] of [
    ['pred_bat:\n  th', 'threads'],
    ['pred_bat:\n  mode: a', 'auto'],
    ['pred_bat:\n  grid_power: sensor.', 'sensor.grid_power'],
    ['pred_bat:\n  sensors:\n    - sensor.', 'sensor.grid_power'],
  ]) {
    const state = stateFor(doc)
    const result = support.createAppsYamlCompletion()(new CompletionContext(state, doc.length, true))
    assert.ok(result.options.some(option => option.label.replaceAll('"', '') === expected), `${expected}: ${JSON.stringify(result)}`)
  }
})

test('schema hover describes the selected field', async () => {
  const doc = 'pred_bat:\n  grid_power: sensor.grid_power'
  const state = stateFor(doc)
  let description
  const tooltip = await support.yamlSchemaHover({ ...support.appsYamlHoverOptions, formatHover: texts => {
    description = texts.message
    return {}
  } })({ state }, doc.indexOf('grid_power') + 2, 0)
  assert.ok(tooltip)
  assert.equal(description, 'Grid power sensor')
})

test('save still sends the source checksum and validates current text before confirmation', async () => {
  const source = await readFile(new URL('../src/pages/AppsEditorPage.tsx', import.meta.url), 'utf8')
  assert.match(source, /JSON.stringify\(\{ content, checksum: source.checksum \}\)/)
  assert.ok(source.indexOf('const errors = createAppsYamlLinter()(editor)') < source.indexOf("window.confirm('Saving changes"))
  assert.match(source, /editor.destroy\(\)/)
  assert.doesNotMatch(source, /monaco|\?worker/i)
})

test('comments do not receive schema suggestions', () => {
  const doc = 'pred_bat:\n  # th'
  const state = stateFor(doc)
  assert.equal(support.createAppsYamlCompletion()(new CompletionContext(state, doc.length, true)), null)
})

test('backend string-list schemas accept scalar and list values without overlapping union errors', () => {
  for (const value of ['GE', '[GE]', '[GE, GS]']) {
    assert.deepEqual(diagnostics(`pred_bat:\n  inverter_type: ${value}`), [])
  }
  for (const value of ['GBP', '[GBP, p]']) {
    assert.deepEqual(diagnostics(`pred_bat:\n  currency_symbols: ${value}`), [])
  }
  for (const value of ['123', '[GE, 123]', '{type: GE}']) {
    assert.ok(diagnostics(`pred_bat:\n  inverter_type: ${value}`).length, value)
  }
})

test('schema help preserves Markdown and escapes HTML without syntax-highlighter assets', () => {
  assert.equal(support.renderMarkdown('Use `sensor.grid` and **power**.'), 'Use <code>sensor.grid</code> and <strong>power</strong>.')
  const rendered = support.renderMarkdown('<img src=x onerror=alert(1)> [bad](javascript:alert(1))')
  assert.ok(rendered.includes('&lt;img'))
  assert.doesNotMatch(rendered, /<img|href="javascript:/)
  assert.match(support.renderMarkdown('```yaml\ngrid_power: sensor.grid\n```', false), /<pre><code class="language-yaml">grid_power: sensor.grid/)
})

test('prefix validation gives a concise correction without technical schema details', () => {
  const errors = diagnostics('pred_bat:\n  prefix: predbat-dev')
  assert.equal(errors.length, 1)
  assert.equal(errors[0].message, 'Prefix must start with a lowercase letter and contain only lowercase letters, numbers or underscores. Use predbat_dev instead of predbat-dev.')
  assert.equal(errors[0].renderMessage, undefined)
  assert.equal(errors[0].source, undefined)
  assert.deepEqual(diagnostics('pred_bat:\n  prefix: predbat_dev'), [])
  const hover = support.appsYamlHoverOptions.getHoverTexts({ schema: { description: 'Prefix help', type: 'string', pattern: '^[a-z][a-z0-9_]*$' }, pointer: '/pred_bat/prefix' })
  assert.deepEqual(hover, { message: 'Prefix help', typeInfo: '' })
  assert.ok(diagnostics('pred_bat:\n  threads: invalid').length)
})


test('backend list settings accept single values and lists while rejecting wrong shapes', () => {
  for (const [name, valid, invalid] of [
    ['inverter_limit', ['5000', '5000.5', 'sensor.grid_power', '[5000, sensor.grid_power]'], ['{limit: 5000}', '[[5000]]']],
    ['inverter_limit_charge', ['3000', '[3000]'], ['{limit: 3000}', '[[3000]]']],
    ['battery_rate_max', ['2600', '[2600, 3000]'], ['{rate: 2600}', '[[2600]]']],
    ['car_charging_exclusive', ['true', 'false', '[true, false]'], ['123', '[true, 123]', '{value: true}']],
    ['days_previous', ['7', '[7, 14]'], ['7.5', '[7, invalid]', '{days: 7}']],
    ['days_previous_weight', ['1', '0.5', '[1, 0.5]'], ['invalid', '[1, invalid]', '{weight: 1}']],
    ['rates_import_override', ['{start: "00:00", end: "06:00", rate: 10}', '[{rate: 10}]'], ['10', '[10]']],
  ]) {
    for (const value of valid) assert.deepEqual(diagnostics(`pred_bat:\n  ${name}: ${value}`), [], `${name}: ${value}`)
    for (const value of invalid) assert.ok(diagnostics(`pred_bat:\n  ${name}: ${value}`).length, `${name}: ${value}`)
  }
})

test('single-value sensor list settings retain entity completion', () => {
  for (const doc of ['pred_bat:\n  inverter_limit: sensor.', 'pred_bat:\n  inverter_limit:\n    - sensor.']) {
    const state = stateFor(doc)
    const result = support.createAppsYamlCompletion()(new CompletionContext(state, doc.length, true))
    assert.ok(result.options.some(option => option.label.replaceAll('"', '') === 'sensor.grid_power'))
  }
})


function fieldDiagnostics(name, value) {
  const fieldSchema = backendSchema[name]
  assert.ok(fieldSchema, `Missing editor schema for ${name}`)
  const state = EditorState.create({
    doc: `value: ${JSON.stringify(value)}`,
    extensions: [yaml(), support.stateExtensions({
      type: 'object', properties: { value: fieldSchema }, $defs: backendDefinitions.schema.$defs,
    })],
  })
  return support.createAppsYamlLinter()({ state })
}

test('every declared apps setting accepts its supported single and list forms', () => {
  const failures = []
  const samples = { string: 'example', integer: 1, float: 1.5, boolean: true, dict: {}, sensor: 'sensor.schema_audit', int_float_dict: { 50: 0.85 } }
  for (const [name, spec] of Object.entries(backendDefinitions.specs)) {
    for (const declared of spec.type.split('|')) {
      const kind = declared.replace(/_list$/, '')
      assert.ok(Object.hasOwn(samples, kind), `Unhandled backend type ${declared}`)
      let value = samples[kind]
      const property = backendSchema[name]
      if (property.enum) {
        value = property.enum.find(option => typeof option === typeof value)
        if (value === undefined) continue
      }
      if (typeof value === 'number') value = Math.max(value, property.minimum ?? spec.min ?? value)
      const forms = declared.endsWith('_list') || kind === 'sensor' || spec.entries ? [value, [value]] : [value]
      for (const form of forms) {
        const errors = fieldDiagnostics(name, form)
        if (errors.length) failures.push({ name, declared, value: form, errors: errors.map(error => error.message) })
      }
    }
    if (spec.or_auto && fieldDiagnostics(name, 'auto').length) failures.push({ name, value: 'auto' })
  }
  assert.deepEqual(failures, [])
})

test('every published editor default has a matching schema type and constraints', () => {
  const failures = []
  for (const [name, property] of Object.entries(backendSchema)) {
    if (!Object.hasOwn(property, 'default')) continue
    const errors = fieldDiagnostics(name, property.default)
    if (errors.length) failures.push({ name, value: property.default, errors: errors.map(error => error.message) })
  }
  assert.deepEqual(failures, [])
})


test('per-device integer arrays and sensor arrays keep their item validation', () => {
  for (const name of ['inverter_battery_rate_min', 'inverter_reserve_max']) {
    assert.deepEqual(fieldDiagnostics(name, [1, 2]), [])
    assert.ok(fieldDiagnostics(name, [1, 'invalid']).length)
    assert.ok(fieldDiagnostics(name, [1.5]).length)
  }
  assert.deepEqual(fieldDiagnostics('car_charging_limit', ['sensor.car_one', 'sensor.car_two']), [])
  assert.ok(fieldDiagnostics('car_charging_limit', [{ invalid: true }]).length)
})

test('every declared numeric range rejects values outside its bounds', () => {
  for (const [name, spec] of Object.entries(backendDefinitions.specs)) {
    if (Object.hasOwn(spec, 'min')) assert.ok(fieldDiagnostics(name, spec.min - 1).length, name)
    if (Object.hasOwn(spec, 'max')) assert.ok(fieldDiagnostics(name, spec.max + 1).length, name)
  }
})
