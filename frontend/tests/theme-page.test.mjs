import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/pages/ThemePage.tsx', import.meta.url), 'utf8')
const themeSource = fs.readFileSync(new URL('../src/hooks/useThemeSettings.ts', import.meta.url), 'utf8')
const compile = input => ts.transpileModule(input, { compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 } }).outputText
const theme = { exports: {} }
vm.runInNewContext(compile(themeSource), { module: theme, exports: theme.exports, require })
const page = { exports: {} }
vm.runInNewContext(compile(source), {
  module: page, exports: page.exports, document: { documentElement: {} },
  getComputedStyle: () => ({ getPropertyValue: name => name.includes('bg') ? '#ffffff' : '#ffffff' }),
  require(specifier) {
    if (specifier.endsWith('.css')) return {}
    if (specifier.endsWith('/useThemeSettings')) return theme.exports
    if (specifier.endsWith('/GridIcon')) return { __esModule: true, default: () => React.createElement('svg', { 'data-icon': 'grid' }) }
    return require(specifier)
  }
})

test('theme editor exposes separate palettes, thresholds and contrast warnings', () => {
  const draft = { ...theme.exports.DEFAULT_THEME, preset: 'custom', colours: { light: { 'battery-low': '#123456', charge: '#112233' }, dark: { 'flow-home': '#445566' } } }
  const props = { saved: draft, draft, setDraft() {}, dirty: true, error: '', conflict: false, save: async () => {}, reload: async () => {}, cancel() {} }
  const html = renderToStaticMarkup(React.createElement(page.exports.default, props))
  assert.match(html, /Light/)
  assert.match(html, /Dark/)
  assert.match(html, /Battery SOC levels/)
  assert.match(html, /<table class="theme-colour-table"/)
  assert.match(html, /data-icon="sun"/)
  assert.match(html, /data-icon="moon"/)
  assert.match(html, /Low contrast/)
  assert.match(html, /aria-label="Battery low light"/)
  assert.match(html, /aria-label="Battery low dark"/)
  assert.match(html, /Reset light colours/)
  assert.match(html, /Reset dark colours/)
  assert.match(html, /Reset battery levels/)
  assert.match(html, /Battery levels/)
  assert.match(html, /Energy flows/)
  assert.match(html, /scope="rowgroup" rowSpan="5"><span>Energy flows/)
  assert.match(html, /scope="rowgroup" rowSpan="3"><span>Battery levels/)
  const names = ['Home', 'Solar', 'Import', 'Export', 'Charging']
  const positions = names.map(name => html.indexOf(`title="${name}"`))
  assert.ok(positions.every((position, index) => position >= 0 && (index === 0 || position > positions[index - 1])))
  assert.match(html, /data-icon="bolt"/)
  assert.match(html, /data-icon="arrow-trend-up"/)
  assert.match(html, /aria-label="Import light"/)
  assert.match(html, /aria-label="Export dark"/)
  assert.ok(html.indexOf('Energy flows') < html.indexOf('Battery levels'))
  assert.equal((html.match(/data-palette="light"/g) || []).length, 8)
  assert.equal((html.match(/data-palette="dark"/g) || []).length, 8)
  assert.match(html, /data-icon="battery-quarter"/)
  assert.match(html, /data-icon="battery-half"/)
  assert.match(html, /data-icon="battery-three-quarters"/)
  assert.match(html, /data-icon="solar-panel"/)
  assert.equal((html.match(/data-icon="grid"/g) || []).length, 2)
  assert.match(html, /class="theme-preset theme-preset-custom" style="--preview-light-charge:#112233;--preview-dark-flow-home:#445566"/)
  assert.match(html, /--soc-preview-low:#123456/)
  assert.match(html, /Showing the light palette/)
  assert.doesNotMatch(html, /browser.s mode stays separate/i)
  assert.match(html, /aria-label="Low SOC boundary"/)
  assert.match(html, /aria-label="Medium SOC boundary"/)
  assert.match(html, /var\(--color-battery-low\)/)
  assert.match(html, /Retro/)
  assert.doesNotMatch(html, /Coastal/)
  const builtin = renderToStaticMarkup(React.createElement(page.exports.default, { ...props, draft: { ...draft, preset: 'retro' } }))
  assert.doesNotMatch(builtin, /Custom colours/)
  assert.match(builtin, /aria-pressed="true"[^>]*><span class="theme-preset-preview"/)
})

test('editing either colour column updates only that palette and selects its slider preview', () => {
  const component = { exports: {} }
  let mode
  vm.runInNewContext(compile(source), {
    module: component, exports: component.exports, document: { documentElement: {} },
    getComputedStyle: () => ({ getPropertyValue: () => '#ffffff' }),
    require(specifier) {
      if (specifier === 'react') return { useState: value => [value, value === 'light' ? next => { mode = next } : () => {}], useEffect: () => {} }
      if (specifier.endsWith('.css')) return {}
      if (specifier.endsWith('/useThemeSettings')) return theme.exports
      if (specifier.endsWith('/GridIcon')) return { __esModule: true, default: () => React.createElement('svg') }
      return require(specifier)
    }
  })
  let next
  const draft = { ...theme.exports.DEFAULT_THEME, preset: 'custom', colours: { light: { 'battery-low': '#112233' }, dark: { 'battery-low': '#445566' } } }
  const tree = component.exports.default({ saved: draft, draft, setDraft: value => { next = value }, dirty: false, error: '', conflict: false, save() {}, reload() {}, cancel() {} })
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)]
  for (const palette of ['light', 'dark']) {
    const input = nodes(tree).find(node => node.props?.['aria-label'] === `Battery low ${palette}`)
    input.props.onChange({ target: { value: '#abcdef' } })
    assert.equal(next.colours[palette]['battery-low'], '#abcdef')
    assert.equal(next.colours[palette === 'light' ? 'dark' : 'light']['battery-low'], draft.colours[palette === 'light' ? 'dark' : 'light']['battery-low'])
    assert.equal(mode, palette)
  }
})

