export type EntitySummary = {
  id: string
  name: string
  group: string
}

export type EntityState = {
  state?: unknown
  attributes?: Record<string, unknown>
  last_updated?: string
}

/** Filter entities by their visible metadata and live state. */
export function filterEntities(entities: EntitySummary[], states: Record<string, EntityState>, search: string) {
  const query = search.trim().toLowerCase()
  if (!query) return entities
  return entities.filter((entity) => `${entity.name} ${entity.id} ${entity.group} ${states[entity.id]?.state ?? ''}`.toLowerCase().includes(query))
}

/** Format an entity state with its Home Assistant unit. */
export function formatEntityState(value?: EntityState) {
  if (value?.state === undefined || value.state === null) return 'Unavailable'
  const unit = value.attributes?.unit_of_measurement
  return `${String(value.state)}${unit ? ` ${String(unit)}` : ''}`
}
