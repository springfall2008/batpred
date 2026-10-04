import SimplePowerFlow from './SimplePowerFlow'
import type { PowerFlowData } from '../types/powerFlow'

import './PowerFlow.css'

type PowerFlowProps = {
  data: PowerFlowData
}

function PowerFlow({ data }: PowerFlowProps) {
  return (
    <section className="power-flow-card">
      <div className="power-flow-header">
        <div>
          <h2>Power Flow</h2>
          <span className="power-flow-subtitle">Live energy flow around your home</span>
        </div>
      </div>

      <SimplePowerFlow data={data} />
    </section>
  )
}

export default PowerFlow