test('the two SOC handles cannot cross and keep whole-percent ranges', () => {
  const component = { exports: {} }
  vm.runInNewContext(compile(source), {
    module: component, exports: component.exports, document: { documentElement: {} },
    getComputedStyle: () => ({ getPropertyValue: () => '#ffffff' }),
    require(specifier) {
      if (specifier === 'react') return { useState: value => [value, () => {}], useEffect: () => {} }
      if (specifier.endsWith('.css')) return {}
      if (specifier.endsWith('/useThemeSettings')) return theme.exports
      if (specifier.endsWith('/GridIcon')) return { __esModule: true, default: () => React.createElement('svg') }
      return require(specifier)
    }
  })
  let next
  const draft = { ...theme.exports.DEFAULT_THEME, socBoundaries: { low: 20, medium: 50 } }
  const tree = component.exports.default({ saved: draft, draft, setDraft: value => { next = value }, dirty: false, error: '', conflict: false, save() {}, reload() {}, cancel() {} })
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)]
  const low = nodes(tree).find(node => node.props?.['aria-label'] === 'Low SOC boundary')
  const medium = nodes(tree).find(node => node.props?.['aria-label'] === 'Medium SOC boundary')
  low.props.onChange({ target: { value: '80' } })
  assert.equal(next.socBoundaries.low, 49)
  medium.props.onChange({ target: { value: '10' } })
  assert.equal(next.socBoundaries.medium, 21)
})

test('the SOC slider previews the palette being edited, independent of the page theme', () => {
  const component = { exports: {} }
  vm.runInNewContext(compile(source), {
    module: component, exports: component.exports, document: { documentElement: {} },
    getComputedStyle: () => ({ getPropertyValue: () => '#ffffff' }),
    require(specifier) {
      if (specifier === 'react') return { useState: value => [value === 'light' ? 'dark' : value, () => {}], useEffect: () => {} }
      if (specifier.endsWith('.css')) return {}
      if (specifier.endsWith('/useThemeSettings')) return theme.exports
      if (specifier.endsWith('/GridIcon')) return { __esModule: true, default: () => React.createElement('svg') }
      return require(specifier)
    }
  })
  const draft = { ...theme.exports.DEFAULT_THEME, preset: 'custom', colours: { light: { 'battery-low': '#112233' }, dark: { 'battery-low': '#445566' } } }
  const props = { saved: draft, draft, setDraft() {}, dirty: false, error: '', conflict: false, save() {}, reload() {}, cancel() {} }
  const html = renderToStaticMarkup(React.createElement(component.exports.default, props))
  assert.match(html, /--soc-preview-low:#445566/)
  assert.match(html, /Showing the dark palette/)
})

test('built-in preview bars match their light and dark energy colours', () => {
  const tokens = fs.readFileSync(new URL('../src/styles/theme.css', import.meta.url), 'utf8')
  const previews = fs.readFileSync(new URL('../src/pages/ThemePage.css', import.meta.url), 'utf8')
  const retro = tokens.slice(tokens.indexOf(":root[data-preset='retro']"))
  const predbat = tokens.slice(0, tokens.indexOf(":root[data-preset='retro']"))
  for (const [preset, css] of [['predbat', predbat], ['retro', retro]]) {
    for (const mode of ['light', 'dark']) {
      const selector = mode === 'dark' ? `:root[data-theme='dark'] .theme-preset-${preset}` : `.theme-preset-${preset}`
      const declaration = previews.slice(previews.indexOf(`${selector} {`)).split('}')[0]
      for (const [key, bar] of [['charge', 'charge'], ['export', 'export'], ['flow-grid', 'grid'], ['flow-home', 'home']]) {
        const actual = css.match(new RegExp(`--default-${mode}-${key}: (#[0-9a-f]{6})`))[1]
        const preview = declaration.match(new RegExp(`--preview-${bar}: (#[0-9a-f]{6})`))[1]
        assert.equal(preview, actual, `${preset} ${mode} ${key}`)
      }
    }
  }
})
