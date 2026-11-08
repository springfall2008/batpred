import assert from 'node:assert/strict'
import fs from 'node:fs'
import { test } from 'node:test'

const theme = fs.readFileSync(new URL('../src/styles/theme.css', import.meta.url), 'utf8')
const [light, dark] = theme.split(":root[data-theme='dark']")

function rgb(css, token) {
  if (token === 'bg') {
    const mode = css === dark ? 'dark' : 'light'
    return theme.match(new RegExp(`--default-${mode}-bg: #([a-f0-9]{6});`))[1].match(/../g).map(value => parseInt(value, 16) / 255)
  }
  const pattern = new RegExp(`--color-${token}: #([a-f0-9]{6});`)
  const hex = (css.match(pattern) ?? light.match(pattern))[1]
  return hex.match(/../g).map(value => parseInt(value, 16) / 255)
}

function luminance(colour) {
  return colour.map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
    .reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0)
}

for (const [name, css] of [['light', light], ['dark', dark]]) {
  test(`${name} focus rings and field boundaries contrast against both surfaces`, () => {
    const tokens = css.replace('--focus-ring:', '--color-focus-ring:')
    for (const token of ['focus-ring', 'field-border']) {
      for (const surface of ['bg', 'surface']) {
        const levels = [luminance(rgb(tokens, token)), luminance(rgb(css, surface))].sort((a, b) => b - a)
        assert.ok((levels[0] + 0.05) / (levels[1] + 0.05) >= 3, `${name} ${token} on ${surface}`)
      }
    }
  })

  test(`${name} muted and energy text remain readable on plain and highlighted rows`, () => {
    for (const token of ['text-muted', 'text-secondary', 'solar-text', 'charge-text', 'export-text']) {
      for (const surface of ['bg', 'surface']) {
        for (const tint of [0, 0.1, 0.24]) {
          const background = rgb(css, surface).map((value, index) => value * (1 - tint) + rgb(css, 'current')[index] * tint)
          const levels = [luminance(rgb(css, token)), luminance(background)].sort((a, b) => b - a)
          const contrast = (levels[0] + 0.05) / (levels[1] + 0.05)
          assert.ok(contrast >= 4.5, `${name} ${token} on ${surface} with ${tint} tint: ${contrast.toFixed(2)}:1`)
        }
      }
    }
  })

  test(`${name} warning and success text meet normal-text contrast on plain and tinted surfaces`, () => {
    for (const semantic of ['success', 'warning']) {
      for (const surface of ['bg', 'surface']) {
        // Includes the comparison badge tint layered over the best-result row.
        for (const tint of [0, 0.1, 0.12, 0.16, 0.2, 1 - (1 - 0.07) * (1 - 0.16)]) {
          const background = rgb(css, surface).map((value, index) => value * (1 - tint) + rgb(css, semantic)[index] * tint)
          const levels = [luminance(rgb(css, `${semantic}-text`)), luminance(background)].sort((a, b) => b - a)
          const contrast = (levels[0] + 0.05) / (levels[1] + 0.05)
          assert.ok(contrast >= 4.5, `${name} ${semantic} on ${surface} with ${tint} tint: ${contrast.toFixed(2)}:1`)
        }
      }
    }
  })
}
