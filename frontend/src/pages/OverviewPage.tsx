import { isValidElement, useLayoutEffect, useRef, useState, type ReactNode } from 'react'

import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import {
  faBatteryEmpty,
  faBatteryFull,
  faBatteryHalf,
  faBatteryQuarter,
  faBatteryThreeQuarters,
  faBolt,
  faCar,
  faCloud,
  faCloudBolt,
  faCloudRain,
  faCloudSun,
  faFan,
  faHouse,
  faSnowflake,
  faSolarPanel,
  faSun,
  faTemperatureHalf
} from '@fortawesome/free-solid-svg-icons'
import type { IconDefinition } from '@fortawesome/fontawesome-svg-core'

import GridIcon from '../components/GridIcon'

import batLogoLight from '../assets/bat_logo_light.png'
import dayHouse from '../assets/house/day-house.webp'
import dayCar from '../assets/house/day-car.webp'
import dayHeatPump from '../assets/house/day-heat-pump.webp'
import dayCarHeatPump from '../assets/house/day-car-heat-pump.webp'
import dayHouseSnow from '../assets/house/day-house-snow.webp'
import dayCarSnow from '../assets/house/day-car-snow.webp'
import dayHeatPumpSnow from '../assets/house/day-heat-pump-snow.webp'
import dayCarHeatPumpSnow from '../assets/house/day-car-heat-pump-snow.webp'
import nightHouse from '../assets/house/night-house.webp'
import nightCar from '../assets/house/night-car.webp'
import nightHeatPump from '../assets/house/night-heat-pump.webp'
import nightCarHeatPump from '../assets/house/night-car-heat-pump.webp'
import nightHouseSnow from '../assets/house/night-house-snow.webp'
import nightCarSnow from '../assets/house/night-car-snow.webp'
import nightHeatPumpSnow from '../assets/house/night-heat-pump-snow.webp'
import nightCarHeatPumpSnow from '../assets/house/night-car-heat-pump-snow.webp'

import type { Plan } from '../types/plan'
import type { PowerFlowData } from '../types/powerFlow'
import { useStoredState } from '../hooks/useStoredState'
import { formatMajorCurrency, formatRate, resolveCurrencySymbols } from '../utils/currency'
import { formatCarStatus, formatOverviewStatus, formatWeatherStatus, getOverviewActions, getOverviewCarbonValues, getOverviewPowerTone, getOverviewSceneKey, getOverviewWeatherEffect, mapOverviewImagePoint, type OverviewWeatherEffect } from '../utils/overview'

import './OverviewPage.css'

type OverviewPageProps = {
  plan: Plan
  powerFlow: PowerFlowData
}

type CardConnectorName = 'grid' | 'solar' | 'home' | 'battery' | 'car' | 'heatPump'

type OverviewCardProps = {
  className: string
  icon: IconDefinition | ReactNode
  title: string
  main?: string
  mainTone?: string
  mainSubvalue?: string | null
  connector?: CardConnectorName
  children: ReactNode
}

const SCENE_IMAGES: Record<string, string> = {
  'day-house': dayHouse,
  'day-car': dayCar,
  'day-heat-pump': dayHeatPump,
  'day-car-heat-pump': dayCarHeatPump,
  'day-house-snow': dayHouseSnow,
  'day-car-snow': dayCarSnow,
  'day-heat-pump-snow': dayHeatPumpSnow,
  'day-car-heat-pump-snow': dayCarHeatPumpSnow,
  'night-house': nightHouse,
  'night-car': nightCar,
  'night-heat-pump': nightHeatPump,
  'night-car-heat-pump': nightCarHeatPump,
  'night-house-snow': nightHouseSnow,
  'night-car-snow': nightCarSnow,
  'night-heat-pump-snow': nightHeatPumpSnow,
  'night-car-heat-pump-snow': nightCarHeatPumpSnow
}

/*
 * Card starts are measured from the rendered card edges. Only the house-side
 * targets remain editable in CSS, in the house image's 1500 x 1200 coordinate
 * system. They are mapped into the responsive stage when the layout changes.
 */
