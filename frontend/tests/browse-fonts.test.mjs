import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { test } from 'node:test'
import { loadConfigFromFile } from 'vite'

test('the file manager keeps its styles without bundling its unused fonts', async () => {
  const configFile = fileURLToPath(new URL('../vite.config.ts', import.meta.url))
  const { config } = await loadConfigFromFile({ command: 'build', mode: 'production' }, configFile)
  const plugin = config.plugins.find(item => item.name === 'remove-unused-file-manager-fonts')
  const path = fileURLToPath(new URL('../node_modules/@cubone/react-file-manager/dist/style.css', import.meta.url))
  const css = await readFile(path, 'utf8')
  const result = plugin.transform(css, path)

  assert.match(css, /@font-face\{font-family:Nunito Sans;/)
  assert.doesNotMatch(result, /@font-face\{font-family:Nunito Sans;/)
  assert.match(result, /\.file-explorer\{/)
  assert.equal(plugin.transform(css, '/another-package/style.css'), undefined)
})
