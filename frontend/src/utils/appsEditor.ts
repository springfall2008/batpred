import { type EditorState } from '@codemirror/state'
import { type EditorView } from '@codemirror/view'
import { type Diagnostic } from '@codemirror/lint'
import { type CompletionContext, type Completion } from '@codemirror/autocomplete'
import { getJSONSchema, getJsonPointers, jsonPointerForPosition, type FoundCursorData } from '@invopop/codemirror-json-schema'
import { yamlCompletion, yamlSchemaLinter } from '@invopop/codemirror-json-schema/yaml'
import { syntaxTree } from '@codemirror/language'
import { parseDocument, stringify } from 'yaml'

/** Keep setting descriptions in hovers without exposing JSON Schema internals. */
export const appsYamlHoverOptions = {
  getHoverTexts: ({ schema }: FoundCursorData) => ({ message: schema.description ?? '', typeInfo: '' }),
}

/** Parse Home Assistant's secret references without resolving or exposing secrets. */
function parseAppsYaml(text: string) {
  return parseDocument(text, { customTags: [{ tag: '!secret', resolve: (value: string) => value }] })
}

/** Supply parsed values and editor positions to schema completion and validation. */
export function parseAppsYamlState(state: EditorState) {
  const document = parseAppsYaml(state.doc.toString())
  const pointers = getJsonPointers(state, 'yaml')
  for (const [path, pointer] of pointers) {
    if (!('valueFrom' in pointer)) pointers.set(path, { ...pointer, valueFrom: pointer.keyFrom, valueTo: pointer.keyTo })
  }
  return {
    data: document.errors.length ? null : document.toJS(),
    pointers,
  }
}

/** Report YAML syntax errors as well as schema errors, including before saving. */
export function createAppsYamlLinter() {
  const validate = yamlSchemaLinter({ jsonParser: parseAppsYamlState })
  return (view: EditorView): Diagnostic[] => {
    const document = parseAppsYaml(view.state.doc.toString())
    const errors = document.errors.map(error => ({
      from: Math.min(error.pos[0], view.state.doc.length),
      to: Math.min(error.pos[1], view.state.doc.length),
      severity: 'error' as const,
      message: error.message,
    }))
    if (errors.length) return errors
    try {
      const prefix = getJsonPointers(view.state, 'yaml').get('/pred_bat/prefix')
      return validate(view).map(diagnostic => {
        if (prefix && 'valueFrom' in prefix && diagnostic.from === prefix.valueFrom && diagnostic.message.includes('^[a-z][a-z0-9_]*$')) {
          return {
            ...diagnostic,
            message: 'Prefix must start with a lowercase letter and contain only lowercase letters, numbers or underscores. Use predbat_dev instead of predbat-dev.',
            renderMessage: undefined,
            source: undefined,
          }
        }
        return diagnostic
      })
    } catch (error) {
      return [{ from: 0, to: 0, severity: 'error', message: error instanceof Error ? error.message : 'Unable to validate YAML' }]
    }
  }
}

/** Fill gaps in the schema package's partial-key and list-item completion. */
export function createAppsYamlCompletion() {
  const complete = yamlCompletion({ jsonParser: parseAppsYamlState })
  return (context: CompletionContext) => {
    if (syntaxTree(context.state).resolveInner(context.pos, -1).name === 'Comment') return null
    const result = complete(context)
    if (!Array.isArray(result) && result.options.length) return result
    const word = context.matchBefore(/[\w.-]*/)
    if (!word || (!word.text && !context.explicit)) return null
    const root = getJSONSchema(context.state)
    if (!root) return null
    const expand = (schema: typeof root): (typeof root)[] => {
      if (schema.$ref?.startsWith('#/')) {
        const referenced = schema.$ref.slice(2).split('/').reduce<unknown>((value, key) => (value as Record<string, unknown>)?.[key.replaceAll('~1', '/').replaceAll('~0', '~')], root)
        if (referenced && typeof referenced === 'object') return expand(referenced as typeof root)
      }
      return [schema, ...(schema.anyOf ?? schema.oneOf ?? []).flatMap(child => typeof child === 'object' ? expand(child) : [])]
    }
    const pointer = jsonPointerForPosition(context.state, context.pos, -1, 'yaml')
    let schemas = expand(root)
    for (const key of pointer.split('/').slice(1)) {
      schemas = schemas.flatMap(schema => {
        const child = /^\d+$/.test(key) ? schema.items : schema.properties?.[key.replaceAll('~1', '/').replaceAll('~0', '~')]
        return child && typeof child === 'object' && !Array.isArray(child) ? expand(child) : []
      })
    }
    const options: Completion[] = schemas.flatMap(schema => [
      ...Object.entries(schema.properties ?? {}).map(([label, value]) => ({
        label, type: 'property', apply: `${label}: `,
        info: typeof value === 'object' ? value.description : undefined,
      })),
      ...(schema.enum ?? []).map(value => ({ label: String(value), type: 'constant', apply: stringify(value).trimEnd() })),
    ])
    return { from: word.from, options: [...new Map(options.map(option => [option.label, option])).values()], validFor: /[\w.-]*/ }
  }
}
