import SimplePowerFlow from './SimplePowerFlow'
import type { PowerFlowData } from '../types/powerFlow'

import './PowerFlow.css'

type PowerFlowProps = {
  data: PowerFlowData
  stale?: boolean
  socBoundaries: { low: number; medium: number }
}

function PowerFlow({ data, stale = false, socBoundaries }: PowerFlowProps) {
  return (
    <section className="power-flow-card">
      <div className="power-flow-header">
        <div>
          <h2>Power Flow</h2>
          <span className="power-flow-subtitle">{stale ? 'Last known energy flow · refresh overdue or failed' : 'Energy flow at the latest live snapshot'}</span>
        </div>
      </div>

      <SimplePowerFlow data={data} socBoundaries={socBoundaries} />
    </section>
  )
}

export default PowerFlow
