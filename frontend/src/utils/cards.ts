import type { EntitySummary } from './entities'

export const PLAN_SUMMARY_FIELDS = [
  { suffix: 'status', name: 'Status' },
  { suffix: 'soc_kw_h0', name: 'Battery level' },
  { suffix: 'rates', name: 'Import rate' },
  { suffix: 'best_charge_start', name: 'Next charge starts' },
  { suffix: 'best_charge_limit', name: 'Charge target' },
  { suffix: 'best_export_start', name: 'Next export starts' },
  { suffix: 'best_export_limit', name: 'Export target' },
  { suffix: 'cost_today', name: 'Cost today' }
] as const

/** Resolve the plan summary entities exposed by this Predbat instance. */
export function getPlanSummaryEntities(entities: EntitySummary[]) {
  return PLAN_SUMMARY_FIELDS.flatMap((field) => {
    const entity = entities.find(({ id }) => id.endsWith(`.${field.suffix}`))
    return entity ? [{ ...field, id: entity.id }] : []
  })
}

/** Build a standard Home Assistant entities card with the resolved entity IDs. */
export function buildPlanSummaryYaml(entities: EntitySummary[]) {
  const rows = getPlanSummaryEntities(entities)
    .flatMap(({ id, name }) => [`  - entity: ${id}`, `    name: ${name}`])

  return [
    'type: entities',
    'title: Predbat Plan Summary',
    'show_header_toggle: false',
    'entities:',
    ...rows
  ].join('\n')
}

/** Build a compact Home Assistant glance card using the same plan summary entities. */
export function buildGlanceSummaryYaml(entities: EntitySummary[]) {
  const rows = getPlanSummaryEntities(entities)
    .flatMap(({ id, name }) => [`  - entity: ${id}`, `    name: ${name}`])

  return [
    'type: glance',
    'title: Predbat Summary',
    'columns: 4',
    'show_name: true',
    'show_icon: true',
    'show_state: true',
    'entities:',
    ...rows
  ].join('\n')
}
