import { FontAwesomeIcon, FontAwesomeLayers } from '@fortawesome/react-fontawesome'
import {
  faArrowRight, faArrowDown, faArrowUp, faArrowUpFromBracket, faArrowTrendUp,
  faBatteryThreeQuarters, faBan, faBolt, faCar, faCircleInfo, faClock,
  faCrosshairs, faGaugeSimpleHigh, faHouse, faPause, faPlay, faSolarPanel,
  faSnowflake, faWater, faFlag, faChevronDown, faCheck, faRotateLeft
} from '@fortawesome/free-solid-svg-icons'
import NordPoolIcon from './NordPoolIcon'

const icon = (value: typeof faBolt, colour?: string) => <FontAwesomeIcon icon={value} style={colour ? { color: `var(--color-${colour})` } : undefined} aria-hidden="true" />
const groups = [
  {
    title: 'Columns',
    entries: [
      [icon(faClock), 'Time / uncertain car energy', 'Slot start time; beside car energy, a scheduled amount that is not guaranteed.'],
      [icon(faPlay), 'Action', 'What the battery will do during the slot.'],
      [icon(faArrowDown), 'Import', 'The price of electricity bought from the grid.'],
      [icon(faArrowUp), 'Export rate', 'The price received for electricity sent to the grid.'],
      [icon(faSolarPanel), 'Solar', 'Forecast solar generation.'],
      [icon(faCrosshairs), 'Target', 'The battery charge or export target.'],
      [icon(faBatteryThreeQuarters), 'State of charge', 'The predicted battery charge level.'],
      [icon(faGaugeSimpleHigh), 'Carbon', 'Grid carbon intensity or cumulative emissions.']
    ]
  },
  {
    title: 'Controls',
    entries: [
      [icon(faChevronDown), 'Override menu', 'Open the available actions for a future slot.'],
      [icon(faArrowUpFromBracket), 'Manual export', 'Choose an export override.'],
      [icon(faCheck), 'Selected', 'The currently selected override.'],
      [icon(faRotateLeft), 'Automatic', 'Clear the override and let Predbat choose.']
    ]
  },
  {
    title: 'Actions',
    entries: [
      [icon(faBolt, 'charge'), 'Charge / hold charge', 'Charge the battery or hold its charge target. Also marks tariff events.'],
      [<FontAwesomeLayers key="export" style={{ color: 'var(--color-export)' }}>{icon(faBolt)}<FontAwesomeIcon icon={faArrowTrendUp} transform="shrink-4 right-6 up-6" aria-hidden="true" /></FontAwesomeLayers>, 'Export', 'Discharge the battery to the grid.'],
      [icon(faSnowflake, 'charge'), 'Freeze charge', 'Freeze battery charging.'],
      [icon(faSnowflake, 'export'), 'Freeze export', 'Freeze battery exporting.'],
      [icon(faPause, 'export'), 'Hold export', 'Hold the export target.'],
      [icon(faCar), 'Car', 'Hold the battery for car charging, or show car charging energy.'],
      [icon(faWater), 'iBoost', 'Hold the battery for hot-water diversion.'],
      [icon(faBan), 'No charge', 'Battery charging is disabled.'],
      [icon(faHouse), 'Demand / load', 'Normal household demand, or forecast household consumption.'],
      [icon(faArrowRight), 'Unknown action', 'The action has no recognised icon.']
    ]
  },
  {
    title: 'Indicators',
    entries: [
      [<span key="warning" className="plan-warning-flag">{icon(faFlag)}</span>, 'Warning / error', 'A problem was recorded during this slot. Hover or focus the action for details.'],
      [<NordPoolIcon key="nord-pool" />, 'Predicted rate', 'Nord Pool data is being used until the tariff publishes its rate.'],
      [<span key="manual" className="plan-key-manual-dot" />, 'Manual override', 'A manually set action, rate or target.'],
      [icon(faCircleInfo), 'Help', 'Hover or focus for an explanation of the column.']
    ]
  }
]

/** Explain the symbols used by the detailed plan and its controls. */
export default function PlanIconKey() {
  return (
    <section className="plan-icon-key" aria-labelledby="plan-icon-key-title">
      <h2 id="plan-icon-key-title">Icon key</h2>
      <div className="plan-icon-key-groups">
        {groups.map((group) => (
          <div key={group.title}>
            <h3>{group.title}</h3>
            <dl>
              {group.entries.map(([symbol, label, description]) => (
                <div className="plan-icon-key-entry" key={String(label)}>
                  <dt><span className="plan-icon-key-symbol" aria-hidden="true">{symbol}</span>{label}</dt>
                  <dd>{description}</dd>
                </div>
              ))}
            </dl>
          </div>
        ))}
      </div>
    </section>
  )
}
