import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/hooks/useThemeSettings.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 } }).outputText

test('remote changes preserve a local draft until reload or explicit overwrite', async () => {
  const slots = []
  const effects = []
  let cursor = 0
  let timer
  const cache = {}
  let remote = { version: 1, revision: 0, colours: { light: {}, dark: {} }, socBoundaries: { low: 20, medium: 50 } }
  const react = {
    useState(initial) {
      const index = cursor++
      if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial
      return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value }]
    },
    useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial } },
    useEffect(effect, dependencies) {
      const index = cursor++
      if (!slots[index] || dependencies.some((value, i) => value !== slots[index][i])) { slots[index] = dependencies; effects.push(effect) }
    }
  }
  const module = { exports: {} }
  vm.runInNewContext(compiled, {
    module, exports: module.exports,
    require: name => name === 'react' ? react : null,
    window: { location: { pathname: '/ingress/site/ui/theme' }, setInterval: fn => { timer = fn; return 1 }, clearInterval() {} },
    localStorage: { setItem: (key, value) => { cache[key] = value } },
    document: { documentElement: { dataset: { theme: 'light' }, style: { setProperty() {}, removeProperty() {} }, setAttribute() {} } },
    fetch: async (_url, options) => {
      if (options.method === 'POST') {
        const data = JSON.parse(options.body)
        if (data.revision !== remote.revision) return { status: 409, ok: false }
        remote = { ...data, revision: data.revision + 1 }
      }
      return { ok: true, status: 200, json: async () => structuredClone(remote) }
    }
  })
  const render = () => { cursor = 0; const result = module.exports.useThemeSettings(); while (effects.length) effects.shift()(); return result }
  let state = render()
  await new Promise(resolve => setImmediate(resolve))
  state = render()
  assert.equal(state.draft.preset, 'predbat')
  const cacheKey = 'predbat-theme-settings:/ingress/site/'
  assert.equal(JSON.parse(cache[cacheKey]).socBoundaries.low, 20)
  state.setDraft({ ...state.draft, socBoundaries: { low: 30, medium: 70 } })
  state = render()
  assert.equal(JSON.parse(cache[cacheKey]).socBoundaries.low, 20)
  remote = { ...remote, revision: 1, colours: { light: { charge: '#123456' }, dark: {} } }
  await timer()
  state = render()
  assert.equal(state.conflict, true)
  assert.equal(state.draft.socBoundaries.low, 30)
  await state.save(true)
  state = render()
  assert.equal(state.conflict, false)
  assert.equal(remote.revision, 2)
  assert.equal(remote.socBoundaries.low, 30)
  assert.equal(JSON.parse(cache[cacheKey]).socBoundaries.low, 30)
  assert.equal(remote.colours.light.charge, undefined)
})
