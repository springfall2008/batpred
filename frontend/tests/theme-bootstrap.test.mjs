import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'

const html = fs.readFileSync(new URL('../index.html', import.meta.url), 'utf8')
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
const settings = { version: 1, revision: 3, preset: 'custom', colours: { light: { charge: '#123456' }, dark: { 'battery-low': '#abcdef' } }, socBoundaries: { low: 25, medium: 60 } }

function bootstrap(cached, preference = 'dark', pathname = '/ingress/a/ui/plan') {
  const props = {}
  const root = { dataset: {}, style: { setProperty: (key, value) => { props[key] = value } } }
  const keys = []
  const window = { location: { pathname }, matchMedia: () => ({ matches: false }) }
  const localStorage = { getItem(key) { keys.push(key); return key === 'predbat-theme' ? preference : JSON.stringify(cached) } }
  vm.runInNewContext(script, { document: { documentElement: root }, window, localStorage })
  return { root, props, window, keys }
}

test('applies the scoped saved palette and mode before the app starts', () => {
  const result = bootstrap(settings)
  assert.equal(result.root.dataset.theme, 'dark')
  assert.equal(result.root.dataset.preset, 'custom')
  assert.equal(result.props['--user-dark-battery-low'], '#abcdef')
  assert.equal(result.window.__predbatTheme.revision, 3)
  assert.equal(result.keys[1], 'predbat-theme-settings:/ingress/a/')
})

test('rejects malformed cached CSS while retaining the browser theme mode', () => {
  const invalid = { ...settings, colours: { light: { charge: 'url(https://example.com)' }, dark: {} } }
  const result = bootstrap(invalid, 'light')
  assert.equal(result.root.dataset.theme, 'light')
  assert.equal(result.root.dataset.preset, undefined)
  assert.equal(Object.keys(result.props).length, 0)
})

test('a previously saved Coastal selection falls back to Predbat', () => {
  const result = bootstrap({ ...settings, preset: 'coastal' })
  assert.equal(result.root.dataset.preset, 'predbat')
  assert.equal(result.window.__predbatTheme.socBoundaries.low, 25)
})