const CARD_CONNECTOR_NAMES: CardConnectorName[] = ['grid', 'solar', 'home', 'battery', 'car', 'heatPump']

const CARD_CONNECTOR_VIEWBOX = { width: 1400, height: 860 } as const
const HOUSE_IMAGE_VIEWBOX = { width: 1500, height: 1200 } as const
const CARD_CONNECTOR_BEND = 140
const CARD_CONNECTOR_END_RADIUS = 2

type CardConnectorGeometry = {
  startX: number
  startY: number
  bendX: number
  targetX: number
  targetY: number
  endpointRadiusX: number
  endpointRadiusY: number
}

function connectorEndAtCircle(geometry: CardConnectorGeometry): { x: number; y: number } {
  const dx = geometry.targetX - geometry.bendX
  const dy = geometry.targetY - geometry.startY
  if (dx === 0 && dy === 0) return { x: geometry.targetX, y: geometry.targetY }

  const scale = 1 / Math.sqrt(
    (dx / geometry.endpointRadiusX) ** 2 +
    (dy / geometry.endpointRadiusY) ** 2
  )

  return {
    x: geometry.targetX - dx * scale,
    y: geometry.targetY - dy * scale
  }
}

const ENERGY_FLOW_PATHS = {
  solarToBattery: 'M 905 330 L 990 500 L 1165 745',
  batteryToHome: 'M 1165 745 L 1000 670 L 835 620',
  batteryToGrid: 'M 1165 745 L 1290 900 L 1430 1080',
  gridToBattery: 'M 1430 1080 L 1290 900 L 1165 745'
} as const

const GRID_POINT = { x: 1430, y: 1080 } as const

function formatPower(watts: number): string {
  const absolute = Math.abs(watts)
  if (absolute >= 1000) return `${(absolute / 1000).toFixed(2)} kW`
  return `${Math.round(absolute)} W`
}

function formatEnergy(value: number | null): string {
  return value === null ? 'Unavailable' : `${value.toFixed(2)} kWh`
}

function formatActionTime(value: string | null): string {
  if (!value) return ''
  return new Date(value).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })
}

function formatTemperature(value: number, unit: string): string {
  const suffix = unit.trim().replace(/^°/, '')
  return `${value.toFixed(1)}°${suffix}`
}

function weatherIcon(state: string | null): IconDefinition {
  const value = state?.toLowerCase() ?? ''
  if (value.includes('lightning')) return faCloudBolt
  if (value.includes('rain') || value.includes('pour')) return faCloudRain
  if (value.includes('snow') || value.includes('hail')) return faSnowflake
  if (value.includes('partly')) return faCloudSun
  if (value.includes('cloud') || value.includes('fog')) return faCloud
  return faSun
}

function actionTone(label: string): string {
  const value = label.toLowerCase()
  if (value.includes('export')) return 'is-export'
  if (value.includes('charge')) return 'is-charge'
  if (value.includes('car')) return 'is-car'
  return 'is-demand'
}

function getBatteryIcon(socPercent: number): IconDefinition {
  if (socPercent >= 88) return faBatteryFull
  if (socPercent >= 63) return faBatteryThreeQuarters
  if (socPercent >= 38) return faBatteryHalf
  if (socPercent >= 13) return faBatteryQuarter
  return faBatteryEmpty
}

function batterySocTone(socPercent: number, charging: boolean): string {
  if (charging) return 'is-charging'
  if (socPercent <= 20) return 'is-low'
  if (socPercent <= 40) return 'is-medium'
  return 'is-healthy'
}

function OverviewCard({ className, icon, title, main, mainTone = '', mainSubvalue, connector, children }: OverviewCardProps) {
  return (
    <article className={`overview-card ${className}`} data-overview-connector={connector}>
      <header>
        <span className="overview-card-icon">
          {isValidElement(icon) ? icon : <FontAwesomeIcon icon={icon as IconDefinition} />}
        </span>
        <h2>{title}</h2>
      </header>
      {main && <strong className={`overview-card-main ${mainTone}`}>{main}</strong>}
      {mainSubvalue && <small className="overview-card-main-subvalue">{mainSubvalue}</small>}
      <div className="overview-card-details">{children}</div>
    </article>
  )
}

