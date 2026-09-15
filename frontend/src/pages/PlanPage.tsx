import PlanSummary from '../components/PlanSummary'
import PlanTable from '../components/PlanTable'
import PlanVisual from '../components/PlanVisual'

import type { Plan, PlanOverrides } from '../types/plan'

import './PlanPage.css'

import {
    Fragment,
    useEffect,
    useState
} from 'react'

type PlanPageProps = {
    plan: Plan
    overrides: PlanOverrides
    onOverrideSubmitted: () => void
}


export default function PlanPage({
    plan,
    overrides,
    onOverrideSubmitted
}: PlanPageProps) {

    return (
        <div className="plan-page">

            <header className="plan-page-header">

                <div>
                    <h1>
                        Plan
                    </h1>

                    <p>
                        Predbat's planned battery and energy behaviour.
                    </p>
                </div>

            </header>


            <PlanSummary
                plan={plan}
                showPlanLink={false}
            />

            <PlanVisual
                plan={plan}
            />


            <PlanTable
                plan={plan}
                overrides={overrides}
                onOverrideSubmitted={
                    onOverrideSubmitted
                }
            />

        </div>
    )
}