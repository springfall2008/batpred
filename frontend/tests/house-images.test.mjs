import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import { test } from 'node:test'

test('all house variants are supplied directly as WebP assets', async () => {
  const page = await readFile(new URL('../src/pages/OverviewPage.tsx', import.meta.url), 'utf8')
  const imports = [...page.matchAll(/from '\.\.\/assets\/house\/([^']+)'/g)].map(match => match[1])
  assert.equal(imports.length, 16)
  assert.equal(new Set(imports).size, 16)
  const directory = new URL('../src/assets/house/', import.meta.url)
  for (const name of imports) {
    assert.ok(name.endsWith('.webp'))
    const image = await readFile(new URL(name, directory))
    assert.equal(image.toString('ascii', 0, 4), 'RIFF')
    assert.equal(image.toString('ascii', 8, 12), 'WEBP')
  }
  assert.equal((await readdir(directory)).filter(name => name.endsWith('.png')).length, 0)
})