function RainRow({ back = false }: { back?: boolean }) {
  const count = back ? 18 : 26

  return (
    <div className={`overview-rain-row ${back ? 'is-back' : 'is-front'}`}>
      {Array.from({ length: count }, (_, index) => {
        const position = (index * (back ? 47 : 37) + (back ? 11 : 3)) % 98 + 1
        const duration = 0.58 + (index % 7) * 0.035 + (back ? 0.12 : 0)
        const delay = -((index * 0.173) % duration)
        const animation = { animationDelay: `${delay.toFixed(3)}s`, animationDuration: `${duration.toFixed(3)}s` }

        return (
          <span
            key={index}
            className="overview-rain-drop"
            style={{ ...animation, left: `${position}%`, bottom: `${102 + index % 8}%` }}
          >
            <i className="overview-rain-stem" style={animation} />
            <i className="overview-rain-splat" style={animation} />
          </span>
        )
      })}
    </div>
  )
}

function CloudComposite() {
  return (
    <div className="overview-cloud-composite">
      <span className="overview-cloud-layer is-back" />
      <span className="overview-cloud-layer is-middle" />
      <span className="overview-cloud-layer is-front" />
      <svg className="overview-cloud-filter-definitions" width="0" height="0" focusable="false">
        <defs>
          <filter id="overview-cloud-filter-back" x="-50%" y="-80%" width="200%" height="260%">
            <feTurbulence type="fractalNoise" baseFrequency="0.012" numOctaves="3" seed="11" result="noise" />
            <feDisplacementMap in="SourceGraphic" in2="noise" scale="120" />
          </filter>
          <filter id="overview-cloud-filter-middle" x="-50%" y="-80%" width="200%" height="260%">
            <feTurbulence type="fractalNoise" baseFrequency="0.012" numOctaves="2" seed="17" result="noise" />
            <feDisplacementMap in="SourceGraphic" in2="noise" scale="100" />
          </filter>
          <filter id="overview-cloud-filter-front" x="-50%" y="-80%" width="200%" height="260%">
            <feTurbulence type="fractalNoise" baseFrequency="0.012" numOctaves="2" seed="23" result="noise" />
            <feDisplacementMap in="SourceGraphic" in2="noise" scale="72" />
          </filter>
        </defs>
      </svg>
    </div>
  )
}

function WeatherEffect({ effect }: { effect: OverviewWeatherEffect }) {
  const raining = effect === 'rain' || effect === 'storm'

  return (
    <div className={`overview-weather-effect is-${effect}`} aria-hidden="true">
      {effect === 'clouds' && <CloudComposite />}
      {effect === 'snow' && <span className="overview-snow-layer" />}
      {raining && <RainRow />}
      {raining && <RainRow back />}
    </div>
  )
}

function Detail({ label, value, tone = '', subvalue }: { label: string; value: string; tone?: string; subvalue?: string | null }) {
  return (
    <div className="overview-detail">
      <span>{label}</span>
      <strong className={tone}>{value}</strong>
      {subvalue && <small className="overview-detail-subvalue">{subvalue}</small>}
    </div>
  )
}

