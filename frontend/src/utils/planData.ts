import type { PlanData } from '../types/plan'

export type PlanDataResponse = Partial<PlanData> & Pick<PlanData, 'unchanged' | 'overrides_hash'>

/** Keep the last complete plan when Predbat returns its compact unchanged response. */
export function resolvePlanData(response: PlanDataResponse, cached: PlanData | null): PlanData | null {
  if (response.unchanged) return cached?.plan ? cached : null
  return response.plan ? response as PlanData : null
}
