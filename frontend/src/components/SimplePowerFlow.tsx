import { FontAwesomeIcon } from '@fortawesome/react-fontawesome'
import { faSolarPanel, faHouse, faBatteryEmpty, faBatteryQuarter, faBatteryHalf, faBatteryThreeQuarters, faBatteryFull, faCar, faFan } from '@fortawesome/free-solid-svg-icons'
import type { IconDefinition } from '@fortawesome/fontawesome-svg-core'
import type { ReactNode } from 'react'
import GridIcon from './GridIcon'
import type { PowerFlowData } from '../types/powerFlow'
import { useStoredState } from '../hooks/useStoredState'
import { getFlowDuration, resolvePowerFlow } from '../utils/powerFlow'
import './SimplePowerFlow.css'
import './StatusCard.css'

/** Format a measured power value in watts or kilowatts. */
function formatPower(power: number) {
  const value = Math.abs(power)
  return value >= 1000 ? `${(value / 1000).toFixed(1)} kW` : `${value.toFixed(0)} W`
}

/** Choose the existing battery icon for the current charge level. */
function getBatteryIcon(soc: number) {
  return soc >= 88 ? faBatteryFull : soc >= 63 ? faBatteryThreeQuarters : soc >= 38 ? faBatteryHalf : soc >= 13 ? faBatteryQuarter : faBatteryEmpty
}

/** Render one coloured route with a power-dependent moving dot. */
function FlowLine({ path, power, tone, reverse = false, threshold = 10 }: { path: string; power: number; tone: string; reverse?: boolean; threshold?: number }) {
  const active = power >= threshold
  return <g className={tone}>
    <path d={path} className={`simple-flow-base ${active ? 'is-active' : 'is-idle'}`} />
    {active && <circle r="4" className="simple-flow-dot">
      <animateMotion dur={getFlowDuration(power)} repeatCount="indefinite" path={path} keyPoints={reverse ? '1;0' : '0;1'} keyTimes="0;1" calcMode="linear" />
    </circle>}
  </g>
}

/** Keep the icon, power and state within a labelled circular node. */
function FlowNode({ x, y, label, value, tone, icon, soc, socTone, direction }: {
  x: number; y: number; label: string; value: string; tone: string; icon: IconDefinition | ReactNode; soc?: number; socTone?: string; direction?: string
}) {
  return <g transform={`translate(${x} ${y})`} className={`simple-node ${tone}`} role="group" aria-label={`${label}: ${direction ?? ''} ${value}${soc === undefined ? '' : `, ${soc}% full`}`}>
    <circle r="43" className="simple-node-circle" />
    {soc !== undefined && <text y="-25" className="simple-node-soc">{soc.toFixed(0)}%</text>}
    <foreignObject x="-13" y={soc === undefined ? -23 : -16} width="26" height="26" aria-hidden="true">
      <div className={`simple-node-icon ${socTone ?? ''}`}>{icon && typeof icon === 'object' && 'iconName' in icon ? <FontAwesomeIcon icon={icon as IconDefinition} /> : icon as ReactNode}</div>
    </foreignObject>
    <text y="23" className={`simple-node-power ${value === 'Idle' ? 'is-muted' : ''}`}>
      {direction && <tspan aria-hidden="true">{direction === 'Importing' || direction === 'Charging' ? '↓ ' : '↑ '}</tspan>}{value}
    </text>
    {label !== 'Home' && <text y={label === 'Solar' || label === 'ASHP' ? -53 : 63} className="simple-node-label">{label}</text>}
  </g>
}

/** Screenshot-inspired live flow diagram, with optional household device subtraction. */
function SimplePowerFlow({ data, socBoundaries }: { data: PowerFlowData; socBoundaries: { low: number; medium: number } }) {
  const [subtract, setSubtract] = useStoredState('predbat-power-flow-subtract-devices', 'off', ['off', 'on'])
  const flow = resolvePowerFlow(data, subtract === 'on')
  const gridTone = data.grid_importing ? 'flow-grid' : 'flow-export'
  const batteryTone = data.battery_charging ? 'flow-charge' : 'flow-battery'
  const socTone = data.soc_percent <= socBoundaries.low ? 'soc-low' : data.soc_percent <= socBoundaries.medium ? 'soc-medium' : 'soc-high'
  const hasDevices = data.car.configured || data.ashp !== null
  return <div className="simple-power-flow">
    <svg viewBox="20 -10 560 400" className="simple-power-flow-diagram" role="group" aria-label="Live energy flow">
      <FlowLine path="M 312 92 C 325 176 350 174 447 174" power={flow.solarHome} tone="flow-solar" />
      <FlowLine path="M 300 97 L 300 269" power={flow.solarBattery} tone="flow-charge" />
      <FlowLine path="M 288 92 C 275 174 250 174 153 174" power={flow.solarGrid} tone="flow-export" />
      <FlowLine path="M 153 190 L 447 190" power={flow.gridHome} tone="flow-grid" />
      <FlowLine path="M 288 273 C 275 206 250 206 153 206" power={flow.gridBattery + flow.batteryGrid} tone={gridTone} reverse={data.grid_importing} />
      <FlowLine path="M 312 273 C 325 206 350 206 447 206" power={flow.batteryHome} tone="flow-battery" />
      {data.ashp && <FlowLine path="M 490 147 L 490 97" power={flow.heatPump} tone="flow-heat-pump" threshold={100} />}
      {data.car.configured && <FlowLine path="M 490 233 L 490 269" power={flow.car} tone="flow-car" threshold={100} />}
      <FlowNode x={110} y={190} label="Grid" value={formatPower(data.grid_power)} tone={gridTone} icon={<GridIcon />} direction={Math.abs(data.grid_power) >= 10 ? data.grid_importing ? 'Importing' : 'Exporting' : undefined} />
      <FlowNode x={300} y={54} label="Solar" value={formatPower(data.pv_power)} tone="flow-solar" icon={faSolarPanel} />
      <FlowNode x={300} y={312} label="Battery" value={formatPower(data.battery_power)} tone={batteryTone} icon={getBatteryIcon(data.soc_percent)} soc={data.soc_percent} socTone={socTone} direction={Math.abs(data.battery_power) >= 10 ? data.battery_charging ? 'Charging' : data.battery_discharging ? 'Discharging' : undefined : undefined} />
      <FlowNode x={490} y={190} label="Home" value={formatPower(flow.home)} tone="flow-home" icon={faHouse} />
      {data.ashp && <FlowNode x={490} y={54} label="ASHP" value={data.ashp.power === null ? '—' : flow.heatPump < 100 ? 'Idle' : formatPower(flow.heatPump)} tone="flow-heat-pump" icon={faFan} />}
      {data.car.configured && <FlowNode x={490} y={312} label="Car" value={flow.car < 100 ? 'Idle' : formatPower(flow.car)} tone="flow-car" icon={faCar} />}
    </svg>
    {hasDevices && <label className="simple-flow-subtract">
      Subtract devices
      <span className="toggle-switch">
        <input type="checkbox" role="switch" checked={subtract === 'on'} onChange={(event) => setSubtract(event.target.checked ? 'on' : 'off')} />
        <span className="toggle-slider" aria-hidden="true" />
      </span>
    </label>}
  </div>
}

export default SimplePowerFlow
