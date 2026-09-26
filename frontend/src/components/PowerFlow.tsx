import { useEffect, useState } from 'react'

import SimplePowerFlow from './SimplePowerFlow'
import DetailedPowerFlow from './DetailedPowerFlow'
import type { PowerFlowData } from '../types/powerFlow'

import './PowerFlow.css'

type PowerFlowProps = {
  data: PowerFlowData
  numCars: number
}
type PowerFlowView = 'simple' | 'detailed'

const POWER_FLOW_VIEW_STORAGE_KEY = 'predbat-power-flow-view'

function PowerFlow({ data, numCars }: PowerFlowProps) {
  const [powerFlowView, setPowerFlowView] = useState<PowerFlowView>(() => {
    /*
     * Read the user's previous Power Flow view preference.
     *
     * The lazy useState initializer only runs when the
     * component is first created, rather than on every
     * render.
     */
    try {
      const storedView = localStorage.getItem(POWER_FLOW_VIEW_STORAGE_KEY)

      if (storedView === 'simple' || storedView === 'detailed') {
        return storedView
      }
    } catch {
      /*
       * localStorage may theoretically be unavailable,
       * for example because of browser privacy settings.
       *
       * In that case just use the default.
       */
    }

    /*
     * Default for users who have never selected a view.
     */
    return 'simple'
  })

  useEffect(() => {
    /*
     * Remember the selected Power Flow view for future visits.
     */
    try {
      localStorage.setItem(POWER_FLOW_VIEW_STORAGE_KEY, powerFlowView)
    } catch {
      /*
       * Failing to store the preference should not affect
       * the dashboard itself.
       */
    }
  }, [powerFlowView])

  return (
    <section className="power-flow-card">
      {/* Card header */}
      <div className="power-flow-header">
        <div>
          <h2>Power Flow</h2>

          <span className="power-flow-subtitle">Live energy flow around your home</span>
        </div>

        {/* View selector */}
        <div className="power-flow-view-toggle">
          <button
            type="button"
            className={`power-flow-view-button ${powerFlowView === 'simple' ? 'active' : ''}`}
            aria-pressed={powerFlowView === 'simple'}
            onClick={() => setPowerFlowView('simple')}
          >
            Simple
          </button>

          <button
            type="button"
            className={`power-flow-view-button ${powerFlowView === 'detailed' ? 'active' : ''}`}
            aria-pressed={powerFlowView === 'detailed'}
            onClick={() => setPowerFlowView('detailed')}
          >
            Detailed
          </button>
        </div>
      </div>

      {powerFlowView === 'simple' ? (
        <SimplePowerFlow data={data} />
      ) : (
        <DetailedPowerFlow data={data} showCar={data.car.configured || numCars > 0} />
      )}
    </section>
  )
}

export default PowerFlow
