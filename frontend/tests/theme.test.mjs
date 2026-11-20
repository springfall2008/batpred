import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/components/theme.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }
}).outputText
const module = { exports: {} }

vm.runInNewContext(compiled, { module, exports: module.exports })

const { getNextThemePreference, readThemePreference, resolveTheme } = module.exports

test('theme preference defaults to auto and preserves valid stored choices', () => {
  assert.equal(readThemePreference(null), 'auto')
  assert.equal(readThemePreference('invalid'), 'auto')
  assert.equal(readThemePreference('light'), 'light')
  assert.equal(readThemePreference('dark'), 'dark')
  assert.equal(readThemePreference('auto'), 'auto')
})

test('auto follows the browser theme while explicit choices override it', () => {
  assert.equal(resolveTheme('auto', false), 'light')
  assert.equal(resolveTheme('auto', true), 'dark')
  assert.equal(resolveTheme('light', true), 'light')
  assert.equal(resolveTheme('dark', false), 'dark')
})

test('theme choices cycle through light, dark and auto', () => {
  assert.equal(getNextThemePreference('light'), 'dark')
  assert.equal(getNextThemePreference('dark'), 'auto')
  assert.equal(getNextThemePreference('auto'), 'light')
})
