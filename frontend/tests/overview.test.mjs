import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
import { test } from 'node:test'

import ts from 'typescript'

const source = fs.readFileSync(new URL('../src/utils/overview.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2023 }
}).outputText
const module = { exports: {} }
vm.runInNewContext(compiled, { module, exports: module.exports })

const { formatCarStatus, formatOverviewAction, formatWeatherStatus, getOverviewActions, getOverviewCarbonValues, getOverviewPowerTone, getOverviewSceneKey, getOverviewWeatherEffect, mapOverviewImagePoint } = module.exports

test('scene selection covers day, night, car and heat pump combinations', () => {
  assert.equal(getOverviewSceneKey('above_horizon', false, false), 'day-house')
  assert.equal(getOverviewSceneKey('below_horizon', true, false), 'night-car')
  assert.equal(getOverviewSceneKey('above_horizon', false, true), 'day-heat-pump')
  assert.equal(getOverviewSceneKey('below_horizon', true, true), 'night-car-heat-pump')
  assert.equal(getOverviewSceneKey('above_horizon', false, false, true), 'day-house-snow')
  assert.equal(getOverviewSceneKey('below_horizon', true, false, true), 'night-car-snow')
  assert.equal(getOverviewSceneKey('above_horizon', false, true, true), 'day-heat-pump-snow')
  assert.equal(getOverviewSceneKey('below_horizon', true, true, true), 'night-car-heat-pump-snow')
})

test('house attachment points stay aligned when the scene is resized and centred', () => {
  const imagePoint = { x: 750, y: 600 }
  const imageSize = { x: 1500, y: 1200 }
  const stageViewBox = { x: 1400, y: 860 }

  const wide = mapOverviewImagePoint(
    imagePoint,
    imageSize,
    { x: 350, y: 100, width: 1000, height: 800 },
    { x: 100, y: 40, width: 1500, height: 900 },
    stageViewBox
  )
  const narrow = mapOverviewImagePoint(
    imagePoint,
    imageSize,
    { x: 250, y: 80, width: 600, height: 480 },
    { x: 100, y: 20, width: 900, height: 600 },
    stageViewBox
  )

  assert.deepEqual({ x: Math.round(wide.x), y: Math.round(wide.y) }, { x: 700, y: 440 })
  assert.deepEqual({ x: Math.round(narrow.x), y: Math.round(narrow.y) }, { x: 700, y: 430 })
})

test('plan actions include the next window and its final target', () => {
  const rows = [
    { time: '2026-10-03T09:00:00Z', state: 'Demand', state_target: '' },
    { time: '2026-10-03T09:30:00Z', state: 'Demand', state_target: '' },
    { time: '2026-10-03T10:00:00Z', state: 'Chrg', state_target: '12' },
    { time: '2026-10-03T10:30:00Z', state: 'Chrg', state_target: '17' },
    { time: '2026-10-03T11:00:00Z', state: 'HoldChrg', state_target: '17' }
  ]
  const actions = getOverviewActions(rows, new Date('2026-10-03T09:40:00Z'))
  assert.equal(actions.current.label, 'Demand')
  assert.equal(actions.next.label, 'Charge')
  assert.equal(actions.next.startsAt, '2026-10-03T10:00:00Z')
  assert.equal(actions.next.endsAt, '2026-10-03T11:00:00Z')
  assert.equal(actions.next.limit, 17)
})

test('current charging action includes its final charge target', () => {
  const rows = [
    { time: '2026-10-03T09:00:00Z', state: 'Chrg', state_target: '12' },
    { time: '2026-10-03T09:30:00Z', state: 'Chrg', state_target: '17' },
    { time: '2026-10-03T10:00:00Z', state: 'Demand', state_target: '' }
  ]
  const actions = getOverviewActions(rows, new Date('2026-10-03T09:10:00Z'))
  assert.equal(actions.current.label, 'Charge')
  assert.equal(actions.current.limit, 17)
})

test('zero-watt overview headlines use the muted tone', () => {
  assert.equal(getOverviewPowerTone(0, 'is-solar'), 'is-muted')
  assert.equal(getOverviewPowerTone(0.49, 'is-charge'), 'is-muted')
  assert.equal(getOverviewPowerTone(1, 'is-home'), 'is-home')
})

test('grid carbon values are formatted only when carbon reporting is enabled', () => {
  const row = { carbon_intensity: 184.6, total_carbon: 8.123 }
  assert.deepEqual(
    { ...getOverviewCarbonValues(true, row) },
    { intensity: '185 gCO₂/kWh', total: '8.12 kg CO₂' }
  )
  assert.deepEqual(
    { ...getOverviewCarbonValues(false, row) },
    { intensity: null, total: null }
  )
})

test('Home Assistant weather states select a restrained scene effect', () => {
  assert.equal(getOverviewWeatherEffect('lightning-rainy'), 'storm')
  assert.equal(getOverviewWeatherEffect('snowy-rainy'), 'snow')
  assert.equal(getOverviewWeatherEffect('pouring'), 'rain')
  assert.equal(getOverviewWeatherEffect('partlycloudy'), 'clouds')
  assert.equal(getOverviewWeatherEffect('fog'), 'fog')
  assert.equal(getOverviewWeatherEffect('sunny'), null)
})

test('Home Assistant weather condition codes are expanded into readable labels', () => {
  assert.equal(formatWeatherStatus('partlycloudy'), 'Partly cloudy')
  assert.equal(formatWeatherStatus('clear-night'), 'Clear night')
  assert.equal(formatWeatherStatus('lightning-rainy'), 'Lightning and rain')
  assert.equal(formatWeatherStatus('windy-variant'), 'Windy and cloudy')
  assert.equal(formatWeatherStatus('custom_weather_state'), 'Custom Weather State')
})

test('charger and Predbat states are expanded into readable labels', () => {
  assert.equal(formatCarStatus('EV connected', false), 'Connected')
  assert.equal(formatCarStatus('disconnected', false), 'Unplugged')
  assert.equal(formatCarStatus('waiting_for_charge', true), 'Charging')
  assert.equal(formatOverviewAction('Hold for car'), 'Hold for Car')
})
