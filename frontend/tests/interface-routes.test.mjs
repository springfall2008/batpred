import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'

const main = fs.readFileSync(new URL('../src/main.tsx', import.meta.url), 'utf8')
const remember = main.slice(main.indexOf('const uiPath'), main.indexOf('createRoot('))
const backend = fs.readFileSync(new URL('../../apps/predbat/web.py', import.meta.url), 'utf8')
const landing = backend.slice(backend.indexOf('const key ='), backend.indexOf('</script>', backend.indexOf('const key ='))).replace('DESTINATION', '"./plan"')

for (const base of ['/', '/api/hassio_ingress/example/', '/another-instance/']) {
  test(`remembers and clears the interface choice under ${base}`, () => {
    const values = new Map()
    const localStorage = {
      setItem: (key, value) => values.set(key, value),
      getItem: (key) => values.get(key),
      removeItem: (key) => values.delete(key),
    }
    vm.runInNewContext(remember, { window: { location: { pathname: `${base}ui/plan` } }, localStorage })
    assert.equal(values.get(`predbat-interface:${base}`), 'modern')
    assert.equal(values.size, 1)
    let destination
    const location = { pathname: base, search: '', replace: (value) => { destination = value } }
    vm.runInNewContext(landing, { location, localStorage, URLSearchParams })
    assert.equal(new URL(destination, `https://example.test${base}`).pathname, `${base}ui/`)
    location.search = '?ui=classic'
    vm.runInNewContext(landing, { location, localStorage, URLSearchParams })
    assert.equal(destination, './plan')
    assert.equal(values.size, 0)
    assert.equal(new URL('../api/plan_data', `https://example.test${base}ui/plan`).pathname, `${base}api/plan_data`)
    assert.equal(new URL('./charts', `https://example.test${base}ui/plan`).pathname, `${base}ui/charts`)
  })
}

test('classic landing still works when browser storage is blocked', () => {
  let destination
  const localStorage = { getItem() { throw new Error('Storage blocked') } }
  vm.runInNewContext(landing, { localStorage, URLSearchParams, location: { pathname: '/', search: '', replace: (value) => { destination = value } } })
  assert.equal(destination, './plan')
})
