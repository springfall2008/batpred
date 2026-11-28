import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/pages/ConfigPage.tsx', import.meta.url), 'utf8')
const appSource = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8')

function configPage(onUpdatePendingChange = () => {}) {
  const slots = [], effects = [], requests = []
  let cursor = 0, tree
  const hooks = {
    useState(initial) {
      const index = cursor++
      if (!(index in slots)) slots[index] = initial
      return [slots[index], next => { slots[index] = typeof next === 'function' ? next(slots[index]) : next }]
    },
    useCallback(callback) { return callback },
    useMemo(callback) { return callback() },
    useEffect(effect) { if (!tree) effects.push(effect) }
  }
  const module = { exports: {} }
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 }
  }).outputText, {
    module, exports: module.exports, Error, URLSearchParams,
    fetch(url) { return new Promise(resolve => requests.push({ url, resolve })) },
    require(specifier) {
      if (specifier === 'react') return hooks
      if (specifier.endsWith('.css')) return {}
      if (specifier.endsWith('/configGroups')) return { CONFIG_GROUPS: ['System'], configGroup: () => 'System' }
      if (specifier.endsWith('/tariffHelper')) return { analyseTariff: () => ({ profile: 'Test tariff', importMin: 1, importMax: 2, exportMin: null, suggestions: [] }) }
      return require(specifier)
    }
  })
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)]
  const render = () => {
    cursor = 0
    tree = module.exports.default({ onUpdatePendingChange })
    effects.splice(0).forEach(effect => effect())
    return nodes(tree)
  }
  const text = node => typeof node === 'string' || typeof node === 'number' ? String(node) : [node?.props?.children].flat(Infinity).map(child => child ? text(child) : '').join('')
  render()
  return {
    requests,
    nodes: render,
    text: () => { render(); return text(tree) },
    refresh() { render().find(node => node.type === 'button').props.onClick(); render() },
    search(value) { render().find(node => node.props.type === 'search').props.onChange({ target: { value } }); render() },
    select(name, value) { render().find(node => node.type === 'select' && node.props.name === name).props.onChange({ target: { value } }); render() },
    async respond(index, data, status = 200) {
      requests[index].resolve({ ok: status === 200, status, json: async () => data })
      await new Promise(resolve => setImmediate(resolve))
      render()
    }
  }
}

const item = { name: 'active', friendly_name: 'Active', entity: 'switch.active', type: 'switch', value: true, default: false }

test('configuration controls identify their settings and associated validation errors', async () => {
  const page = configPage()
  const select = { name: 'mode', friendly_name: 'Operating mode', entity: 'select.mode', type: 'select', value: 'one', default: 'one', options: ['one'] }
  const number = { name: 'limit', friendly_name: 'Charge limit', entity: 'input_number.limit', type: 'number', value: 50, default: 50 }
  await page.respond(0, { items: [item, select, number], errors: { active: 'Check active', mode: 'Check mode', limit: 'Check limit' } })
  await page.respond(1, {})
  const nodes = page.nodes()
  for (const [name, label] of [['active', 'Active'], ['mode', 'Operating mode'], ['limit', 'Charge limit']]) {
    const control = nodes.find(node => node.props?.['aria-label'] === label && ['input', 'select'].includes(node.type))
    assert.equal(control.props['aria-invalid'], true)
    assert.equal(control.props['aria-' + 'described' + 'by'], `config-error-${name}`)
    assert.ok(nodes.some(node => node.props?.id === `config-error-${name}`))
  }
})

test('switch labels match the displayed boolean values for enabled and disabled settings', async () => {
  const page = configPage()
  const off = { ...item, name: 'carbon_enable', friendly_name: 'Carbon enable', entity: 'switch.carbon', value: false, default: true }
  await page.respond(0, { items: [item, off] })
  await page.respond(1, {})
  const switches = page.nodes().filter(node => node.type === 'label' && node.props.className === 'config-switch')
  assert.deepEqual(switches.map(node => node.props.children[2].props.children), ['true', 'false'])
})

test('the update selector shows pending immediately and clears it on success or failure', async () => {
  const pending = []
  const page = configPage(value => pending.push(value))
  const update = { name: 'update', friendly_name: 'Predbat update', entity: 'select.predbat_update', type: 'select', value: 'old', default: 'old', options: ['old', 'new'] }
  await page.respond(0, { items: [update] })
  await page.respond(1, {})
  page.select('update', 'new')
  assert.deepEqual(pending, [true])
  await page.respond(2, {})
  assert.deepEqual(pending, [true, false])
  page.select('update', 'old')
  assert.deepEqual(pending, [true, false, true])
  await page.respond(3, null, 503)
  assert.deepEqual(pending, [true, false, true, false])
  assert.match(page.text(), /Unable to update configuration: HTTP 503/)
})

test('the global update indicator responds to a pending selector or server update', () => {
  const expression = appSource.match(/const updating = (.+)/)[1]
  for (const [updating, configUpdatePending, expected] of [[false, false, false], [false, true, true], [true, false, true]]) {
    assert.equal(vm.runInNewContext(expression, { statusData: { updating }, configUpdatePending }), expected)
  }
  assert.match(appSource, /<ConfigPage onUpdatePendingChange=\{setConfigUpdatePending\}/)
})

test('failed initial configuration load reports unavailable analysis without a false empty result', async () => {
  const page = configPage()
  await page.respond(0, null, 503)
  assert.match(page.text(), /Unable to update configuration: HTTP 503/)
  assert.match(page.text(), /Tariff analysis is unavailable because configuration could not be loaded/)
  assert.doesNotMatch(page.text(), /Analysing tariff|Loading configuration|No settings match|No configuration settings/)
  assert.equal(page.requests.length, 1)

  page.refresh()
  assert.match(page.text(), /Loading configuration/)
  await page.respond(1, { items: [item] })
  await page.respond(2, {})
  assert.match(page.text(), /Test tariff/)
  assert.match(page.text(), /Active/)
  assert.doesNotMatch(page.text(), /HTTP 503|unavailable|Loading configuration/)
})

test('successful empty configuration differs from an unmatched filter', async () => {
  const page = configPage()
  await page.respond(0, { items: [] })
  await page.respond(1, {})
  assert.match(page.text(), /No configuration settings are available/)
  assert.doesNotMatch(page.text(), /No settings match/)

  page.refresh()
  await page.respond(2, { items: [item] })
  await page.respond(3, {})
  page.search('missing setting')
  assert.match(page.text(), /No settings match that filter/)
})

test('failed refresh removes stale tariff suggestions while retaining loaded settings', async () => {
  const page = configPage()
  await page.respond(0, { items: [item] })
  await page.respond(1, {})
  page.refresh()
  await page.respond(2, null, 500)
  assert.match(page.text(), /Active/)
  assert.match(page.text(), /Tariff analysis is unavailable/)
  assert.doesNotMatch(page.text(), /Test tariff|Analysing tariff/)
})
