export type HaEntity = {
  id: string
  name: string
}

/**
 * Add Home Assistant entity IDs to fields marked with `x-ha-entity`
 * without restricting the field to known entity IDs.
 *
 * This means:
 *
 *   grid_power: sensor.
 *
 * gets HA entity completion, while:
 *
 *   timezone:
 *
 * continues to use its normal schema completion.
 */
export function addEntitySuggestions(
  value: unknown,
  entities: HaEntity[],
): unknown {
  const entityList = Array.from(
    new Map(
      entities
        .filter((entity) => entity.id)
        .map((entity) => [entity.id, entity]),
    ).values(),
  ).sort((a, b) => a.id.localeCompare(b.id))

  function replaceMarkers(item: unknown): unknown {
    if (Array.isArray(item)) {
      return item.map(replaceMarkers)
    }

    if (!item || typeof item !== 'object') {
      return item
    }

    const {
      ['x-ha-entity']: entityField,
      ...source
    } = item as Record<string, unknown>

    const schema = Object.fromEntries(
      Object.entries(source).map(([key, child]) => [
        key,
        replaceMarkers(child),
      ]),
    )

    if (entityField !== true || !entityList.length) {
      return schema
    }

    /*
     * The original schema remains valid, so regex values, numbers,
     * booleans etc. aren't rejected.
     *
     * The second branch exists purely to provide entity suggestions.
     */
    return {
      anyOf: [
        schema,
        {
          $ref: '#/$defs/haEntitySuggestion',
        },
      ],
    }
  }

  const schema = replaceMarkers(value) as Record<string, unknown>

  if (!entityList.length) {
    return schema
  }

  const definitions = schema.$defs as
    | Record<string, unknown>
    | undefined

  return {
    ...schema,

    $defs: {
      ...definitions,

      haEntitySuggestion: {
        type: 'string',

        enum: entityList.map((entity) => entity.id),

        markdownEnumDescriptions: entityList.map(
          (entity) => entity.name || entity.id,
        ),
      },
    },
  }
}
