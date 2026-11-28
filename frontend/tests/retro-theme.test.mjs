import assert from 'node:assert/strict'
import fs from 'node:fs'
import { test } from 'node:test'

test('Retro uses the reference palette and bundles both fonts locally', () => {
  const css = fs.readFileSync(new URL('../src/styles/theme.css', import.meta.url), 'utf8')
  const retro = css.slice(css.indexOf(":root[data-preset='retro']"))
  for (const colour of ['#0f0f0f', '#1a2212', '#2b4e33', '#448053', '#41ec9d']) assert.ok(retro.includes(colour), colour)
  assert.match(retro, /font-family: 'Courier Prime'/)
  assert.match(retro, /font-family: 'Press Start 2P'/)
  for (const name of ['courier_prime', 'courier_prime_bold', 'press_start']) {
    assert.match(css, new RegExp(`predbat_ui_${name}\\.woff2`))
    assert.ok(fs.statSync(new URL(`../../apps/predbat/predbat_ui_${name}.woff2`, import.meta.url)).size > 0)
  }
})

test('the monitor texture is limited to the Retro dark page background', () => {
  const css = fs.readFileSync(new URL('../src/styles/theme.css', import.meta.url), 'utf8')
  const rule = css.match(/:root\[data-preset='retro'\]\[data-theme='dark'\] body\s*\{([^}]*)\}/)
  assert.ok(rule)
  assert.match(rule[1], /repeating-conic-gradient/)
  assert.match(rule[1], /background-size/)
  const overview = fs.readFileSync(new URL('../src/pages/OverviewPage.css', import.meta.url), 'utf8')
  assert.match(overview, /:root\[data-preset='retro'\]\[data-theme='dark'\] \.overview-page\s*\{\s*background:\s*transparent;/)
  assert.match(overview, /:root\[data-preset='retro'\]\[data-theme='light'\] \.overview-page\s*\{\s*background:\s*transparent;/)
})
