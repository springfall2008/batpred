import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'
import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/components/PlanTable.tsx', import.meta.url), 'utf8')
const tree = ts.createSourceFile('PlanTable.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
let functionSource
function visit(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'constrainFloatingElement') functionSource = node.getText(tree)
  ts.forEachChild(node, visit)
}
visit(tree)
assert.ok(functionSource)

function constrain({ left, width, container, viewport = { offsetLeft: 0, width: 1000 }, priorShift = 0 }) {
  let shift = priorShift
  let maxWidth
  const element = {
    isConnected: true,
    style: {
      removeProperty() { shift = 0 },
      setProperty(key, value) { shift = parseFloat(value) },
      set maxWidth(value) { maxWidth = parseFloat(value) }
    },
    closest: () => container ? { getBoundingClientRect: () => container } : null,
    getBoundingClientRect: () => ({ left: left + shift, width: Math.min(width, maxWidth ?? width) })
  }
  const context = { requestAnimationFrame: (callback) => callback(), window: { visualViewport: viewport, innerWidth: 1000 } }
  vm.createContext(context)
  vm.runInContext(ts.transpileModule(functionSource, { compilerOptions: { target: ts.ScriptTarget.ES2023 } }).outputText, context)
  context.constrainFloatingElement(element, '--plan-action-tooltip-shift')
  return { left: left + shift, right: left + shift + Math.min(width, maxWidth), maxWidth }
}

test('tooltip stays within left and right viewport edges', () => {
  assert.equal(constrain({ left: -120, width: 400 }).left, 8)
  assert.equal(constrain({ left: 800, width: 400 }).right, 992)
  assert.equal(constrain({ left: 300, width: 400 }).left, 300)
})
test('tooltip respects table clipping edges and wraps oversized content', () => {
  const container = { left: 38, right: 850 }
  assert.equal(constrain({ left: -120, width: 700, container }).left, 46)
  assert.equal(constrain({ left: 700, width: 400, container }).right, 842)
  const large = constrain({ left: -120, width: 1200, container })
  assert.equal(large.maxWidth, 796)
  assert.equal(large.left, 46)
  assert.equal(large.right, 842)
})
test('repeated measurement resets old shifts and respects a zoomed visual viewport', () => {
  assert.equal(constrain({ left: -120, width: 400, priorShift: 128 }).left, 8)
  const result = constrain({ left: -120, width: 700, viewport: { offsetLeft: 100, width: 400 } })
  assert.equal(result.left, 108)
  assert.equal(result.right, 492)
})
