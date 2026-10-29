import SimplePowerFlow from './SimplePowerFlow'
import type { PowerFlowData } from '../types/powerFlow'

import './PowerFlow.css'

type PowerFlowProps = {
  data: PowerFlowData
  stale?: boolean
}

function PowerFlow({ data, stale = false }: PowerFlowProps) {
  return (
    <section className="power-flow-card">
      <div className="power-flow-header">
        <div>
          <h2>Power Flow</h2>
          <span className="power-flow-subtitle">{stale ? 'Last known energy flow · refresh overdue or failed' : 'Energy flow at the latest live snapshot'}</span>
        </div>
      </div>

      <SimplePowerFlow data={data} />
    </section>
  )
}

export default PowerFlow
