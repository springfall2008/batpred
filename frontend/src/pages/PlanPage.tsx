import { useState } from 'react'

import PlanSummary from '../components/PlanSummary'
import PlanTable from '../components/PlanTable'
import PlanVisual from '../components/PlanVisual'

import type { Plan, PlanOverrides } from '../types/plan'
import { selectPlanView, shouldShowPlanDebug, type PlanView } from '../utils/plan'

import './PlanPage.css'

type PlanPageProps = {
  plan: Plan
  yesterday: Plan | null
  baseline: Plan | null
  overrides: PlanOverrides
  debugEnabled: boolean
  onOverrideSubmitted: () => void
}

const EMPTY_OVERRIDES: PlanOverrides = {
  manual_charge_times: [],
  manual_export_times: [],
  manual_freeze_charge_times: [],
  manual_freeze_export_times: [],
  manual_demand_times: [],
  manual_import_rates: [],
  manual_export_rates: [],
  manual_soc: []
}

export default function PlanPage({
  plan,
  yesterday,
  baseline,
  overrides,
  debugEnabled,
  onOverrideSubmitted
}: PlanPageProps) {
  const [view, setView] = useState<PlanView>('plan')
  const selectedPlan = selectPlanView(view, plan, yesterday, baseline)
  const historical = view !== 'plan'
  const showPlanDebug = shouldShowPlanDebug(view, plan, debugEnabled)

  return (
    <div className="plan-page">
      <header className="plan-page-header">
        <div>
          <h1>Plan</h1>

          <p>Predbat's planned battery and energy behaviour.</p>
        </div>
      </header>

      <PlanSummary plan={plan} showPlanLink={false} />

      <div className="plan-view-tabs" aria-label="Plan view">
        {([
          ['plan', 'Plan'],
          ['yesterday', 'History'],
          ['baseline', 'Yesterday without Predbat']
        ] as const).map(([value, label]) => (
          <button
            type="button"
            className={view === value ? 'is-active' : ''}
            aria-pressed={view === value}
            key={value}
            onClick={() => setView(value)}
          >
            {label}
          </button>
        ))}
      </div>

      {selectedPlan ? (
        <>
          <PlanVisual plan={selectedPlan} />

          <PlanTable
            plan={selectedPlan}
            overrides={historical ? EMPTY_OVERRIDES : overrides}
            debugEnabled={showPlanDebug}
            readOnly={historical}
            onOverrideSubmitted={onOverrideSubmitted}
          />
        </>
      ) : (
        <section className="plan-view-empty">
          <h2>No data for this view yet</h2>
          <p>Predbat calculates yesterday's views after enough history is available.</p>
        </section>
      )}
    </div>
  )
}