export default function OverviewPage({ plan, powerFlow }: OverviewPageProps) {
  const stageRef = useRef<HTMLElement>(null)
  const sceneImageRef = useRef<HTMLImageElement>(null)
  const [connectorGeometry, setConnectorGeometry] = useState<Partial<Record<CardConnectorName, CardConnectorGeometry>>>({})
  const [flowPaths, setFlowPaths] = useState<Record<keyof typeof ENERGY_FLOW_PATHS, string>>({ ...ENERGY_FLOW_PATHS })
  const [gridPoint, setGridPoint] = useState<{ x: number; y: number }>({ ...GRID_POINT })
  const actions = getOverviewActions(plan.rows)
  const hasCar = plan.num_cars > 0
  const hasHeatPump = powerFlow.ashp !== null
  const { major: currencyMajor, minor: currencyMinor } = resolveCurrencySymbols(powerFlow.currency_symbols ?? plan.currency_symbols)
  const gridExporting = powerFlow.grid_power >= 10 && !powerFlow.grid_importing
  const currentRowIndex = plan.rows.findLastIndex((row) => new Date(row.time) <= new Date())
  const currentRow = plan.rows[currentRowIndex >= 0 ? currentRowIndex : 0]
  const currentRate = gridExporting ? currentRow?.export_rate : currentRow?.import_rate
  const carStatus = formatCarStatus(powerFlow.car.status, powerFlow.car.charging)
  const batteryPowerTone = powerFlow.battery_charging ? 'is-charge' : ''
  const batteryState = powerFlow.battery_charging ? 'Charging' : powerFlow.battery_discharging ? 'Discharging' : 'Idle'
  const batteryStateTone = powerFlow.battery_charging ? 'is-charge' : 'is-muted'
  const batteryIcon = getBatteryIcon(powerFlow.soc_percent)
  const batteryTone = batterySocTone(powerFlow.soc_percent, powerFlow.battery_charging)
  const gridTone = gridExporting ? 'is-export' : ''
  const heatPumpStatus = powerFlow.ashp?.status?.trim().toLowerCase()
  const heatPumpRunning = Boolean(heatPumpStatus && heatPumpStatus !== 'off')
  const carbon = getOverviewCarbonValues(Boolean(plan.carbon_enable), currentRow)
  const nextStartTime = formatActionTime(actions.next.startsAt)
  const nextEndTime = formatActionTime(actions.next.endsAt)
  const weather = powerFlow.weather
  const [weatherEffectsPreference, setWeatherEffectsPreference] = useStoredState(
    'predbat-overview-weather-effects',
    'on',
    ['on', 'off'] as const
  )
  const weatherEffectsEnabled = weatherEffectsPreference === 'on'
  const weatherEffect = weatherEffectsEnabled ? getOverviewWeatherEffect(weather?.state ?? null) : null
  const sceneKey = getOverviewSceneKey(powerFlow.sun_state, hasCar, hasHeatPump, weatherEffect === 'snow')
  const sceneImage = SCENE_IMAGES[sceneKey] ?? dayHouse
  const isNight = sceneKey.startsWith('night-')

  useLayoutEffect(() => {
    const stage = stageRef.current
    const sceneImageElement = sceneImageRef.current
    if (!stage || !sceneImageElement) return

    const updatePaths = () => {
      const stageRect = stage.getBoundingClientRect()
      const sceneImageRect = sceneImageElement.getBoundingClientRect()
      if (stageRect.width === 0 || stageRect.height === 0 || sceneImageRect.width === 0 || sceneImageRect.height === 0) return

      const styles = getComputedStyle(stage)
      const cssNumber = (property: string, fallback: number) => {
        const value = Number.parseFloat(styles.getPropertyValue(property))
        return Number.isFinite(value) ? value : fallback
      }
      const cssPath = (property: string, fallback: string) => {
        const value = styles.getPropertyValue(property).trim()
        const match = value.match(/^path\(["'](.+)["']\)$/)
        return match?.[1] ?? fallback
      }
      const bendDistance = cssNumber('--overview-card-connector-bend', CARD_CONNECTOR_BEND)
      const endpointRadiusX = CARD_CONNECTOR_END_RADIUS * CARD_CONNECTOR_VIEWBOX.width / stageRect.width
      const endpointRadiusY = CARD_CONNECTOR_END_RADIUS * CARD_CONNECTOR_VIEWBOX.height / stageRect.height
      const nextGeometry: Partial<Record<CardConnectorName, CardConnectorGeometry>> = {}
      for (const name of CARD_CONNECTOR_NAMES) {
        const card = stage.querySelector<HTMLElement>(`[data-overview-connector="${name}"]`)
        if (!card) continue

        const cardRect = card.getBoundingClientRect()
        const startsOnLeft = cardRect.left + cardRect.width / 2 < stageRect.left + stageRect.width / 2
        const startX = ((startsOnLeft ? cardRect.right : cardRect.left) - stageRect.left) * CARD_CONNECTOR_VIEWBOX.width / stageRect.width
        const startY = (cardRect.top + cardRect.height / 2 - stageRect.top) * CARD_CONNECTOR_VIEWBOX.height / stageRect.height
        const bendX = startX + (startsOnLeft ? bendDistance : -bendDistance)
        const cssName = name === 'heatPump' ? 'heat-pump' : name
        const imageTarget = {
          x: cssNumber(`--overview-card-${cssName}-x`, HOUSE_IMAGE_VIEWBOX.width / 2),
          y: cssNumber(`--overview-card-${cssName}-y`, HOUSE_IMAGE_VIEWBOX.height / 2)
        }
        const target = mapOverviewImagePoint(
          imageTarget,
          { x: HOUSE_IMAGE_VIEWBOX.width, y: HOUSE_IMAGE_VIEWBOX.height },
          sceneImageRect,
          stageRect,
          { x: CARD_CONNECTOR_VIEWBOX.width, y: CARD_CONNECTOR_VIEWBOX.height }
        )
        nextGeometry[name] = {
          startX,
          startY,
          bendX,
          targetX: target.x,
          targetY: target.y,
          endpointRadiusX,
          endpointRadiusY
        }
      }

      setConnectorGeometry((current) => JSON.stringify(current) === JSON.stringify(nextGeometry) ? current : nextGeometry)
      const nextFlowPaths = {
        solarToBattery: cssPath('--overview-flow-solar-path', ENERGY_FLOW_PATHS.solarToBattery),
        batteryToHome: cssPath('--overview-flow-battery-to-home-path', ENERGY_FLOW_PATHS.batteryToHome),
        batteryToGrid: cssPath('--overview-flow-battery-to-grid-path', ENERGY_FLOW_PATHS.batteryToGrid),
        gridToBattery: cssPath('--overview-flow-grid-to-battery-path', ENERGY_FLOW_PATHS.gridToBattery)
      }
      setFlowPaths((current) => JSON.stringify(current) === JSON.stringify(nextFlowPaths) ? current : nextFlowPaths)
      const nextGridPoint = {
        x: cssNumber('--overview-grid-point-x', GRID_POINT.x),
        y: cssNumber('--overview-grid-point-y', GRID_POINT.y)
      }
      setGridPoint((current) => current.x === nextGridPoint.x && current.y === nextGridPoint.y ? current : nextGridPoint)
    }

    const observer = new ResizeObserver(updatePaths)
    observer.observe(stage)
    observer.observe(sceneImageElement)
    stage.querySelectorAll<HTMLElement>('[data-overview-connector]').forEach((card) => observer.observe(card))
    updatePaths()
    const cssControlTimer = window.setInterval(updatePaths, 250)
    return () => {
      observer.disconnect()
      window.clearInterval(cssControlTimer)
    }
  }, [hasCar, hasHeatPump])

  return (
    <div className={`overview-page ${isNight ? 'is-night' : ''}`}>
      {weatherEffect && <WeatherEffect effect={weatherEffect} />}
      <header className="overview-page-header">
        <div>
          <h1>Overview</h1>
          <p>Live energy use, generation and Predbat's next action.</p>
        </div>
      </header>

      <section ref={stageRef} className="overview-stage" aria-label="Live home energy overview">
        <svg className="overview-connectors" viewBox="0 0 1400 860" preserveAspectRatio="none" aria-hidden="true">
          {Object.entries(connectorGeometry).map(([name, geometry]) => {
            const endpoint = name === 'grid'
              ? { x: geometry.targetX, y: geometry.targetY }
              : connectorEndAtCircle(geometry)

            return (
              <g key={name} className="overview-connector" data-connector-target={name}>
                <line x1={geometry.startX} y1={geometry.startY} x2={geometry.bendX} y2={geometry.startY} />
                <line className="overview-connector-target" x1={geometry.bendX} y1={geometry.startY} x2={endpoint.x} y2={endpoint.y} />
                {name !== 'grid' && <ellipse className="overview-connector-end" cx={geometry.targetX} cy={geometry.targetY} rx={geometry.endpointRadiusX} ry={geometry.endpointRadiusY} />}
              </g>
            )
          })}
        </svg>

        <div className="overview-column overview-column-left">
          <OverviewCard
            className="overview-card-predbat"
            icon={<img src={batLogoLight} alt="" />}
            title="Predbat"
            main={actions.current.label}
            mainTone={actionTone(actions.current.label)}
            mainSubvalue={actions.current.limit !== null && ['Charge', 'Hold Charge', 'Freeze Charge'].includes(actions.current.label) ? `Charge target ${actions.current.limit}%` : null}
          >
            <div className="overview-predbat-next">
              <div className="overview-predbat-next-row">
                <span>Next Action</span>
                <strong className={actionTone(actions.next.label)}>{actions.next.label}</strong>
              </div>
              {nextStartTime && <span>{nextStartTime}{nextEndTime && ` → ${nextEndTime}`}</span>}
              {actions.next.limit !== null && <span>Limit {actions.next.limit}%</span>}
            </div>
          </OverviewCard>

          <OverviewCard className="overview-card-home" connector="home" icon={faHouse} title="Home" main={formatPower(powerFlow.house_power)} mainTone={getOverviewPowerTone(powerFlow.house_power, 'is-home')}>
            <Detail label="Used today" value={formatEnergy(powerFlow.totals.load_today)} />
          </OverviewCard>

          {hasCar && (
            <OverviewCard className="overview-card-ev" connector="car" icon={faCar} title="EV Charger" main={formatPower(powerFlow.car.power)} mainTone={getOverviewPowerTone(powerFlow.car.power, powerFlow.car.charging ? 'is-charge' : '')}>
              <Detail label="Status" value={carStatus} tone={powerFlow.car.charging ? 'is-charge' : ''} />
              {powerFlow.car.soc !== null && <Detail label="Car SOC" value={`${powerFlow.car.soc.toFixed(0)}%`} />}
              <Detail label="Energy today" value={formatEnergy(powerFlow.car.energy_today)} />
            </OverviewCard>
          )}


          {weather && (
            <button
              type="button"
              className="overview-weather-toggle"
              role="switch"
              aria-checked={weatherEffectsEnabled}
              onClick={() => setWeatherEffectsPreference(weatherEffectsEnabled ? 'off' : 'on')}
            >
              <FontAwesomeIcon icon={faCloudSun} />
              <span>Weather effects</span>
              <span className="overview-weather-toggle-track" aria-hidden="true"><i /></span>
            </button>
          )}
        </div>

        <figure className="overview-scene">
          <img ref={sceneImageRef} src={sceneImage} alt={`Isometric home shown in ${sceneKey.replace(/-/g, ' ')} mode`} width="1500" height="1200" />
          <svg className="overview-flow-lines" viewBox="0 0 1500 1200" aria-hidden="true">
            <defs>
              <marker id="overview-arrow-solar" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker>
              <marker id="overview-arrow-battery" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker>
              <marker id="overview-arrow-grid-import" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker>
              <marker id="overview-arrow-grid-export" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" /></marker>
            </defs>
            {powerFlow.pv_generating && <path className="overview-flow overview-flow-solar is-active" d={flowPaths.solarToBattery} markerEnd="url(#overview-arrow-solar)" />}
            {powerFlow.battery_discharging && <path className="overview-flow overview-flow-battery is-discharging is-active" d={flowPaths.batteryToHome} markerEnd="url(#overview-arrow-battery)" />}
            <path className={`overview-flow overview-flow-grid ${gridExporting ? 'is-export is-active' : powerFlow.grid_importing ? 'is-import is-active' : 'is-idle'}`} d={gridExporting ? flowPaths.batteryToGrid : flowPaths.gridToBattery} markerEnd={gridExporting ? 'url(#overview-arrow-grid-export)' : 'url(#overview-arrow-grid-import)'} />
            <g className="overview-grid-point" transform={`translate(${gridPoint.x} ${gridPoint.y})`}>
              <circle r="13" />
              <text x="-8" y="40" textAnchor="end">GRID</text>
            </g>
          </svg>
        </figure>

        <div className="overview-column overview-column-right">
          <OverviewCard className="overview-card-solar" connector="solar" icon={faSolarPanel} title="Solar" main={formatPower(powerFlow.pv_power)} mainTone={getOverviewPowerTone(powerFlow.pv_power, powerFlow.pv_generating ? 'is-solar' : '')}>
            {weather && (
              <div className="overview-weather">
                <FontAwesomeIcon icon={weatherIcon(weather.state)} />
                <span>{formatWeatherStatus(weather.state)}</span>
                {weather.temperature !== null && <strong><FontAwesomeIcon icon={faTemperatureHalf} /> {formatTemperature(weather.temperature, weather.temperature_unit)}</strong>}
              </div>
            )}
            <Detail label="Generated today" value={formatEnergy(powerFlow.totals.pv_today)} />
            <Detail label="Forecast today" value={formatEnergy(powerFlow.pv_forecast_today)} />
          </OverviewCard>

          <OverviewCard
            className="overview-card-battery"
            connector="battery"
            icon={(
              <span className={`overview-battery-icon ${batteryTone}`}>
                <FontAwesomeIcon icon={batteryIcon} />
                {powerFlow.battery_charging && <FontAwesomeIcon icon={faBolt} className="overview-battery-charge-badge" />}
              </span>
            )}
            title="Battery"
            main={`${powerFlow.soc_percent.toFixed(0)}%`}
          >
            <div className={`overview-battery-soc ${batteryTone}`}>
              <div className="overview-battery-track" aria-label={`Battery ${powerFlow.soc_percent.toFixed(0)}% full`}>
                <span style={{ width: `${Math.min(100, Math.max(0, powerFlow.soc_percent))}%` }} />
              </div>
            </div>
            <Detail label="State" value={batteryState} tone={batteryStateTone} />
            <Detail label="Power" value={formatPower(powerFlow.battery_power)} tone={batteryPowerTone} />
          </OverviewCard>

          <OverviewCard
            className="overview-card-grid"
            connector="grid"
            icon={<GridIcon />}
            title="Grid"
            main={formatPower(powerFlow.grid_power)}
            mainTone={getOverviewPowerTone(powerFlow.grid_power, gridTone)}
            mainSubvalue={carbon.intensity}
          >
            <Detail
              label={gridExporting ? 'Export rate' : 'Import rate'}
              value={currentRate === undefined ? 'Unavailable' : formatRate(currentRate, currencyMinor)}
              tone={gridExporting ? 'is-export' : ''}
            />
            <Detail label="Cost today" value={formatMajorCurrency(powerFlow.totals.cost_today / 100, currencyMajor)} />
            <Detail label="Imported today" value={formatEnergy(powerFlow.totals.import_today)} subvalue={carbon.total} />
            <Detail label="Exported today" value={formatEnergy(powerFlow.totals.export_today)} tone="is-export" />
          </OverviewCard>

          {powerFlow.ashp && (
            <OverviewCard
              className={`overview-card-ashp ${heatPumpRunning ? 'is-running' : ''}`}
              connector="heatPump"
              icon={faFan}
              title="ASHP"
              main={powerFlow.ashp.power === null ? 'Enabled' : formatPower(powerFlow.ashp.power)}
              mainTone={powerFlow.ashp.power === null ? '' : getOverviewPowerTone(powerFlow.ashp.power, powerFlow.ashp.power >= 10 ? 'is-home' : '')}
            >
              {powerFlow.ashp.status !== null && <Detail label="Status" value={formatOverviewStatus(powerFlow.ashp.status)} />}
              {powerFlow.ashp.energy_today !== null && <Detail label="Energy today" value={formatEnergy(powerFlow.ashp.energy_today)} />}
            </OverviewCard>
          )}
        </div>
      </section>
    </div>
  )
}
