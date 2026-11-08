import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { createRequire } from 'node:module'
import { test } from 'node:test'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = fs.readFileSync(new URL('../src/pages/ChartsPage.tsx', import.meta.url), 'utf8')

/** Exercise the real effects with controlled requests and timers, without browser dependencies. */
function chartPage() {
  const slots = [], timers = new Map(), requests = [], effects = [], loadingChanges = []
  const onLoadingChange = (loading) => loadingChanges.push(loading)
  let cursor = 0, timerId = 0, tree
  const same = (a, b) => a && b && a.length === b.length && a.every((value, index) => Object.is(value, b[index]))
  const hooks = {
    useState(initial) {
      const index = cursor++
      if (!(index in slots)) slots[index] = initial
      return [slots[index], (next) => { slots[index] = typeof next === 'function' ? next(slots[index]) : next }]
    },
    useCallback(callback, deps) {
      const index = cursor++
      if (!same(slots[index]?.deps, deps)) slots[index] = { deps, callback }
      return slots[index].callback
    },
    useEffect(effect, deps) {
      const index = cursor++
      if (!same(slots[index]?.deps, deps)) effects.push(() => {
        slots[index]?.cleanup?.()
        slots[index] = { deps, cleanup: effect() }
      })
    }
  }
  const module = { exports: {} }
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 }
  }).outputText, {
    module, exports: module.exports, AbortController, DOMException,
    window: {
      setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id },
      clearTimeout(id) { timers.delete(id) }
    },
    fetch(url, options) {
      return new Promise((resolve, reject) => requests.push({ url, signal: options.signal, resolve, reject }))
    },
    require(specifier) {
      if (specifier === 'react') return hooks
      if (specifier.endsWith('/useStoredState')) return { useStoredState: (_key, fallback) => hooks.useState(fallback) }
      if (specifier.endsWith('.css')) return {}
      if (specifier.includes('/components/')) return { default: () => null }
      return require(specifier)
    }
  })
  const nodes = (node) => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(Infinity).flatMap(nodes)]
  const render = () => {
    cursor = 0
    tree = module.exports.default({ plan: {}, loadMlEnabled: false, predheatEnabled: false, onLoadingChange })
    effects.splice(0).forEach((effect) => effect())
    return nodes(tree)
  }
  render()
  return {
    requests, timers, render, loadingChanges,
    unmount() { slots.forEach((slot) => slot?.cleanup?.()) },
    select(label) { nodes(tree).find((node) => node.type === 'button' && node.props.children === label).props.onClick(); return render() },
    tick() {
      const [id, timer] = timers.entries().next().value
      timers.delete(id)
      timer.callback()
    },
    async respond(index, data) {
      requests[index].resolve({ ok: true, json: async () => data })
      await new Promise((resolve) => setImmediate(resolve))
      return render()
    }
  }
}

test('unready charts retry in five seconds and ready charts refresh after five minutes', async () => {
  const page = chartPage()
  page.tick()
  assert.equal(page.timers.size, 0, 'slow requests do not overlap')
  await page.respond(0, { chart: 'battery', ready: false })
  assert.equal([...page.timers.values()][0].delay, 5000)
  page.tick()
  await page.respond(1, { chart: 'battery', ready: true })
  assert.equal([...page.timers.values()][0].delay, 300_000)
})

test('returning to a chart renders its cached data immediately while refreshing', async () => {
  const page = chartPage()
  const battery = { chart: 'battery', ready: true }
  page.tick()
  await page.respond(0, battery)
  page.select('Rates')
  page.tick()
  await page.respond(1, { chart: 'rates', ready: true })
  assert.ok(page.select('Battery').some((node) => node.props?.data === battery))
  assert.equal([...page.timers.values()][0].delay, 0)
})

test('switching charts aborts the previous request and ignores its late response', async () => {
  const page = chartPage()
  page.tick()
  page.select('Rates')
  assert.equal(page.requests[0].signal.aborted, true)
  page.tick()
  await page.respond(1, { chart: 'rates', ready: true })
  await page.respond(0, { chart: 'battery', ready: true })
  assert.equal(page.timers.size, 1, 'aborted request cannot schedule another poll')
  assert.ok(!page.select('Battery').some((node) => node.props?.data?.chart === 'battery'))
})

test('request errors retry after thirty seconds without a tight loop', async () => {
  const page = chartPage()
  page.tick()
  page.requests[0].reject(new Error('offline'))
  await new Promise((resolve) => setImmediate(resolve))
  page.render()
  assert.equal([...page.timers.values()][0].delay, 30_000)
  assert.equal(page.loadingChanges.at(-1), false)
})

test('loading indicator tracks initial and cached requests and clears on page exit', async () => {
  const page = chartPage()
  assert.deepEqual(page.loadingChanges, [])
  page.tick()
  assert.equal(page.loadingChanges.at(-1), true)
  await page.respond(0, { chart: 'battery', ready: true })
  assert.equal(page.loadingChanges.at(-1), false)
  page.select('Rates')
  page.tick()
  await page.respond(1, { chart: 'rates', ready: true })
  page.select('Battery')
  page.tick()
  assert.equal(page.loadingChanges.at(-1), true)
  page.unmount()
  assert.equal(page.loadingChanges.at(-1), false)
  await page.respond(2, { chart: 'battery', ready: true })
  assert.equal(page.loadingChanges.at(-1), false)
})
