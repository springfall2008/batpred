import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { readFile, readdir, stat } from 'node:fs/promises'
import { test } from 'node:test'

const directory = new URL('../../apps/predbat/', import.meta.url)

test('production frontend files are flat, prefixed, non-empty and reference installed assets', async () => {
  const names = (await readdir(directory)).filter(name => name.startsWith('predbat_ui'))
  const ignored = spawnSync('git', ['check-ignore', '--no-index', '--', ...names.map(name => fileURLToPath(new URL(name, directory)))], { encoding: 'utf8' })
  assert.equal(ignored.status, 1, `Generated assets must be eligible for Git: ${ignored.stdout}${ignored.stderr}`)
  for (const name of await readdir(new URL('../public/', import.meta.url))) assert.ok(names.includes(name), `Missing public asset: ${name}`)
  assert.ok(names.includes('predbat_ui.html'))
  assert.ok(names.includes('predbat_ui.js'))
  assert.ok(names.includes('predbat_ui_style.css'))
  for (const name of names) {
    assert.match(name, /^predbat_ui(?:_[A-Za-z0-9_.-]+)?\.(?:html|js|css|webp|svg|png|woff2)$/)
    const file = new URL(name, directory)
    const info = await stat(file)
    assert.ok(info.isFile() && info.size > 0, name)
    if (/\.(?:html|js|css)$/.test(name)) {
      const content = await readFile(file, 'utf8')
      for (const [reference] of content.matchAll(/predbat_ui(?:_[A-Za-z0-9_.-]+)?\.(?:js|css|webp|svg|png|woff2)/g)) {
        assert.ok(names.includes(reference), `${name} references missing asset ${reference}`)
      }
    }
  }
})
